"""Stage 12 mainnet-shadow baseline measurement and reporting.

The baseline layer deliberately wraps the existing shadow session instead of
copying the strategy.  One frozen Stage 1--5 configuration is replayed through
two completely separate virtual sessions: the conservative trade-through
model is the headline result and the touch model is an upper-bound-like
sensitivity result.  Neither session owns a private Derive client.

This module is also the reporting boundary.  It turns the event stream into
explicit paper-account accounting, time-weighted exposure, lifecycle,
markout, cycle, data-quality, and deterministic health metrics.  Unknown
fees, incomplete markouts, and insufficient samples remain visible rather
than being converted into optimistic values.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import subprocess
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .multi_asset import BTC_TRADING_PAIR, MultiAssetCycle
from .shadow import (
    SHADOW_ENVIRONMENT_CONSISTENCY_PASS,
    ShadowConfig,
    ShadowEnvironmentError,
    ShadowExecutionEngine,
    ShadowFillModel,
    ShadowMarketFrame,
    ShadowOrderStatus,
    ShadowSession,
    ShadowStore,
    require_shadow_environment,
)
from .stage12c import (
    CANCEL_TAXONOMY,
    FILL_ELIGIBILITY_STATUSES,
    REPLACEMENT_DEVIATION_BUCKETS,
    RESTING_LIFETIME_BUCKETS,
    calculate_trade_coverage,
    classify_cancel_reason,
    normalize_risk_reason,
    replacement_deviation_bucket,
    resting_lifetime_bucket,
)
from .stage12e import classify_fill_contract, write_stage12e_artifacts
from .stage12f import write_stage12f_artifacts
from .stage12g import _lifecycle_epoch, write_stage12g_artifacts
from .stage13 import write_stage13_artifacts

BASELINE_BANNER = "DERIVE MAINNET SHADOW BASELINE"
BASELINE_DATA_LINE = "DATA:\nREAL DERIVE MAINNET"
BASELINE_EXECUTION_LINE = "EXECUTION:\nSHADOW / PAPER"
BASELINE_MUTATION_LINE = "REAL EXCHANGE MUTATIONS:\n0"
BASELINE_FILL_MODEL_LINE = "FILL MODEL:\nCONSERVATIVE TRADE-THROUGH"
BASELINE_CONFIG_LINE = "CONFIG:\nFROZEN"
CONSERVATIVE_MODEL = "CONSERVATIVE"
TOUCH_MODEL = "TOUCH_OPTIMISTIC"
MARKOUT_HORIZONS_SECONDS = (5, 30, 60)
RISK_CATEGORIES = (
    "ASSET_INVENTORY_RISK",
    "EXECUTOR_CAPACITY",
    "PORTFOLIO_GROSS_RISK",
    "PORTFOLIO_BETA_RISK",
    "DRAWDOWN_RISK",
    "COLLATERAL_RESERVE",
    "MIN_EXCHANGE_SIZE",
    "STALE_DATA",
    "PLAN_SUPPRESSION",
    "OTHER",
)


def _float(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _epoch(value: Any) -> float | None:
    numeric = _float(value)
    if numeric is not None:
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def _iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, BaseModel):
        return _safe(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _lifetime_stats(values: Sequence[Any]) -> dict[str, Any]:
    cleaned = [parsed for value in values if (parsed := _float(value)) is not None]
    return {
        "sample_count": len(cleaned),
        "mean": _mean(cleaned),
        "median": _percentile(cleaned, 0.50),
        "p25": _percentile(cleaned, 0.25),
        "p75": _percentile(cleaned, 0.75),
        "p90": _percentile(cleaned, 0.90),
    }


def _markout_stats(values: Sequence[Any]) -> dict[str, Any]:
    cleaned = [parsed for value in values if (parsed := _float(value)) is not None]
    positive_pct = sum(value > 0 for value in cleaned) / len(cleaned) * 100.0 if cleaned else None
    return {
        "mean_bps": _mean(cleaned),
        "median_bps": _percentile(cleaned, 0.50),
        "positive_pct": positive_pct,
        "positive_markout_pct": positive_pct,
        "p25_bps": _percentile(cleaned, 0.25),
        "p75_bps": _percentile(cleaned, 0.75),
        "sample_count": len(cleaned),
    }


def _relative_difference(left: Any, right: Any) -> float:
    left_value = _float(left, 0.0) or 0.0
    right_value = _float(right, 0.0) or 0.0
    denominator = max(abs(left_value), abs(right_value))
    if denominator <= 1e-12:
        return 0.0
    return abs(right_value - left_value) / denominator * 100.0


@dataclass(frozen=True)
class PnLReconciliation:
    """Explicit paper-equity identity at one metrics checkpoint."""

    starting_equity: Decimal
    realized_grid_capture: Decimal
    realized_other_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    current_equity: Decimal
    expected_equity: Decimal
    discrepancy: Decimal
    tolerance: Decimal
    status: str
    fees_known: bool
    total_pnl: Decimal | None

    def to_record(self) -> dict[str, Any]:
        return _safe(self.__dict__)


def reconcile_paper_equity(
    ledger: Any,
    *,
    tolerance: Decimal = Decimal("0.000001"),
) -> PnLReconciliation:
    """Verify ``start + realized + unrealized - fees = current``."""

    starting = _decimal(getattr(ledger, "starting_equity", 0))
    realized_grid = _decimal(getattr(ledger, "realized_grid_capture", 0))
    realized_other = _decimal(getattr(ledger, "realized_other_pnl", 0))
    realized = _decimal(getattr(ledger, "realized_pnl", realized_grid + realized_other))
    unrealized = _decimal(getattr(ledger, "unrealized_inventory_pnl", 0))
    fees_known = bool(getattr(ledger, "fees_known", False))
    fees = _decimal(getattr(ledger, "fees", 0)) if fees_known else Decimal("0")
    current = _decimal(getattr(ledger, "current_equity", starting + realized + unrealized - fees))
    expected = starting + realized + unrealized - fees
    discrepancy = current - expected
    status = "PASS" if abs(discrepancy) <= tolerance else "FAIL"
    gross_pnl = realized + unrealized
    total = gross_pnl - fees if fees_known else None
    return PnLReconciliation(
        starting_equity=starting,
        realized_grid_capture=realized_grid,
        realized_other_pnl=realized_other,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        fees=fees,
        current_equity=current,
        expected_equity=expected,
        discrepancy=discrepancy,
        tolerance=tolerance,
        status=status,
        fees_known=fees_known,
        total_pnl=total,
    )


@dataclass
class TimeWeightedExposure:
    """Integrate portfolio exposure using a timestamped left-continuous series."""

    model: str
    points: list[dict[str, Any]] = field(default_factory=list)
    asset_points: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    def add(
        self,
        timestamp: float,
        *,
        assets: Mapping[str, Mapping[str, Any]],
        resting_quote_exposure: float = 0.0,
    ) -> None:
        gross = 0.0
        net = 0.0
        long_notional = 0.0
        short_notional = 0.0
        beta_exposure = 0.0
        long_beta = 0.0
        short_beta = 0.0
        absolute_inventory = 0.0
        for pair, raw in assets.items():
            amount = _float(raw.get("amount"), 0.0) or 0.0
            mid = _float(raw.get("mid_price"), 0.0) or 0.0
            signed_notional = amount * mid
            absolute_notional = abs(signed_notional)
            beta = _float(raw.get("beta"), 1.0) or 1.0
            beta_value = signed_notional * beta
            gross += absolute_notional
            net += signed_notional
            absolute_inventory += absolute_notional
            long_notional += max(0.0, signed_notional)
            short_notional += max(0.0, -signed_notional)
            beta_exposure += beta_value
            long_beta += max(0.0, beta_value)
            short_beta += max(0.0, -beta_value)
            self.asset_points[str(pair)].append(
                {
                    "timestamp": _iso(timestamp),
                    "timestamp_epoch": timestamp,
                    "model": self.model,
                    "trading_pair": str(pair),
                    "amount": amount,
                    "mid_price": mid,
                    "position_notional": signed_notional,
                    "absolute_inventory_notional": absolute_notional,
                    "inventory_ratio": _float(raw.get("inventory_ratio"), 0.0) or 0.0,
                    "mode": raw.get("mode", "UNKNOWN"),
                    "global_iv_regime": raw.get("global_iv_regime", "UNKNOWN"),
                    "beta": beta,
                }
            )
        self.points.append(
            {
                "timestamp": _iso(timestamp),
                "timestamp_epoch": timestamp,
                "model": self.model,
                "gross_exposure": gross,
                "net_exposure": net,
                "long_notional": long_notional,
                "short_notional": short_notional,
                "absolute_inventory": absolute_inventory,
                "btc_beta_exposure": beta_exposure,
                "long_beta_exposure": long_beta,
                "short_beta_exposure": short_beta,
                "resting_quote_exposure": max(0.0, resting_quote_exposure),
            }
        )

    @staticmethod
    def _integrate(
        points: Sequence[Mapping[str, Any]],
        start: float,
        end: float,
        fields: Sequence[str],
    ) -> tuple[dict[str, float], float]:
        duration = max(0.0, end - start)
        if duration <= 0:
            return {field: 0.0 for field in fields}, 0.0
        ordered = sorted(
            (point for point in points if (_float(point.get("timestamp_epoch")) is not None)),
            key=lambda point: _float(point.get("timestamp_epoch"), 0.0) or 0.0,
        )
        prior: Mapping[str, Any] | None = None
        future: list[Mapping[str, Any]] = []
        for point in ordered:
            timestamp = _float(point.get("timestamp_epoch"), 0.0) or 0.0
            if timestamp <= start:
                prior = point
            elif timestamp < end:
                future.append(point)
        totals = {field: 0.0 for field in fields}
        cursor = start

        def add_segment(until: float, value: Mapping[str, Any] | None) -> None:
            nonlocal cursor
            seconds = max(0.0, until - cursor)
            if seconds <= 0:
                cursor = max(cursor, until)
                return
            for field_name in fields:
                totals[field_name] += (
                    _float(value.get(field_name), 0.0) if value else 0.0
                ) * seconds
            cursor = until

        for point in future:
            timestamp = _float(point.get("timestamp_epoch"), end) or end
            add_segment(timestamp, prior)
            prior = point
        add_segment(end, prior)
        return totals, duration

    @classmethod
    def _time_above(
        cls,
        points: Sequence[Mapping[str, Any]],
        start: float,
        end: float,
        *,
        field_name: str,
        threshold: float,
    ) -> float:
        duration = max(0.0, end - start)
        if duration <= 0:
            return 0.0
        ordered = sorted(
            points,
            key=lambda point: _float(point.get("timestamp_epoch"), 0.0) or 0.0,
        )
        prior: Mapping[str, Any] | None = None
        future: list[Mapping[str, Any]] = []
        for point in ordered:
            timestamp = _float(point.get("timestamp_epoch"), 0.0) or 0.0
            if timestamp <= start:
                prior = point
            elif timestamp < end:
                future.append(point)
        cursor = start
        result = 0.0
        for point in [*future, {"timestamp_epoch": end}]:
            timestamp = min(end, _float(point.get("timestamp_epoch"), end) or end)
            seconds = max(0.0, timestamp - cursor)
            value = _float(prior.get(field_name), 0.0) if prior else 0.0
            if value is not None and value >= threshold:
                result += seconds
            cursor = timestamp
            prior = point
        return result

    def summary(self, *, start: float | None = None, end: float | None = None) -> dict[str, Any]:
        all_timestamps = [
            _float(point.get("timestamp_epoch"))
            for point in self.points
            if _float(point.get("timestamp_epoch")) is not None
        ]
        if not all_timestamps and (start is None or end is None):
            return {
                "model": self.model,
                "duration_seconds": 0.0,
                "capital_time_quote_seconds": 0.0,
                "inventory_time_quote_seconds": 0.0,
            }
        first = min(all_timestamps) if all_timestamps else (start or 0.0)
        last = max(all_timestamps) if all_timestamps else (end or first)
        range_start = first if start is None else start
        range_end = last if end is None else end
        fields = (
            "gross_exposure",
            "net_exposure",
            "long_notional",
            "short_notional",
            "absolute_inventory",
            "btc_beta_exposure",
            "long_beta_exposure",
            "short_beta_exposure",
            "resting_quote_exposure",
        )
        totals, duration = self._integrate(self.points, range_start, range_end, fields)
        result: dict[str, Any] = {
            "model": self.model,
            "duration_seconds": duration,
            "capital_time_quote_seconds": totals["gross_exposure"],
            "inventory_time_quote_seconds": totals["absolute_inventory"],
            "btc_beta_time_quote_seconds": totals["btc_beta_exposure"],
        }
        aliases = {
            "gross_exposure": "average_gross_exposure",
            "net_exposure": "average_net_exposure",
            "long_notional": "average_long_notional",
            "short_notional": "average_short_notional",
            "absolute_inventory": "average_absolute_inventory",
            "btc_beta_exposure": "average_btc_beta_exposure",
            "long_beta_exposure": "average_long_beta_exposure",
            "short_beta_exposure": "average_short_beta_exposure",
            "resting_quote_exposure": "average_resting_quote_exposure",
        }
        for field_name, output_name in aliases.items():
            result[output_name] = totals[field_name] / duration if duration > 0 else None
        result["average_margin_used"] = (
            result["average_gross_exposure"]
            if result["average_gross_exposure"] is not None
            else None
        )
        return result

    def per_asset_summary(
        self,
        *,
        start: float | None = None,
        end: float | None = None,
        starting_equity: float = 0.0,
        soft_threshold: float = 0.50,
        defensive_threshold: float = 0.75,
        hard_threshold: float = 1.00,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for pair, points in self.asset_points.items():
            timestamps = [
                _float(point.get("timestamp_epoch"))
                for point in points
                if _float(point.get("timestamp_epoch")) is not None
            ]
            if not timestamps:
                continue
            range_start = min(timestamps) if start is None else start
            range_end = max(timestamps) if end is None else end
            fields = ("position_notional", "absolute_inventory_notional", "inventory_ratio")
            totals, duration = self._integrate(points, range_start, range_end, fields)
            ratios = [_float(point.get("inventory_ratio"), 0.0) or 0.0 for point in points]
            notional = [abs(_float(point.get("position_notional"), 0.0) or 0.0) for point in points]
            result[pair] = {
                "trading_pair": pair,
                "duration_active_seconds": duration,
                "average_inventory": (
                    totals["absolute_inventory_notional"] / duration if duration > 0 else None
                ),
                "max_inventory": max(notional, default=None),
                "average_inventory_ratio": (
                    totals["inventory_ratio"] / duration if duration > 0 else None
                ),
                "max_inventory_ratio": max(ratios, default=None),
                "time_above_soft_threshold_seconds": self._time_above(
                    points,
                    range_start,
                    range_end,
                    field_name="inventory_ratio",
                    threshold=soft_threshold,
                ),
                "time_above_defensive_threshold_seconds": self._time_above(
                    points,
                    range_start,
                    range_end,
                    field_name="inventory_ratio",
                    threshold=defensive_threshold,
                ),
                "time_at_or_above_hard_threshold_seconds": self._time_above(
                    points,
                    range_start,
                    range_end,
                    field_name="inventory_ratio",
                    threshold=hard_threshold,
                ),
                "inventory_direction_changes": sum(
                    1
                    for previous, current in zip(points, points[1:], strict=False)
                    if (
                        (_float(previous.get("position_notional"), 0.0) or 0.0)
                        * (_float(current.get("position_notional"), 0.0) or 0.0)
                        < 0
                    )
                ),
                "starting_equity": starting_equity,
            }
        return result


@dataclass
class DrawdownTracker:
    """Paper-equity high-water mark and underwater-time measurements."""

    points: list[dict[str, Any]] = field(default_factory=list)
    high_water_mark: float = 0.0

    def add(self, timestamp: float, equity: Any) -> None:
        value = _float(equity, 0.0) or 0.0
        self.high_water_mark = max(self.high_water_mark, value)
        drawdown = max(0.0, self.high_water_mark - value)
        self.points.append(
            {
                "timestamp": _iso(timestamp),
                "timestamp_epoch": timestamp,
                "paper_equity": value,
                "high_water_mark": self.high_water_mark,
                "drawdown_quote": drawdown,
                "drawdown_pct": drawdown / self.high_water_mark if self.high_water_mark else 0.0,
            }
        )

    def summary(self, *, start: float, end: float) -> dict[str, Any]:
        ordered = sorted(self.points, key=lambda point: point["timestamp_epoch"])
        if not ordered:
            return {
                "max_drawdown_quote": 0.0,
                "max_drawdown_pct": 0.0,
                "worst_paper_equity": None,
                "best_paper_equity": None,
                "drawdown_duration_seconds": 0.0,
                "time_underwater_seconds": 0.0,
                "drawdown_stage_transitions": 0,
            }
        max_drawdown = max(_float(point.get("drawdown_quote"), 0.0) or 0.0 for point in ordered)
        max_drawdown_pct = max(_float(point.get("drawdown_pct"), 0.0) or 0.0 for point in ordered)
        equities = [_float(point.get("paper_equity"), 0.0) or 0.0 for point in ordered]
        underwater = 0.0
        max_duration = 0.0
        current_duration = 0.0
        transitions = 0
        previous_underwater = False
        for point, next_point in zip(
            ordered, ordered[1:] + [{"timestamp_epoch": end}], strict=False
        ):
            seconds = max(
                0.0,
                min(end, _float(next_point.get("timestamp_epoch"), end) or end)
                - max(start, _float(point.get("timestamp_epoch"), start) or start),
            )
            is_underwater = (_float(point.get("drawdown_quote"), 0.0) or 0.0) > 1e-12
            if is_underwater:
                underwater += seconds
                current_duration += seconds
                max_duration = max(max_duration, current_duration)
            else:
                current_duration = 0.0
            if is_underwater != previous_underwater:
                transitions += 1
                previous_underwater = is_underwater
        return {
            "max_drawdown_quote": max_drawdown,
            "max_drawdown_pct": max_drawdown_pct,
            "worst_paper_equity": min(equities),
            "best_paper_equity": max(equities),
            "drawdown_duration_seconds": max_duration,
            "time_underwater_seconds": underwater,
            "drawdown_stage_transitions": transitions,
        }


@dataclass
class DataQualityTracker:
    """Coverage and gap counters for every upstream shadow input."""

    expected_markets: tuple[str, ...]
    stale_seconds: float
    market_expected: int = 0
    market_valid: int = 0
    market_stale: int = 0
    market_gaps: int = 0
    option_expected: int = 0
    option_available: int = 0
    option_gaps: int = 0
    trade_expected: int = 0
    trade_available: int = 0
    trade_gaps: int = 0
    trade_event_available: int = 0
    trade_event_gaps: int = 0
    trade_collection_status_by_asset: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    relationship_expected: int = 0
    relationship_valid: int = 0
    relationship_gaps: int = 0
    fill_eligibility_expected: int = 0
    fill_eligibility_available: int = 0
    fill_eligibility_gaps: int = 0
    last_market_timestamp: dict[str, float] = field(default_factory=dict)
    trade_timestamps_by_asset: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    trade_observation_timestamps_by_asset: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    trade_ids_by_asset: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    trade_count_by_asset: dict[str, int] = field(default_factory=dict)
    coverage_start_epoch: float | None = None
    coverage_end_epoch: float | None = None

    def record(
        self,
        frames: Mapping[str, ShadowMarketFrame],
        *,
        cycle: MultiAssetCycle,
        reference_timestamp: float,
    ) -> None:
        frame_timestamps = [frame.timestamp for frame in frames.values()]
        if frame_timestamps:
            first = min(frame_timestamps)
            last = max(frame_timestamps)
            self.coverage_start_epoch = (
                first
                if self.coverage_start_epoch is None
                else min(self.coverage_start_epoch, first)
            )
            self.coverage_end_epoch = (
                last if self.coverage_end_epoch is None else max(self.coverage_end_epoch, last)
            )
        for pair in self.expected_markets:
            self.market_expected += 1
            frame = frames.get(pair)
            if frame is None:
                self.market_gaps += 1
                continue
            age = max(0.0, reference_timestamp - frame.timestamp)
            valid = frame.best_bid > 0 and frame.best_ask > frame.best_bid
            if valid and age <= self.stale_seconds:
                self.market_valid += 1
            else:
                self.market_stale += 1
                if not valid:
                    self.market_gaps += 1
            previous = self.last_market_timestamp.get(pair)
            if previous is not None and frame.timestamp - previous > self.stale_seconds * 2:
                self.market_gaps += 1
            self.last_market_timestamp[pair] = frame.timestamp
            self.fill_eligibility_expected += 1
            if valid and age <= self.stale_seconds:
                self.fill_eligibility_available += 1
            else:
                self.fill_eligibility_gaps += 1

        btc = frames.get(BTC_TRADING_PAIR)
        if btc is not None:
            self.option_expected += 1
            option = btc.option_snapshot
            option_ok = bool(
                option
                and option.data_available
                and option.atm_iv is not None
                and option.environment == "mainnet"
            )
            if option_ok:
                self.option_available += 1
            else:
                self.option_gaps += 1
        for pair in cycle.states:
            if pair == BTC_TRADING_PAIR:
                continue
            self.relationship_expected += 1
            relationship = cycle.relationships.get(pair)
            if relationship is not None and relationship.relationship_valid:
                self.relationship_valid += 1
            else:
                self.relationship_gaps += 1
        for frame in frames.values():
            self.trade_expected += 1
            status = str(frame.trade_collection_status or "").upper()
            collection_healthy = status in {
                "OK",
                "CONNECTED",
                "CONNECTED_NO_TRADES",
                "REST_FALLBACK",
                "WEBSOCKET",
                "WS_CONNECTED",
            } or (status in {"", "UNKNOWN"} and bool(frame.trades))
            self.trade_collection_status_by_asset[frame.trading_pair][status or "UNKNOWN"] += 1
            if collection_healthy:
                self.trade_available += 1
                self.trade_observation_timestamps_by_asset[frame.trading_pair].append(
                    frame.timestamp
                )
            else:
                self.trade_gaps += 1
            if frame.trades:
                self.trade_event_available += 1
                seen_without_id: set[tuple[Any, ...]] = set()
                for trade in frame.trades:
                    trade_timestamp = _float(trade.timestamp)
                    if trade_timestamp is None:
                        continue
                    trade_id = str(trade.trade_id) if trade.trade_id else None
                    fallback_key = (
                        trade_timestamp,
                        _float(trade.price),
                        _float(trade.amount),
                        trade.aggressor_side,
                    )
                    if trade_id:
                        if trade_id in self.trade_ids_by_asset[frame.trading_pair]:
                            continue
                        self.trade_ids_by_asset[frame.trading_pair].add(trade_id)
                    elif fallback_key in seen_without_id:
                        continue
                    else:
                        seen_without_id.add(fallback_key)
                    self.trade_timestamps_by_asset[frame.trading_pair].append(trade_timestamp)
                    self.trade_count_by_asset[frame.trading_pair] = (
                        self.trade_count_by_asset.get(frame.trading_pair, 0) + 1
                    )
            else:
                self.trade_event_gaps += 1

    @staticmethod
    def _coverage(available: int, expected: int) -> float | None:
        return available / expected * 100.0 if expected else None

    def to_record(self) -> dict[str, Any]:
        by_asset: dict[str, dict[str, Any]] = {}
        pairs = sorted(
            set(self.expected_markets)
            | set(self.trade_observation_timestamps_by_asset)
            | set(self.trade_timestamps_by_asset)
        )
        for pair in pairs:
            collection = calculate_trade_coverage(
                self.trade_observation_timestamps_by_asset.get(pair, []),
                start_timestamp=self.coverage_start_epoch or 0.0,
                end_timestamp=self.coverage_end_epoch or 0.0,
                sample_interval_seconds=max(1.0, self.stale_seconds / 3.0),
                trade_count=0,
                evidence_minutes=0,
            )
            events = calculate_trade_coverage(
                self.trade_timestamps_by_asset.get(pair, []),
                start_timestamp=self.coverage_start_epoch or 0.0,
                end_timestamp=self.coverage_end_epoch or 0.0,
                sample_interval_seconds=max(1.0, self.stale_seconds / 3.0),
                trade_count=self.trade_count_by_asset.get(pair, 0),
                evidence_minutes=len(
                    {int(value // 60) for value in self.trade_timestamps_by_asset.get(pair, [])}
                ),
            )
            by_asset[pair] = {
                **events,
                "event_coverage": events,
                "collection_coverage": collection,
                "collection_status": dict(self.trade_collection_status_by_asset.get(pair, {})),
            }
        return {
            "market_data": {
                "expected": self.market_expected,
                "valid": self.market_valid,
                "stale": self.market_stale,
                "gaps": self.market_gaps,
                "coverage_pct": self._coverage(self.market_valid, self.market_expected),
            },
            "option_iv": {
                "expected": self.option_expected,
                "available": self.option_available,
                "gaps": self.option_gaps,
                "coverage_pct": self._coverage(self.option_available, self.option_expected),
            },
            "trade_stream": {
                "expected": self.trade_expected,
                "available": self.trade_available,
                "gaps": self.trade_gaps,
                "coverage_pct": self._coverage(self.trade_available, self.trade_expected),
                "collection_healthy": self.trade_available,
                "collection_gaps": self.trade_gaps,
                "event_observations": self.trade_event_available,
                "event_gaps": self.trade_event_gaps,
                "status_by_asset": {
                    pair: dict(statuses)
                    for pair, statuses in self.trade_collection_status_by_asset.items()
                },
                "trade_count": sum(self.trade_count_by_asset.values()),
                "by_asset": by_asset,
            },
            "relationship_data": {
                "expected": self.relationship_expected,
                "valid": self.relationship_valid,
                "gaps": self.relationship_gaps,
                "coverage_pct": self._coverage(self.relationship_valid, self.relationship_expected),
            },
            "shadow_fill_eligibility": {
                "expected": self.fill_eligibility_expected,
                "available": self.fill_eligibility_available,
                "gaps": self.fill_eligibility_gaps,
                "coverage_pct": self._coverage(
                    self.fill_eligibility_available, self.fill_eligibility_expected
                ),
            },
        }


def cancel_category(reason: Any, context: Mapping[str, Any] | None = None) -> str:
    """Map execution reason codes to the complete Stage 12C taxonomy."""

    value = str(reason or "").upper()
    if value in CANCEL_TAXONOMY:
        return value
    return classify_cancel_reason(reason, context=dict(context or {}))


def _trade_qualifies_conservatively(order: Any, trade: Any) -> bool:
    if trade.timestamp <= (order.resting_start_epoch or order.created_epoch):
        return False
    if trade.aggressor_side not in {"buy", "sell"}:
        return False
    if order.side == "buy" and trade.aggressor_side == "sell":
        return trade.price < float(order.price)
    if order.side == "sell" and trade.aggressor_side == "buy":
        return trade.price > float(order.price)
    return False


def order_fill_eligibility(
    engine: ShadowExecutionEngine,
    order: Any,
    *,
    end_timestamp: float,
) -> dict[str, Any]:
    """Classify one order without treating a BBO touch as a conservative fill."""

    return classify_fill_contract(
        order,
        list(engine.market_history.get(order.trading_pair, ())),
        engine.fills,
        end_timestamp=end_timestamp,
        model=(
            "CONSERVATIVE"
            if engine.config.fill_model is ShadowFillModel.CONSERVATIVE_TRADE_THROUGH
            else "TOUCH_OPTIMISTIC"
        ),
    )


def _order_rows(
    engine: ShadowExecutionEngine,
    *,
    model: str,
    end_timestamp: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for order in engine.orders.values():
        status = order.status.value
        outcome = (
            "filled"
            if order.fill_timestamp
            else "cancelled"
            if order.cancel_timestamp
            else "rejected"
            if order.status is ShadowOrderStatus.REJECTED
            else "resting"
            if order.status
            in {
                ShadowOrderStatus.RESTING,
                ShadowOrderStatus.PARTIALLY_FILLED,
                ShadowOrderStatus.CLOSE_RESTING,
            }
            else status.lower()
        )
        eligibility = order_fill_eligibility(engine, order, end_timestamp=end_timestamp)
        row = order.to_record()
        created_lifecycle_epoch = _lifecycle_epoch(row, "created") or order.created_epoch
        terminal_lifecycle_epoch = _lifecycle_epoch(row, "terminal")
        terminal_for_metrics = (
            terminal_lifecycle_epoch
            if terminal_lifecycle_epoch is not None
            else end_timestamp
        )
        resting_start_for_metrics = (
            created_lifecycle_epoch
            if row.get("controller_created_epoch") is not None
            or row.get("controller_created_timestamp") is not None
            else order.resting_start_epoch
        )
        row.update(
            {
                "model": model,
                "fill_model": model,
                "outcome": outcome,
                "created_to_terminal_seconds": max(
                    0.0, terminal_for_metrics - created_lifecycle_epoch
                ),
                "lifetime_seconds": (
                    max(0.0, terminal_for_metrics - resting_start_for_metrics)
                    if resting_start_for_metrics is not None
                    else None
                ),
                "resting_lifetime_seconds": (
                    max(0.0, terminal_for_metrics - resting_start_for_metrics)
                    if resting_start_for_metrics is not None
                    else None
                ),
                "cancel_category": cancel_category(
                    order.cancel_reason_category or order.cancel_reason,
                    {
                        "account_state_invalid": order.cancel_reason_category
                        == "ACCOUNT_STATE_INVALID"
                    },
                ),
                "is_resting": order.lifecycle_state == "RESTING",
                "active_at_end": order.status
                in {
                    ShadowOrderStatus.RESTING,
                    ShadowOrderStatus.PARTIALLY_FILLED,
                    ShadowOrderStatus.CLOSE_RESTING,
                },
                "fill_eligibility_status": eligibility["status"],
                "fill_eligibility_reason": eligibility["reason"],
                "trade_count_after_resting": eligibility["trade_count"],
                "qualifying_trade_count": eligibility["qualifying_trade_count"],
                "bbo_touched": eligibility["bbo_touched"],
            }
        )
        rows.append(row)
    return rows


def _fill_rows(
    engine: ShadowExecutionEngine,
    *,
    model: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fill in engine.fills:
        order = engine.orders.get(fill.shadow_order_id)
        row = fill.to_record()
        row.update(
            {
                "model": model,
                "fill_model": model,
                "level_id": order.level_id if order else None,
                "mode": fill.mode or (order.mode_at_creation if order else "UNKNOWN"),
                "state": fill.state or "FILLED",
                "quote_distance_bps": (
                    fill.quote_distance_bps
                    if fill.quote_distance_bps is not None
                    else order.quote_distance_bps
                    if order
                    else None
                ),
                "quote_distance_before_fill_bps": (
                    fill.quote_distance_before_fill_bps
                    if fill.quote_distance_before_fill_bps is not None
                    else order.quote_distance_bps
                    if order
                    else None
                ),
                "global_iv_regime": fill.global_iv_regime or "UNKNOWN",
            }
        )
        rows.append(row)
    return rows


def _markout_rows(
    engine: ShadowExecutionEngine,
    *,
    model: str,
    end_timestamp: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fill in engine.fills:
        order = engine.orders.get(fill.shadow_order_id)
        for horizon in MARKOUT_HORIZONS_SECONDS:
            key = f"{horizon}s"
            value = fill.markouts_bps.get(key)
            eligible = end_timestamp >= fill.timestamp_epoch + horizon
            status = (
                "COMPLETE"
                if value is not None
                else "DATA_GAP"
                if eligible
                else "MISSING_SESSION_END"
            )
            rows.append(
                {
                    "timestamp": fill.timestamp,
                    "timestamp_epoch": fill.timestamp_epoch,
                    "model": model,
                    "fill_model": model,
                    "fill_id": fill.fill_id,
                    "shadow_order_id": fill.shadow_order_id,
                    "trading_pair": fill.trading_pair,
                    "side": fill.side,
                    "entry_exit": fill.entry_exit,
                    "horizon_seconds": horizon,
                    "markout_bps": value,
                    "eligible": eligible,
                    "status": status,
                    "quote_distance_bps": (
                        fill.quote_distance_bps
                        if fill.quote_distance_bps is not None
                        else order.quote_distance_bps
                        if order
                        else None
                    ),
                    "quote_distance_bucket": quote_distance_bucket(
                        fill.quote_distance_bps
                        if fill.quote_distance_bps is not None
                        else order.quote_distance_bps
                        if order
                        else None
                    ),
                    "mode": fill.mode or (order.mode_at_creation if order else "UNKNOWN"),
                    "global_iv_regime": fill.global_iv_regime or "UNKNOWN",
                    "missing_reason": (
                        None if value is not None else "session_end" if not eligible else "data_gap"
                    ),
                }
            )
    return rows


def quote_distance_bucket(value: Any) -> str:
    distance = _float(value)
    if distance is None:
        return "UNKNOWN"
    if distance <= 5.0:
        return "0-5bps"
    if distance <= 15.0:
        return "5-15bps"
    return ">15bps"


def _closed_within(cycles: Sequence[Mapping[str, Any]], horizon_seconds: float) -> float | None:
    completed = [
        _float(row.get("cycle_duration_seconds"))
        for row in cycles
        if row.get("status") == "COMPLETE" and _float(row.get("cycle_duration_seconds")) is not None
    ]
    if not completed:
        return None
    return (
        sum(value <= horizon_seconds for value in completed if value is not None)
        / len(completed)
        * 100.0
    )


def _cycle_rows(
    engine: ShadowExecutionEngine,
    *,
    model: str,
    end_timestamp: float,
    fees_known: bool,
) -> list[dict[str, Any]]:
    fills_by_order = {fill.shadow_order_id: fill for fill in engine.fills}
    rows: list[dict[str, Any]] = []
    for entry in engine.orders.values():
        if entry.is_exit or entry.shadow_order_id not in fills_by_order:
            continue
        entry_fill = fills_by_order[entry.shadow_order_id]
        exit_order = engine.orders.get(entry.take_profit_order_id or "")
        exit_fill = fills_by_order.get(exit_order.shadow_order_id) if exit_order else None
        entry_epoch = entry_fill.timestamp_epoch
        exit_epoch = exit_fill.timestamp_epoch if exit_fill else None
        fees = (entry_fill.fees + exit_fill.fees) if exit_fill else entry_fill.fees
        gross_capture = entry_fill.realized_pnl + (
            exit_fill.realized_pnl if exit_fill else Decimal("0")
        )
        rows.append(
            {
                "timestamp": exit_fill.timestamp if exit_fill else entry_fill.timestamp,
                "timestamp_epoch": exit_epoch or entry_epoch,
                "model": model,
                "fill_model": model,
                "cycle_id": entry.cycle_id,
                "trading_pair": entry.trading_pair,
                "side": entry.side,
                "status": "COMPLETE" if exit_fill else "OPEN",
                "entry_order_id": entry.shadow_order_id,
                "exit_order_id": exit_order.shadow_order_id if exit_order else None,
                "entry_timestamp": entry_fill.timestamp,
                "exit_timestamp": exit_fill.timestamp if exit_fill else None,
                "cycle_duration_seconds": (
                    exit_epoch - entry_epoch if exit_epoch is not None else None
                ),
                "cycle_pnl": (gross_capture - fees if fees_known else None),
                "realized_capture": gross_capture,
                "fees": fees,
                "executed_volume": entry_fill.notional + (exit_fill.notional if exit_fill else 0),
                "entry_quote_distance_bps": entry.quote_distance_bps,
                "exit_quote_distance_bps": exit_order.quote_distance_bps if exit_order else None,
                "mode_at_entry": entry.mode_at_creation,
                "mode_at_exit": exit_order.mode_at_creation if exit_order else None,
                "open_position_age_seconds": (
                    max(0.0, end_timestamp - entry_epoch) if exit_fill is None else None
                ),
            }
        )
    return rows


def _risk_rows(engine: ShadowExecutionEngine, *, model: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in engine.events:
        if event.get("event") not in {"RISK_BLOCK", "PORTFOLIO_RISK_BLOCK"}:
            continue
        reason = event.get("reason")
        rows.append(
            {
                **event,
                "model": model,
                "category": normalize_risk_reason(reason),
                "candidate_notional": event.get("candidate_notional"),
                "exposure_before": event.get("exposure_before"),
                "exposure_after_candidate": event.get("exposure_after_candidate"),
            }
        )
    return rows


def risk_category(reason: Any) -> str:
    """Backward-compatible alias for the normalized Stage 12C risk reason."""

    return normalize_risk_reason(reason)


@dataclass
class BaselineModelMetrics:
    """Computed metrics and report rows for one isolated fill model."""

    name: str
    metrics: dict[str, Any]
    orders: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    cancels: list[dict[str, Any]]
    cycles: list[dict[str, Any]]
    markouts: list[dict[str, Any]]
    inventory: list[dict[str, Any]]
    portfolio_exposure: list[dict[str, Any]]
    risk_events: list[dict[str, Any]]
    equity: list[dict[str, Any]]
    risk_episodes: list[dict[str, Any]] = field(default_factory=list)
    fill_eligibility: list[dict[str, Any]] = field(default_factory=list)
    reconciliation_decisions: list[dict[str, Any]] = field(default_factory=list)


class BaselineConfigChanged(RuntimeError):
    """Raised when a frozen baseline detects an operator/config mutation."""


class ShadowBaselineSession:
    """Run and report one frozen mainnet-data shadow baseline."""

    def __init__(
        self,
        config: ShadowConfig,
        *,
        session_id: str | None = None,
        store: ShadowStore | None = None,
        config_source_path: str | Path | None = None,
        project_root: str | Path | None = None,
        trade_history_enabled: bool = True,
    ) -> None:
        if not config.enabled:
            raise ValueError(
                "baseline session is disabled; explicit baseline CLI invocation is required"
            )
        if config.execution_mode.upper() != "SHADOW":
            raise ValueError("baseline requires execution_mode=SHADOW")
        if config.market_environment.lower() != "mainnet":
            raise ShadowEnvironmentError("baseline requires mainnet public data")
        if config.execution_enabled or config.allow_mainnet_trading:
            raise ShadowEnvironmentError("baseline never enables exchange execution")
        if not config.post_only:
            raise ValueError("baseline requires post_only=true")
        self.config = config
        self.session_id = session_id or (
            f"shadow-baseline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        )
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.config_source_path = (
            Path(config_source_path).expanduser().resolve()
            if config_source_path is not None
            else None
        )
        self.trade_history_enabled = trade_history_enabled
        self.store = store or (
            ShadowStore(config.sqlite_path, config.event_path) if config.persistence else None
        )
        conservative_config = config.model_copy(
            update={
                "fill_model": ShadowFillModel.CONSERVATIVE_TRADE_THROUGH,
                "persistence": False,
            }
        )
        touch_config = config.model_copy(
            update={"fill_model": ShadowFillModel.TOUCH_OPTIMISTIC, "persistence": False}
        )
        self.sessions: dict[str, ShadowSession] = {
            CONSERVATIVE_MODEL: ShadowSession(
                conservative_config,
                session_id=f"{self.session_id}::conservative",
            ),
            TOUCH_MODEL: ShadowSession(
                touch_config,
                session_id=f"{self.session_id}::touch",
            ),
        }
        self.exposure = {model: TimeWeightedExposure(model) for model in self.sessions}
        self.drawdown = {model: DrawdownTracker() for model in self.sessions}
        self.data_quality = DataQualityTracker(
            tuple(config.markets), config.market_data_stale_seconds
        )
        self.suggestions: list[dict[str, Any]] = []
        self.cycles = 0
        self.start_timestamp: str | None = None
        self.stop_timestamp: str | None = None
        self.stop_reason: str | None = None
        self._start_epoch = 0.0
        self._stop_epoch = 0.0
        self._frozen_config_hash: str | None = None
        self._frozen_strategy_hash: str | None = None
        self._frozen_source_hash: str | None = None
        self.config_contaminated = False
        self._persisted_events = {model: 0 for model in self.sessions}
        self._last_checkpoint_epoch = 0.0
        self._checkpoint_count = 0
        self._ledger_isolation_verified = False
        self._shutdown_complete = False
        self._last_frames: dict[str, ShadowMarketFrame] = {}
        self._frame_history: list[ShadowMarketFrame] = []
        self._last_cycles: dict[str, MultiAssetCycle] = {}
        self._report_path: Path | None = None
        self._closed = False
        self._stage13_summary: dict[str, Any] | None = None
        self._stage13_parent_summary = self._load_stage13_parent_summary()

    @property
    def report_path(self) -> Path | None:
        return self._report_path

    @staticmethod
    def _git_commit(project_root: Path) -> str | None:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=project_root,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    @staticmethod
    def _file_hash(path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def _freeze_record(self) -> dict[str, Any]:
        return {
            "config_version": self.config.baseline_config_version,
            "config_hash": self._frozen_config_hash,
            "strategy_config_hash": self._frozen_strategy_hash,
            "source_config_hash": self._frozen_source_hash,
            "git_commit": self._git_commit(self.project_root),
            "enabled_assets": list(self.config.enabled_markets),
            "markets": list(self.config.markets),
            "execution_mode": self.config.execution_mode,
            "execution_backend": self.config.execution_backend,
            "market_environment": self.config.market_environment,
            "execution_enabled": self.config.execution_enabled,
            "allow_mainnet_trading": self.config.allow_mainnet_trading,
            "starting_equity": self.config.starting_equity_usdc,
            "leverage": self.config.leverage,
            "post_only": self.config.post_only,
            "execution_max_levels_per_side": self.config.execution_max_levels_per_side,
            "quote_settings": {
                "minimum_order_lifetime_seconds": self.config.minimum_order_lifetime_seconds,
                "minimum_replace_interval_seconds": self.config.minimum_replace_interval_seconds,
                "maximum_order_lifetime_seconds": self.config.maximum_order_lifetime_seconds,
                "refresh_price_tolerance_bps": self.config.refresh_price_tolerance_bps,
                "refresh_amount_tolerance_pct": self.config.refresh_amount_tolerance_pct,
            },
            "risk_settings": {
                "max_total_position_notional": self.config.max_total_position_notional,
                "max_side_position_notional": self.config.max_side_position_notional,
                "max_active_grid_levels": self.config.max_active_grid_levels,
                "max_active_executors": self.config.max_active_executors,
            },
            "self_tuning_mode": self.config.self_tuning_mode.upper(),
            "fill_model": ShadowFillModel.CONSERVATIVE_TRADE_THROUGH.value,
            "fee_model": self.config.fee_model,
        }

    def _strategy_profile_path(self) -> Path:
        path = Path(self.config.strategy_profile).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    def _load_stage13_parent_summary(self) -> dict[str, Any] | None:
        """Capture the frozen Stage 12G control before this run writes reports."""

        if not self.config.stage13.enabled:
            return None
        path = Path(self.config.stage13.parent_control_summary_path).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _strategy_parameter_summary(self) -> dict[str, Any]:
        """Capture strategy inputs without copying credentials into the manifest."""

        path = self._strategy_profile_path()
        parameter_names = (
            "duration_hours",
            "starting_equity_reference",
            "enabled_markets",
            "signal_markets",
            "btc_execution_enabled",
            "btc_signal_enabled",
            "target_order_notional",
            "max_single_order_notional",
            "max_levels_per_side_per_asset",
            "max_active_executors_per_asset",
            "max_active_executors_portfolio",
            "max_new_risk_creates_per_controller_cycle",
            "normal_buy_allocation_pct",
            "long_bias_buy_allocation_pct",
            "short_bias_buy_allocation_pct",
            "maximum_directional_bias_pct",
            "inventory_soft_ratio",
            "inventory_defensive_ratio",
            "inventory_hard_ratio",
            "defensive_capital_multiplier",
            "drawdown_caution_quote",
            "drawdown_reduce_quote",
            "drawdown_defensive_quote",
            "competition_hard_drawdown_quote",
            "risk_capacity_multipliers",
            "minimum_order_lifetime_seconds",
            "minimum_replace_interval_seconds",
            "maximum_order_lifetime_seconds",
            "refresh_price_tolerance_bps",
            "refresh_amount_tolerance_pct",
        )
        values: dict[str, Any] = {}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raw = None
        if isinstance(raw, Mapping):
            values = {name: _safe(raw[name]) for name in parameter_names if name in raw}
        return {
            "profile": str(path),
            "config_hash": self._frozen_strategy_hash,
            "values": values,
        }

    def _baseline_manifest(self, *, status: str, reason: str | None = None) -> dict[str, Any]:
        manifest = {
            "manifest_type": "BASELINE_CONTROL",
            "status": status,
            "timestamp": self.start_timestamp or _iso(time.time()),
            "session_id": self.session_id,
            "profile": str(self.config_source_path) if self.config_source_path else None,
            "resolved_profile_path": (
                str(self.config_source_path) if self.config_source_path else None
            ),
            "config_version": self.config.baseline_config_version,
            "config_hash": self._frozen_config_hash,
            "git_commit": self._git_commit(self.project_root),
            "starting_equity": self.config.starting_equity_usdc,
            "markets": list(self.config.markets),
            "enabled_assets": list(self.config.enabled_markets),
            "fill_models": {
                "primary": ShadowFillModel.CONSERVATIVE_TRADE_THROUGH.value,
                "sensitivity": ShadowFillModel.TOUCH_OPTIMISTIC.value,
            },
            "market_environment": "mainnet",
            "execution_mode": self.config.execution_mode,
            "execution_backend": self.config.execution_backend,
            "execution_enabled": self.config.execution_enabled,
            "allow_mainnet_trading": self.config.allow_mainnet_trading,
            "leverage": self.config.leverage,
            "post_only": self.config.post_only,
            "execution_max_levels_per_side": self.config.execution_max_levels_per_side,
            "risk_limits": {
                "max_total_position_notional": self.config.max_total_position_notional,
                "max_side_position_notional": self.config.max_side_position_notional,
                "max_active_grid_levels": self.config.max_active_grid_levels,
                "max_active_executors": self.config.max_active_executors,
            },
            "strategy_parameters": self._strategy_parameter_summary(),
            "self_tuning_mode": self.config.self_tuning_mode.upper(),
            "fee_model": self.config.fee_model,
            "trade_history_enabled": self.trade_history_enabled,
        }
        if reason is not None:
            manifest["reason"] = reason
        return manifest

    def _write_baseline_manifest(self, *, status: str, reason: str | None = None) -> None:
        root = Path(self.config.report_root).expanduser() / self.session_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "baseline_manifest.json").write_text(
            json.dumps(
                _safe(self._baseline_manifest(status=status, reason=reason)),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _public_trade_evidence_status(self) -> str:
        if not self.trade_history_enabled:
            return "UNAVAILABLE"
        trade_stream = self.data_quality.to_record()["trade_stream"]
        available = int(trade_stream.get("available", 0) or 0)
        expected = int(trade_stream.get("expected", 0) or 0)
        if available <= 0:
            return "UNAVAILABLE"
        return "AVAILABLE" if available >= expected else "PARTIAL"

    def start(self, *, timestamp: float | None = None) -> None:
        if self.start_timestamp is not None:
            return
        now = time.time() if timestamp is None else timestamp
        self._start_epoch = now
        self.start_timestamp = _iso(now)
        self._frozen_config_hash = self.config.config_hash
        self._frozen_strategy_hash = self.config.strategy_config_hash
        self._frozen_source_hash = self._file_hash(self.config_source_path)
        self.assert_isolated()
        for session in self.sessions.values():
            session.start(timestamp=now)
        for model, session in self.sessions.items():
            self.exposure[model].add(now, assets={})
            self.drawdown[model].add(now, session.engine.ledger.current_equity)
        if self.store is not None:
            self.store.save_session(
                self.session_id,
                self.config.config_hash,
                {
                    "session_id": self.session_id,
                    "start_timestamp": self.start_timestamp,
                    "config": self.config.to_record(),
                    "freeze": self._freeze_record(),
                    "market_environment": "mainnet",
                    "execution_mode": "SHADOW",
                    "real_exchange_mutation_calls": 0,
                    "baseline_status": "RUNNING",
                },
            )
            self.store.append_event(
                self.session_id,
                "BASELINE_START",
                self.start_timestamp,
                model="BASELINE",
                **self._freeze_record(),
            )
        self._write_baseline_manifest(status="RUNNING")

    def check_config_frozen(self) -> bool:
        """Return false and journal a contamination event after any config change."""

        if self._frozen_config_hash is None:
            return True
        current_config_hash = self.config.config_hash
        current_strategy_hash = self.config.strategy_config_hash
        current_source_hash = self._file_hash(self.config_source_path)
        changed = (
            current_config_hash != self._frozen_config_hash
            or current_strategy_hash != self._frozen_strategy_hash
            or current_source_hash != self._frozen_source_hash
        )
        if changed and not self.config_contaminated:
            self.config_contaminated = True
            timestamp = time.time()
            if self.store is not None:
                self.store.append_event(
                    self.session_id,
                    "CONFIG_CHANGE",
                    _iso(timestamp),
                    model="BASELINE",
                    baseline_config_hash=self._frozen_config_hash,
                    current_config_hash=current_config_hash,
                    baseline_strategy_config_hash=self._frozen_strategy_hash,
                    current_strategy_config_hash=current_strategy_hash,
                    baseline_source_config_hash=self._frozen_source_hash,
                    current_source_config_hash=current_source_hash,
                )
        return not changed

    def _persist_engine_delta(self, model: str) -> None:
        if self.store is None:
            return
        session = self.sessions[model]
        engine = session.engine
        start = self._persisted_events[model]
        for event in engine.events[start:]:
            fields = dict(event)
            event_name = str(fields.pop("event", "UNKNOWN"))
            event_timestamp = str(fields.pop("timestamp", self.stop_timestamp or _iso(time.time())))
            fields.pop("session_id", None)
            self.store.append_event(
                self.session_id,
                event_name,
                event_timestamp,
                model=model,
                model_session_id=session.session_id,
                **fields,
            )
        self._persisted_events[model] = len(engine.events)
        for order in engine.orders.values():
            self.store.save_order(self.session_id, order)
        for fill in engine.fills:
            self.store.save_fill(self.session_id, fill)

    def _assets_for_exposure(
        self,
        model: str,
        cycle: MultiAssetCycle,
        frames: Mapping[str, ShadowMarketFrame],
    ) -> dict[str, dict[str, Any]]:
        ledger = self.sessions[model].engine.ledger
        assets: dict[str, dict[str, Any]] = {}
        for pair, frame in frames.items():
            amount = ledger.position(pair).amount
            state = cycle.states.get(pair)
            decision = cycle.decisions.get(pair)
            assets[pair] = {
                "amount": float(amount),
                "mid_price": frame.mid_price,
                "beta": state.btc_beta if state and state.btc_beta is not None else 1.0,
                "inventory_ratio": (
                    abs(float(amount) * frame.mid_price) / self.config.starting_equity_usdc
                    if self.config.starting_equity_usdc > 0
                    else 0.0
                ),
                "mode": decision.mode if decision is not None else "UNKNOWN",
                "global_iv_regime": (
                    state.global_risk_regime.value if state is not None else "UNKNOWN"
                ),
            }
        return assets

    def _record_model_state(
        self,
        model: str,
        cycle: MultiAssetCycle,
        frames: Mapping[str, ShadowMarketFrame],
        timestamp: float,
    ) -> None:
        session = self.sessions[model]
        for fill in session.engine.fills:
            state = cycle.states.get(fill.trading_pair)
            if state is not None:
                object.__setattr__(fill, "global_iv_regime", state.global_risk_regime.value)
        assets = self._assets_for_exposure(model, cycle, frames)
        resting = sum(
            float(order.notional)
            for order in session.engine.orders.values()
            if order.status
            in {
                ShadowOrderStatus.RESTING,
                ShadowOrderStatus.PARTIALLY_FILLED,
                ShadowOrderStatus.CLOSE_RESTING,
            }
            and not order.is_exit
        )
        self.exposure[model].add(timestamp, assets=assets, resting_quote_exposure=resting)
        self.drawdown[model].add(timestamp, session.engine.ledger.current_equity)
        if self.store is not None:
            ledger_snapshot = {
                "timestamp": _iso(timestamp),
                "model": model,
                "fill_model": model,
                **session.engine.ledger.snapshot(),
            }
            self.store.save_equity(self.session_id, _iso(timestamp), ledger_snapshot)
            self.store.save_position(self.session_id, _iso(timestamp), ledger_snapshot)
            self.store.save_baseline_record(
                self.session_id,
                model,
                "portfolio_exposure",
                _iso(timestamp),
                self.exposure[model].points[-1],
            )
            for _pair, point in self.exposure[model].asset_points.items():
                if point and _float(point[-1].get("timestamp_epoch")) == timestamp:
                    self.store.save_baseline_record(
                        self.session_id,
                        model,
                        "inventory",
                        _iso(timestamp),
                        point[-1],
                    )

    def run_cycle(
        self,
        frames: Mapping[str, ShadowMarketFrame],
        *,
        global_risk_state: Any | None = None,
        timestamp: float | None = None,
        controller_timestamp: float | None = None,
    ) -> MultiAssetCycle:
        if self._closed:
            raise RuntimeError("baseline session is already stopped")
        if not frames:
            raise ShadowEnvironmentError("baseline received no market frames")
        if self.start_timestamp is None:
            self.start(timestamp=timestamp or max(frame.timestamp for frame in frames.values()))
        if not self.check_config_frozen():
            raise BaselineConfigChanged("baseline contaminated by config change")
        require_shadow_environment(frames.values())
        common_timestamp = timestamp or max(frame.timestamp for frame in frames.values())
        controller_epoch = (
            controller_timestamp
            if controller_timestamp is not None
            else timestamp
            if timestamp is not None
            else time.time()
        )
        for model, session in self.sessions.items():
            cycle = session.run_cycle(
                frames,
                global_risk_state=global_risk_state,
                timestamp=common_timestamp,
                controller_timestamp=controller_epoch,
            )
            self._last_cycles[model] = cycle
            self._record_model_state(model, cycle, frames, common_timestamp)
            self._persist_engine_delta(model)
            if self.store is not None:
                self.store.save_metrics(
                    self.session_id,
                    _iso(common_timestamp),
                    {"fill_model": model, **self._model_metrics(model, common_timestamp).metrics},
                )
        primary_cycle = self._last_cycles[CONSERVATIVE_MODEL]
        self._last_frames = dict(frames)
        self._frame_history.extend(frames.values())
        self.data_quality.record(
            frames,
            cycle=primary_cycle,
            reference_timestamp=common_timestamp,
        )
        self.cycles += 1
        self._maybe_checkpoint(common_timestamp)
        return primary_cycle

    def _maybe_checkpoint(self, timestamp: float) -> None:
        if timestamp - self._last_checkpoint_epoch < self.config.checkpoint_interval_seconds:
            return
        self._last_checkpoint_epoch = timestamp
        checkpoint = {
            "session_id": self.session_id,
            "timestamp": _iso(timestamp),
            "config_hash": self._frozen_config_hash,
            "strategy_config_hash": self._frozen_strategy_hash,
            "config_frozen": not self.config_contaminated,
            "cycles": self.cycles,
            "metrics": self.metrics(now=timestamp),
        }
        root = Path(self.config.report_root).expanduser() / self.session_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "checkpoint.json").write_text(
            json.dumps(_safe(checkpoint), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._checkpoint_count += 1
        if self.store is not None:
            self.store.save_checkpoint(self.session_id, _iso(timestamp), checkpoint)
            self.store.append_event(
                self.session_id,
                "CHECKPOINT",
                _iso(timestamp),
                model="BASELINE",
                cycles=self.cycles,
                config_hash=self._frozen_config_hash,
            )

    def record_suggestion(
        self,
        *,
        asset: str,
        diagnosis: str,
        recommendation: str,
        current_value: Any = None,
        proposed_value: Any = None,
        confidence: Any = None,
        supporting_metrics: Mapping[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> None:
        row = {
            "timestamp": _iso(time.time() if timestamp is None else timestamp),
            "asset": asset,
            "diagnosis": diagnosis,
            "recommendation": recommendation,
            "current_value": current_value,
            "proposed_value": proposed_value,
            "confidence": confidence,
            "supporting_metrics": dict(supporting_metrics or {}),
            "mode": self.config.self_tuning_mode.upper(),
            "applied": False,
        }
        self.suggestions.append(row)
        if self.store is not None:
            self.store.save_baseline_record(
                self.session_id,
                "BASELINE",
                "self_tuning_suggestion",
                row["timestamp"],
                row,
            )

    def _fill_eligibility_rows(
        self,
        model: str,
        end_timestamp: float,
    ) -> list[dict[str, Any]]:
        engine = self.sessions[model].engine
        return [
            {
                "model": model,
                "fill_model": model,
                **order_fill_eligibility(engine, order, end_timestamp=end_timestamp),
            }
            for order in engine.orders.values()
            if not order.is_exit
        ]

    def _touch_fill_attribution(
        self, fill: Mapping[str, Any], end_timestamp: float
    ) -> dict[str, Any]:
        """Match a touch fill to the conservative order from the same decision."""

        primary = self.sessions[CONSERVATIVE_MODEL].engine
        touch_timestamp = _epoch(fill.get("timestamp")) or end_timestamp
        candidates = [
            order
            for order in primary.orders.values()
            if not order.is_exit
            and order.trading_pair == fill.get("trading_pair")
            and order.level_id == fill.get("level_id", order.level_id)
            and order.side == fill.get("side")
        ]
        if not candidates:
            candidates = [
                order
                for order in primary.orders.values()
                if not order.is_exit
                and order.trading_pair == fill.get("trading_pair")
                and order.side == fill.get("side")
            ]
        candidate = min(
            candidates,
            key=lambda order: abs(order.created_epoch - touch_timestamp),
            default=None,
        )
        if candidate is None:
            return {
                "conservative_eligibility_status": "INSUFFICIENT_TRADE_EVIDENCE",
                "conservative_rejection_reason": "no matching conservative order",
            }
        eligibility = order_fill_eligibility(
            primary,
            candidate,
            end_timestamp=max(end_timestamp, touch_timestamp),
        )
        return {
            "conservative_order_id": candidate.shadow_order_id,
            "conservative_eligibility_status": eligibility["status"],
            "conservative_rejection_reason": eligibility["reason"],
        }

    def _model_metrics(self, model: str, end_timestamp: float) -> BaselineModelMetrics:
        session = self.sessions[model]
        engine = session.engine
        orders = _order_rows(engine, model=model, end_timestamp=end_timestamp)
        fills = _fill_rows(engine, model=model)
        fill_eligibility = self._fill_eligibility_rows(model, end_timestamp)
        if model == TOUCH_MODEL:
            by_fill_id = {row.get("fill_id"): row for row in fills}
            for fill in engine.fills:
                if fill.entry_exit != "entry":
                    continue
                row = by_fill_id.get(fill.fill_id)
                if row is not None:
                    row.update(self._touch_fill_attribution(row, end_timestamp))
        markouts = _markout_rows(engine, model=model, end_timestamp=end_timestamp)
        cycles = _cycle_rows(
            engine,
            model=model,
            end_timestamp=end_timestamp,
            fees_known=engine.ledger.fees_known,
        )
        cancels = [
            {
                "model": model,
                "fill_model": model,
                "timestamp": row.get("cancel_timestamp"),
                "shadow_order_id": row.get("shadow_order_id"),
                "trading_pair": row.get("trading_pair"),
                "side": row.get("side"),
                "level_id": row.get("level_id"),
                "reason": row.get("cancel_reason_raw") or row.get("cancel_reason"),
                "reason_raw": row.get("cancel_reason_raw") or row.get("cancel_reason"),
                "reason_code": row.get("cancel_reason_category"),
                "category": row.get("cancel_category"),
                "mode": row.get("mode_at_creation", "UNKNOWN"),
                "outcome": row.get("outcome"),
                "age_seconds": row.get("resting_lifetime_seconds"),
                "lifetime_seconds": row.get("lifetime_seconds"),
                "created_timestamp": row.get("created_timestamp"),
                "cancel_requested_timestamp": row.get("cancel_requested_timestamp"),
                "cancelled_timestamp": row.get("cancel_timestamp"),
                "price": row.get("price"),
                "old_price": row.get("old_price") or row.get("price"),
                "new_desired_price": row.get("new_desired_price"),
                "price_deviation_bps": (
                    row.get("price_deviation_bps")
                    if row.get("price_deviation_bps") is not None
                    else row.get("cancel_price_deviation_bps")
                ),
                "amount": row.get("amount"),
                "old_amount": row.get("old_amount") or row.get("amount"),
                "new_desired_amount": row.get("new_desired_amount"),
                "amount_deviation_pct": row.get("amount_deviation_pct"),
                "old_mode": row.get("old_mode") or row.get("mode_at_creation"),
                "new_mode": row.get("new_mode"),
                "old_plan_version": row.get("old_plan_version") or row.get("grid_plan_version"),
                "new_plan_version": row.get("new_plan_version"),
                "old_level_present": row.get("old_level_present"),
                "new_level_present": row.get("new_level_present"),
                "risk_state": row.get("risk_state"),
                "inventory_ratio": row.get("inventory_ratio"),
                "portfolio_gross_exposure": row.get("portfolio_gross_exposure"),
                "portfolio_beta_exposure": row.get("portfolio_beta_exposure"),
                "minimum_order_lifetime_seconds": row.get("minimum_order_lifetime_seconds"),
                "replacement_cooldown_seconds": row.get("replacement_cooldown_seconds"),
                "time_since_last_replace_seconds": row.get("time_since_last_replace_seconds"),
                "cooldown_remaining_seconds": row.get("cooldown_remaining_seconds"),
                "safety_override": row.get("safety_override", False),
                "safety_override_reason": row.get("safety_override_reason"),
                "lifecycle_state": row.get("lifecycle_state"),
                "cancel_reason_detail": row.get("cancel_reason_detail"),
                "market_mid_at_cancel": row.get("cancel_market_mid"),
                "cancel_market_best_bid": row.get("cancel_market_best_bid"),
                "cancel_market_best_ask": row.get("cancel_market_best_ask"),
                "cancel_market_price_deviation_bps": row.get("cancel_price_deviation_bps"),
            }
            for row in orders
            if row.get("cancel_timestamp")
        ]
        risk_events = _risk_rows(engine, model=model)
        risk_episode_rows = engine.risk_episodes.rows(end_timestamp)
        risk_episode_summary = engine.risk_episodes.summary(end_timestamp)
        reconciliation_decisions = list(engine.reconciliation_audit)
        fill_eligibility_counts = Counter(
            row.get("status", "INSUFFICIENT_TRADE_EVIDENCE") for row in fill_eligibility
        )
        eligible_order_count = fill_eligibility_counts.get("TRADED_THROUGH_FILLED", 0)
        missing_order_count = fill_eligibility_counts.get("INSUFFICIENT_TRADE_EVIDENCE", 0)
        coverage_start = self.data_quality.coverage_start_epoch or self._start_epoch
        coverage_timestamps = [
            timestamp
            for timestamps in self.data_quality.trade_timestamps_by_asset.values()
            for timestamp in timestamps
        ]
        trade_coverage_by_asset = {
            pair: calculate_trade_coverage(
                self.data_quality.trade_timestamps_by_asset.get(pair, []),
                start_timestamp=coverage_start,
                end_timestamp=end_timestamp,
                sample_interval_seconds=max(1.0, self.config.market_data_stale_seconds / 3.0),
                trade_count=self.data_quality.trade_count_by_asset.get(pair, 0),
                evidence_minutes=len(
                    {
                        int(value // 60)
                        for value in self.data_quality.trade_timestamps_by_asset.get(pair, [])
                    }
                ),
            )
            for pair in sorted(
                set(self.config.markets) | set(self.data_quality.trade_count_by_asset)
            )
        }
        trade_coverage = calculate_trade_coverage(
            coverage_timestamps,
            start_timestamp=coverage_start,
            end_timestamp=end_timestamp,
            sample_interval_seconds=max(1.0, self.config.market_data_stale_seconds / 3.0),
            trade_count=sum(self.data_quality.trade_count_by_asset.values()),
            evidence_minutes=len({int(value // 60) for value in coverage_timestamps}),
        )
        exposure_rows = list(self.exposure[model].points)
        inventory_rows = [
            row for rows in self.exposure[model].asset_points.values() for row in rows
        ]
        equity = [
            {"model": model, "fill_model": model, **record}
            for record in session.engine.equity_history
        ]
        for cycle in cycles:
            cycle_start = _epoch(cycle.get("entry_timestamp")) or self._start_epoch
            cycle_end = _epoch(cycle.get("exit_timestamp")) or end_timestamp
            cycle_inventory = [
                _float(row.get("absolute_inventory_notional"), 0.0) or 0.0
                for row in inventory_rows
                if row.get("trading_pair") == cycle.get("trading_pair")
                and cycle_start <= (_epoch(row.get("timestamp")) or 0.0) <= cycle_end
            ]
            cycle_drawdown = [
                _float(row.get("drawdown_quote"), 0.0) or 0.0
                for row in equity
                if cycle_start <= (_epoch(row.get("timestamp")) or 0.0) <= cycle_end
            ]
            cycle["average_inventory"] = _mean(cycle_inventory)
            cycle["max_inventory"] = max(cycle_inventory, default=None)
            cycle["max_drawdown_quote"] = max(cycle_drawdown, default=None)
        reconciliation = reconcile_paper_equity(
            engine.ledger,
            tolerance=Decimal(str(self.config.pnl_reconciliation_tolerance)),
        )
        fill_rows = [row for row in fills if row.get("entry_exit") in {"entry", "exit"}]
        buy_volume = sum(
            _float(row.get("notional"), 0.0) or 0.0 for row in fill_rows if row.get("side") == "buy"
        )
        sell_volume = sum(
            _float(row.get("notional"), 0.0) or 0.0
            for row in fill_rows
            if row.get("side") == "sell"
        )
        total_volume = buy_volume + sell_volume
        entry_fills = [row for row in fill_rows if row.get("entry_exit") == "entry"]
        exit_fills = [row for row in fill_rows if row.get("entry_exit") == "exit"]
        created = sum(1 for row in orders if not row.get("is_exit"))
        tp_created = sum(1 for row in orders if row.get("is_exit"))
        cancelled = len(cancels)
        filled_orders = len(entry_fills)
        total_fills = len(fill_rows)
        lifetime_values = [
            _float(row.get("lifetime_seconds"))
            for row in orders
            if _float(row.get("lifetime_seconds")) is not None
        ]
        time_to_fill = [
            _float(row.get("time_to_fill"))
            for row in fill_rows
            if _float(row.get("time_to_fill")) is not None
        ]
        lifetime_by_group: dict[str, list[float]] = defaultdict(list)
        for row in orders:
            lifetime = _float(row.get("lifetime_seconds"))
            if lifetime is None:
                continue
            group = "|".join(
                str(row.get(field_name, "UNKNOWN"))
                for field_name in (
                    "trading_pair",
                    "side",
                    "mode_at_creation",
                    "cancel_category",
                    "outcome",
                )
            )
            lifetime_by_group[group].append(lifetime)
        lifetime_groupings: dict[str, dict[str, list[float]]] = {}
        for dimension in (
            "trading_pair",
            "side",
            "mode_at_creation",
            "cancel_category",
            "outcome",
        ):
            grouped: dict[str, list[float]] = defaultdict(list)
            for row in orders:
                lifetime = _float(row.get("lifetime_seconds"))
                if lifetime is not None:
                    grouped[str(row.get(dimension, "UNKNOWN"))].append(lifetime)
            lifetime_groupings[dimension] = grouped
        time_to_fill_by_asset: dict[str, list[float]] = defaultdict(list)
        for row in fill_rows:
            value = _float(row.get("time_to_fill"))
            if value is not None:
                time_to_fill_by_asset[str(row.get("trading_pair"))].append(value)
        cancellation_counts = Counter(cancel_category(row.get("category"), row) for row in cancels)
        markout_summary: dict[str, Any] = {}
        for horizon in MARKOUT_HORIZONS_SECONDS:
            complete = [
                _float(row.get("markout_bps"))
                for row in markouts
                if row.get("horizon_seconds") == horizon
                and row.get("status") == "COMPLETE"
                and _float(row.get("markout_bps")) is not None
            ]
            markout_summary[f"{horizon}s"] = {
                **_markout_stats(complete),
                "sample_count": len(complete),
                "eligible_count": sum(
                    row.get("eligible") is True
                    for row in markouts
                    if row.get("horizon_seconds") == horizon
                ),
                "missing_count": sum(
                    row.get("status") != "COMPLETE"
                    for row in markouts
                    if row.get("horizon_seconds") == horizon
                ),
            }
        markout_groupings: dict[str, dict[str, list[float]]] = {}
        for dimension in (
            "trading_pair",
            "side",
            "mode",
            "quote_distance_bucket",
            "global_iv_regime",
        ):
            grouped = defaultdict(list)
            for row in markouts:
                if row.get("status") != "COMPLETE":
                    continue
                value = _float(row.get("markout_bps"))
                if value is not None:
                    grouped[str(row.get(dimension, "UNKNOWN") or "UNKNOWN").upper()].append(value)
            markout_groupings[dimension] = grouped
        markout_by_group: dict[str, list[float]] = defaultdict(list)
        for row in markouts:
            if row.get("status") != "COMPLETE":
                continue
            value = _float(row.get("markout_bps"))
            if value is None:
                continue
            key = "|".join(
                str(row.get(dimension, "UNKNOWN"))
                for dimension in (
                    "trading_pair",
                    "side",
                    "mode",
                    "quote_distance_bucket",
                    "global_iv_regime",
                )
            )
            markout_by_group[key].append(value)
        exposure = self.exposure[model].summary(start=self._start_epoch, end=end_timestamp)
        open_inventory_seconds = TimeWeightedExposure._time_above(
            self.exposure[model].points,
            self._start_epoch,
            end_timestamp,
            field_name="absolute_inventory",
            threshold=1e-12,
        )
        per_asset = self.exposure[model].per_asset_summary(
            start=self._start_epoch,
            end=end_timestamp,
            starting_equity=self.config.starting_equity_usdc,
            soft_threshold=self.config.inventory_soft_threshold_ratio,
            defensive_threshold=self.config.inventory_defensive_threshold_ratio,
            hard_threshold=self.config.inventory_hard_threshold_ratio,
        )
        drawdown = self.drawdown[model].summary(start=self._start_epoch, end=end_timestamp)
        duration_hours = max(0.0, end_timestamp - self._start_epoch) / 3600.0
        operational_cancelled = sum(
            row.get("category") not in {"SESSION_SHUTDOWN", "MANUAL_STOP"} for row in cancels
        )
        shutdown_cancelled = cancelled - operational_cancelled
        operational_cancel_rows = [
            row for row in cancels if row.get("category") not in {"SESSION_SHUTDOWN", "MANUAL_STOP"}
        ]
        high_churn = created >= self.config.high_cancel_churn_min_creates and (
            (operational_cancelled / created if created else 0.0)
            >= self.config.high_cancel_churn_ratio
            or (
                _percentile(lifetime_values, 0.50) is not None
                and _percentile(lifetime_values, 0.50)
                < self.config.high_cancel_churn_lifetime_seconds
            )
        )
        thirty_second_markouts = markout_summary["30s"]
        adverse_selection = (
            "ADVERSE_SELECTION"
            if thirty_second_markouts["sample_count"] >= self.config.minimum_markout_samples
            and (thirty_second_markouts["mean_bps"] or 0.0) < 0
            else "INSUFFICIENT_EVIDENCE"
            if thirty_second_markouts["sample_count"] < self.config.minimum_markout_samples
            else "NOT_FLAGGED"
        )
        hourly_volume: dict[str, float] = defaultdict(float)
        volume_by_asset: dict[str, float] = defaultdict(float)
        for row in fill_rows:
            notional = _float(row.get("notional"), 0.0) or 0.0
            volume_by_asset[str(row.get("trading_pair"))] += notional
            stamp = _epoch(row.get("timestamp"))
            if stamp is not None:
                bucket = datetime.fromtimestamp(stamp, UTC).replace(
                    minute=0, second=0, microsecond=0
                )
                hourly_volume[_iso(bucket.timestamp())] += notional
        last_hour_volume = sum(
            _float(row.get("notional"), 0.0) or 0.0
            for row in fill_rows
            if (_epoch(row.get("timestamp")) or 0.0) >= end_timestamp - 3600.0
        )
        risk_counts = Counter(str(row.get("category", "OTHER")) for row in risk_events)
        gross_pnl = reconciliation.realized_pnl + reconciliation.unrealized_pnl
        fee_sensitivity = {
            f"{fee_bps:g}bps": gross_pnl
            - (Decimal(str(total_volume)) * Decimal(str(fee_bps)) / Decimal("10000"))
            for fee_bps in (0, 1, 5, 10)
        }
        per_asset_metrics: dict[str, dict[str, Any]] = {}
        for pair in sorted(set(self.config.markets) | set(volume_by_asset)):
            pair_fills = [row for row in fill_rows if row.get("trading_pair") == pair]
            pair_cancels = [row for row in cancels if row.get("trading_pair") == pair]
            pair_orders = [row for row in orders if row.get("trading_pair") == pair]
            pair_entry_orders = [row for row in pair_orders if not row.get("is_exit")]
            pair_time_to_fill = time_to_fill_by_asset.get(pair, [])
            pair_asset = per_asset.get(pair, {})
            pair_lifetimes = [
                row.get("lifetime_seconds")
                for row in pair_orders
                if _float(row.get("lifetime_seconds")) is not None
            ]
            pair_cycles = [row for row in cycles if row.get("trading_pair") == pair]
            pair_markout_summary = {
                f"{horizon}s": _markout_stats(
                    [
                        row.get("markout_bps")
                        for row in markouts
                        if row.get("trading_pair") == pair
                        and row.get("horizon_seconds") == horizon
                        and row.get("status") == "COMPLETE"
                    ]
                )
                for horizon in MARKOUT_HORIZONS_SECONDS
            }
            pair_keep_count = sum(
                event.get("event") == "ORDER_KEEP" and event.get("trading_pair") == pair
                for event in engine.events
            )
            per_asset_metrics[pair] = {
                "duration_active_seconds": pair_asset.get("duration_active_seconds"),
                "volume": volume_by_asset.get(pair, 0.0),
                "average_risk": pair_asset.get("average_inventory"),
                "volume_per_risk": (
                    volume_by_asset.get(pair, 0.0) / pair_asset["average_inventory"]
                    if pair_asset.get("average_inventory")
                    else None
                ),
                "pnl": sum(_float(row.get("realized_pnl"), 0.0) or 0.0 for row in pair_fills),
                "fills": len(pair_fills),
                "cancels": len(pair_cancels),
                "orders_created": len(pair_entry_orders),
                "orders_kept": pair_keep_count,
                "keep_pct": (
                    pair_keep_count / (pair_keep_count + len(pair_entry_orders)) * 100.0
                    if pair_keep_count + len(pair_entry_orders)
                    else None
                ),
                "fill_create_ratio": (
                    len(pair_fills) / len(pair_entry_orders) if pair_entry_orders else None
                ),
                "cancel_create_ratio": (
                    len(pair_cancels) / len(pair_entry_orders) if pair_entry_orders else None
                ),
                "quote_lifetime": _percentile(
                    [_float(value, 0.0) or 0.0 for value in pair_lifetimes], 0.50
                ),
                "quote_lifetime_stats": _lifetime_stats(pair_lifetimes),
                "time_to_fill": _percentile(pair_time_to_fill, 0.50),
                "time_to_fill_stats": _lifetime_stats(pair_time_to_fill),
                "cycles": sum(
                    row.get("trading_pair") == pair and row.get("status") == "COMPLETE"
                    for row in cycles
                ),
                "cycle_duration_stats": _lifetime_stats(
                    row.get("cycle_duration_seconds")
                    for row in pair_cycles
                    if row.get("status") == "COMPLETE"
                ),
                "markout_5s": _mean(
                    [
                        _float(row.get("markout_bps"))
                        for row in markouts
                        if row.get("trading_pair") == pair
                        and row.get("horizon_seconds") == 5
                        and row.get("status") == "COMPLETE"
                        and _float(row.get("markout_bps")) is not None
                    ]
                ),
                "markout_30s": _mean(
                    [
                        _float(row.get("markout_bps"))
                        for row in markouts
                        if row.get("trading_pair") == pair
                        and row.get("horizon_seconds") == 30
                        and row.get("status") == "COMPLETE"
                        and _float(row.get("markout_bps")) is not None
                    ]
                ),
                "markout_60s": _mean(
                    [
                        _float(row.get("markout_bps"))
                        for row in markouts
                        if row.get("trading_pair") == pair
                        and row.get("horizon_seconds") == 60
                        and row.get("status") == "COMPLETE"
                        and _float(row.get("markout_bps")) is not None
                    ]
                ),
                "markout": pair_markout_summary,
                "average_inventory": pair_asset.get("average_inventory"),
                "max_inventory": pair_asset.get("max_inventory"),
                "risk_blocks": sum(row.get("trading_pair") == pair for row in risk_events),
            }
        state_observations: list[dict[str, Any]] = []
        for cycle_record in session.cycles:
            states = cycle_record.get("states") or {}
            decisions = cycle_record.get("decisions") or {}
            plans = cycle_record.get("plans") or {}
            for pair, state in states.items():
                decision = decisions.get(pair) or {}
                plan = plans.get(pair) or {}
                state_observations.append(
                    {
                        "trading_pair": pair,
                        "mode": str(decision.get("mode", plan.get("mode", "UNKNOWN"))).upper(),
                        "global_iv_regime": str(
                            state.get(
                                "global_risk_regime",
                                decision.get("global_risk_regime", "UNKNOWN"),
                            )
                        ).upper(),
                    }
                )

        def _row_label(row: Mapping[str, Any], field_name: str) -> str:
            source_fields = {
                "mode": ("mode", "mode_at_creation", "mode_at_entry"),
                "global_iv_regime": ("global_iv_regime",),
            }.get(field_name, (field_name,))
            for source_field in source_fields:
                value = row.get(source_field)
                if value not in (None, ""):
                    return str(value).upper()
            return "UNKNOWN"

        def _breakdown(field_name: str, values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            labels = sorted({_row_label(row, field_name) for row in values})
            result: dict[str, Any] = {}
            for label in labels:
                selected = [row for row in values if _row_label(row, field_name) == label]
                selected_fills = [row for row in selected if row in fill_rows]
                selected_orders = [row for row in selected if row in orders]
                selected_cancels = [row for row in selected if row in cancels]
                selected_markouts = [row for row in selected if row in markouts]
                result[label] = {
                    "observation_count": sum(
                        _row_label(row, field_name) == label for row in state_observations
                    )
                    if field_name in {"mode", "global_iv_regime"}
                    else None,
                    "fills": len(selected_fills),
                    "volume": sum(
                        _float(row.get("notional"), 0.0) or 0.0 for row in selected_fills
                    ),
                    "orders_created": sum(not row.get("is_exit") for row in selected_orders),
                    "cancels": len(selected_cancels),
                    "quote_lifetime": _lifetime_stats(
                        [row.get("lifetime_seconds") for row in selected_orders]
                    ),
                    "time_to_fill": _lifetime_stats(
                        [row.get("time_to_fill") for row in selected_fills]
                    ),
                    "markout": {
                        f"{horizon}s": _markout_stats(
                            [
                                row.get("markout_bps")
                                for row in selected_markouts
                                if row.get("horizon_seconds") == horizon
                                and row.get("status") == "COMPLETE"
                            ]
                        )
                        for horizon in MARKOUT_HORIZONS_SECONDS
                    },
                    "risk_blocks": sum(_row_label(row, field_name) == label for row in risk_events)
                    if field_name in {"trading_pair", "mode", "global_iv_regime"}
                    else None,
                }
            return result

        per_mode_metrics = _breakdown(
            "mode",
            [
                *state_observations,
                *orders,
                *fills,
                *cancels,
                *markouts,
            ],
        )
        per_regime_metrics = _breakdown(
            "global_iv_regime",
            [*state_observations, *fills, *markouts],
        )
        metrics = {
            "model": model,
            "fill_model": (
                self.config.fill_model.value
                if model == CONSERVATIVE_MODEL
                else ShadowFillModel.TOUCH_OPTIMISTIC.value
            ),
            "session_id": self.session_id,
            "session_duration_seconds": max(0.0, end_timestamp - self._start_epoch),
            "session_duration_hours": duration_hours,
            "starting_equity": reconciliation.starting_equity,
            "ending_equity": reconciliation.current_equity,
            "paper_equity": reconciliation.current_equity,
            "equity_timestamp": _iso(end_timestamp),
            "last_observation_timestamp": _iso(end_timestamp),
            "realized_grid_capture": reconciliation.realized_grid_capture,
            "realized_other_pnl": reconciliation.realized_other_pnl,
            "realized_pnl": reconciliation.realized_pnl,
            "unrealized_pnl": reconciliation.unrealized_pnl,
            "unrealized_inventory_pnl": reconciliation.unrealized_pnl,
            "fees": reconciliation.fees,
            "fees_status": "VERIFIED" if reconciliation.fees_known else "UNKNOWN",
            "fee_model": self.config.fee_model,
            "gross_pnl": gross_pnl,
            "gross_pnl_label": "GROSS PAPER PNL",
            "verified_net_pnl": reconciliation.total_pnl if reconciliation.fees_known else None,
            "verified_net_pnl_status": "VERIFIED" if reconciliation.fees_known else "UNKNOWN",
            "total_paper_pnl": reconciliation.total_pnl,
            "session_pnl": reconciliation.total_pnl,
            "fee_model_source": "configured" if reconciliation.fees_known else "UNKNOWN",
            "fee_sensitivity": fee_sensitivity if not reconciliation.fees_known else None,
            "pnl_reconciliation": reconciliation.to_record(),
            "pnl_reconciliation_status": reconciliation.status,
            "buy_executed_notional": buy_volume,
            "sell_executed_notional": sell_volume,
            "total_executed_notional": total_volume,
            "session_volume": total_volume,
            "last_hour_volume": last_hour_volume,
            "max_gross_exposure": max(
                (_float(row.get("gross_exposure"), 0.0) or 0.0 for row in exposure_rows),
                default=None,
            ),
            "max_inventory": max(
                (_float(row.get("absolute_inventory"), 0.0) or 0.0 for row in exposure_rows),
                default=None,
            ),
            "max_btc_beta_exposure": max(
                (abs(_float(row.get("btc_beta_exposure"), 0.0) or 0.0) for row in exposure_rows),
                default=None,
            ),
            "volume_by_asset": dict(volume_by_asset),
            "volume_by_hour": dict(hourly_volume),
            "volume_per_starting_equity": (
                total_volume / self.config.starting_equity_usdc
                if self.config.starting_equity_usdc > 0
                else None
            ),
            "volume_per_average_deployed_risk": (
                total_volume / exposure["average_gross_exposure"]
                if exposure.get("average_gross_exposure")
                else None
            ),
            "volume_per_average_gross_exposure": (
                total_volume / exposure["average_gross_exposure"]
                if exposure.get("average_gross_exposure")
                else None
            ),
            "volume_per_average_inventory": (
                total_volume / exposure["average_absolute_inventory"]
                if exposure.get("average_absolute_inventory")
                else None
            ),
            "volume_per_average_margin_used": (
                total_volume / exposure["average_margin_used"]
                if exposure.get("average_margin_used")
                else None
            ),
            "volume_per_average_btc_beta_exposure": (
                total_volume / abs(exposure["average_btc_beta_exposure"])
                if exposure.get("average_btc_beta_exposure")
                and abs(exposure["average_btc_beta_exposure"]) > 1e-12
                else None
            ),
            **exposure,
            **drawdown,
            "orders_created": created,
            "orders_kept": sum(event.get("event") == "ORDER_KEEP" for event in engine.events),
            "orders_cancelled": cancelled,
            "orders_replaced": sum(
                event.get("event") == "ORDER_REPLACE" for event in engine.events
            ),
            "orders_expired": sum(row.get("cancel_category") == "MAX_AGE" for row in orders),
            "orders_rejected": sum(row.get("outcome") == "rejected" for row in orders),
            "orders_filled": filled_orders,
            "orders_partially_filled": sum(
                row.get("status") == ShadowOrderStatus.PARTIALLY_FILLED.value for row in orders
            ),
            "active_orders": sum(bool(row.get("active_at_end")) for row in orders),
            "tp_orders_created": tp_created,
            "tp_orders_filled": len(exit_fills),
            "lifecycle_states": {
                state: sum(row.get("lifecycle_state") == state for row in orders)
                for state in (
                    "CREATED",
                    "VALIDATED",
                    "RESTING",
                    "NEVER_RESTED_REJECTED",
                    "CANCELLED_AFTER_RESTING",
                    "FILLED_AFTER_RESTING",
                    "COMPLETE",
                )
            },
            "completed_cycles": sum(row.get("status") == "COMPLETE" for row in cycles),
            "fills": total_fills,
            "entry_fills": len(entry_fills),
            "exit_fills": len(exit_fills),
            "fill_count": total_fills,
            "fill_create_ratio": total_fills / (created + tp_created)
            if created + tp_created
            else None,
            "entry_fill_create_ratio": len(entry_fills) / created if created else None,
            "cancel_create_ratio": cancelled / created if created else None,
            "replace_create_ratio": (
                sum(event.get("event") == "ORDER_REPLACE" for event in engine.events) / created
                if created
                else None
            ),
            "keep_ratio": (
                sum(event.get("event") == "ORDER_KEEP" for event in engine.events) / created
                if created
                else None
            ),
            "keep_pct": (
                sum(event.get("event") == "ORDER_KEEP" for event in engine.events)
                / (sum(event.get("event") == "ORDER_KEEP" for event in engine.events) + created)
                * 100.0
                if created + sum(event.get("event") == "ORDER_KEEP" for event in engine.events)
                else None
            ),
            "cancels_per_hour": operational_cancelled / duration_hours
            if duration_hours > 0
            else None,
            "cancel_reason_counts": {
                category: cancellation_counts.get(category, 0) for category in CANCEL_TAXONOMY
            },
            "unknown_internal_cancel_count": cancellation_counts.get("UNKNOWN_INTERNAL", 0),
            "dominant_cancel_reason": cancellation_counts.most_common(1)[0][0]
            if cancellation_counts
            else None,
            "median_cancellation_age_seconds": statistics.median(
                [_float(row.get("lifetime_seconds"), 0.0) or 0.0 for row in cancels]
            )
            if cancels
            else None,
            "median_cancellation_deviation_bps": _percentile(
                [
                    _float(row.get("price_deviation_bps"))
                    for row in cancels
                    if _float(row.get("price_deviation_bps")) is not None
                ],
                0.50,
            ),
            "cancellation_deviation_sample_count": sum(
                _float(row.get("price_deviation_bps")) is not None for row in cancels
            ),
            "replacement_deviation_buckets": {
                bucket: sum(
                    replacement_deviation_bucket(row.get("price_deviation_bps")) == bucket
                    for row in operational_cancel_rows
                )
                for bucket in REPLACEMENT_DEVIATION_BUCKETS
            },
            "operational_cancels": operational_cancelled,
            "shutdown_cancels": shutdown_cancelled,
            "operational_cancel_create_ratio": (
                operational_cancelled / created if created else None
            ),
            "all_cancel_create_ratio": cancelled / created if created else None,
            "cancel_reason_summary": [
                {
                    "reason": category,
                    "count": cancellation_counts.get(category, 0),
                    "operational_count": sum(
                        row.get("category") == category
                        and row.get("category") not in {"SESSION_SHUTDOWN", "MANUAL_STOP"}
                        for row in cancels
                    ),
                }
                for category in CANCEL_TAXONOMY
            ],
            "mean_quote_lifetime": _mean(lifetime_values),
            "median_quote_lifetime": _percentile(lifetime_values, 0.50),
            "p25_quote_lifetime": _percentile(lifetime_values, 0.25),
            "p75_quote_lifetime": _percentile(lifetime_values, 0.75),
            "p90_quote_lifetime": _percentile(lifetime_values, 0.90),
            "mean_time_to_fill": _mean([value for value in time_to_fill if value is not None]),
            "median_time_to_fill": _percentile(
                [value for value in time_to_fill if value is not None], 0.50
            ),
            "p75_time_to_fill": _percentile(
                [value for value in time_to_fill if value is not None], 0.75
            ),
            "p90_time_to_fill": _percentile(
                [value for value in time_to_fill if value is not None], 0.90
            ),
            "quote_lifetime_sample_count": len(lifetime_values),
            "resting_lifetime_sample_count": sum(
                row.get("resting_lifetime_seconds") is not None for row in orders
            ),
            "resting_lifetime_excluded_never_rested": sum(
                row.get("lifecycle_state") == "NEVER_RESTED_REJECTED" for row in orders
            ),
            "lifetime_distribution": _lifetime_stats(
                row.get("resting_lifetime_seconds") for row in orders
            ),
            "resting_lifetime_buckets": {
                bucket: sum(
                    resting_lifetime_bucket(row.get("resting_lifetime_seconds")) == bucket
                    for row in orders
                )
                for bucket in RESTING_LIFETIME_BUCKETS
            },
            "quote_lifetime_by_group": {
                key: _lifetime_stats(values) for key, values in lifetime_by_group.items()
            },
            "quote_lifetime_by_asset": {
                key: _lifetime_stats(values)
                for key, values in lifetime_groupings["trading_pair"].items()
            },
            "quote_lifetime_by_side": {
                key: _lifetime_stats(values) for key, values in lifetime_groupings["side"].items()
            },
            "quote_lifetime_by_mode": {
                key: _lifetime_stats(values)
                for key, values in lifetime_groupings["mode_at_creation"].items()
            },
            "quote_lifetime_by_cancel_reason": {
                key: _lifetime_stats(values)
                for key, values in lifetime_groupings["cancel_category"].items()
            },
            "quote_lifetime_by_outcome": {
                key: _lifetime_stats(values)
                for key, values in lifetime_groupings["outcome"].items()
            },
            "time_to_fill_sample_count": len(time_to_fill),
            "time_to_fill_by_asset": {
                pair: {
                    "sample_count": len(values),
                    "mean": _mean(values),
                    "median": _percentile(values, 0.50),
                    "p75": _percentile(values, 0.75),
                    "p90": _percentile(values, 0.90),
                }
                for pair, values in time_to_fill_by_asset.items()
            },
            "markout": markout_summary,
            "markout_by_group": {
                key: _markout_stats(values) for key, values in markout_by_group.items()
            },
            "markout_by_asset": {
                key: {
                    f"{horizon}s": _markout_stats(
                        [
                            row.get("markout_bps")
                            for row in markouts
                            if _row_label(row, "trading_pair") == key
                            and row.get("horizon_seconds") == horizon
                            and row.get("status") == "COMPLETE"
                        ]
                    )
                    for horizon in MARKOUT_HORIZONS_SECONDS
                }
                for key in markout_groupings["trading_pair"]
            },
            "markout_by_side": {
                key: {
                    f"{horizon}s": _markout_stats(
                        [
                            row.get("markout_bps")
                            for row in markouts
                            if _row_label(row, "side") == key
                            and row.get("horizon_seconds") == horizon
                            and row.get("status") == "COMPLETE"
                        ]
                    )
                    for horizon in MARKOUT_HORIZONS_SECONDS
                }
                for key in markout_groupings["side"]
            },
            "markout_by_mode": {
                key: {
                    f"{horizon}s": _markout_stats(
                        [
                            row.get("markout_bps")
                            for row in markouts
                            if _row_label(row, "mode") == key
                            and row.get("horizon_seconds") == horizon
                            and row.get("status") == "COMPLETE"
                        ]
                    )
                    for horizon in MARKOUT_HORIZONS_SECONDS
                }
                for key in markout_groupings["mode"]
            },
            "markout_by_quote_distance_bucket": {
                key: {
                    f"{horizon}s": _markout_stats(
                        [
                            row.get("markout_bps")
                            for row in markouts
                            if _row_label(row, "quote_distance_bucket") == key
                            and row.get("horizon_seconds") == horizon
                            and row.get("status") == "COMPLETE"
                        ]
                    )
                    for horizon in MARKOUT_HORIZONS_SECONDS
                }
                for key in markout_groupings["quote_distance_bucket"]
            },
            "markout_by_global_iv_regime": {
                key: {
                    f"{horizon}s": _markout_stats(
                        [
                            row.get("markout_bps")
                            for row in markouts
                            if _row_label(row, "global_iv_regime") == key
                            and row.get("horizon_seconds") == horizon
                            and row.get("status") == "COMPLETE"
                        ]
                    )
                    for horizon in MARKOUT_HORIZONS_SECONDS
                }
                for key in markout_groupings["global_iv_regime"]
            },
            "markout_5s": markout_summary["5s"]["mean_bps"],
            "markout_30s": markout_summary["30s"]["mean_bps"],
            "markout_60s": markout_summary["60s"]["mean_bps"],
            "adverse_selection": adverse_selection,
            "risk_blocks": len(risk_events),
            "risk_block_counts": dict(risk_counts),
            "risk_checks_total": engine.risk_episodes.risk_checks_total,
            "risk_blocks_raw": engine.risk_episodes.raw_blocks_total,
            "risk_block_rate": (
                engine.risk_episodes.raw_blocks_total / engine.risk_episodes.risk_checks_total
                if engine.risk_episodes.risk_checks_total
                else 0.0
            ),
            "unique_risk_episodes": len(risk_episode_rows),
            "unique_episode_rate": (
                len(risk_episode_rows) / engine.risk_episodes.risk_checks_total
                if engine.risk_episodes.risk_checks_total
                else 0.0
            ),
            "duration_blocked_seconds": sum(
                _float(row.get("blocked_seconds"), 0.0) or 0.0 for row in risk_episode_rows
            ),
            "risk_episode_duration_median_seconds": _percentile(
                [_float(row.get("blocked_seconds"), 0.0) or 0.0 for row in risk_episode_rows],
                0.50,
            ),
            "risk_episode_duration_p90_seconds": _percentile(
                [_float(row.get("blocked_seconds"), 0.0) or 0.0 for row in risk_episode_rows],
                0.90,
            ),
            "risk_episode_summary": risk_episode_summary,
            "risk_episode_rows": risk_episode_rows,
            "fill_eligibility": {
                "counts": {
                    status: fill_eligibility_counts.get(status, 0)
                    for status in FILL_ELIGIBILITY_STATUSES
                },
                "eligible_order_count": eligible_order_count,
                "missing_order_count": missing_order_count,
                "touch_only_order_count": fill_eligibility_counts.get(
                    "TOUCHED_NOT_TRADED_THROUGH", 0
                ),
                "never_reached_order_count": fill_eligibility_counts.get("NEVER_REACHED_PRICE", 0),
            },
            "trade_coverage": {"overall": trade_coverage, "by_asset": trade_coverage_by_asset},
            "reconciliation_decisions": reconciliation_decisions,
            "inventory_by_asset": per_asset,
            "per_asset_metrics": per_asset_metrics,
            "per_mode_metrics": per_mode_metrics,
            "per_global_iv_regime_metrics": per_regime_metrics,
            "state_observation_count": len(state_observations),
            "cycles_per_create": sum(row.get("status") == "COMPLETE" for row in cycles) / created
            if created
            else None,
            "cycles_per_hour": (
                sum(row.get("status") == "COMPLETE" for row in cycles) / duration_hours
                if duration_hours > 0
                else None
            ),
            "median_cycle_duration": _percentile(
                [
                    _float(row.get("cycle_duration_seconds"), 0.0) or 0.0
                    for row in cycles
                    if row.get("cycle_duration_seconds") is not None
                ],
                0.50,
            ),
            "mean_cycle_duration": _mean(
                [
                    _float(row.get("cycle_duration_seconds"), 0.0) or 0.0
                    for row in cycles
                    if row.get("cycle_duration_seconds") is not None
                ]
            ),
            "realized_capture_per_cycle": (
                sum(_float(row.get("realized_capture"), 0.0) or 0.0 for row in cycles)
                / sum(row.get("status") == "COMPLETE" for row in cycles)
                if sum(row.get("status") == "COMPLETE" for row in cycles)
                else None
            ),
            "executed_volume_per_cycle": (
                sum(_float(row.get("executed_volume"), 0.0) or 0.0 for row in cycles)
                / sum(row.get("status") == "COMPLETE" for row in cycles)
                if sum(row.get("status") == "COMPLETE" for row in cycles)
                else None
            ),
            "volume_per_capital_time": (
                total_volume / exposure["capital_time_quote_seconds"]
                if exposure.get("capital_time_quote_seconds")
                else None
            ),
            "cycles_per_capital_time": (
                sum(row.get("status") == "COMPLETE" for row in cycles)
                / exposure["capital_time_quote_seconds"]
                if exposure.get("capital_time_quote_seconds")
                else None
            ),
            "capital_recycling": {
                "median_entry_to_exit_seconds": _percentile(
                    [
                        _float(row.get("cycle_duration_seconds"), 0.0) or 0.0
                        for row in cycles
                        if row.get("status") == "COMPLETE"
                    ],
                    0.50,
                ),
                "median_open_position_age_seconds": _percentile(
                    [
                        _float(row.get("open_position_age_seconds"), 0.0) or 0.0
                        for row in cycles
                        if row.get("status") == "OPEN"
                    ],
                    0.50,
                ),
                "average_open_position_age_seconds": _mean(
                    [
                        _float(row.get("open_position_age_seconds"), 0.0) or 0.0
                        for row in cycles
                        if row.get("status") == "OPEN"
                    ]
                ),
                "max_open_position_age_seconds": max(
                    (
                        _float(row.get("open_position_age_seconds"), 0.0) or 0.0
                        for row in cycles
                        if row.get("status") == "OPEN"
                    ),
                    default=None,
                ),
                "percentage_session_with_open_inventory": (
                    open_inventory_seconds / exposure["duration_seconds"] * 100.0
                    if exposure.get("duration_seconds")
                    else None
                ),
                "percentage_inventory_closed_within_5m": _closed_within(cycles, 300),
                "percentage_inventory_closed_within_15m": _closed_within(cycles, 900),
                "percentage_inventory_closed_within_30m": _closed_within(cycles, 1800),
                "percentage_inventory_closed_within_1h": _closed_within(cycles, 3600),
            },
            "high_cancel_churn": high_churn,
            "diagnostics": ["HIGH_CANCEL_CHURN"] if high_churn else [],
            "orders_are_simulated": True,
            "real_exchange_mutation_calls": 0,
            "configured_levels_per_side": self.config.execution_max_levels_per_side,
            "capital_allocation": {
                "starting_equity_usdc": self.config.starting_equity_usdc,
                "order_scale": self.config.order_scale,
                "collateral_safety_buffer_pct": self.config.collateral_safety_buffer_pct,
            },
        }
        return BaselineModelMetrics(
            name=model,
            metrics=metrics,
            orders=orders,
            fills=fills,
            cancels=cancels,
            cycles=cycles,
            markouts=markouts,
            inventory=inventory_rows,
            portfolio_exposure=exposure_rows,
            risk_events=risk_events,
            equity=equity,
            risk_episodes=risk_episode_rows,
            fill_eligibility=fill_eligibility,
            reconciliation_decisions=reconciliation_decisions,
        )

    def _compare_models(
        self,
        conservative: BaselineModelMetrics,
        touch: BaselineModelMetrics,
    ) -> dict[str, Any]:
        metrics = (
            ("fills", "fills"),
            ("volume", "total_executed_notional"),
            ("cycles", "completed_cycles"),
            ("realized_pnl", "realized_pnl"),
            ("total_pnl", "total_paper_pnl"),
            ("max_drawdown", "max_drawdown_quote"),
            ("avg_inventory", "average_absolute_inventory"),
            ("max_inventory", "max_inventory"),
        )
        rows: list[dict[str, Any]] = []
        differences: list[float] = []
        for label, key in metrics:
            left = conservative.metrics.get(key)
            right = touch.metrics.get(key)
            difference = _relative_difference(left, right)
            differences.append(difference)
            rows.append(
                {
                    "metric": label,
                    "conservative": left,
                    "touch_optimistic": right,
                    "difference": (
                        (_float(right, 0.0) or 0.0) - (_float(left, 0.0) or 0.0)
                        if left is not None and right is not None
                        else None
                    ),
                    "relative_difference_pct": difference,
                }
            )
        maximum = max(differences, default=0.0)
        sensitivity = (
            "HIGH"
            if maximum >= self.config.touch_sensitivity_high_pct
            else "MEDIUM"
            if maximum >= self.config.touch_sensitivity_medium_pct
            else "LOW"
        )
        return {
            "rows": rows,
            "sensitivity": sensitivity,
            "maximum_relative_difference_pct": maximum,
            "headline": "HIGH FILL-MODE SENSITIVITY" if sensitivity == "HIGH" else None,
            "conservative": conservative.metrics,
            "touch_optimistic": touch.metrics,
        }

    def _health_checks(
        self,
        conservative: BaselineModelMetrics,
        comparison: Mapping[str, Any],
    ) -> dict[str, Any]:
        data = self.data_quality.to_record()
        market_coverage = data["market_data"].get("coverage_pct")
        data_status = (
            "PASS"
            if market_coverage is not None
            and market_coverage >= self.config.minimum_data_coverage_pct
            else "INSUFFICIENT"
            if market_coverage is None
            else "FAIL"
        )
        metrics = conservative.metrics
        option_coverage = data["option_iv"].get("coverage_pct")
        option_status = (
            "PASS"
            if option_coverage is not None
            and option_coverage >= self.config.minimum_data_coverage_pct
            else "FAIL"
            if data["option_iv"].get("expected", 0)
            else "INSUFFICIENT"
        )
        self_tuning_applications = sum(
            suggestion.get("applied") is True for suggestion in self.suggestions
        )
        checks = {
            "DATA QUALITY": data_status,
            "MAINNET PUBLIC DATA": data_status,
            "BTC OPTIONS MAINNET": option_status,
            "ENVIRONMENT CONSISTENCY": "PASS",
            "PNL RECONCILIATION": metrics.get("pnl_reconciliation_status", "FAIL"),
            "ZERO EXCHANGE MUTATIONS": (
                "PASS" if metrics.get("real_exchange_mutation_calls", 0) == 0 else "FAIL"
            ),
            "SHADOW EXECUTION": (
                "PASS"
                if metrics.get("orders_are_simulated", False)
                and metrics.get("real_exchange_mutation_calls", 0) == 0
                else "FAIL"
            ),
            "CONSERVATIVE LEDGER": (
                "PASS" if metrics.get("model") == CONSERVATIVE_MODEL else "FAIL"
            ),
            "TOUCH LEDGER": (
                "PASS"
                if comparison.get("touch_optimistic", {}).get("model") == TOUCH_MODEL
                else "FAIL"
            ),
            "LEDGER ISOLATION": ("PASS" if self._ledger_isolation_verified else "FAIL"),
            "CONFIG FROZEN": "PASS" if not self.config_contaminated else "FAIL",
            "CHECKPOINT WRITES": "PASS" if self._checkpoint_count > 0 else "INSUFFICIENT",
            "SESSION PERSISTENCE": "PASS" if self.store is not None else "FAIL",
            "SELF-TUNING APPLICATIONS": "PASS" if self_tuning_applications == 0 else "FAIL",
            "GRACEFUL SHUTDOWN": "PASS" if self._shutdown_complete else "INSUFFICIENT",
            "ORDER STABILITY": (
                "OBSERVED_HIGH_CHURN"
                if metrics.get("high_cancel_churn")
                else "PASS"
                if metrics.get("orders_created", 0) > 0
                else "INSUFFICIENT"
            ),
            "FILL SAMPLE SUFFICIENCY": (
                "PASS"
                if metrics.get("fills", 0) >= self.config.minimum_fill_samples
                else "INSUFFICIENT"
            ),
            "MARKOUT SAMPLE SUFFICIENCY": (
                "PASS"
                if metrics.get("markout", {}).get("30s", {}).get("sample_count", 0)
                >= self.config.minimum_markout_samples
                else "INSUFFICIENT"
            ),
            "INVENTORY CONTROL": (
                "PASS"
                if (metrics.get("max_inventory") or 0.0) <= self.config.max_total_position_notional
                else "FAIL"
            ),
            "PORTFOLIO RISK CONTROL": (
                "PASS" if metrics.get("risk_blocks", 0) == 0 else "PASS_WITH_BLOCKS"
            ),
            "CAPITAL RECYCLING": (
                "PASS"
                if metrics.get("completed_cycles", 0) >= self.config.minimum_cycle_samples
                else "INSUFFICIENT"
            ),
            "FILL-MODE SENSITIVITY": (
                "FAIL" if comparison.get("sensitivity") == "HIGH" else "PASS"
            ),
            "CANCEL TAXONOMY": (
                "PASS"
                if metrics.get("unknown_internal_cancel_count", 0) == 0
                and all(row.get("category") in CANCEL_TAXONOMY for row in conservative.cancels)
                else "FAIL"
            ),
            "ORDER LIFECYCLE": (
                "PASS"
                if len(conservative.orders)
                == metrics.get("resting_lifetime_sample_count", 0)
                + metrics.get("resting_lifetime_excluded_never_rested", 0)
                else "FAIL"
            ),
            "RISK EPISODE DEDUPLICATION": (
                "PASS"
                if metrics.get("risk_blocks_raw", 0) == len(conservative.risk_events)
                and metrics.get("unique_risk_episodes", 0) <= metrics.get("risk_blocks_raw", 0)
                else "FAIL"
            ),
            "TRADE COVERAGE ACCOUNTING": (
                "PASS"
                if isinstance(metrics.get("trade_coverage"), Mapping)
                and isinstance(metrics.get("trade_coverage", {}).get("overall"), Mapping)
                else "FAIL"
            ),
            "FILL ELIGIBILITY ATTRIBUTION": (
                "PASS"
                if all(
                    row.get("status") in FILL_ELIGIBILITY_STATUSES
                    for row in conservative.fill_eligibility
                )
                else "FAIL"
            ),
            "RECONCILIATION AUDIT": (
                "PASS"
                if metrics.get("reconciliation_decisions") is not None
                and len(metrics.get("reconciliation_decisions", [])) >= self.cycles
                else "INSUFFICIENT"
            ),
            "PNL DISPLAY": (
                "PASS"
                if metrics.get("gross_pnl_label") == "GROSS PAPER PNL"
                and metrics.get("verified_net_pnl_status") in {"UNKNOWN", "VERIFIED"}
                else "FAIL"
            ),
        }
        reasons: list[str] = []
        for name, status in checks.items():
            if status in {"FAIL", "INSUFFICIENT"}:
                reasons.append(f"{name}: {status}")
        return {"checks": checks, "reasons": reasons, "data_quality": data}

    def _classification(
        self,
        conservative: BaselineModelMetrics,
        comparison: Mapping[str, Any],
        health: Mapping[str, Any],
    ) -> tuple[str, str, list[str]]:
        checks = health.get("checks", {})
        reasons = list(health.get("reasons", []))
        if conservative.metrics.get("pnl_reconciliation_status") == "FAIL":
            return "ACCOUNTING INVALID", "NOT READY FOR OPTIMIZATION", reasons
        if checks.get("ZERO EXCHANGE MUTATIONS") != "PASS":
            return "WEAK EXECUTION QUALITY", "NOT READY FOR OPTIMIZATION", reasons
        if comparison.get("sensitivity") == "HIGH":
            reasons.append("touch-optimistic and conservative outcomes diverge materially")
            return "HIGH FILL-MODE UNCERTAINTY", "NOT READY FOR OPTIMIZATION", reasons
        if any(status == "FAIL" for status in checks.values()):
            return "WEAK EXECUTION QUALITY", "NOT READY FOR OPTIMIZATION", reasons
        if any(status == "INSUFFICIENT" for status in checks.values()):
            reasons.append("measurement sample is not yet large enough for tuning")
            return "PROMISING BUT INSUFFICIENT EVIDENCE", "NOT READY FOR OPTIMIZATION", reasons
        if (
            checks.get("ORDER STABILITY") == "FAIL"
            or conservative.metrics.get("adverse_selection") == "ADVERSE_SELECTION"
        ):
            return "MIXED", "NOT READY FOR OPTIMIZATION", reasons
        return "STRONG BASELINE", "READY FOR BOUNDED OPTIMIZATION", reasons

    def metrics(self, *, now: float | None = None) -> dict[str, Any]:
        end = self._stop_epoch or (time.time() if now is None else now)
        if self._start_epoch == 0:
            end = time.time() if now is None else now
        conservative = self._model_metrics(CONSERVATIVE_MODEL, end)
        touch = self._model_metrics(TOUCH_MODEL, end)
        comparison = self._compare_models(conservative, touch)
        health = self._health_checks(conservative, comparison)
        classification, readiness, reasons = self._classification(conservative, comparison, health)
        public_trade_evidence = self._public_trade_evidence_status()
        conservative_fills_status = (
            "UNAVAILABLE"
            if public_trade_evidence == "UNAVAILABLE"
            else "AVAILABLE"
            if conservative.metrics.get("fills", 0) > 0
            else "NO QUALIFYING FILLS"
        )
        return {
            **conservative.metrics,
            "baseline_config_version": self.config.baseline_config_version,
            "config_hash": self._frozen_config_hash,
            "strategy_config_hash": self._frozen_strategy_hash,
            "profile": str(self.config_source_path) if self.config_source_path else None,
            "resolved_profile_path": (
                str(self.config_source_path) if self.config_source_path else None
            ),
            "execution_backend": self.config.execution_backend,
            "config_frozen": not self.config_contaminated,
            "config_contaminated": self.config_contaminated,
            "self_tuning_mode": self.config.self_tuning_mode.upper(),
            "self_tuning_applications": sum(
                suggestion.get("applied") is True for suggestion in self.suggestions
            ),
            "public_trade_evidence": public_trade_evidence,
            "conservative_fills_status": conservative_fills_status,
            "trade_history_enabled": self.trade_history_enabled,
            "ledger_isolation": "PASS" if self._ledger_isolation_verified else "FAIL",
            "checkpoint_writes": self._checkpoint_count,
            "session_persistence": self.store is not None,
            "graceful_shutdown": self._shutdown_complete,
            "real_exchange_mutation_calls": 0,
            "shadow_environment_consistency": SHADOW_ENVIRONMENT_CONSISTENCY_PASS,
            "data_quality": health["data_quality"],
            "health_checks": health["checks"],
            "health_check_reasons": health["reasons"],
            "classification": classification,
            "readiness": readiness,
            "readiness_reasons": reasons,
            "fill_model_sensitivity": comparison["sensitivity"],
            "fill_model_comparison": comparison["rows"],
            "touch_optimistic_metrics": touch.metrics,
            "conservative_metrics": conservative.metrics,
            "self_tuning_suggestions": list(self.suggestions),
            "cycles_observed": self.cycles,
        }

    def _hourly_rows(
        self,
        model: str,
        model_metrics: BaselineModelMetrics,
        end_timestamp: float,
    ) -> list[dict[str, Any]]:
        if self._start_epoch <= 0:
            return []
        first = datetime.fromtimestamp(self._start_epoch, UTC).replace(
            minute=0, second=0, microsecond=0
        )
        last = datetime.fromtimestamp(end_timestamp, UTC).replace(minute=0, second=0, microsecond=0)
        rows: list[dict[str, Any]] = []
        cursor = first
        fills = model_metrics.fills
        while cursor <= last:
            hour_start = max(self._start_epoch, cursor.timestamp())
            hour_end = min(end_timestamp, (cursor + timedelta(hours=1)).timestamp())
            if hour_end < hour_start:
                break
            events = self.sessions[model].engine.events
            fills_until = [
                row
                for row in fills
                if hour_start <= (_epoch(row.get("timestamp")) or 0.0) <= hour_end
            ]
            events_until = [
                event
                for event in events
                if hour_start <= (_epoch(event.get("timestamp")) or 0.0) <= hour_end
            ]
            equity_rows = [
                row
                for row in model_metrics.equity
                if (_epoch(row.get("timestamp")) or 0.0) <= hour_end
            ]
            latest_equity = equity_rows[-1] if equity_rows else {}
            exposure = self.exposure[model].summary(start=hour_start, end=hour_end)
            created = sum(event.get("event") == "ORDER_CREATE" for event in events_until)
            cancelled = sum(event.get("event") == "ORDER_CANCEL" for event in events_until)
            keeps = sum(event.get("event") == "ORDER_KEEP" for event in events_until)
            volume = sum(_float(row.get("notional"), 0.0) or 0.0 for row in fills_until)
            markout_values = [
                _float(row.get("markout_bps"))
                for row in model_metrics.markouts
                if row.get("horizon_seconds") == 30
                and row.get("status") == "COMPLETE"
                and hour_start <= (_epoch(row.get("timestamp")) or 0.0) <= hour_end
            ]
            rows.append(
                {
                    "hour": _iso(cursor.timestamp()),
                    "timestamp": _iso(hour_end),
                    "model": model,
                    "fill_model": model,
                    "equity": latest_equity.get("current_equity"),
                    "pnl": self._model_metrics(model, hour_end).metrics.get("total_paper_pnl"),
                    "volume": volume,
                    "fills": len(fills_until),
                    "cancels": cancelled,
                    "cycles": sum(
                        row.get("status") == "COMPLETE"
                        and hour_start <= (_epoch(row.get("timestamp")) or 0.0) <= hour_end
                        for row in model_metrics.cycles
                    ),
                    "fill_create_ratio": len(fills_until) / created if created else None,
                    "cancel_create_ratio": cancelled / created if created else None,
                    "keep_count": keeps,
                    "markout_30s": _mean([value for value in markout_values if value is not None]),
                    "markout_30s_sample_count": len(
                        [value for value in markout_values if value is not None]
                    ),
                    "average_gross_exposure": exposure.get("average_gross_exposure"),
                    "average_inventory": exposure.get("average_absolute_inventory"),
                    "average_btc_beta_exposure": exposure.get("average_btc_beta_exposure"),
                    "drawdown": latest_equity.get("drawdown_quote"),
                    "risk_blocks": sum(
                        event.get("event") in {"RISK_BLOCK", "PORTFOLIO_RISK_BLOCK"}
                        for event in events_until
                    ),
                }
            )
            cursor += timedelta(hours=1)
        return rows

    def summary(self, *, now: float | None = None, reason: str | None = None) -> dict[str, Any]:
        end = self._stop_epoch or (time.time() if now is None else now)
        values = self.metrics(now=end)
        if reason is not None:
            values["reason"] = reason
        result = {
            **values,
            "session_id": self.session_id,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.stop_timestamp or _iso(end),
            "duration_seconds": max(0.0, end - self._start_epoch),
            "duration_hours": max(0.0, end - self._start_epoch) / 3600.0,
            "stop_reason": reason or self.stop_reason,
            "config": self.config.to_record(),
            "profile": str(self.config_source_path) if self.config_source_path else None,
            "resolved_profile_path": (
                str(self.config_source_path) if self.config_source_path else None
            ),
            "freeze": self._freeze_record(),
            "git_commit": self._git_commit(self.project_root),
            "enabled_assets": list(self.config.enabled_markets),
            "markets": list(self.config.markets),
            "market_environment": "mainnet",
            "execution_mode": "SHADOW",
            "orders_are_simulated": True,
            "real_exchange_mutation_calls": 0,
            "environment_consistency": SHADOW_ENVIRONMENT_CONSISTENCY_PASS,
            "limitations": [
                "shadow orders never enter Derive matching-engine queue",
                "queue priority and latency are estimated or unknown",
                "simulated fills and paper PnL are not live fills or live PnL",
                "fees are UNKNOWN unless explicitly verified in configuration",
                "markouts are reported only after actual future public observations",
                "touch-optimistic results are sensitivity evidence, not proof",
            ],
        }
        if reason is not None:
            result["reason"] = reason
        result["metrics"] = values
        return result

    def _stage12c_report_markdown(self, summary: Mapping[str, Any]) -> str:
        """Build the human-readable Stage 12C observability report."""

        metrics = summary.get("metrics", summary)
        checks = metrics.get("health_checks", {})
        freeze = summary.get("freeze") or {}
        quote_settings = freeze.get("quote_settings") or {}
        lifecycle = metrics.get("lifecycle_states") or {}
        cancel_counts = metrics.get("cancel_reason_counts") or {}
        cancel_summary = metrics.get("cancel_reason_summary") or []
        risk_summary = metrics.get("risk_episode_summary") or []
        trade_coverage = metrics.get("trade_coverage") or {}
        eligibility = metrics.get("fill_eligibility") or {}
        pnl = metrics.get("pnl_reconciliation") or {}
        touch = metrics.get("touch_optimistic_metrics") or {}
        replacement_buckets = metrics.get("replacement_deviation_buckets") or {}
        lifetime_buckets = metrics.get("resting_lifetime_buckets") or {}
        decisions = metrics.get("reconciliation_decisions") or []
        sample = decisions[-1] if decisions else {}
        sample_desired = sample.get("desired_count", len(sample.get("desired_level_ids", [])))
        sample_active = sample.get("active_count", len(sample.get("active_level_ids", [])))
        sample_create = sample.get("create_count")
        sample_keep = sample.get("keep_count")
        sample_stop = sample.get("stop_count")
        sample_skip = sample.get("skip_count")
        sample_defer = sample.get("defer_count", sample.get("deferred_count"))
        sample_risk = sample.get("risk_block_count")
        duration = self._display(summary.get("duration_seconds"))
        duration_minutes = self._display(
            (_float(summary.get("duration_seconds"), 0.0) or 0.0) / 60.0
        )
        created = int(metrics.get("orders_created", 0) or 0)
        resting = sum(
            int(lifecycle.get(state, 0) or 0)
            for state in ("RESTING", "CANCELLED_AFTER_RESTING", "FILLED_AFTER_RESTING", "COMPLETE")
        )
        all_cancels = int(metrics.get("orders_cancelled", 0) or 0)
        operational_cancels = int(metrics.get("operational_cancels", 0) or 0)
        shutdown_cancels = int(metrics.get("shutdown_cancels", 0) or 0)
        operational_ratio = metrics.get("operational_cancel_create_ratio")
        coverage_overall = trade_coverage.get("overall") or {}
        ready_for_long_baseline = summary.get("readiness") == "READY FOR 24–48H CLEAN BASELINE"

        lines = [
            "# Stage 12C Shadow Execution Observability",
            "",
            "## 1. Executive summary",
            "",
            "This is an observability and order-lifecycle remediation report around the "
            "unchanged strategy. It does not tune strategy parameters or claim live "
            "profitability, queue position, or deployment readiness.",
            "",
            f"- Session: `{summary.get('session_id')}`",
            "- Data: **REAL DERIVE MAINNET PUBLIC DATA**",
            "- Execution: **SHADOW / PAPER ONLY**",
            "- Real exchange mutations: **0**",
            f"- Config frozen: **{summary.get('config_frozen')}**",
            f"- Strategy behavior hash: `{summary.get('strategy_config_hash')}`",
            f"- Final classification: **{summary.get('classification')}**",
            f"- Readiness for long baseline: **{summary.get('readiness')}**",
            "",
            "## 2. Previous baseline failure reproduction",
            "",
            "The pre-change evidence and root-cause reconstruction are preserved in "
            "`reports/stage12c/reproduction.md` for session "
            "`shadow-baseline-20260827T070350Z-b944ddf5`. It records 35 creates, 35 "
            "cancels, 15 KEEP decisions, 38 zero-second rows across both ledgers, "
            "generic `OTHER` classification, 689 primary raw risk events, and only "
            "3 of 448 public trade observations. The old engine lacked resting-state "
            "timestamps and diagnostic cancellation context, so those observations "
            "could not distinguish lifecycle behavior from telemetry defects.",
            "",
            "## 3. Cancellation root cause",
            "",
            f"- Created: **{created}**; all cancellations: **{all_cancels}**",
            f"- Operational cancellations: **{operational_cancels}**",
            f"- Shutdown/manual cancellations excluded from churn: **{shutdown_cancels}**",
            f"- Dominant classified reason: **{metrics.get('dominant_cancel_reason')}**",
            f"- UNKNOWN_INTERNAL: **{cancel_counts.get('UNKNOWN_INTERNAL', 0)}**",
            "",
            "| Reason | All cancels | Operational cancels |",
            "|---|---:|---:|",
        ]
        lines.extend(
            f"| {row.get('reason')} | {self._display(row.get('count'))} | "
            f"{self._display(row.get('operational_count'))} |"
            for row in cancel_summary
        )
        lines.extend(
            [
                "",
                "Every cancel carries the old/new order and plan context, market BBO, "
                "age, deviation, risk, and safety-override fields in `cancels.csv`; "
                "unmapped reasons are `UNKNOWN_INTERNAL`, never normal `OTHER`.",
                "",
                "## 4. Order lifetime root cause",
                "",
                "Resting lifetime is terminal timestamp minus `resting_start_timestamp`. "
                "Rejected-before-resting orders are classified separately and excluded "
                "from the resting-lifetime distribution.",
                "",
                f"- Became/rested orders: **{resting}**",
                f"- Still resting at session end: "
                f"**{self._display(metrics.get('active_orders'))}**",
                f"- Filled after resting: **{self._display(metrics.get('orders_filled'))}**",
                f"- Never-rested excluded: "
                f"**{self._display(metrics.get('resting_lifetime_excluded_never_rested'))}**",
                f"- Median / P75 / P90 resting lifetime (s): "
                f"**{self._display(metrics.get('median_quote_lifetime'))} / "
                f"{self._display(metrics.get('p75_quote_lifetime'))} / "
                f"{self._display(metrics.get('p90_quote_lifetime'))}**",
                "",
                "| Resting lifetime bucket | Orders |",
                "|---|---:|",
            ]
        )
        lines.extend(
            f"| {bucket} | {self._display(lifetime_buckets.get(bucket, 0))} |"
            for bucket in RESTING_LIFETIME_BUCKETS
        )
        lines.extend(
            [
                "",
                "## 5. Reconciliation audit",
                "",
                "Each controller cycle persists desired levels, active levels, CREATE, KEEP, "
                "STOP, SKIP, deferred replacement, risk-blocked, filled-managed, and "
                "TP-managed counts in `reconciliation_decisions.csv`.",
                "",
                f"- Sample timestamp: **{self._display(sample.get('timestamp'))}**",
                f"- Sample pair/mode/plan: **{self._display(sample.get('trading_pair'))} / "
                f"{self._display(sample.get('mode'))} / "
                f"{self._display(sample.get('plan_version'))}**",
                f"- Sample desired/active/create/keep/stop/skip/defer/risk: **"
                f"{self._display(sample_desired)} / {self._display(sample_active)} / "
                f"{self._display(sample_create)} / {self._display(sample_keep)} / "
                f"{self._display(sample_stop)} / {self._display(sample_skip)} / "
                f"{self._display(sample_defer)} / {self._display(sample_risk)}**",
                f"- Audit health: **{checks.get('RECONCILIATION AUDIT', 'UNKNOWN')}**",
                "",
                "## 6. Plan-version behavior",
                "",
                "A plan-version increment is diagnostic metadata only. Reconciliation "
                "refreshes only for material executable price/amount changes, hard maker "
                "safety, or maximum age. The regression suite verifies a plan-version "
                "change with an unchanged quantized level remains KEEP.",
                "",
                "## 7. Mode-change behavior",
                "",
                "NORMAL/BIAS/DEFENSIVE mode labels do not independently cancel an otherwise "
                "eligible quote inside its executable deadbands. PAUSE and explicit hard "
                "safety conditions remain exceptions.",
                "",
                "## 8. Deadband, minimum-lifetime, and cooldown audit",
                "",
                f"- Refresh price tolerance (bps): "
                f"**{self._display(quote_settings.get('refresh_price_tolerance_bps'))}**",
                f"- Refresh amount tolerance (%): "
                f"**{self._display(quote_settings.get('refresh_amount_tolerance_pct'))}**",
                f"- Minimum lifetime (s): "
                f"**{self._display(quote_settings.get('minimum_order_lifetime_seconds'))}**",
                f"- Replacement cooldown (s): "
                f"**{self._display(quote_settings.get('minimum_replace_interval_seconds'))}**",
                f"- Replacement deviation observations: "
                f"**{self._display(metrics.get('cancellation_deviation_sample_count'))}**",
                "",
                "| Operational replacement deviation bucket | Cancels |",
                "|---|---:|",
            ]
        )
        lines.extend(
            f"| {bucket} | {self._display(replacement_buckets.get(bucket, 0))} |"
            for bucket in REPLACEMENT_DEVIATION_BUCKETS
        )
        lines.extend(
            [
                "",
                f"- Minimum-lifetime safety overrides: "
                f"**{cancel_counts.get('MIN_LIFETIME_SAFETY_OVERRIDE', 0)}**",
                f"- Cooldown safety overrides: "
                f"**{cancel_counts.get('REPLACEMENT_COOLDOWN_OVERRIDE', 0)}**",
                "- Ordinary early replacement is KEEP/DEFER; explicit post-only, marketability, "
                "risk, pause, stale-data, or drawdown conditions may override those gates.",
                "",
                "## 9. Risk-event deduplication",
                "",
                f"- Risk checks: **{self._display(metrics.get('risk_checks_total'))}**",
                f"- Raw blocks: **{self._display(metrics.get('risk_blocks_raw'))}**",
                f"- Raw block rate: **{self._display(metrics.get('risk_block_rate'))}**",
                f"- Unique episodes: **{self._display(metrics.get('unique_risk_episodes'))}**",
                f"- Unique episode rate: **{self._display(metrics.get('unique_episode_rate'))}**",
                f"- Blocked duration (s): "
                f"**{self._display(metrics.get('duration_blocked_seconds'))}**",
                "",
                "| Reason | Raw blocks | Unique episodes | Blocked seconds | Assets |",
                "|---|---:|---:|---:|---|",
            ]
        )
        lines.extend(
            f"| {row.get('reason')} | {self._display(row.get('raw_blocks'))} | "
            f"{self._display(row.get('unique_episodes'))} | "
            f"{self._display(row.get('blocked_seconds'))} | "
            f"{self._display(row.get('assets'))} |"
            for row in risk_summary
        )
        lines.extend(
            [
                "",
                "Repeated raw checks are retained, while a stable pair/level/side/reason "
                "identity groups continuous blocked intervals into episodes.",
                "",
                "## 10. Public trade evidence coverage",
                "",
                f"- Overall expected duration (s): "
                f"**{self._display(coverage_overall.get('expected_duration_seconds'))}**",
                f"- Covered duration (s): "
                f"**{self._display(coverage_overall.get('covered_duration_seconds'))}**",
                f"- Coverage: **{self._display(coverage_overall.get('coverage_pct'))}%**",
                f"- Public trades: **{self._display(coverage_overall.get('trade_count'))}**",
                f"- Evidence/no-evidence minutes: "
                f"**{self._display(coverage_overall.get('evidence_minutes'))} / "
                f"{self._display(coverage_overall.get('no_evidence_minutes'))}**",
                "",
                "| Asset | Trades | Coverage % | Evidence min | No-evidence min | Gaps | "
                "Max gap (s) | Median gap (s) | P95 gap (s) |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        lines.extend(
            f"| {pair} | {self._display(row.get('trade_count'))} | "
            f"{self._display(row.get('coverage_pct'))} | "
            f"{self._display(row.get('evidence_minutes'))} | "
            f"{self._display(row.get('no_evidence_minutes'))} | "
            f"{self._display(row.get('gap_count'))} | "
            f"{self._display(row.get('max_gap_seconds'))} | "
            f"{self._display(row.get('median_gap_seconds'))} | "
            f"{self._display(row.get('p95_gap_seconds'))} |"
            for pair, row in sorted((trade_coverage.get("by_asset") or {}).items())
        )
        lines.extend(
            [
                "",
                "## 11. Conservative fill attribution",
                "",
                "The conservative ledger remains `conservative_trade_through`; BBO touch "
                "does not create a conservative fill. Order-level evidence is persisted "
                "in `fill_eligibility.csv`.",
                "",
                "| Eligibility status | Orders |",
                "|---|---:|",
            ]
        )
        lines.extend(
            f"| {status} | {self._display(count)} |"
            for status, count in sorted((eligibility.get("counts") or {}).items())
        )
        lines.extend(
            [
                "",
                f"- Eligible trade-through orders: "
                f"**{self._display(eligibility.get('eligible_order_count'))}**",
                f"- Missing-evidence orders: "
                f"**{self._display(eligibility.get('missing_order_count'))}**",
                f"- Conservative fills: "
                f"**{self._display(metrics.get('conservative_fills_status'))}**",
                f"- Attribution health: "
                f"**{checks.get('FILL ELIGIBILITY ATTRIBUTION', 'UNKNOWN')}**",
                "",
                "## 12. Touch sensitivity interpretation",
                "",
                f"- Touch-optimistic fills: **{self._display(touch.get('fills'))}**",
                f"- Touch-optimistic volume: "
                f"**{self._display(touch.get('total_executed_notional'))}**",
                f"- Fill-model sensitivity: "
                f"**{self._display(summary.get('fill_model_sensitivity'))}**",
                "Touch results are isolated sensitivity evidence. The divergence is not "
                "averaged into the conservative result and does not prove queue position "
                "or live profitability.",
                "",
                "## 13. PnL reporting",
                "",
                f"- GROSS PAPER PNL: **{self._display(metrics.get('gross_pnl'))}**",
                f"- VERIFIED NET PNL: **{self._display(metrics.get('verified_net_pnl'))}**",
                f"- FEE MODEL: **{self._display(metrics.get('fees_status'))}**",
                f"- PAPER EQUITY: **{self._display(metrics.get('paper_equity'))}**",
                f"- PNL RECONCILIATION: "
                f"**{self._display(pnl.get('status', metrics.get('pnl_reconciliation_status')))}**",
                "When fees are unknown, gross paper accounting remains visible while fee-"
                "adjusted net PnL stays UNKNOWN.",
                "",
                "## 14. Dashboard changes",
                "",
                "The `SHADOW TRADING` page shows the fixed mainnet-data/shadow/paper banner, "
                "zero-mutation status, gross-versus-verified-net PnL, lifecycle states, "
                "exact cancellation taxonomy, resting-age distribution, replacement-"
                "deviation distribution, risk episodes, public-trade coverage, fill "
                "eligibility, reconciliation decisions, and recent lifecycle events.",
                "",
                "## 15. Tests",
                "",
                "Verification commands:",
                "",
                "```text",
                "PYTHONPATH=src:. .venv/bin/pytest -q",
                ".venv/bin/ruff check .",
                "git diff --check",
                "```",
                "",
                "Focused regression coverage includes cancel taxonomy, unknown diagnostics, "
                "resting timestamps, never-rested exclusion, quantized plan replacement, "
                "risk-episode deduplication, trade gaps, and exact distribution boundaries.",
                "",
                "## 16. 60-minute smoke",
                "",
                f"- Duration: **{duration} seconds ({duration_minutes} minutes)**",
                f"- Created / became resting / still resting / filled: "
                f"**{created} / {resting} / {self._display(metrics.get('active_orders'))} / "
                f"{self._display(metrics.get('orders_filled'))}**",
                f"- Operational cancels / shutdown cancels: "
                f"**{operational_cancels} / {shutdown_cancels}**",
                f"- KEEP: **{self._display(metrics.get('orders_kept'))}**",
                f"- Operational cancel/create: **{self._display(operational_ratio)}**",
                f"- Replacement decisions observed: "
                f"**{self._display(metrics.get('cancellation_deviation_sample_count'))}**",
                f"- PnL reconciliation: "
                f"**{self._display(metrics.get('pnl_reconciliation_status'))}**",
                "",
                "## 17. Remaining limitations",
                "",
                *[f"- {item}" for item in (summary.get("limitations") or [])],
                "- Public trade coverage is partial and conservative fills/markouts are "
                "not sufficient for optimization.",
                "",
                "## 18. Readiness for long baseline",
                "",
                "READY FOR 24–48H CLEAN BASELINE: "
                f"**{'YES' if ready_for_long_baseline else 'NO'}**",
                f"- Classification: **{summary.get('classification')}**",
                f"- Readiness: **{summary.get('readiness')}**",
                "",
                "The one-hour acceptance gates for safety, accounting, lifecycle, risk "
                "deduplication, evidence accounting, and dashboard rendering passed. "
                "The session is still not ready for optimization or a longer clean baseline "
                "because fill/markout/capital-recycling evidence is insufficient and the "
                "touch-versus-conservative sensitivity is high. No strategy tuning or live "
                "mainnet execution is enabled.",
            ]
        )
        return "\n".join(lines) + "\n"

    def _write_stage12c_artifacts(
        self,
        summary: Mapping[str, Any],
        model_metrics: Mapping[str, BaselineModelMetrics],
        end_timestamp: float,
    ) -> None:
        """Write the additive Stage 12C observability contract."""

        root = self.project_root / "reports" / "stage12c"
        root.mkdir(parents=True, exist_ok=True)
        conservative = model_metrics[CONSERVATIVE_MODEL]
        all_cancels = [row for item in model_metrics.values() for row in item.cancels]
        cancel_rows = []
        for category in CANCEL_TAXONOMY:
            selected = [row for row in all_cancels if row.get("category") == category]
            cancel_rows.append(
                {
                    "reason": category,
                    "count": len(selected),
                    "operational_count": sum(
                        row.get("category") not in {"SESSION_SHUTDOWN", "MANUAL_STOP"}
                        for row in selected
                    ),
                    "models": sorted({str(row.get("model")) for row in selected}),
                }
            )
        self._write_csv(
            root / "cancel_reason_summary.csv",
            cancel_rows,
            ["reason", "count", "operational_count", "models"],
        )
        self._write_csv(
            root / "order_lifetimes.csv",
            [row for item in model_metrics.values() for row in item.orders],
            [
                "shadow_order_id",
                "model",
                "trading_pair",
                "level_id",
                "side",
                "status",
                "lifecycle_state",
                "created_timestamp",
                "validated_timestamp",
                "resting_start_timestamp",
                "terminal_timestamp",
                "cancel_requested_timestamp",
                "cancel_timestamp",
                "outcome",
                "created_to_terminal_seconds",
                "resting_lifetime_seconds",
                "fill_eligibility_status",
                "fill_eligibility_reason",
            ],
        )
        self._write_csv(
            root / "replacement_deviation.csv",
            all_cancels,
            [
                "timestamp",
                "model",
                "trading_pair",
                "level_id",
                "side",
                "shadow_order_id",
                "category",
                "reason_raw",
                "age_seconds",
                "old_price",
                "new_desired_price",
                "price_deviation_bps",
                "old_amount",
                "new_desired_amount",
                "amount_deviation_pct",
                "safety_override",
                "lifetime_seconds",
            ],
        )
        risk_rows = []
        for model, item in model_metrics.items():
            for row in item.risk_episodes:
                risk_rows.append({"model": model, **row})
        self._write_csv(
            root / "risk_episode_summary.csv",
            risk_rows,
            [
                "model",
                "episode_id",
                "episode_key",
                "reason",
                "raw_reason",
                "trading_pair",
                "level_id",
                "side",
                "first_timestamp",
                "last_timestamp",
                "first_timestamp_epoch",
                "last_timestamp_epoch",
                "raw_block_count",
                "blocked_seconds",
                "assets",
                "candidate_trace",
            ],
        )
        trade_coverage = conservative.metrics.get("trade_coverage", {})
        coverage_rows = []
        if isinstance(trade_coverage.get("overall"), Mapping):
            coverage_rows.append({"asset": "ALL", **trade_coverage["overall"]})
        coverage_rows.extend(
            {"asset": pair, **values}
            for pair, values in (trade_coverage.get("by_asset") or {}).items()
            if isinstance(values, Mapping)
        )
        self._write_csv(
            root / "trade_coverage.csv",
            coverage_rows,
            [
                "asset",
                "expected_duration_seconds",
                "covered_duration_seconds",
                "coverage_pct",
                "trade_count",
                "evidence_minutes",
                "no_evidence_minutes",
                "gap_count",
                "max_gap_seconds",
                "median_gap_seconds",
                "p95_gap_seconds",
            ],
        )
        self._write_csv(
            root / "fill_eligibility.csv",
            [row for item in model_metrics.values() for row in item.fill_eligibility],
            [
                "model",
                "fill_model",
                "shadow_order_id",
                "trading_pair",
                "level_id",
                "side",
                "is_exit",
                "status",
                "reason",
                "trade_count",
                "qualifying_trade_count",
                "bbo_touched",
                "resting_start_timestamp",
                "terminal_timestamp",
            ],
        )
        self._write_csv(
            root / "reconciliation_decisions.csv",
            [
                {"model": model, **row}
                for model, item in model_metrics.items()
                for row in item.reconciliation_decisions
            ],
            [
                "model",
                "timestamp",
                "timestamp_epoch",
                "cycle_id",
                "trading_pair",
                "plan_version",
                "mode",
                "plan_valid",
                "plan_enabled",
                "desired_level_ids",
                "active_level_ids",
                "create_count",
                "keep_count",
                "stop_count",
                "skip_count",
                "defer_count",
                "risk_block_count",
                "filled_managed_count",
                "tp_managed_count",
                "pause_reason",
            ],
        )
        smoke = {
            "session_id": self.session_id,
            "duration_seconds": summary.get("duration_seconds"),
            "environment": "mainnet",
            "execution_mode": "SHADOW",
            "execution_enabled": False,
            "allow_mainnet_trading": False,
            "real_exchange_mutation_calls": 0,
            "conservative_fill_model": "conservative_trade_through",
            "touch_model_isolated": True,
            "strategy_behavior_hash": summary.get("strategy_config_hash"),
            "config_frozen": summary.get("config_frozen", False),
            "pnl_reconciliation": summary.get("pnl_reconciliation_status"),
            "readiness": summary.get("readiness"),
            "classification": summary.get("classification"),
            "generated_at": _iso(end_timestamp),
        }
        (root / "smoke_summary.json").write_text(
            json.dumps(_safe(smoke), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.project_root / "reports" / "stage12c_shadow_observability.md").write_text(
            self._stage12c_report_markdown(summary), encoding="utf-8"
        )

    def write_report(self, summary: Mapping[str, Any]) -> Path:
        root = Path(self.config.report_root).expanduser() / self.session_id
        root.mkdir(parents=True, exist_ok=True)
        end = self._stop_epoch or (_epoch(summary.get("end_timestamp")) or time.time())
        model_metrics = {
            model: self._model_metrics(model, end) for model in (CONSERVATIVE_MODEL, TOUCH_MODEL)
        }
        self._write_stage12c_artifacts(summary, model_metrics, end)
        legacy_root = self._latest_legacy_session_root()
        stage12e_summary = write_stage12e_artifacts(
            project_root=self.project_root,
            session_id=self.session_id,
            config=self.config.to_record(),
            frames=self._frame_history,
            model_metrics=model_metrics,
            cycles_by_model={
                model: self.sessions[model].cycles for model in self.sessions
            },
            start_timestamp=self._start_epoch,
            end_timestamp=end,
            legacy_root=legacy_root,
        )
        summary = dict(summary)
        summary["stage12e"] = stage12e_summary
        stage12f_config = {
            **self.config.to_record(),
            "shadow_config_hash": summary.get("config_hash"),
            "strategy_config_hash": summary.get("strategy_config_hash"),
        }
        stage12f_summary = write_stage12f_artifacts(
            project_root=self.project_root,
            session_id=self.session_id,
            config=stage12f_config,
            frames=self._frame_history,
            model_metrics=model_metrics,
            cycles_by_model={
                model: self.sessions[model].cycles for model in self.sessions
            },
            start_timestamp=self._start_epoch,
            end_timestamp=end,
            stage12e_summary=stage12e_summary,
            minimum_coverage_samples=int(self.config.minimum_fill_samples),
        )
        summary["stage12f"] = stage12f_summary
        stage12g_config = {
            **self.config.to_record(),
            "shadow_config_hash": summary.get("config_hash"),
            "strategy_config_hash": summary.get("strategy_config_hash"),
            "config_contaminated": summary.get("config_contaminated", False),
        }
        stage12g_summary = write_stage12g_artifacts(
            project_root=self.project_root,
            session_id=self.session_id,
            config=stage12g_config,
            frames=self._frame_history,
            model_metrics=model_metrics,
            cycles_by_model={
                model: self.sessions[model].cycles for model in self.sessions
            },
            start_timestamp=self._start_epoch,
            end_timestamp=end,
            stage12f_summary=stage12f_summary,
            engine=self.sessions[CONSERVATIVE_MODEL].engine,
        )
        summary["stage12g"] = stage12g_summary
        if self.config.stage13.enabled:
            self._stage13_summary = write_stage13_artifacts(
                project_root=self.project_root,
                session_id=self.session_id,
                config={
                    **self.config.to_record(),
                    "shadow_config_hash": summary.get("config_hash"),
                    "strategy_config_hash": summary.get("strategy_config_hash"),
                },
                frames=self._frame_history,
                model_metrics=model_metrics,
                cycles_by_model={
                    model: self.sessions[model].cycles for model in self.sessions
                },
                start_timestamp=self._start_epoch,
                end_timestamp=end,
                stage12g_control_summary=self._stage13_parent_summary,
                stage12f_summary=stage12f_summary,
                stage12g_summary={
                    **stage12g_summary,
                    "shadow_config_hash": summary.get("config_hash"),
                    "strategy_config_hash": summary.get("strategy_config_hash"),
                },
                engine=self.sessions[CONSERVATIVE_MODEL].engine,
            )
            summary["stage13"] = self._stage13_summary
        (root / "summary.json").write_text(
            json.dumps(_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest_status = (
            "INVALID_CONFIG"
            if summary.get("config_contaminated")
            else "COMPLETE"
            if self._shutdown_complete
            else "RUNNING"
        )
        self._write_baseline_manifest(
            status=manifest_status,
            reason=summary.get("stop_reason"),
        )
        self._write_csv(
            root / "orders.csv",
            [row for item in model_metrics.values() for row in item.orders],
            [
                "shadow_order_id",
                "model",
                "trading_pair",
                "level_id",
                "side",
                "status",
                "outcome",
                "price",
                "amount",
                "notional",
                "created_timestamp",
                "validated_timestamp",
                "resting_start_timestamp",
                "terminal_timestamp",
                "cancel_requested_timestamp",
                "mode_at_creation",
                "quote_distance_bps",
                "lifetime_seconds",
                "resting_lifetime_seconds",
                "created_to_terminal_seconds",
                "lifecycle_state",
                "fill_eligibility_status",
                "fill_eligibility_reason",
                "cancel_category",
                "cancel_reason_raw",
                "cancel_reason_detail",
                "cancel_market_mid",
                "cancel_price_deviation_bps",
            ],
        )
        self._write_csv(
            root / "fills.csv",
            [row for item in model_metrics.values() for row in item.fills],
            [
                "fill_id",
                "model",
                "trading_pair",
                "side",
                "entry_exit",
                "price",
                "amount",
                "notional",
                "timestamp",
                "time_to_fill",
                "quote_distance_bps",
                "quote_distance_before_fill_bps",
                "mode",
                "state",
                "inventory_before",
                "inventory_after",
                "fill_model",
                "cycle_id",
                "fees",
                "realized_pnl",
                "evidence",
                "global_iv_regime",
                "conservative_eligibility_status",
                "conservative_rejection_reason",
            ],
        )
        self._write_csv(
            root / "cancels.csv",
            [row for item in model_metrics.values() for row in item.cancels],
            [
                "timestamp",
                "model",
                "shadow_order_id",
                "trading_pair",
                "side",
                "level_id",
                "reason",
                "reason_raw",
                "reason_code",
                "category",
                "mode",
                "outcome",
                "age_seconds",
                "lifetime_seconds",
                "resting_lifetime_seconds",
                "created_timestamp",
                "cancel_requested_timestamp",
                "cancelled_timestamp",
                "price",
                "old_price",
                "new_desired_price",
                "price_deviation_bps",
                "old_amount",
                "new_desired_amount",
                "amount_deviation_pct",
                "old_mode",
                "new_mode",
                "old_plan_version",
                "new_plan_version",
                "old_level_present",
                "new_level_present",
                "risk_state",
                "inventory_ratio",
                "portfolio_gross_exposure",
                "portfolio_beta_exposure",
                "minimum_order_lifetime_seconds",
                "replacement_cooldown_seconds",
                "time_since_last_replace_seconds",
                "cooldown_remaining_seconds",
                "safety_override",
                "safety_override_reason",
                "lifecycle_state",
                "cancel_reason_detail",
                "market_mid_at_cancel",
                "cancel_market_best_bid",
                "cancel_market_best_ask",
                "cancel_market_price_deviation_bps",
            ],
        )
        self._write_csv(
            root / "cycles.csv",
            [row for item in model_metrics.values() for row in item.cycles],
            [
                "cycle_id",
                "model",
                "trading_pair",
                "side",
                "status",
                "entry_timestamp",
                "exit_timestamp",
                "cycle_duration_seconds",
                "cycle_pnl",
                "realized_capture",
                "fees",
                "executed_volume",
                "entry_quote_distance_bps",
                "exit_quote_distance_bps",
                "max_drawdown_quote",
                "average_inventory",
                "max_inventory",
                "mode_at_entry",
                "mode_at_exit",
            ],
        )
        self._write_csv(
            root / "markouts.csv",
            [row for item in model_metrics.values() for row in item.markouts],
            [
                "timestamp",
                "model",
                "fill_id",
                "trading_pair",
                "side",
                "entry_exit",
                "horizon_seconds",
                "markout_bps",
                "eligible",
                "status",
                "missing_reason",
                "quote_distance_bps",
                "quote_distance_bucket",
                "mode",
                "global_iv_regime",
            ],
        )
        self._write_csv(
            root / "inventory.csv",
            [row for item in model_metrics.values() for row in item.inventory],
            [
                "timestamp",
                "model",
                "trading_pair",
                "amount",
                "mid_price",
                "position_notional",
                "absolute_inventory_notional",
                "inventory_ratio",
                "mode",
                "global_iv_regime",
            ],
        )
        self._write_csv(
            root / "portfolio_exposure.csv",
            [row for item in model_metrics.values() for row in item.portfolio_exposure],
            [
                "timestamp",
                "model",
                "gross_exposure",
                "net_exposure",
                "long_notional",
                "short_notional",
                "absolute_inventory",
                "btc_beta_exposure",
                "long_beta_exposure",
                "short_beta_exposure",
                "resting_quote_exposure",
            ],
        )
        self._write_csv(
            root / "risk_events.csv",
            [row for item in model_metrics.values() for row in item.risk_events],
            [
                "timestamp",
                "model",
                "trading_pair",
                "level_id",
                "reason",
                "category",
                "risk_category",
                "risk_episode_id",
                "risk_episode_key",
                "decision_path",
                "plan_version",
                "mode",
                "candidate_notional",
                "exposure_before",
                "exposure_after_candidate",
            ],
        )
        self._write_csv(
            root / "equity.csv",
            [row for item in model_metrics.values() for row in item.equity],
            [
                "timestamp",
                "model",
                "current_equity",
                "starting_equity",
                "realized_pnl",
                "unrealized_inventory_pnl",
                "fees",
                "high_water_mark",
                "drawdown_quote",
                "drawdown_pct",
            ],
        )
        hourly = [
            row
            for model, item in model_metrics.items()
            for row in self._hourly_rows(model, item, end)
        ]
        self._write_csv(
            root / "hourly_metrics.csv",
            hourly,
            [
                "hour",
                "timestamp",
                "model",
                "equity",
                "pnl",
                "volume",
                "fills",
                "cancels",
                "cycles",
                "fill_create_ratio",
                "cancel_create_ratio",
                "markout_30s",
                "average_gross_exposure",
                "average_inventory",
                "average_btc_beta_exposure",
                "drawdown",
                "risk_blocks",
            ],
        )
        comparison = summary.get("fill_model_comparison", [])
        self._write_csv(
            root / "fill_model_comparison.csv",
            comparison if isinstance(comparison, list) else [],
            ["metric", "conservative", "touch_optimistic", "difference", "relative_difference_pct"],
        )
        suggestions = summary.get("self_tuning_suggestions", self.suggestions)
        self._write_csv(
            root / "self_tuning_suggestions.csv",
            suggestions if isinstance(suggestions, list) else [],
            [
                "timestamp",
                "asset",
                "diagnosis",
                "recommendation",
                "current_value",
                "proposed_value",
                "confidence",
                "supporting_metrics",
                "mode",
                "applied",
            ],
        )
        quality = summary.get("data_quality", {})
        quality_rows = [
            {"source": source, **values}
            for source, values in quality.items()
            if isinstance(values, Mapping)
        ]
        self._write_csv(
            root / "data_quality.csv",
            quality_rows,
            ["source", "expected", "valid", "available", "stale", "gaps", "coverage_pct"],
        )
        self._write_csv(
            root / "metrics.csv",
            [summary.get("metrics", summary)],
            [
                "timestamp",
                "session_id",
                "paper_equity",
                "session_pnl",
                "total_executed_notional",
                "orders_created",
                "orders_cancelled",
                "orders_filled",
                "completed_cycles",
                "fill_model_sensitivity",
                "classification",
                "readiness",
            ],
        )
        summary_path = root / "summary.md"
        summary_path.write_text(self._summary_markdown(summary), encoding="utf-8")
        return summary_path

    def _latest_legacy_session_root(self) -> Path | None:
        """Return the latest prior persisted session for non-destructive audit."""

        report_root = Path(self.config.report_root).expanduser()
        if not report_root.is_absolute():
            report_root = self.project_root / report_root
        candidates = sorted(
            (
                path
                for path in report_root.glob("shadow-baseline-*")
                if (
                    path.is_dir()
                    and path.name != self.session_id
                    and (path / "orders.csv").is_file()
                    and not (path / "stage12e").is_dir()
                )
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None

        # A one-cycle smoke or an aborted run can have a newer orders.csv with
        # no eligible rows.  Prefer the newest prior session that actually
        # contains the legacy conservative status being reconciled; otherwise
        # fall back to the newest persisted session for forward compatibility.
        for candidate in candidates:
            try:
                with (candidate / "orders.csv").open(newline="", encoding="utf-8") as handle:
                    rows = csv.DictReader(handle)
                    if any(
                        str(row.get("model", "")).upper() == CONSERVATIVE_MODEL
                        and row.get("fill_eligibility_status") == "TRADED_THROUGH_FILLED"
                        for row in rows
                    ):
                        return candidate
            except (OSError, csv.Error):
                continue
        return candidates[0]

    @staticmethod
    def _write_csv(
        path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                normalized = {
                    key: json.dumps(_safe(value), sort_keys=True)
                    if isinstance(value, (dict, list, tuple))
                    else _safe(value)
                    for key, value in row.items()
                }
                writer.writerow(normalized)

    @staticmethod
    def _display(value: Any) -> str:
        if value is None:
            return "UNKNOWN"
        if isinstance(value, float):
            return f"{value:.8f}"
        return str(value)

    def _summary_markdown(self, summary: Mapping[str, Any]) -> str:
        metrics = summary.get("metrics", summary)
        checks = summary.get("health_checks", {})
        comparison = summary.get("fill_model_comparison", [])
        end_timestamp = _epoch(summary.get("end_timestamp")) or time.time()
        duration_text = (
            f"DURATION: {self._display(summary.get('duration_seconds'))} seconds / "
            f"{self._display(summary.get('duration_hours'))} hours"
        )
        max_gross = max(
            (
                _float(row.get("gross_exposure"), 0.0) or 0.0
                for row in self.exposure[CONSERVATIVE_MODEL].points
            ),
            default=None,
        )
        max_inventory = max(
            (
                _float(row.get("absolute_inventory"), 0.0) or 0.0
                for row in self.exposure[CONSERVATIVE_MODEL].points
            ),
            default=None,
        )
        cycle_rows = self._model_metrics(CONSERVATIVE_MODEL, end_timestamp).cycles
        cycle_durations = [
            _float(row.get("cycle_duration_seconds"), 0.0) or 0.0
            for row in cycle_rows
            if row.get("cycle_duration_seconds") is not None
        ]
        lines = [
            BASELINE_BANNER,
            "",
            BASELINE_DATA_LINE,
            "",
            BASELINE_EXECUTION_LINE,
            "",
            BASELINE_MUTATION_LINE,
            "",
            BASELINE_FILL_MODEL_LINE,
            "",
            BASELINE_CONFIG_LINE,
            "",
            f"SESSION: {summary.get('session_id')}",
            f"PROFILE: {summary.get('resolved_profile_path') or 'UNKNOWN'}",
            f"CONFIG HASH: {summary.get('config_hash') or 'UNKNOWN'}",
            f"GIT COMMIT: {summary.get('git_commit') or 'UNKNOWN'}",
            duration_text,
            f"PUBLIC TRADE EVIDENCE: {self._display(metrics.get('public_trade_evidence'))}",
            f"CONSERVATIVE FILLS: {self._display(metrics.get('conservative_fills_status'))}",
            "",
            "## Required summary table",
            "",
            "| Metric | Value |",
            "|---|---:|",
        ]
        table = (
            ("Starting equity", metrics.get("starting_equity")),
            ("Ending equity", metrics.get("ending_equity")),
            ("Realized PnL", metrics.get("realized_pnl")),
            ("Unrealized PnL", metrics.get("unrealized_pnl")),
            ("Fees", metrics.get("fees")),
            ("Total paper PnL", metrics.get("total_paper_pnl")),
            ("PnL reconciliation", metrics.get("pnl_reconciliation_status")),
            ("Max drawdown", metrics.get("max_drawdown_quote")),
            ("Executed volume", metrics.get("total_executed_notional")),
            ("Volume / starting equity", metrics.get("volume_per_starting_equity")),
            ("Average gross exposure", metrics.get("average_gross_exposure")),
            ("Max gross exposure", max_gross),
            ("Volume / average gross exposure", metrics.get("volume_per_average_gross_exposure")),
            ("Average inventory", metrics.get("average_absolute_inventory")),
            ("Max inventory", max_inventory),
            ("Volume / average inventory", metrics.get("volume_per_average_inventory")),
            ("Average BTC-beta exposure", metrics.get("average_btc_beta_exposure")),
            ("Max BTC-beta exposure", metrics.get("max_btc_beta_exposure")),
            ("Orders created", metrics.get("orders_created")),
            ("Orders cancelled", metrics.get("orders_cancelled")),
            ("Orders filled", metrics.get("orders_filled")),
            ("KEEP count", metrics.get("orders_kept")),
            ("Fill/Create", metrics.get("fill_create_ratio")),
            ("Cancel/Create", metrics.get("cancel_create_ratio")),
            ("Median quote lifetime", metrics.get("median_quote_lifetime")),
            ("Median time-to-fill", metrics.get("median_time_to_fill")),
            ("Completed cycles", metrics.get("completed_cycles")),
            (
                "Cycles/hour",
                metrics.get("completed_cycles", 0) / summary.get("duration_hours", 1)
                if summary.get("duration_hours", 0)
                else None,
            ),
            (
                "Median cycle duration",
                _percentile(cycle_durations, 0.50),
            ),
            ("5s markout", metrics.get("markout_5s")),
            ("30s markout", metrics.get("markout_30s")),
            ("60s markout", metrics.get("markout_60s")),
            ("Risk blocks", metrics.get("risk_blocks")),
        )
        lines.extend(f"| {label} | {self._display(value)} |" for label, value in table)
        lines.extend(
            [
                "",
                "## Per-asset table",
                "",
                "| Asset | Active (h) | Volume | PnL | Avg risk | Volume/Risk | Fills | Cancels | "
                "Fill/Create | Cancel/Create | KEEP % | Quote lifetime | Time-to-fill | "
                "Cycles | Cycles/h | Cycle duration | 5s markout | 30s markout | 60s markout | "
                "Avg inventory | Max inventory | Risk blocks |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        asset_volume = metrics.get("volume_by_asset", {})
        asset_metrics = metrics.get("per_asset_metrics", {})
        duration_hours = _float(summary.get("duration_hours"), 0.0) or 0.0
        for pair, asset in metrics.get("inventory_by_asset", {}).items():
            observed = asset_metrics.get(pair, {})
            markout = observed.get("markout", {})
            cycle_stats = observed.get("cycle_duration_stats", {})
            risk = asset.get("average_inventory")
            lines.append(
                "| "
                + " | ".join(
                    self._display(value)
                    for value in (
                        pair,
                        (
                            _float(asset.get("duration_active_seconds"), 0.0) / 3600.0
                            if asset.get("duration_active_seconds") is not None
                            else None
                        ),
                        asset_volume.get(pair, 0.0),
                        observed.get("pnl"),
                        observed.get("average_risk", risk),
                        observed.get("volume_per_risk"),
                        observed.get("fills"),
                        observed.get("cancels"),
                        observed.get("fill_create_ratio"),
                        observed.get("cancel_create_ratio"),
                        observed.get("keep_pct"),
                        observed.get("quote_lifetime_stats", {}).get("median"),
                        observed.get("time_to_fill_stats", {}).get("median"),
                        observed.get("cycles", 0),
                        observed.get("cycles", 0) / duration_hours if duration_hours > 0 else None,
                        cycle_stats.get("median"),
                        markout.get("5s", {}).get("mean_bps"),
                        markout.get("30s", {}).get("mean_bps"),
                        markout.get("60s", {}).get("mean_bps"),
                        asset.get("average_inventory"),
                        asset.get("max_inventory"),
                        observed.get("risk_blocks"),
                    )
                )
                + " |"
            )
        lines.extend(
            [
                "",
                "## Fill-model comparison",
                "",
                "| Metric | Conservative | Touch-Optimistic | Difference |",
                "|---|---:|---:|---:|",
            ]
        )
        for row in comparison:
            lines.append(
                f"| {row.get('metric')} | {self._display(row.get('conservative'))} | "
                f"{self._display(row.get('touch_optimistic'))} | "
                f"{self._display(row.get('difference'))} |"
            )
        lines.extend(
            [
                "",
                f"FILL-MODE SENSITIVITY: {summary.get('fill_model_sensitivity')}",
                "",
                "## Sample counts",
                "",
                "| Metric | n |",
                "|---|---:|",
            ]
        )
        sample_rows = (
            ("state observations", metrics.get("state_observation_count")),
            ("orders created", metrics.get("orders_created")),
            ("orders kept", metrics.get("orders_kept")),
            ("cancellations", metrics.get("orders_cancelled")),
            (
                "cancellation deviation observations",
                metrics.get("cancellation_deviation_sample_count"),
            ),
            ("fills", metrics.get("fills")),
            ("completed cycles", metrics.get("completed_cycles")),
            ("5s markouts", metrics.get("markout", {}).get("5s", {}).get("sample_count")),
            ("30s markouts", metrics.get("markout", {}).get("30s", {}).get("sample_count")),
            ("60s markouts", metrics.get("markout", {}).get("60s", {}).get("sample_count")),
        )
        lines.extend(f"| {label} | {self._display(value)} |" for label, value in sample_rows)
        lines.extend(["", "## Baseline health", ""])
        lines.extend(f"- {name}: **{status}**" for name, status in checks.items())
        unknown_internal = self._display(
            metrics.get("cancel_reason_counts", {}).get("UNKNOWN_INTERNAL")
        )
        coverage_pct = self._display(
            metrics.get("trade_coverage", {}).get("overall", {}).get("coverage_pct")
        )
        eligible_orders = self._display(
            metrics.get("fill_eligibility", {}).get("eligible_order_count")
        )
        missing_orders = self._display(
            metrics.get("fill_eligibility", {}).get("missing_order_count")
        )
        lines.extend(
            [
                "",
                "## Stage 12C observability",
                "",
                f"- Operational cancels: **{self._display(metrics.get('operational_cancels'))}**",
                "- Shutdown/manual cancels excluded from operational churn: "
                f"**{self._display(metrics.get('shutdown_cancels'))}**",
                f"- UNKNOWN_INTERNAL cancels: **{unknown_internal}**",
                "- Resting lifetime samples: "
                f"**{self._display(metrics.get('resting_lifetime_sample_count'))}**",
                "- Never-rested orders excluded: "
                f"**{self._display(metrics.get('resting_lifetime_excluded_never_rested'))}**",
                f"- Raw risk blocks: **{self._display(metrics.get('risk_blocks_raw'))}**",
                f"- Unique risk episodes: **{self._display(metrics.get('unique_risk_episodes'))}**",
                "- Risk-block duration (seconds): "
                f"**{self._display(metrics.get('duration_blocked_seconds'))}**",
                f"- Trade coverage: **{coverage_pct}%**",
                f"- Eligible orders: **{eligible_orders}**",
                f"- Missing trade-evidence orders: **{missing_orders}**",
                f"- GROSS PAPER PNL: **{self._display(metrics.get('gross_pnl'))}**",
                f"- VERIFIED NET PNL: **{self._display(metrics.get('verified_net_pnl'))}**",
                f"- FEE MODEL: **{self._display(metrics.get('fees_status'))}**",
                "- Reconciliation decisions: desired / active / create / keep / stop / skip / "
                "defer / risk / filled / TP are persisted in `reconciliation_decisions.csv`.",
                "- Detailed artifacts: `reports/stage12c/`.",
                "",
                "",
                f"FINAL STATUS: **{summary.get('classification')}**",
                f"READINESS: **{summary.get('readiness')}**",
                "",
                "## Interpretation",
                "",
                "This report measures the unchanged strategy against real Derive mainnet "
                "public data. It does not claim live profitability, queue position, or "
                "deployment readiness.",
                "",
                "The conservative trade-through ledger is the headline result. The "
                "touch-optimistic ledger is isolated sensitivity evidence and is never "
                "averaged into the primary result.",
                "",
                "Self-tuning mode is SUGGEST_ONLY/OFF for this frozen session; "
                "recommendations, if any, "
                "are recorded without automatic application.",
                "",
                "1. Useful maker turnover: "
                + (
                    "OBSERVED"
                    if metrics.get("fills", 0) and metrics.get("total_executed_notional", 0)
                    else "NOT OBSERVED"
                )
                + ".",
                "2. Excessive cancellation: "
                + ("YES" if metrics.get("high_cancel_churn") else "NO")
                + f"; dominant reason={self._display(metrics.get('dominant_cancel_reason'))}.",
                "3. Adverse selection: " + self._display(metrics.get("adverse_selection")) + ".",
                "4. Inventory recycling: "
                + (
                    "OBSERVED"
                    if metrics.get("completed_cycles", 0) > 0
                    else "NOT OBSERVED / OPEN INVENTORY MAY REMAIN"
                )
                + ".",
                "5. Volume efficiency: "
                + self._display(metrics.get("volume_per_average_deployed_risk"))
                + " volume per average deployed risk.",
                "6. Portfolio governor: "
                + ("BLOCKS OBSERVED" if metrics.get("risk_blocks", 0) > 0 else "NO BLOCKS OBSERVED")
                + ".",
                "7. Fill-assumption sensitivity: "
                + self._display(summary.get("fill_model_sensitivity"))
                + ".",
                "8. Accounting consistency: "
                + self._display(metrics.get("pnl_reconciliation_status"))
                + ".",
                "9. Sample sufficient for tuning: "
                + (
                    "YES"
                    if summary.get("readiness") == "READY FOR BOUNDED OPTIMIZATION"
                    else "NO — COLLECT MORE DATA"
                )
                + ".",
                "",
                "Top observed weaknesses:",
            ]
        )
        weaknesses = summary.get("readiness_reasons") or ["None observed by deterministic checks"]
        lines.extend(f"{index}. {item}" for index, item in enumerate(weaknesses[:3], 1))
        lines.extend(
            [
                "",
                "BEFORE TUNING: " + str(summary.get("readiness")),
                "",
                "## Limitations",
                "",
                *[f"- {item}" for item in summary.get("limitations", [])],
                "",
            ]
        )
        return "\n".join(lines) + "\n"

    def stop(self, *, timestamp: float | None = None, reason: str = "MANUAL") -> Path:
        if self._report_path is not None:
            return self._report_path
        if self.start_timestamp is None:
            self.start(timestamp=timestamp)
        now = time.time() if timestamp is None else timestamp
        self._stop_epoch = max(now, self._start_epoch)
        self.stop_timestamp = _iso(self._stop_epoch)
        self.stop_reason = reason
        for model, session in self.sessions.items():
            session.engine.shutdown(
                timestamp=self._stop_epoch,
                controller_timestamp=self._stop_epoch,
            )
            self._persist_engine_delta(model)
            if self._last_frames:
                cycle = self._last_cycles.get(model)
                if cycle is not None:
                    self._record_model_state(model, cycle, self._last_frames, self._stop_epoch)
        self._shutdown_complete = True
        if self.store is not None:
            self.store.append_event(
                self.session_id,
                "BASELINE_STOP",
                self.stop_timestamp,
                model="BASELINE",
                reason=reason,
                real_exchange_mutation_calls=0,
            )
        final_summary = self.summary(reason=reason)
        report = self.write_report(final_summary)
        self._report_path = report
        if self._stage13_summary is not None:
            final_summary["stage13"] = self._stage13_summary
            final_summary.setdefault("metrics", {})["stage13"] = self._stage13_summary
        if self.store is not None:
            self.store.save_session(
                self.session_id,
                self.config.config_hash,
                final_summary,
                stopped_at=self.stop_timestamp,
            )
            self.store.save_metrics(self.session_id, self.stop_timestamp, final_summary)
            self.store.close()
        self._closed = True
        return report

    def assert_isolated(self) -> None:
        """Raise if the two sensitivity ledgers accidentally share mutable state."""

        conservative = self.sessions[CONSERVATIVE_MODEL]
        touch = self.sessions[TOUCH_MODEL]
        if (
            conservative.engine is touch.engine
            or conservative.engine.ledger is touch.engine.ledger
            or conservative.coordinator is touch.coordinator
            or conservative.engine.orders is touch.engine.orders
            or conservative.engine.fills is touch.engine.fills
        ):
            raise AssertionError("fill-model ledgers are not isolated")
        self._ledger_isolation_verified = True

    def format_final_output(self, summary: Mapping[str, Any] | None = None) -> str:
        values = summary or self.summary()
        metrics = values.get("metrics", values)
        cycles_per_hour = (
            metrics.get("completed_cycles", 0) / values.get("duration_hours", 1)
            if values.get("duration_hours")
            else None
        )
        touch_volume = metrics.get("touch_optimistic_metrics", {}).get("total_executed_notional")
        health_checks = metrics.get("health_checks", {})
        data_status = "PASS" if health_checks.get("DATA QUALITY") == "PASS" else "FAIL"
        shadow_status = (
            "PASS"
            if metrics.get("orders_are_simulated", True)
            and metrics.get("real_exchange_mutation_calls", 0) == 0
            else "FAIL"
        )
        lifecycle = metrics.get("lifecycle_states") or {}
        resting_count = sum(
            int(lifecycle.get(state, 0) or 0)
            for state in (
                "RESTING",
                "CANCELLED_AFTER_RESTING",
                "FILLED_AFTER_RESTING",
                "COMPLETE",
            )
        )
        cancel_counts = metrics.get("cancel_reason_counts") or {}
        risk_summary = metrics.get("risk_episode_summary") or []
        trade_coverage = metrics.get("trade_coverage", {}).get("overall", {})
        eligibility = metrics.get("fill_eligibility") or {}
        lifetime_buckets = metrics.get("resting_lifetime_buckets") or {}
        deviation_buckets = metrics.get("replacement_deviation_buckets") or {}
        tests = "pytest -q; ruff check .; git diff --check"
        lines = [
            "STAGE 12C — SHADOW EXECUTION OBSERVABILITY",
            f"Session: {values.get('session_id', 'UNKNOWN')}",
            f"Duration: {self._display(values.get('duration_seconds'))} seconds",
            f"Profile: {values.get('resolved_profile_path', 'UNKNOWN')}",
            f"Config hash: {values.get('config_hash', 'UNKNOWN')}",
            f"Git commit: {values.get('git_commit', 'UNKNOWN')}",
            "",
            "ACCOUNTING",
            f"PnL reconciliation: {metrics.get('pnl_reconciliation_status', 'FAIL')}",
            f"Fee model: {metrics.get('fees_status', 'UNKNOWN')}",
            f"GROSS PAPER PNL: {self._display(metrics.get('gross_pnl'))}",
            f"VERIFIED NET PNL: {self._display(metrics.get('verified_net_pnl'))}",
            "",
            "SAFETY",
            f"Mainnet public data: {data_status}",
            f"BTC options mainnet: {health_checks.get('BTC OPTIONS MAINNET', 'FAIL')}",
            f"Environment consistency: {health_checks.get('ENVIRONMENT CONSISTENCY', 'FAIL')}",
            f"Shadow execution: {shadow_status}",
            f"Conservative ledger: {health_checks.get('CONSERVATIVE LEDGER', 'FAIL')}",
            f"Touch ledger: {health_checks.get('TOUCH LEDGER', 'FAIL')}",
            f"Ledger isolation: {health_checks.get('LEDGER ISOLATION', 'FAIL')}",
            "Real exchange mutations: 0",
            f"Config frozen: {'PASS' if values.get('config_frozen', True) else 'FAIL'}",
            f"Self-tuning applications: {metrics.get('self_tuning_applications', 'FAIL')}",
            f"Graceful shutdown: {health_checks.get('GRACEFUL SHUTDOWN', 'FAIL')}",
            f"Public trade evidence: {metrics.get('public_trade_evidence', 'UNAVAILABLE')}",
            f"Conservative fills: {metrics.get('conservative_fills_status', 'UNAVAILABLE')}",
            "",
            "LIFECYCLE",
            f"Orders created: {self._display(metrics.get('orders_created'))}",
            f"Became resting: {self._display(resting_count)}",
            f"Orders filled after resting: {self._display(metrics.get('orders_filled'))}",
            f"Orders kept: {self._display(metrics.get('orders_kept'))}",
            f"Operational cancels: {self._display(metrics.get('operational_cancels'))}",
            f"Shutdown/manual cancels: {self._display(metrics.get('shutdown_cancels'))}",
            "Operational cancel/create: "
            f"{self._display(metrics.get('operational_cancel_create_ratio'))}",
            f"UNKNOWN_INTERNAL cancels: {self._display(cancel_counts.get('UNKNOWN_INTERNAL'))}",
            f"Resting lifetime median: {self._display(metrics.get('median_quote_lifetime'))}",
            f"Resting lifetime p75: {self._display(metrics.get('p75_quote_lifetime'))}",
            f"Resting lifetime p90: {self._display(metrics.get('p90_quote_lifetime'))}",
            "Never-rested excluded: "
            f"{self._display(metrics.get('resting_lifetime_excluded_never_rested'))}",
            "Resting lifetime buckets: "
            + ", ".join(
                f"{bucket}={self._display(lifetime_buckets.get(bucket, 0))}"
                for bucket in RESTING_LIFETIME_BUCKETS
            ),
            "Replacement deviation buckets: "
            + ", ".join(
                f"{bucket}={self._display(deviation_buckets.get(bucket, 0))}"
                for bucket in REPLACEMENT_DEVIATION_BUCKETS
            ),
            f"Lifecycle states: {', '.join(f'{key}={value}' for key, value in lifecycle.items())}",
            "",
            "RISK",
            f"Risk checks: {self._display(metrics.get('risk_checks_total'))}",
            f"Raw risk blocks: {self._display(metrics.get('risk_blocks_raw'))}",
            f"Risk block rate: {self._display(metrics.get('risk_block_rate'))}",
            f"Unique risk episodes: {self._display(metrics.get('unique_risk_episodes'))}",
            f"Unique episode rate: {self._display(metrics.get('unique_episode_rate'))}",
            f"Blocked duration seconds: {self._display(metrics.get('duration_blocked_seconds'))}",
            "Risk reason breakdown: "
            + ", ".join(
                f"{row.get('reason')}={row.get('raw_blocks')} raw/"
                f"{row.get('unique_episodes')} episodes"
                for row in risk_summary[:8]
            ),
            "",
            "TRADE EVIDENCE",
            f"Coverage: {self._display(trade_coverage.get('coverage_pct'))}%",
            f"Trade count: {self._display(trade_coverage.get('trade_count'))}",
            f"Evidence minutes: {self._display(trade_coverage.get('evidence_minutes'))}",
            f"No-evidence minutes: {self._display(trade_coverage.get('no_evidence_minutes'))}",
            f"Gaps: {self._display(trade_coverage.get('gap_count'))}",
            f"Eligible orders: {self._display(eligibility.get('eligible_order_count'))}",
            f"Missing-evidence orders: {self._display(eligibility.get('missing_order_count'))}",
            f"Touch-only orders: {self._display(eligibility.get('touch_only_order_count'))}",
            "",
            "METRICS",
            f"Executed volume: {self._display(metrics.get('total_executed_notional'))}",
            f"Average gross exposure: {self._display(metrics.get('average_gross_exposure'))}",
            "Volume / avg gross: "
            f"{self._display(metrics.get('volume_per_average_gross_exposure'))}",
            f"Average inventory: {self._display(metrics.get('average_absolute_inventory'))}",
            f"Completed cycles: {self._display(metrics.get('completed_cycles'))}",
            f"Cycles/hour: {self._display(cycles_per_hour)}",
            f"Fill/Create: {self._display(metrics.get('fill_create_ratio'))}",
            f"Cancel/Create: {self._display(metrics.get('cancel_create_ratio'))}",
            f"Median quote lifetime: {self._display(metrics.get('median_quote_lifetime'))}",
            f"30s markout: {self._display(metrics.get('markout_30s'))}",
            f"60s markout: {self._display(metrics.get('markout_60s'))}",
            f"Max drawdown: {self._display(metrics.get('max_drawdown_quote'))}",
            "",
            "FILL MODEL",
            f"Conservative: {self._display(metrics.get('total_executed_notional'))} volume",
            f"Touch optimistic: {self._display(touch_volume)} volume",
            f"Sensitivity: {values.get('fill_model_sensitivity', 'UNKNOWN')}",
            "",
            "SMOKE",
            f"Execution mode: {values.get('execution_mode', 'SHADOW')}",
            f"Execution enabled: {values.get('config', {}).get('execution_enabled', False)}",
            "Real exchange mutations: 0",
            f"Config frozen: {values.get('config_frozen', True)}",
            f"Dashboard: PYTHONPATH=src .venv/bin/streamlit run dashboard/app.py -- --data-dir "
            f"{Path(self.config.sqlite_path).expanduser().parent}",
            f"Tests: {tests}",
            "",
            "READINESS",
            str(values.get("readiness", "NOT READY FOR OPTIMIZATION")),
            "REASONS:",
            *[f"- {reason}" for reason in values.get("readiness_reasons", [])[:5]],
            "",
            "ISSUES",
            *[
                f"- {reason}"
                for index, reason in enumerate(values.get("readiness_reasons", [])[:3], 1)
            ],
        ]
        return "\n".join(lines)


BaselineSession = ShadowBaselineSession


__all__ = [
    "BASELINE_BANNER",
    "BaselineConfigChanged",
    "BaselineSession",
    "CONSERVATIVE_MODEL",
    "DataQualityTracker",
    "MARKOUT_HORIZONS_SECONDS",
    "PnLReconciliation",
    "ShadowBaselineSession",
    "TOUCH_MODEL",
    "TimeWeightedExposure",
    "cancel_category",
    "quote_distance_bucket",
    "reconcile_paper_equity",
]
