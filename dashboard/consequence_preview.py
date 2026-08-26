"""Deterministic, non-mutating consequence previews for staged settings."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from derive_options_mm.competition_risk import (
    CompetitionMarketRule,
    CompetitionProfile,
    assess_order_sizing,
)


@dataclass(frozen=True)
class PreviewResult:
    """A serializable consequence summary for the UI."""

    title: str
    values: dict[str, Any]
    warnings: tuple[str, ...] = ()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def risk_consequence_preview(
    current: CompetitionProfile,
    proposed: CompetitionProfile,
    *,
    account_equity: float | None = None,
    gross_notional: float = 0.0,
    beta_long: float = 0.0,
    beta_short: float = 0.0,
) -> PreviewResult:
    equity = _number(account_equity, current.starting_equity_reference)
    return PreviewResult(
        title="Deterministic portfolio consequence preview",
        values={
            "current_equity": equity,
            "old_soft_gross": current.portfolio_soft_gross_notional,
            "new_soft_gross": proposed.portfolio_soft_gross_notional,
            "old_hard_gross": current.portfolio_max_gross_notional,
            "new_hard_gross": proposed.portfolio_max_gross_notional,
            "old_hard_beta": current.portfolio_hard_beta_exposure,
            "new_hard_beta": proposed.portfolio_hard_beta_exposure,
            "old_hard_beta_reference_multiple": current.portfolio_hard_beta_exposure / equity,
            "new_hard_beta_reference_multiple": proposed.portfolio_hard_beta_exposure / equity,
            "current_gross_utilization": gross_notional / proposed.portfolio_max_gross_notional,
            "current_long_beta_utilization": beta_long / proposed.portfolio_max_long_beta_exposure,
            "current_short_beta_utilization": (
                beta_short / proposed.portfolio_max_short_beta_exposure
            ),
            "old_reserve": current.collateral_reserve_quote,
            "new_reserve": proposed.collateral_reserve_quote,
        },
    )


def _rule(value: Mapping[str, Any]) -> CompetitionMarketRule:
    return CompetitionMarketRule.model_validate(value)


def order_size_consequence_preview(
    current: CompetitionProfile,
    proposed: CompetitionProfile,
    rules: Mapping[str, Mapping[str, Any]],
) -> PreviewResult:
    rows: dict[str, dict[str, Any]] = {}
    for pair in ("BTC-USDC", "ETH-USDC", "SOL-USDC", "HYPE-USDC"):
        raw = rules.get(pair)
        if not raw:
            rows[pair] = {"status": "UNKNOWN", "reason": "no exchange rule snapshot"}
            continue
        market_rule = _rule(raw)
        current_sizing = assess_order_sizing(
            market_rule,
            target_order_notional=current.target_order_notional,
            max_single_order_notional=current.max_single_order_notional,
        )
        proposed_sizing = assess_order_sizing(
            market_rule,
            target_order_notional=proposed.target_order_notional,
            max_single_order_notional=proposed.max_single_order_notional,
        )
        rows[pair] = {
            "current_eligible": current_sizing.eligible,
            "proposed_eligible": proposed_sizing.eligible,
            "current_notional": current_sizing.actual_target_notional,
            "proposed_notional": proposed_sizing.actual_target_notional,
            "minimum_valid_notional": proposed_sizing.minimum_valid_notional,
            "reason": proposed_sizing.reason or "",
        }
    current_active = sum(
        2 * (value.get("current_notional", 0.0) if value.get("current_eligible") else 0.0)
        for value in rows.values()
    )
    proposed_active = sum(
        2 * (value.get("proposed_notional", 0.0) if value.get("proposed_eligible") else 0.0)
        for value in rows.values()
    )
    return PreviewResult(
        title="Order-size consequence preview",
        values={
            "markets": rows,
            "current_potential_active_entry_notional": current_active,
            "proposed_potential_active_entry_notional": proposed_active,
            "current_all_candidate_gross": current_active,
            "proposed_all_candidate_gross": proposed_active,
        },
    )


def _level_map(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    levels: dict[str, Mapping[str, Any]] = {}
    for side in ("buy", "sell"):
        for level in record.get(f"{side}_levels", []) or []:
            if isinstance(level, Mapping):
                levels[f"{side}_{level.get('level_index', 0)}"] = level
    return levels


def refresh_stability_estimate(
    plan_records: Sequence[Mapping[str, Any]],
    *,
    price_tolerance_bps: float,
    amount_tolerance_pct: float,
) -> PreviewResult:
    """Estimate KEEP/REFRESH/NEW/REMOVED from recent GridPlan history.

    This is intentionally labeled historical: it does not know queue position,
    order age, marketability, or whether an executor was already filled.
    """

    totals = {"KEEP": 0, "REFRESH": 0, "NEW": 0, "REMOVED": 0}
    previous: dict[str, Mapping[str, Any]] = {}
    for record in plan_records:
        current = _level_map(record)
        for level_id, level in current.items():
            old = previous.get(level_id)
            if old is None:
                totals["NEW"] += 1
                continue
            old_price = _number(old.get("theoretical_price"))
            new_price = _number(level.get("theoretical_price"))
            old_amount = _number(old.get("quote_amount"))
            new_amount = _number(level.get("quote_amount"))
            price_bps = (
                abs(new_price - old_price) / old_price * 10_000 if old_price > 0 else math.inf
            )
            amount_pct = abs(new_amount - old_amount) / old_amount if old_amount > 0 else math.inf
            if price_bps >= price_tolerance_bps or amount_pct >= amount_tolerance_pct:
                totals["REFRESH"] += 1
            else:
                totals["KEEP"] += 1
        totals["REMOVED"] += len(set(previous) - set(current))
        previous = current
    denominator = sum(totals.values()) or 1
    percentages = {key: value / denominator for key, value in totals.items()}
    return PreviewResult(
        title="HISTORICAL ESTIMATE — recent GridPlan history",
        values={
            "counts": totals,
            "percentages": percentages,
            "observations": len(plan_records),
        },
        warnings=(
            "Historical estimate only; it is not a future guarantee and does not model "
            "queue position or fills.",
        ),
    )


__all__ = [
    "PreviewResult",
    "order_size_consequence_preview",
    "refresh_stability_estimate",
    "risk_consequence_preview",
]
