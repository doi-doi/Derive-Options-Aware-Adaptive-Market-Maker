"""Explicit, snapshot-safe maker fill models for offline replay.

The canonical Condor snapshots do not contain a raw public trade stream.  The
two BBO models below are therefore deliberately separated: conservative
cross-through evidence is the primary result and touch-based filling is an
optimistic sensitivity range.  Neither model can fill an order on its
creation timestamp.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .data_loader import finite_float, parse_timestamp


class FillModelName(StrEnum):
    CONSERVATIVE_CROSS_THROUGH = "conservative_cross_through"
    TOUCH_OPTIMISTIC = "touch_optimistic"
    TRADE_BASED = "trade_based"


@dataclass(frozen=True)
class FillDecision:
    """One deterministic fill decision and its evidence label."""

    filled: bool
    model: str
    evidence_timestamp: str | None = None
    evidence_price: float | None = None
    reason: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "filled": self.filled,
            "model": self.model,
            "evidence_timestamp": self.evidence_timestamp,
            "evidence_price": self.evidence_price,
            "reason": self.reason,
        }


def _book_price(snapshot: Mapping[str, Any], field_name: str) -> float | None:
    return finite_float(snapshot.get(field_name))


def bbo_fill_condition(
    *,
    side: str,
    order_price: float,
    snapshot: Mapping[str, Any],
    model: FillModelName | str,
) -> FillDecision:
    """Evaluate one future BBO observation against a resting maker order."""

    selected = FillModelName(str(model))
    best_bid = _book_price(snapshot, "best_bid")
    best_ask = _book_price(snapshot, "best_ask")
    side_name = str(side).lower()
    if side_name not in {"buy", "sell"}:
        return FillDecision(False, selected.value, reason="unsupported side")
    if selected is FillModelName.TRADE_BASED:
        return FillDecision(False, selected.value, reason="trade evidence is required")
    if side_name == "buy" and best_ask is not None:
        threshold = (
            best_ask < order_price
            if selected is FillModelName.CONSERVATIVE_CROSS_THROUGH
            else best_ask <= order_price
        )
        if threshold:
            return FillDecision(
                True,
                selected.value,
                evidence_timestamp=str(snapshot.get("timestamp"))
                if snapshot.get("timestamp")
                else None,
                evidence_price=best_ask,
                reason=(
                    "future best ask crossed below resting buy"
                    if selected is FillModelName.CONSERVATIVE_CROSS_THROUGH
                    else "future best ask touched resting buy"
                ),
            )
    if side_name == "sell" and best_bid is not None:
        threshold = (
            best_bid > order_price
            if selected is FillModelName.CONSERVATIVE_CROSS_THROUGH
            else best_bid >= order_price
        )
        if threshold:
            return FillDecision(
                True,
                selected.value,
                evidence_timestamp=str(snapshot.get("timestamp"))
                if snapshot.get("timestamp")
                else None,
                evidence_price=best_bid,
                reason=(
                    "future best bid crossed above resting sell"
                    if selected is FillModelName.CONSERVATIVE_CROSS_THROUGH
                    else "future best bid touched resting sell"
                ),
            )
    return FillDecision(False, selected.value, reason="future BBO did not qualify")


def trade_fill_condition(
    *,
    side: str,
    order_price: float,
    trade: Mapping[str, Any],
) -> FillDecision:
    """Evaluate a Derive public trade row when a real trade stream is supplied."""

    price = finite_float(trade.get("price") or trade.get("trade_price"))
    if price is None:
        return FillDecision(False, FillModelName.TRADE_BASED.value, reason="trade price missing")
    aggressor = str(
        trade.get("aggressor_side") or trade.get("taker_side") or trade.get("side") or ""
    ).lower()
    side_name = str(side).lower()
    qualifies = (
        side_name == "buy" and price <= order_price and aggressor in {"sell", "ask", "short"}
    ) or (side_name == "sell" and price >= order_price and aggressor in {"buy", "bid", "long"})
    if qualifies:
        return FillDecision(
            True,
            FillModelName.TRADE_BASED.value,
            evidence_timestamp=str(trade.get("timestamp")) if trade.get("timestamp") else None,
            evidence_price=price,
            reason="qualifying aggressor trade at or through maker price",
        )
    return FillDecision(False, FillModelName.TRADE_BASED.value, reason="trade did not qualify")


def first_future_bbo_fill(
    *,
    side: str,
    order_price: float,
    created_at_seconds: float,
    future_snapshots: Sequence[Mapping[str, Any]],
    model: FillModelName | str,
) -> tuple[int, FillDecision] | None:
    """Return the first qualifying future snapshot index and decision."""

    selected = FillModelName(str(model))
    for index, snapshot in enumerate(future_snapshots):
        timestamp = parse_timestamp(snapshot.get("timestamp"))
        if timestamp is None or timestamp <= created_at_seconds:
            continue
        decision = bbo_fill_condition(
            side=side,
            order_price=order_price,
            snapshot=snapshot,
            model=selected,
        )
        if decision.filled:
            return index, decision
    return None


__all__ = [
    "FillDecision",
    "FillModelName",
    "bbo_fill_condition",
    "first_future_bbo_fill",
    "trade_fill_condition",
]
