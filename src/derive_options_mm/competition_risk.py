"""Competition-scoped portfolio risk controls for the Stage 8 basket.

This module is an execution-side risk layer.  It deliberately does not alter
the Stage 1--4 volatility, direction, mode, or geometric-grid calculations.
It evaluates the resulting candidates against exchange minimums, account
collateral, correlated BTC-beta exposure, inventory, and the competition
drawdown ladder before a later execution adapter can create an order.

The committed profile is testnet-only and execution-disabled.  Network access
belongs to the read-only validation tool; the pure governor below has no
exchange or Hummingbot dependency and is therefore deterministic in tests and
offline replay.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .environment import environment_profile
from .multi_asset import GridPlan, pair_level_id

COMPETITION_PROFILE_NAME = "48-hour competition profile"
COMPETITION_MARKETS = ("ETH-USDC", "SOL-USDC", "HYPE-USDC")
BTC_MARKET = "BTC-USDC"
_EPSILON = 1e-12


class CompetitionRiskStage(StrEnum):
    """Latched session drawdown stages."""

    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    REDUCE = "REDUCE"
    DEFENSIVE = "DEFENSIVE"
    HARD_STOP_NEW_RISK = "HARD_STOP_NEW_RISK"


class CompetitionAssetLimit(BaseModel):
    """Soft and hard net-position notional limits for one market."""

    soft_position_notional: float = Field(gt=0)
    max_position_notional: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> CompetitionAssetLimit:
        if self.max_position_notional <= self.soft_position_notional:
            raise ValueError("asset hard position limit must exceed its soft limit")
        return self


def _default_asset_limits() -> dict[str, CompetitionAssetLimit]:
    return {
        "ETH-USDC": CompetitionAssetLimit(
            soft_position_notional=200.0, max_position_notional=280.0
        ),
        "SOL-USDC": CompetitionAssetLimit(
            soft_position_notional=200.0, max_position_notional=280.0
        ),
        "HYPE-USDC": CompetitionAssetLimit(
            soft_position_notional=160.0, max_position_notional=220.0
        ),
    }


def _default_risk_multipliers() -> dict[str, float]:
    return {
        CompetitionRiskStage.NORMAL.value: 1.0,
        CompetitionRiskStage.CAUTION.value: 0.80,
        CompetitionRiskStage.REDUCE.value: 0.60,
        CompetitionRiskStage.DEFENSIVE.value: 0.40,
        CompetitionRiskStage.HARD_STOP_NEW_RISK.value: 0.0,
    }


class CompetitionProfile(BaseModel):
    """Immutable-in-practice 800-USDC competition configuration."""

    model_config = ConfigDict(extra="forbid")

    profile_name: str = COMPETITION_PROFILE_NAME
    duration_hours: int = Field(default=48, ge=1)
    starting_equity_reference: float = Field(default=800.0, gt=0)
    collateral_reserve_pct: float = Field(default=0.20, ge=0, lt=1)

    market_environment: Literal["testnet", "mainnet"] = "testnet"
    connector_name: str = "derive_perpetual_testnet"
    allow_mainnet_trading: bool = False
    execution_enabled: bool = False
    post_only: bool = True
    leverage: float = Field(default=2.0, gt=0)

    enabled_markets: tuple[str, ...] = COMPETITION_MARKETS
    signal_markets: tuple[str, ...] = (BTC_MARKET,)
    btc_execution_enabled: bool = False
    btc_signal_enabled: bool = True

    portfolio_soft_gross_notional: float = Field(default=900.0, gt=0)
    portfolio_max_gross_notional: float = Field(default=1100.0, gt=0)
    portfolio_soft_beta_exposure: float = Field(default=600.0, gt=0)
    portfolio_hard_beta_exposure: float = Field(default=800.0, gt=0)
    portfolio_max_long_beta_exposure: float = Field(default=800.0, gt=0)
    portfolio_max_short_beta_exposure: float = Field(default=800.0, gt=0)

    asset_limits: dict[str, CompetitionAssetLimit] = Field(default_factory=_default_asset_limits)
    capital_allocation_pct: dict[str, float] = Field(
        default_factory=lambda: {"ETH-USDC": 0.35, "SOL-USDC": 0.35, "HYPE-USDC": 0.30}
    )

    target_order_notional: float = Field(default=70.0, gt=0)
    max_single_order_notional: float = Field(default=100.0, gt=0)
    max_levels_per_side_per_asset: int = Field(default=1, ge=1)
    max_active_executors_per_asset: int = Field(default=2, ge=1)
    max_active_executors_portfolio: int = Field(default=6, ge=1)
    max_new_risk_creates_per_controller_cycle: int = Field(default=2, ge=1)

    normal_buy_allocation_pct: float = Field(default=0.50, ge=0, le=1)
    long_bias_buy_allocation_pct: float = Field(default=0.60, ge=0, le=1)
    short_bias_buy_allocation_pct: float = Field(default=0.40, ge=0, le=1)
    maximum_directional_bias_pct: float = Field(default=0.65, ge=0.5, le=1)

    inventory_soft_ratio: float = Field(default=0.50, gt=0, lt=1)
    inventory_defensive_ratio: float = Field(default=0.70, gt=0, lt=1)
    inventory_hard_ratio: float = Field(default=0.90, gt=0, le=1)
    defensive_capital_multiplier: float = Field(default=0.50, gt=0, le=1)

    drawdown_caution_quote: float = Field(default=40.0, gt=0)
    drawdown_reduce_quote: float = Field(default=60.0, gt=0)
    drawdown_defensive_quote: float = Field(default=80.0, gt=0)
    competition_hard_drawdown_quote: float = Field(default=100.0, gt=0)
    risk_capacity_multipliers: dict[str, float] = Field(default_factory=_default_risk_multipliers)

    minimum_order_lifetime_seconds: float = Field(default=120.0, ge=0)
    minimum_replace_interval_seconds: float = Field(default=60.0, ge=0)
    refresh_price_tolerance_bps: float = Field(default=12.0, gt=0)
    refresh_amount_tolerance_pct: float = Field(default=0.15, ge=0)
    maximum_order_lifetime_seconds: float = Field(default=900.0, gt=0)

    @model_validator(mode="after")
    def validate_profile(self) -> CompetitionProfile:
        environment = environment_profile(self.market_environment)
        if self.connector_name != environment.connector_name:
            raise ValueError(
                f"{environment.name} profile requires {environment.connector_name}"
            )
        if self.allow_mainnet_trading:
            raise ValueError(
                "allow_mainnet_trading is not enabled by the dashboard profile; "
                "use the separate Hummingbot mainnet canary gates"
            )
        if environment.is_mainnet and self.execution_enabled:
            raise ValueError(
                "mainnet dashboard profiles are read-only; execution must remain disabled"
            )
        if not self.post_only:
            raise ValueError("competition profile requires post_only=true")
        if self.leverage > 2:
            raise ValueError("competition profile leverage must not exceed 2x")
        if self.portfolio_max_gross_notional <= self.portfolio_soft_gross_notional:
            raise ValueError("hard gross notional must exceed soft gross notional")
        if self.portfolio_hard_beta_exposure <= self.portfolio_soft_beta_exposure:
            raise ValueError("hard beta exposure must exceed soft beta exposure")
        if (
            self.portfolio_max_long_beta_exposure <= 0
            or self.portfolio_max_short_beta_exposure <= 0
        ):
            raise ValueError("portfolio beta maxima must be positive")
        if self.target_order_notional > self.max_single_order_notional:
            raise ValueError("target order notional must not exceed max single order notional")
        if self.competition_hard_drawdown_quote <= 0:
            raise ValueError("hard drawdown must be positive")
        if self.minimum_replace_interval_seconds < 0:
            raise ValueError("replacement interval must not be negative")
        if self.refresh_price_tolerance_bps <= 0:
            raise ValueError("price refresh tolerance must be positive")
        if self.maximum_order_lifetime_seconds < self.minimum_order_lifetime_seconds:
            raise ValueError("maximum order lifetime must exceed minimum lifetime")
        if not set(self.enabled_markets).issubset(set(self.asset_limits)):
            raise ValueError("enabled markets must have independent asset limits")
        if set(self.signal_markets) != {BTC_MARKET}:
            raise ValueError("BTC-USDC must remain the sole global signal market")
        if self.btc_execution_enabled:
            raise ValueError("BTC execution is signal-only in the committed profile")
        allocation_total = sum(
            self.capital_allocation_pct.get(pair, 0.0) for pair in self.enabled_markets
        )
        if not math.isclose(allocation_total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("enabled-market capital allocations must sum to 1")
        if any(value < 0 for value in self.capital_allocation_pct.values()):
            raise ValueError("capital allocations must not be negative")
        for pair, limit in self.asset_limits.items():
            if limit.max_position_notional <= 0:
                raise ValueError(f"{pair} max position notional must be positive")
        for stage in CompetitionRiskStage:
            multiplier = self.risk_capacity_multipliers.get(stage.value)
            if multiplier is None or not 0 <= multiplier <= 1:
                raise ValueError(f"missing or invalid risk multiplier for {stage.value}")
        if not (
            self.drawdown_caution_quote
            < self.drawdown_reduce_quote
            < self.drawdown_defensive_quote
            < self.competition_hard_drawdown_quote
        ):
            raise ValueError("drawdown thresholds must be strictly increasing")
        if not (
            0 <= self.normal_buy_allocation_pct <= 1
            and 0 <= self.long_bias_buy_allocation_pct <= 1
            and 0 <= self.short_bias_buy_allocation_pct <= 1
        ):
            raise ValueError("grid allocations must be percentages")
        for buy_pct in (
            self.normal_buy_allocation_pct,
            self.long_bias_buy_allocation_pct,
            self.short_bias_buy_allocation_pct,
        ):
            if abs(buy_pct - 0.5) > (self.maximum_directional_bias_pct - 0.5):
                raise ValueError("directional allocation exceeds the 65/35 competition bound")
        return self

    @property
    def collateral_reserve_quote(self) -> float:
        return self.starting_equity_reference * self.collateral_reserve_pct

    def asset_limit(self, trading_pair: str) -> CompetitionAssetLimit | None:
        return self.asset_limits.get(trading_pair)

    def execution_overrides(self) -> dict[str, Any]:
        """Return values for an execution adapter without changing Stage 4."""

        return {
            "execution_enabled": self.execution_enabled,
            "execution_max_levels_per_side": self.max_levels_per_side_per_asset,
            "max_active_executors": self.max_active_executors_per_asset,
            "max_active_grid_levels": self.max_active_executors_per_asset,
            "minimum_order_lifetime_seconds": self.minimum_order_lifetime_seconds,
            "minimum_replace_interval_seconds": self.minimum_replace_interval_seconds,
            "maximum_order_lifetime_seconds": self.maximum_order_lifetime_seconds,
            "refresh_price_tolerance_bps": self.refresh_price_tolerance_bps,
            "refresh_amount_tolerance_pct": self.refresh_amount_tolerance_pct,
            "collateral_safety_buffer_pct": self.collateral_reserve_pct,
            "leverage": self.leverage,
            "post_only": self.post_only,
        }

    def to_record(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CompetitionMarketRule(BaseModel):
    """Public Derive rule fields needed for minimum-order validation."""

    model_config = ConfigDict(frozen=True)

    trading_pair: str
    instrument_name: str
    minimum_amount: float = Field(gt=0)
    amount_step: float = Field(gt=0)
    price_increment: float = Field(gt=0)
    minimum_notional: float = Field(default=0.0, ge=0)
    reference_price: float = Field(gt=0)
    best_bid: float | None = Field(default=None, gt=0)
    best_ask: float | None = Field(default=None, gt=0)
    mark_price: float | None = Field(default=None, gt=0)
    index_price: float | None = Field(default=None, gt=0)
    observed_at: str


class CompetitionOrderSizing(BaseModel):
    """Target and exchange-minimum-aware sizing result for one market."""

    model_config = ConfigDict(frozen=True)

    trading_pair: str
    target_order_notional: float
    max_single_order_notional: float
    desired_base_amount: float
    quantized_base_amount: float
    minimum_valid_amount: float
    minimum_valid_notional: float
    actual_target_notional: float
    eligible: bool
    reason: str = ""


def _ceil_to_increment(value: float, increment: float) -> float:
    if value <= 0:
        return 0.0
    return math.ceil((value - _EPSILON) / increment) * increment


def assess_order_sizing(
    rule: CompetitionMarketRule,
    *,
    target_order_notional: float = 70.0,
    max_single_order_notional: float = 100.0,
) -> CompetitionOrderSizing:
    """Calculate a rule-valid amount without silently increasing the budget."""

    desired_amount = target_order_notional / rule.reference_price
    quantized_amount = _ceil_to_increment(desired_amount, rule.amount_step)
    minimum_from_notional = _ceil_to_increment(
        rule.minimum_notional / rule.reference_price, rule.amount_step
    )
    minimum_amount = max(rule.minimum_amount, minimum_from_notional)
    actual_amount = max(quantized_amount, minimum_amount)
    minimum_notional = rule.reference_price * minimum_amount
    actual_target_notional = rule.reference_price * actual_amount
    eligible = minimum_notional <= max_single_order_notional + _EPSILON
    reason = "" if eligible else "MIN_ORDER_EXCEEDS_BUDGET"
    return CompetitionOrderSizing(
        trading_pair=rule.trading_pair,
        target_order_notional=target_order_notional,
        max_single_order_notional=max_single_order_notional,
        desired_base_amount=desired_amount,
        quantized_base_amount=quantized_amount,
        minimum_valid_amount=minimum_amount,
        minimum_valid_notional=minimum_notional,
        actual_target_notional=actual_target_notional,
        eligible=eligible,
        reason=reason,
    )


class CompetitionCandidate(BaseModel):
    """One candidate entry presented to the deterministic risk governor."""

    model_config = ConfigDict(frozen=True)

    trading_pair: str
    level_id: str
    side: Literal["buy", "sell"]
    quote_notional: float = Field(gt=0)
    state_confidence: float = Field(default=0.0, ge=0, le=1)
    position_correction: bool = False


class CompetitionRiskState(BaseModel):
    """Session equity and drawdown state."""

    model_config = ConfigDict(frozen=True)

    session_start_equity: float
    current_equity: float
    session_pnl: float
    session_drawdown_quote: float
    session_drawdown_pct: float
    risk_stage: CompetitionRiskStage
    risk_capacity_multiplier: float
    hard_stop_latched: bool
    reasons: tuple[str, ...] = ()


class CompetitionExposure(BaseModel):
    """Current positions plus pending entry exposure."""

    model_config = ConfigDict(frozen=True)

    gross_notional: float = 0.0
    net_notional: float = 0.0
    btc_beta_equivalent_exposure: float = 0.0
    long_beta_exposure: float = 0.0
    short_beta_exposure: float = 0.0
    per_asset_exposure: dict[str, float] = Field(default_factory=dict)
    pending_buy_notional: dict[str, float] = Field(default_factory=dict)
    pending_sell_notional: dict[str, float] = Field(default_factory=dict)
    pending_order_count: int = 0
    per_asset_pending_order_count: dict[str, int] = Field(default_factory=dict)


class CompetitionRiskDecision(BaseModel):
    """Full explainable routing result for one controller cycle."""

    timestamp: str
    state: CompetitionRiskState
    exposure: CompetitionExposure
    usable_collateral: float
    collateral_reserve: float
    available_new_risk: float
    preferred_new_risk: float
    allowed_level_ids: dict[str, list[str]] = Field(default_factory=dict)
    blocked_level_ids: dict[str, list[str]] = Field(default_factory=dict)
    blocked_reasons: dict[str, str] = Field(default_factory=dict)
    blocked_reason_counts: dict[str, int] = Field(default_factory=dict)
    risk_reducing_sides: dict[str, list[str]] = Field(default_factory=dict)
    risk_increasing_creates: int = 0
    risk_create_cap_triggered: bool = False
    global_pause_new_exposure: bool = False
    reasons: list[str] = Field(default_factory=list)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _timestamp(value: Any) -> str:
    if value:
        return str(value)
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class CompetitionRiskGovernor:
    """Apply portfolio, drawdown, collateral, and deterministic create limits."""

    def __init__(self, profile: CompetitionProfile | None = None) -> None:
        self.profile = profile or CompetitionProfile()
        self._session_start_equity = self.profile.starting_equity_reference
        self._current_equity = self._session_start_equity
        self._hard_stop_latched = False

    @property
    def session_start_equity(self) -> float:
        return self._session_start_equity

    @property
    def hard_stop_latched(self) -> bool:
        return self._hard_stop_latched

    def start_session(self, current_equity: float | None = None) -> CompetitionRiskState:
        """Capture session equity once at activation/dry-run start."""

        equity = (
            self.profile.starting_equity_reference
            if current_equity is None
            else float(current_equity)
        )
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("session start equity must be positive and finite")
        self._session_start_equity = equity
        self._current_equity = equity
        self._hard_stop_latched = False
        return self.equity_state(equity)

    def reset_session(
        self, current_equity: float, *, operator_reset: bool = False
    ) -> CompetitionRiskState:
        """Reset the latched governor only through an explicit operator action."""

        if not operator_reset:
            raise PermissionError("hard-stop reset requires explicit operator_reset=True")
        return self.start_session(current_equity)

    def equity_state(
        self,
        current_equity: float | None = None,
        *,
        global_vol_risk_multiplier: float = 1.0,
    ) -> CompetitionRiskState:
        equity = self._current_equity if current_equity is None else float(current_equity)
        if not math.isfinite(equity):
            raise ValueError("current equity must be finite")
        self._current_equity = equity
        pnl = equity - self._session_start_equity
        drawdown = max(0.0, -pnl)
        if drawdown >= self.profile.competition_hard_drawdown_quote:
            self._hard_stop_latched = True
        if self._hard_stop_latched:
            stage = CompetitionRiskStage.HARD_STOP_NEW_RISK
        elif drawdown >= self.profile.drawdown_defensive_quote:
            stage = CompetitionRiskStage.DEFENSIVE
        elif drawdown >= self.profile.drawdown_reduce_quote:
            stage = CompetitionRiskStage.REDUCE
        elif drawdown >= self.profile.drawdown_caution_quote:
            stage = CompetitionRiskStage.CAUTION
        else:
            stage = CompetitionRiskStage.NORMAL
        configured = self.profile.risk_capacity_multipliers[stage.value]
        vol_multiplier = max(0.0, min(1.0, float(global_vol_risk_multiplier)))
        multiplier = min(configured, vol_multiplier)
        reasons: list[str] = []
        if stage is CompetitionRiskStage.CAUTION:
            reasons.append("COMPETITION_CAUTION")
        elif stage is CompetitionRiskStage.REDUCE:
            reasons.append("COMPETITION_REDUCE")
        elif stage is CompetitionRiskStage.DEFENSIVE:
            reasons.append("COMPETITION_DEFENSIVE")
        elif stage is CompetitionRiskStage.HARD_STOP_NEW_RISK:
            reasons.append("COMPETITION_HARD_STOP")
        if vol_multiplier < 1.0:
            reasons.append("GLOBAL_VOLATILITY_CAPACITY_REDUCTION")
        return CompetitionRiskState(
            session_start_equity=self._session_start_equity,
            current_equity=equity,
            session_pnl=pnl,
            session_drawdown_quote=drawdown,
            session_drawdown_pct=drawdown / self._session_start_equity,
            risk_stage=stage,
            risk_capacity_multiplier=multiplier,
            hard_stop_latched=self._hard_stop_latched,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _pending(
        pending_entries: Mapping[str, Any] | None,
    ) -> tuple[dict[str, dict[str, float]], dict[str, int]]:
        pending: dict[str, dict[str, float]] = {}
        counts: dict[str, int] = {}
        for pair, value in (pending_entries or {}).items():
            raw = value if isinstance(value, Mapping) else {}
            buy = max(0.0, _finite(raw.get("buy", raw.get("pending_buy_notional"))) or 0.0)
            sell = max(0.0, _finite(raw.get("sell", raw.get("pending_sell_notional"))) or 0.0)
            count = max(0, int(_finite(raw.get("count", raw.get("pending_count"))) or 0.0))
            pending[str(pair)] = {"buy": buy, "sell": sell}
            counts[str(pair)] = count
        return pending, counts

    def exposure(
        self,
        *,
        positions: Mapping[str, Any] | None = None,
        pending_entries: Mapping[str, Any] | None = None,
        betas: Mapping[str, Any] | None = None,
    ) -> CompetitionExposure:
        pending, counts = self._pending(pending_entries)
        pairs = set(str(pair) for pair in (positions or {})) | set(pending)
        per_asset: dict[str, float] = {}
        pending_buy: dict[str, float] = {}
        pending_sell: dict[str, float] = {}
        gross = net = beta_total = long_beta = short_beta = 0.0
        for pair in sorted(pairs):
            position = _finite((positions or {}).get(pair)) or 0.0
            buy = pending.get(pair, {}).get("buy", 0.0)
            sell = pending.get(pair, {}).get("sell", 0.0)
            effective = position + buy - sell
            exposure = abs(position) + buy + sell
            beta = _finite((betas or {}).get(pair))
            beta = 1.0 if beta is None else beta
            beta_value = effective * beta
            per_asset[pair] = exposure
            pending_buy[pair] = buy
            pending_sell[pair] = sell
            gross += exposure
            net += effective
            beta_total += beta_value
            long_beta += max(0.0, beta_value)
            short_beta += max(0.0, -beta_value)
        return CompetitionExposure(
            gross_notional=gross,
            net_notional=net,
            btc_beta_equivalent_exposure=beta_total,
            long_beta_exposure=long_beta,
            short_beta_exposure=short_beta,
            per_asset_exposure=per_asset,
            pending_buy_notional=pending_buy,
            pending_sell_notional=pending_sell,
            pending_order_count=sum(counts.values()),
            per_asset_pending_order_count=counts,
        )

    @staticmethod
    def _effective_position(
        pair: str,
        positions: Mapping[str, Any] | None,
        pending: Mapping[str, dict[str, float]],
    ) -> float:
        return (
            (_finite((positions or {}).get(pair)) or 0.0)
            + pending.get(pair, {}).get("buy", 0.0)
            - pending.get(pair, {}).get("sell", 0.0)
        )

    @staticmethod
    def _candidate_delta(candidate: CompetitionCandidate, beta: float) -> tuple[float, float]:
        direction = 1.0 if candidate.side == "buy" else -1.0
        return candidate.quote_notional, direction * candidate.quote_notional * beta

    def evaluate(
        self,
        *,
        timestamp: str,
        positions: Mapping[str, Any] | None = None,
        pending_entries: Mapping[str, Any] | None = None,
        proposed_entries: Mapping[str, Sequence[CompetitionCandidate | Mapping[str, Any]]]
        | None = None,
        betas: Mapping[str, Any] | None = None,
        active_executors: Mapping[str, Any] | None = None,
        inventory_ratios: Mapping[str, Any] | None = None,
        available_collateral: float | None = None,
        current_equity: float | None = None,
        global_vol_risk_multiplier: float = 1.0,
    ) -> CompetitionRiskDecision:
        """Evaluate positions, pending orders, and candidates in stable order."""

        state = self.equity_state(
            current_equity,
            global_vol_risk_multiplier=global_vol_risk_multiplier,
        )
        pending, pending_counts = self._pending(pending_entries)
        exposure = self.exposure(positions=positions, pending_entries=pending_entries, betas=betas)
        collateral = (
            self.profile.starting_equity_reference
            if available_collateral is None
            else max(0.0, float(available_collateral))
        )
        reserve = self.profile.collateral_reserve_quote
        usable_collateral = max(0.0, collateral - reserve)
        collateral_capacity = max(
            0.0, usable_collateral * self.profile.leverage - exposure.gross_notional
        )
        gross_capacity = max(
            0.0, self.profile.portfolio_max_gross_notional - exposure.gross_notional
        )
        multiplier = state.risk_capacity_multiplier
        available_new_risk = max(0.0, min(collateral_capacity, gross_capacity) * multiplier)
        preferred_new_risk = max(
            0.0,
            min(
                max(0.0, self.profile.portfolio_soft_gross_notional - exposure.gross_notional),
                collateral_capacity,
            )
            * multiplier,
        )
        current_positions = {
            str(pair): _finite(value) or 0.0 for pair, value in (positions or {}).items()
        }
        for pair in set(pending) | set(current_positions):
            current_positions[pair] = self._effective_position(pair, current_positions, pending)
        working_asset = dict(exposure.per_asset_exposure)
        working_gross = exposure.gross_notional
        working_beta = exposure.btc_beta_equivalent_exposure
        working_executors = {
            pair: int(_finite((active_executors or {}).get(pair)) or 0.0)
            + pending_counts.get(pair, 0)
            for pair in set(working_asset) | set(pending) | set(active_executors or {})
        }
        working_new_capacity = available_new_risk
        risk_creates = 0
        allowed: dict[str, list[str]] = {}
        blocked: dict[str, list[str]] = {}
        blocked_reasons: dict[str, str] = {}
        reason_counts: Counter[str] = Counter()
        risk_reducing_sides: dict[str, list[str]] = {}
        reasons: list[str] = list(state.reasons)
        normalised: list[CompetitionCandidate] = []
        for pair, entries in (proposed_entries or {}).items():
            for raw in entries:
                candidate = (
                    raw
                    if isinstance(raw, CompetitionCandidate)
                    else CompetitionCandidate.model_validate(raw)
                )
                if candidate.trading_pair != pair:
                    candidate = candidate.model_copy(update={"trading_pair": str(pair)})
                normalised.append(candidate)

        def is_risk_reducing(candidate: CompetitionCandidate) -> bool:
            if candidate.position_correction:
                return True
            position = current_positions.get(candidate.trading_pair, 0.0)
            return (position > _EPSILON and candidate.side == "sell") or (
                position < -_EPSILON and candidate.side == "buy"
            )

        def sort_key(candidate: CompetitionCandidate) -> tuple[Any, ...]:
            reducing = is_risk_reducing(candidate)
            beta = _finite((betas or {}).get(candidate.trading_pair))
            beta = 1.0 if beta is None else beta
            return (
                0 if reducing else 1,
                0 if candidate.position_correction else 1,
                -candidate.state_confidence,
                abs(candidate.quote_notional * beta),
                self.profile.enabled_markets.index(candidate.trading_pair)
                if candidate.trading_pair in self.profile.enabled_markets
                else len(self.profile.enabled_markets),
                candidate.trading_pair,
                candidate.level_id,
            )

        normalised.sort(key=sort_key)
        for candidate in normalised:
            pair = candidate.trading_pair
            reducing = is_risk_reducing(candidate)
            beta = _finite((betas or {}).get(pair))
            beta = 1.0 if beta is None else beta
            quote_delta, beta_delta = self._candidate_delta(candidate, beta)
            current_position = current_positions.get(pair, 0.0)
            if reducing:
                risk_reducing_sides.setdefault(pair, []).append(candidate.side)
            candidate_asset = working_asset.get(pair, abs(current_position)) + quote_delta
            candidate_gross = working_gross + quote_delta
            candidate_beta = working_beta + beta_delta
            candidate_long = max(0.0, candidate_beta)
            candidate_short = max(0.0, -candidate_beta)
            inventory_ratio = _finite((inventory_ratios or {}).get(pair))
            if inventory_ratio is None:
                limit = self.profile.asset_limit(pair)
                max_position = limit.max_position_notional if limit else None
                inventory_ratio = (
                    current_position / max_position if max_position and max_position > 0 else 0.0
                )
            worsening_inventory = abs(inventory_ratio) >= self.profile.inventory_hard_ratio and (
                (inventory_ratio > 0 and candidate.side == "buy")
                or (inventory_ratio < 0 and candidate.side == "sell")
            )
            if abs(inventory_ratio) >= self.profile.inventory_defensive_ratio:
                reasons.append(f"{pair}:ASSET_DEFENSIVE_INVENTORY")
            elif abs(inventory_ratio) >= self.profile.inventory_soft_ratio:
                reasons.append(f"{pair}:ASSET_SOFT_INVENTORY")

            block_reason = ""
            if not reducing and pair == BTC_MARKET and not self.profile.btc_execution_enabled:
                block_reason = "BTC_SIGNAL_ONLY"
            elif not reducing and state.hard_stop_latched:
                block_reason = "COMPETITION_HARD_STOP"
            elif (
                not reducing
                and risk_creates >= self.profile.max_new_risk_creates_per_controller_cycle
            ):
                block_reason = "MAX_NEW_RISK_CREATES"
            elif not reducing and candidate.quote_notional > self.profile.max_single_order_notional:
                block_reason = "MAX_SINGLE_ORDER_BUDGET"
            elif not reducing and worsening_inventory:
                block_reason = "ASSET_HARD_INVENTORY"
            else:
                limit = self.profile.asset_limit(pair)
                if not reducing and limit and candidate_asset > limit.max_position_notional:
                    block_reason = "ASSET_HARD_INVENTORY"
                elif (
                    not reducing
                    and working_executors.get(pair, 0)
                    >= self.profile.max_active_executors_per_asset
                ):
                    block_reason = "MAX_ACTIVE_EXECUTORS_ASSET"
                elif (
                    not reducing
                    and sum(working_executors.values())
                    >= self.profile.max_active_executors_portfolio
                ):
                    block_reason = "MAX_ACTIVE_EXECUTORS_PORTFOLIO"
                elif not reducing and candidate_gross > self.profile.portfolio_max_gross_notional:
                    block_reason = "GROSS_NOTIONAL_LIMIT"
                elif (
                    not reducing and candidate_long > self.profile.portfolio_max_long_beta_exposure
                ):
                    block_reason = "PORTFOLIO_HARD_BETA_LONG"
                elif (
                    not reducing
                    and candidate_short > self.profile.portfolio_max_short_beta_exposure
                ):
                    block_reason = "PORTFOLIO_HARD_BETA_SHORT"
                elif (
                    not reducing
                    and beta_delta > 0
                    and candidate_long >= self.profile.portfolio_hard_beta_exposure
                ):
                    block_reason = "PORTFOLIO_HARD_BETA_LONG"
                elif (
                    not reducing
                    and beta_delta < 0
                    and candidate_short >= self.profile.portfolio_hard_beta_exposure
                ):
                    block_reason = "PORTFOLIO_HARD_BETA_SHORT"
                elif (
                    not reducing
                    and beta_delta > 0
                    and candidate_long >= self.profile.portfolio_soft_beta_exposure
                ):
                    block_reason = "PORTFOLIO_SOFT_BETA_LONG"
                elif (
                    not reducing
                    and beta_delta < 0
                    and candidate_short >= self.profile.portfolio_soft_beta_exposure
                ):
                    block_reason = "PORTFOLIO_SOFT_BETA_SHORT"
                elif not reducing and candidate_gross > self.profile.portfolio_soft_gross_notional:
                    reasons.append(f"{pair}:PORTFOLIO_SOFT_GROSS")
                elif not reducing and candidate.quote_notional > working_new_capacity + _EPSILON:
                    block_reason = "COLLATERAL_RESERVE"
            if block_reason:
                blocked.setdefault(pair, []).append(candidate.level_id)
                blocked_reasons[candidate.level_id] = block_reason
                reason_counts[block_reason] += 1
                reasons.append(f"{pair}:{candidate.side}:{candidate.level_id}:{block_reason}")
                continue
            allowed.setdefault(pair, []).append(candidate.level_id)
            # Risk-reducing exits are allowed through hard limits and do not
            # consume the new-risk capacity or create budget.  This also
            # prevents a same-cycle exit from making a replacement entry look
            # like doubled exposure.
            if not reducing:
                working_asset[pair] = candidate_asset
                working_gross = candidate_gross
                working_beta = candidate_beta
                working_executors[pair] = working_executors.get(pair, 0) + 1
                risk_creates += 1
                working_new_capacity = max(0.0, working_new_capacity - candidate.quote_notional)

        global_pause = state.hard_stop_latched or state.risk_capacity_multiplier <= 0
        if global_pause:
            reasons.append("COMPETITION_HARD_STOP")
        return CompetitionRiskDecision(
            timestamp=_timestamp(timestamp),
            state=state,
            exposure=exposure,
            usable_collateral=usable_collateral,
            collateral_reserve=reserve,
            available_new_risk=available_new_risk,
            preferred_new_risk=preferred_new_risk,
            allowed_level_ids=allowed,
            blocked_level_ids=blocked,
            blocked_reasons=blocked_reasons,
            blocked_reason_counts=dict(reason_counts),
            risk_reducing_sides={
                pair: sorted(set(sides)) for pair, sides in risk_reducing_sides.items()
            },
            risk_increasing_creates=risk_creates,
            risk_create_cap_triggered=reason_counts.get("MAX_NEW_RISK_CREATES", 0) > 0,
            global_pause_new_exposure=global_pause,
            reasons=list(dict.fromkeys(reasons)),
        )

    def route_plans(
        self,
        plans: Mapping[str, GridPlan],
        *,
        positions: Mapping[str, Any] | None = None,
        pending_entries: Mapping[str, Any] | None = None,
        betas: Mapping[str, Any] | None = None,
        active_executors: Mapping[str, Any] | None = None,
        inventory_ratios: Mapping[str, Any] | None = None,
        available_collateral: float | None = None,
        current_equity: float | None = None,
        global_vol_risk_multiplier: float = 1.0,
        quote_scale: float = 1.0,
    ) -> tuple[CompetitionRiskDecision, dict[str, tuple[str, ...]]]:
        """Route first-level plans without recalculating Stage 4 geometry."""

        if quote_scale <= 0:
            raise ValueError("quote_scale must be positive")
        proposals: dict[str, list[CompetitionCandidate]] = {}
        for pair, plan in plans.items():
            candidates: list[CompetitionCandidate] = []
            for side, levels in (("buy", plan.buy_levels), ("sell", plan.sell_levels)):
                for level in list(levels)[: self.profile.max_levels_per_side_per_asset]:
                    candidates.append(
                        CompetitionCandidate(
                            trading_pair=pair,
                            level_id=pair_level_id(pair, side, level.level_index),
                            side=side,
                            quote_notional=float(level.quote_amount) * quote_scale,
                        )
                    )
            proposals[pair] = candidates
        timestamp = next((plan.timestamp for plan in plans.values()), "")
        decision = self.evaluate(
            timestamp=timestamp,
            positions=positions,
            pending_entries=pending_entries,
            proposed_entries=proposals,
            betas=betas,
            active_executors=active_executors,
            inventory_ratios=inventory_ratios,
            available_collateral=available_collateral,
            current_equity=current_equity,
            global_vol_risk_multiplier=global_vol_risk_multiplier,
        )
        routes = {pair: tuple(decision.allowed_level_ids.get(pair, ())) for pair in proposals}
        return decision, routes


def load_competition_profile(path: str | Path) -> CompetitionProfile:
    """Load the committed YAML profile without permitting silent defaults."""

    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(config_path)
    raw_text = config_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - exercised only in minimal installs
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as json_exc:
            raise RuntimeError(
                "PyYAML is required to load the competition YAML profile"
            ) from json_exc
    else:
        raw = yaml.safe_load(raw_text)
    if not isinstance(raw, Mapping):
        raise ValueError("competition profile must contain a mapping")
    return CompetitionProfile.model_validate(raw)


def default_competition_profile() -> CompetitionProfile:
    """Return the validated in-code equivalent of the committed profile."""

    return CompetitionProfile()


__all__ = [
    "BTC_MARKET",
    "COMPETITION_MARKETS",
    "COMPETITION_PROFILE_NAME",
    "CompetitionAssetLimit",
    "CompetitionCandidate",
    "CompetitionExposure",
    "CompetitionMarketRule",
    "CompetitionOrderSizing",
    "CompetitionProfile",
    "CompetitionRiskDecision",
    "CompetitionRiskGovernor",
    "CompetitionRiskStage",
    "CompetitionRiskState",
    "assess_order_sizing",
    "default_competition_profile",
    "load_competition_profile",
]
