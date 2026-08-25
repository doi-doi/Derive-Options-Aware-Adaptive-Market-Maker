"""Pure shared-risk routing for the BTC/HYPE execution controller.

The Stage 8 coordinator remains the source of truth for read-only multi-asset
state and theoretical plans.  This small adapter is intentionally independent
of that strategy package so the Hummingbot container can enforce one shared
portfolio boundary while consuming the existing JSONL plans.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .execution_logic import BlockedLevel, DesiredLevel, ReconciliationResult

ZERO = Decimal("0")
ONE = Decimal("1")


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


@dataclass(frozen=True)
class PortfolioExecutionPolicy:
    """Hard and soft limits shared by every configured pair."""

    portfolio_max_gross_notional: Decimal = Decimal("700")
    portfolio_soft_beta_exposure: Decimal = Decimal("450")
    portfolio_hard_beta_exposure: Decimal = Decimal("650")
    portfolio_max_long_beta_exposure: Decimal = Decimal("650")
    portfolio_max_short_beta_exposure: Decimal = Decimal("650")
    per_asset_max_position_notional: Decimal = Decimal("400")
    max_active_executors_per_asset: int = 2
    max_active_executors_portfolio: int = 4
    collateral_safety_buffer_pct: Decimal = Decimal("0.20")
    leverage: Decimal = Decimal("1")
    betas: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.portfolio_max_gross_notional <= ZERO:
            raise ValueError("portfolio_max_gross_notional must be positive")
        if self.portfolio_soft_beta_exposure <= ZERO:
            raise ValueError("portfolio_soft_beta_exposure must be positive")
        if self.portfolio_hard_beta_exposure <= self.portfolio_soft_beta_exposure:
            raise ValueError("portfolio_hard_beta_exposure must exceed the soft limit")
        if self.portfolio_max_long_beta_exposure <= ZERO:
            raise ValueError("portfolio_max_long_beta_exposure must be positive")
        if self.portfolio_max_short_beta_exposure <= ZERO:
            raise ValueError("portfolio_max_short_beta_exposure must be positive")
        if self.per_asset_max_position_notional <= ZERO:
            raise ValueError("per_asset_max_position_notional must be positive")
        if self.max_active_executors_per_asset < 1:
            raise ValueError("max_active_executors_per_asset must be positive")
        if self.max_active_executors_portfolio < 1:
            raise ValueError("max_active_executors_portfolio must be positive")
        if not ZERO <= self.collateral_safety_buffer_pct < ONE:
            raise ValueError("collateral_safety_buffer_pct must be in [0, 1)")
        if self.leverage <= ZERO:
            raise ValueError("leverage must be positive")


@dataclass(frozen=True)
class PortfolioRiskDecision:
    """Decision for the prospective entries in one controller tick."""

    allowed_level_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    blocked_level_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    blocked_reasons: dict[str, dict[str, str]] = field(default_factory=dict)
    gross_notional: Decimal = ZERO
    net_notional: Decimal = ZERO
    beta_exposure: Decimal = ZERO
    soft_limit_triggered: bool = False
    hard_limit_triggered: bool = False
    global_pause_new_exposure: bool = False
    active_executors: int = 0
    reasons: tuple[str, ...] = ()


def _pending_values(
    pending: Mapping[str, Mapping[str, Any]] | None,
    pair: str,
) -> tuple[Decimal, Decimal, int]:
    raw = (pending or {}).get(pair, {})
    return (
        max(ZERO, _decimal(raw.get("buy", raw.get("pending_buy_notional")))),
        max(ZERO, _decimal(raw.get("sell", raw.get("pending_sell_notional")))),
        max(0, int(_decimal(raw.get("count", raw.get("pending_count"))))) if raw else 0,
    )


def evaluate_portfolio_risk(
    proposed: Mapping[str, Sequence[DesiredLevel]],
    *,
    positions: Mapping[str, Any] | None = None,
    pending: Mapping[str, Mapping[str, Any]] | None = None,
    active_executors: Mapping[str, Any] | None = None,
    available_collateral: Decimal = ZERO,
    policy: PortfolioExecutionPolicy | None = None,
) -> PortfolioRiskDecision:
    """Route exposure-increasing entries without duplicating portfolio risk.

    ``positions`` are signed quote-notionals.  Existing unfilled entries are
    represented by ``pending``.  Risk-reducing entries are allowed even when a
    soft or hard limit is already reached so PAUSE/DEFENSIVE behavior can still
    manage an existing position.
    """

    policy = policy or PortfolioExecutionPolicy()
    pairs = tuple(proposed)
    position_values = {pair: _decimal((positions or {}).get(pair)) for pair in pairs}
    pending_values = {pair: _pending_values(pending, pair) for pair in pairs}
    betas = {pair: _decimal(policy.betas.get(pair), ONE) for pair in pairs}

    asset_exposure: dict[str, Decimal] = {}
    working_positions: dict[str, Decimal] = {}
    gross = ZERO
    net = ZERO
    beta_exposure = ZERO
    working_pending = ZERO
    working_asset_executors = {
        pair: max(0, int(_decimal((active_executors or {}).get(pair)))) for pair in pairs
    }
    for pair in pairs:
        position = position_values[pair]
        pending_buy, pending_sell, _ = pending_values[pair]
        effective = position + pending_buy - pending_sell
        exposure = abs(position) + pending_buy + pending_sell
        beta_value = effective * betas[pair]
        asset_exposure[pair] = exposure
        working_positions[pair] = effective
        gross += exposure
        net += effective
        beta_exposure += beta_value
        working_pending += pending_buy + pending_sell

    soft = (
        max(ZERO, beta_exposure) >= policy.portfolio_soft_beta_exposure
        or (max(ZERO, -beta_exposure) >= policy.portfolio_soft_beta_exposure)
        or gross >= policy.portfolio_max_gross_notional
    )
    hard = (
        gross >= policy.portfolio_max_gross_notional
        or abs(beta_exposure) >= policy.portfolio_hard_beta_exposure
        or max(ZERO, beta_exposure) >= policy.portfolio_max_long_beta_exposure
        or max(ZERO, -beta_exposure) >= policy.portfolio_max_short_beta_exposure
    )

    available_new_collateral = max(
        ZERO,
        _decimal(available_collateral) * (ONE - policy.collateral_safety_buffer_pct),
    )
    allowed: dict[str, list[str]] = {}
    blocked: dict[str, list[str]] = {}
    reasons: dict[str, dict[str, str]] = {}
    reason_list: list[str] = []
    portfolio_executors = sum(working_asset_executors.values())

    for pair in pairs:
        for entry in proposed.get(pair, ()):
            level_id = entry.level_id
            position = working_positions.get(pair, ZERO)
            beta = betas[pair]
            risk_reducing = (position > ZERO and entry.side.value == "sell") or (
                position < ZERO and entry.side.value == "buy"
            )
            candidate_asset = asset_exposure.get(pair, abs(position)) + entry.quote_notional
            candidate_gross = gross + entry.quote_notional
            candidate_beta = beta_exposure + (
                entry.quote_notional * beta * (ONE if entry.side.value == "buy" else Decimal("-1"))
            )
            candidate_long = max(ZERO, candidate_beta)
            candidate_short = max(ZERO, -candidate_beta)
            candidate_pending = working_pending + entry.quote_notional
            block_reason = ""
            if not risk_reducing and candidate_asset > policy.per_asset_max_position_notional:
                block_reason = "portfolio per-asset position notional limit"
            elif not risk_reducing and candidate_gross > policy.portfolio_max_gross_notional:
                block_reason = "portfolio gross notional hard limit"
            elif not risk_reducing and candidate_long > policy.portfolio_max_long_beta_exposure:
                block_reason = "portfolio long beta hard limit"
            elif not risk_reducing and candidate_short > policy.portfolio_max_short_beta_exposure:
                block_reason = "portfolio short beta hard limit"
            elif (
                not risk_reducing
                and max(candidate_long, candidate_short)
                > policy.portfolio_soft_beta_exposure
            ):
                block_reason = "portfolio soft beta limit"
            elif (
                not risk_reducing
                and candidate_pending / policy.leverage > available_new_collateral
            ):
                block_reason = "portfolio collateral reserve limit"
            elif (
                not risk_reducing
                and working_asset_executors.get(pair, 0) >= policy.max_active_executors_per_asset
            ):
                block_reason = "portfolio per-asset executor limit"
            elif not risk_reducing and portfolio_executors >= policy.max_active_executors_portfolio:
                block_reason = "portfolio active executor limit"

            if block_reason:
                blocked.setdefault(pair, []).append(level_id)
                reasons.setdefault(pair, {})[level_id] = block_reason
                reason_list.append(f"{pair} {level_id} blocked: {block_reason}")
                continue

            allowed.setdefault(pair, []).append(level_id)
            asset_exposure[pair] = candidate_asset
            gross = candidate_gross
            net += entry.quote_notional * (ONE if entry.side.value == "buy" else Decimal("-1"))
            beta_exposure = candidate_beta
            working_pending = candidate_pending
            working_asset_executors[pair] = working_asset_executors.get(pair, 0) + 1
            portfolio_executors += 1

    if soft:
        reason_list.append("portfolio soft limit active")
    if hard:
        reason_list.append("portfolio hard limit active")

    return PortfolioRiskDecision(
        allowed_level_ids={pair: tuple(ids) for pair, ids in allowed.items()},
        blocked_level_ids={pair: tuple(ids) for pair, ids in blocked.items()},
        blocked_reasons=reasons,
        gross_notional=gross,
        net_notional=net,
        beta_exposure=beta_exposure,
        soft_limit_triggered=soft,
        hard_limit_triggered=hard,
        global_pause_new_exposure=hard,
        active_executors=portfolio_executors,
        reasons=tuple(dict.fromkeys(reason_list)),
    )


def apply_portfolio_risk(
    results: Mapping[str, ReconciliationResult],
    decision: PortfolioRiskDecision,
) -> None:
    """Remove portfolio-blocked creates and retain an auditable reason."""

    for pair, result in results.items():
        allowed = set(decision.allowed_level_ids.get(pair, ()))
        blocked_reasons = decision.blocked_reasons.get(pair, {})
        creates: list[DesiredLevel] = []
        for desired in result.creates:
            if desired.level_id in allowed:
                creates.append(desired)
                continue
            reason = blocked_reasons.get(desired.level_id, "portfolio risk limit")
            result.blocked.append(
                BlockedLevel(
                    desired.level_id,
                    reason,
                    desired.side,
                    desired.quote_amount,
                )
            )
        result.creates = creates


__all__ = [
    "PortfolioExecutionPolicy",
    "PortfolioRiskDecision",
    "apply_portfolio_risk",
    "evaluate_portfolio_risk",
]
