"""Pure Stage 5 execution planning for the Derive adaptive grid.

The Hummingbot controller is an adapter around this module.  Keeping the
planning, quantization checks, risk gates, and reconciliation deterministic
makes the execution boundary testable without importing Hummingbot and keeps
Stage 4 as the only producer of strategy levels.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from derive_options_mm.environment import environment_profile

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


class ExecutionSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class PlanLevel:
    """One level read from a serialized Stage 4 GridPlan."""

    side: ExecutionSide
    level_index: int
    theoretical_price: Decimal
    quote_amount: Decimal

    @property
    def level_id(self) -> str:
        return f"{self.side.value}_{self.level_index}"

    def scoped_level_id(self, trading_pair: str) -> str:
        """Return a stable key that cannot collide across markets."""

        return f"{trading_pair}::{self.level_id}"


@dataclass(frozen=True)
class GridPlanView:
    """Execution-relevant view of a Stage 4 record.

    The parser intentionally copies values from the record; it never fills in
    missing strategy values or recalculates prices, widths, or allocations.
    """

    timestamp: str
    trading_pair: str
    mode: str
    enabled: bool
    valid: bool
    plan_version: int
    plan_change_significant: bool
    center_price: Decimal | None
    total_grid_width_pct: Decimal
    buy_levels: tuple[PlanLevel, ...]
    sell_levels: tuple[PlanLevel, ...]

    @property
    def levels(self) -> tuple[PlanLevel, ...]:
        return self.buy_levels + self.sell_levels

    @property
    def scoped_level_ids(self) -> tuple[str, ...]:
        """Return pair-qualified level IDs for portfolio-aware routing."""

        return tuple(level.scoped_level_id(self.trading_pair) for level in self.levels)


@dataclass(frozen=True)
class TradingRuleView:
    """The rule fields required by Stage 5.

    Values are copied from Hummingbot's ``TradingRule`` at runtime.  The
    controller obtains them through ``MarketDataProvider`` and its official
    quantization methods; this type exists only for the pure decision layer.
    """

    min_order_size: Decimal = ZERO
    min_notional_size: Decimal = ZERO
    min_price_increment: Decimal = Decimal("0")
    min_base_amount_increment: Decimal = Decimal("0")


@dataclass(frozen=True)
class ActiveLevel:
    """Snapshot of one active Hummingbot executor."""

    executor_id: str
    level_id: str
    side: ExecutionSide
    price: Decimal
    amount: Decimal
    quote_notional: Decimal
    created_at: float
    is_filled: bool
    is_active: bool = True
    plan_mode: str | None = None
    last_replace_at: float | None = None


@dataclass(frozen=True)
class RuntimeHealth:
    """Fail-closed account, connector, and market-data state."""

    testnet_verified: bool
    connector_ready: bool
    market_data_ready: bool
    trading_rules_available: bool
    balance_verified: bool
    position_verified: bool
    best_bid: Decimal | None
    best_ask: Decimal | None
    position_notional: Decimal = ZERO
    available_collateral: Decimal = ZERO
    trading_rules: TradingRuleView | None = None
    reason: str = ""
    environment: str = "testnet"
    environment_verified: bool = False
    environment_consistent: bool = True
    mainnet_canary_authorized: bool = False

    @property
    def ready_for_new_entries(self) -> bool:
        environment = environment_profile(self.environment)
        environment_ready = (
            self.testnet_verified
            if not environment.is_mainnet
            else self.environment_verified
            and self.environment_consistent
            and self.mainnet_canary_authorized
        )
        return all(
            (
                environment_ready,
                self.connector_ready,
                self.market_data_ready,
                self.trading_rules_available,
                self.balance_verified,
                self.position_verified,
                self.best_bid is not None,
                self.best_ask is not None,
                self.best_bid > ZERO if self.best_bid is not None else False,
                self.best_ask > ZERO if self.best_ask is not None else False,
                self.best_bid < self.best_ask
                if self.best_bid is not None and self.best_ask is not None
                else False,
            )
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    """Execution-only controls; Stage 4 strategy parameters stay upstream."""

    execution_max_levels_per_side: int = 1
    testnet_order_scale: Decimal | None = Decimal("0.05")
    max_total_position_notional: Decimal | None = Decimal("1000")
    max_side_position_notional: Decimal | None = Decimal("1000")
    max_active_grid_levels: int = 2
    max_active_executors: int = 2
    minimum_order_lifetime_seconds: float = 60.0
    minimum_replace_interval_seconds: float = 30.0
    maximum_order_lifetime_seconds: float = 600.0
    refresh_price_tolerance_bps: Decimal = Decimal("15")
    refresh_amount_tolerance_pct: Decimal = Decimal("0.15")
    collateral_safety_buffer_pct: Decimal = Decimal("0.10")
    leverage: Decimal = Decimal("1")
    stale_plan_timeout_seconds: float = 30.0
    cancel_orders_on_pause: bool = True
    manual_kill_switch: bool = False
    emergency_close_positions_on_pause: bool = False
    post_only: bool = True
    take_profit_mode: str = "adjacent_grid"
    take_profit_pct: Decimal = Decimal("0.001")
    take_profit_step_multiplier: Decimal = ONE
    stop_loss_pct: Decimal | None = None
    time_limit_seconds: int | None = None
    forced_pause_reason: str = ""
    environment: str = "testnet"
    mainnet_canary_authorized: bool = False
    mainnet_canary_max_order_notional: Decimal | None = None
    mainnet_canary_max_loss_quote: Decimal | None = None

    def __post_init__(self) -> None:
        if self.execution_max_levels_per_side < 1:
            raise ValueError("execution_max_levels_per_side must be positive")
        environment = environment_profile(self.environment)
        if environment.is_mainnet:
            for name, value in (
                ("mainnet order scale", self.testnet_order_scale),
                ("max_total_position_notional", self.max_total_position_notional),
                ("max_side_position_notional", self.max_side_position_notional),
                ("mainnet_canary_max_order_notional", self.mainnet_canary_max_order_notional),
                ("mainnet_canary_max_loss_quote", self.mainnet_canary_max_loss_quote),
            ):
                if value is not None and value <= ZERO:
                    raise ValueError(f"{name} must be positive when configured")
        else:
            if self.testnet_order_scale is None or self.testnet_order_scale <= ZERO:
                raise ValueError("testnet_order_scale must be positive")
            if self.max_total_position_notional is None or self.max_total_position_notional <= ZERO:
                raise ValueError("max_total_position_notional must be positive")
            if self.max_side_position_notional is None or self.max_side_position_notional <= ZERO:
                raise ValueError("max_side_position_notional must be positive")
        if self.max_active_grid_levels < 1 or self.max_active_executors < 1:
            raise ValueError("active executor limits must be positive")
        if self.leverage <= ZERO:
            raise ValueError("leverage must be positive")
        if not ZERO <= self.collateral_safety_buffer_pct < ONE:
            raise ValueError("collateral_safety_buffer_pct must be in [0, 1)")
        if self.refresh_price_tolerance_bps < ZERO:
            raise ValueError("refresh_price_tolerance_bps must be non-negative")
        if self.refresh_amount_tolerance_pct < ZERO:
            raise ValueError("refresh_amount_tolerance_pct must be non-negative")
        if self.minimum_order_lifetime_seconds < 0:
            raise ValueError("minimum_order_lifetime_seconds must be non-negative")
        if self.minimum_replace_interval_seconds < 0:
            raise ValueError("minimum_replace_interval_seconds must be non-negative")
        if self.maximum_order_lifetime_seconds < self.minimum_order_lifetime_seconds:
            raise ValueError("maximum_order_lifetime_seconds must not be below minimum lifetime")
        if self.stale_plan_timeout_seconds <= 0:
            raise ValueError("stale_plan_timeout_seconds must be positive")
        if self.take_profit_mode not in {"adjacent_grid", "fixed"}:
            raise ValueError("take_profit_mode must be adjacent_grid or fixed")
        if self.take_profit_pct < ZERO or self.take_profit_step_multiplier <= ZERO:
            raise ValueError("take-profit parameters must be non-negative and multiplier positive")


@dataclass(frozen=True)
class DesiredLevel:
    """A validated, quantized entry plus its executor exit distance."""

    level_id: str
    side: ExecutionSide
    level_index: int
    theoretical_price: Decimal
    price: Decimal
    amount: Decimal
    quote_amount: Decimal
    quote_notional: Decimal
    take_profit_pct: Decimal
    maker_price_adjusted: bool
    plan_version: int
    mode: str


@dataclass(frozen=True)
class StopIntent:
    executor_id: str
    level_id: str
    reason: str
    keep_position: bool = False
    reason_code: str = ""


@dataclass(frozen=True)
class BlockedLevel:
    level_id: str
    reason: str
    side: ExecutionSide | None = None
    quote_amount: Decimal = ZERO


@dataclass
class ReconciliationResult:
    """Level-by-level desired-versus-active result."""

    creates: list[DesiredLevel] = field(default_factory=list)
    stops: list[StopIntent] = field(default_factory=list)
    keeps: list[str] = field(default_factory=list)
    blocked: list[BlockedLevel] = field(default_factory=list)
    pause_reason: str = ""
    plan_age_seconds: float | None = None
    deferred_create_count: int = 0
    pending_buy_notional: Decimal = ZERO
    pending_sell_notional: Decimal = ZERO
    potential_long_exposure: Decimal = ZERO
    potential_short_exposure: Decimal = ZERO
    testnet_verified: bool = False
    keep_reasons: dict[str, str] = field(default_factory=dict)
    replacement_reason_counts: dict[str, int] = field(default_factory=dict)

    @property
    def paused(self) -> bool:
        return bool(self.pause_reason)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _required_decimal(record: Mapping[str, Any], key: str) -> Decimal:
    value = _decimal(record.get(key))
    if value is None:
        raise ValueError(f"GridPlan field {key!r} is missing or non-numeric")
    return value


def _parse_levels(raw: Any, side: ExecutionSide) -> tuple[PlanLevel, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError(f"GridPlan {side.value}_levels must be a list")
    levels: list[PlanLevel] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"GridPlan {side.value}_levels contains a non-object")
        level_side = str(item.get("side", side.value)).lower()
        if level_side != side.value:
            raise ValueError(f"GridPlan level side mismatch for {side.value}")
        try:
            level_index = int(item["level_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"GridPlan {side.value} level index is invalid") from exc
        theoretical_price = _required_decimal(item, "theoretical_price")
        quote_amount = _required_decimal(item, "quote_amount")
        if level_index < 0 or theoretical_price <= ZERO or quote_amount < ZERO:
            raise ValueError(f"GridPlan {side.value}_{level_index} has invalid values")
        levels.append(PlanLevel(side, level_index, theoretical_price, quote_amount))
    levels.sort(key=lambda level: level.level_index)
    ids = [level.level_id for level in levels]
    if len(ids) != len(set(ids)):
        raise ValueError(f"GridPlan has duplicate {side.value} level IDs")
    return tuple(levels)


def parse_grid_plan(record: Mapping[str, Any], expected_pair: str = "BTC-USDC") -> GridPlanView:
    """Parse only the execution fields from one Stage 4 JSON record."""

    if not isinstance(record, Mapping):
        raise ValueError("GridPlan record must be an object")
    trading_pair = str(record.get("trading_pair", ""))
    if trading_pair != expected_pair:
        raise ValueError(f"GridPlan pair {trading_pair!r} does not match {expected_pair!r}")
    timestamp = str(record.get("timestamp", ""))
    if not timestamp:
        raise ValueError("GridPlan timestamp is missing")
    try:
        plan_version = int(record.get("plan_version", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("GridPlan plan_version is invalid") from exc
    if plan_version < 0:
        raise ValueError("GridPlan plan_version must be non-negative")
    center_price = _decimal(record.get("center_price"))
    return GridPlanView(
        timestamp=timestamp,
        trading_pair=trading_pair,
        mode=str(record.get("mode", "")).lower(),
        enabled=bool(record.get("enabled", False)),
        valid=bool(record.get("valid", False)),
        plan_version=plan_version,
        plan_change_significant=bool(record.get("plan_change_significant", False)),
        center_price=center_price,
        total_grid_width_pct=_decimal(record.get("total_grid_width_pct")) or ZERO,
        buy_levels=_parse_levels(record.get("buy_levels"), ExecutionSide.BUY),
        sell_levels=_parse_levels(record.get("sell_levels"), ExecutionSide.SELL),
    )


def timestamp_age_seconds(timestamp: str, now_epoch: float) -> float | None:
    """Return receipt-independent age from the GridPlan event timestamp."""

    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, float(now_epoch - parsed.timestamp()))
    except (TypeError, ValueError, OverflowError):
        return None


def _take_profit_pct(
    level: PlanLevel,
    levels: Sequence[PlanLevel],
    center_price: Decimal | None,
    policy: ExecutionPolicy,
    entry_price: Decimal,
) -> Decimal:
    if policy.take_profit_mode == "fixed":
        return policy.take_profit_pct
    target_price: Decimal | None = None
    if level.level_index > 0:
        previous = next(
            (candidate for candidate in levels if candidate.level_index == level.level_index - 1),
            None,
        )
        if previous is not None:
            target_price = previous.theoretical_price
    if target_price is None:
        target_price = center_price
    if target_price is None or target_price <= ZERO or entry_price <= ZERO:
        return policy.take_profit_pct
    if level.side is ExecutionSide.BUY:
        distance = (target_price - entry_price) / entry_price
    else:
        distance = (entry_price - target_price) / entry_price
    if distance <= ZERO:
        return policy.take_profit_pct
    return distance * policy.take_profit_step_multiplier


def quantize_level(
    level: PlanLevel,
    *,
    plan: GridPlanView,
    policy: ExecutionPolicy,
    rules: TradingRuleView,
    best_bid: Decimal,
    best_ask: Decimal,
    quantize_price: Callable[[Decimal], Decimal],
    quantize_amount: Callable[[Decimal], Decimal],
) -> tuple[DesiredLevel | None, str | None]:
    """Quantize and validate one theoretical level using official callbacks."""

    if not policy.post_only:
        return None, "post-only execution is required"
    if policy.testnet_order_scale is None:
        return None, "mainnet canary order scale is not configured"
    scaled_quote = level.quote_amount * policy.testnet_order_scale
    if scaled_quote <= ZERO:
        return None, "scaled quote amount is zero"
    price = _decimal(quantize_price(level.theoretical_price))
    if price is None or price <= ZERO:
        return None, "quantized price is invalid"

    adjusted = False
    tick = max(ZERO, rules.min_price_increment)
    if level.side is ExecutionSide.BUY and price >= best_ask:
        adjusted = True
        candidate = _decimal(quantize_price(best_ask - tick)) if tick > ZERO else None
        if candidate is None or candidate <= ZERO or candidate >= best_ask:
            candidate = _decimal(quantize_price(best_bid))
        price = candidate or ZERO
    elif level.side is ExecutionSide.SELL and price <= best_bid:
        adjusted = True
        candidate = _decimal(quantize_price(best_bid + tick)) if tick > ZERO else None
        if candidate is None or candidate <= best_bid:
            candidate = _decimal(quantize_price(best_ask))
        price = candidate or ZERO

    if price <= ZERO:
        return None, "maker-safe price is invalid"
    if level.side is ExecutionSide.BUY and price >= best_ask:
        return None, "buy price crosses executable ask after quantization"
    if level.side is ExecutionSide.SELL and price <= best_bid:
        return None, "sell price crosses executable bid after quantization"

    amount = _decimal(quantize_amount(scaled_quote / price))
    if amount is None or amount <= ZERO:
        return None, "quantized amount is zero"
    quote_notional = price * amount
    if amount < max(ZERO, rules.min_order_size):
        return None, "amount below exchange minimum"
    if quote_notional < max(ZERO, rules.min_notional_size):
        return None, "notional below exchange minimum"
    return (
        DesiredLevel(
            level_id=level.level_id,
            side=level.side,
            level_index=level.level_index,
            theoretical_price=level.theoretical_price,
            price=price,
            amount=amount,
            quote_amount=scaled_quote,
            quote_notional=quote_notional,
            take_profit_pct=_take_profit_pct(
                level,
                plan.buy_levels if level.side is ExecutionSide.BUY else plan.sell_levels,
                plan.center_price,
                policy,
                price,
            ),
            maker_price_adjusted=adjusted,
            plan_version=plan.plan_version,
            mode=plan.mode,
        ),
        None,
    )


def _deviation_bps(current: Decimal, desired: Decimal) -> Decimal:
    if current <= ZERO or desired <= ZERO:
        return Decimal("Infinity")
    return abs(current - desired) / current * BPS


def _amount_deviation(current: Decimal, desired: Decimal) -> Decimal:
    if current <= ZERO or desired <= ZERO:
        return Decimal("Infinity")
    return abs(current - desired) / desired


def _pause_reason(
    plan: GridPlanView | None,
    health: RuntimeHealth,
    policy: ExecutionPolicy,
    now_epoch: float,
) -> tuple[str, float | None]:
    if policy.manual_kill_switch:
        return "manual_kill_switch", None
    if plan is None:
        return "GridPlan missing", None
    age = timestamp_age_seconds(plan.timestamp, now_epoch)
    if age is None:
        return "GridPlan timestamp invalid", None
    if age > policy.stale_plan_timeout_seconds:
        return "GridPlan stale — new entry creation blocked", age
    if not plan.valid:
        return "GridPlan invalid", age
    if not plan.enabled or plan.mode == "pause":
        return "GridPlan PAUSE", age
    if policy.forced_pause_reason:
        return policy.forced_pause_reason, age
    if environment_profile(policy.environment).is_mainnet:
        if policy.mainnet_canary_max_loss_quote is None:
            return "mainnet canary loss budget is not configured", age
        if policy.stop_loss_pct is None or policy.stop_loss_pct <= ZERO:
            return "mainnet canary loss control is not configured", age
        if (
            policy.max_total_position_notional is None
            or policy.max_total_position_notional * policy.stop_loss_pct
            > policy.mainnet_canary_max_loss_quote
        ):
            return "mainnet canary loss budget is below configured exposure", age
    if not health.ready_for_new_entries:
        return health.reason or "connector/account health unavailable", age
    return "", age


def reconcile_grid_plan(
    plan: GridPlanView | None,
    *,
    active: Sequence[ActiveLevel],
    health: RuntimeHealth,
    policy: ExecutionPolicy,
    now_epoch: float,
    quantize_price: Callable[[Decimal], Decimal] | None = None,
    quantize_amount: Callable[[Decimal], Decimal] | None = None,
) -> ReconciliationResult:
    """Return deterministic stop/keep/create intents for one controller tick."""

    result = ReconciliationResult(testnet_verified=health.testnet_verified)
    reason, age = _pause_reason(plan, health, policy, now_epoch)
    result.pause_reason = reason
    result.plan_age_seconds = age
    result.pending_buy_notional = sum(
        (
            item.quote_notional
            for item in active
            if item.is_active and not item.is_filled and item.side is ExecutionSide.BUY
        ),
        ZERO,
    )
    result.pending_sell_notional = sum(
        (
            item.quote_notional
            for item in active
            if item.is_active and not item.is_filled and item.side is ExecutionSide.SELL
        ),
        ZERO,
    )
    current_long = max(ZERO, health.position_notional)
    current_short = max(ZERO, -health.position_notional)
    result.potential_long_exposure = current_long + result.pending_buy_notional
    result.potential_short_exposure = current_short + result.pending_sell_notional

    unfilled_by_level: dict[str, ActiveLevel] = {}
    for item in active:
        if not item.is_active:
            continue
        if item.is_filled:
            result.keeps.append(item.level_id)
            continue
        previous = unfilled_by_level.get(item.level_id)
        if previous is None:
            unfilled_by_level[item.level_id] = item
        else:
            result.stops.append(
                StopIntent(
                    item.executor_id,
                    item.level_id,
                    "duplicate active level",
                    keep_position=False,
                )
            )

    def stop_unfilled(
        item: ActiveLevel,
        stop_reason: str,
        *,
        reason_code: str = "",
    ) -> None:
        if not any(stop.executor_id == item.executor_id for stop in result.stops):
            result.stops.append(
                StopIntent(
                    item.executor_id,
                    item.level_id,
                    stop_reason,
                    False,
                    reason_code,
                )
            )

    if reason:
        if policy.cancel_orders_on_pause or policy.manual_kill_switch:
            for item in unfilled_by_level.values():
                stop_unfilled(item, reason)
        return result

    if (
        plan is None
        or health.trading_rules is None
        or quantize_price is None
        or quantize_amount is None
    ):
        result.pause_reason = "execution inputs unavailable"
        return result

    desired_levels: list[DesiredLevel] = []
    for side, levels in (
        (ExecutionSide.BUY, plan.buy_levels[: policy.execution_max_levels_per_side]),
        (ExecutionSide.SELL, plan.sell_levels[: policy.execution_max_levels_per_side]),
    ):
        for level in levels:
            desired, blocked_reason = quantize_level(
                level,
                plan=plan,
                policy=policy,
                rules=health.trading_rules,
                best_bid=health.best_bid or ZERO,
                best_ask=health.best_ask or ZERO,
                quantize_price=quantize_price,
                quantize_amount=quantize_amount,
            )
            if desired is None:
                result.blocked.append(
                    BlockedLevel(
                        level.level_id,
                        blocked_reason or "level rejected",
                        side,
                        level.quote_amount,
                    )
                )
            else:
                desired_levels.append(desired)

    desired_by_level = {item.level_id: item for item in desired_levels}
    for item in unfilled_by_level.values():
        desired = desired_by_level.get(item.level_id)
        if desired is None:
            stop_unfilled(item, "level no longer desired")
            continue
        age_seconds = max(0.0, now_epoch - item.created_at)
        crossing = (
            item.side is ExecutionSide.BUY
            and health.best_ask is not None
            and item.price >= health.best_ask
        ) or (
            item.side is ExecutionSide.SELL
            and health.best_bid is not None
            and item.price <= health.best_bid
        )
        mode_changed = item.plan_mode is not None and item.plan_mode != plan.mode
        price_deviation_bps = _deviation_bps(item.price, desired.price)
        amount_deviation_pct = _amount_deviation(item.quote_notional, desired.quote_notional)
        # Stage 4 can emit records without a meaningful plan movement.  Do
        # not surrender maker queue position for those records; significant
        # plan changes still reprice when the actual order is materially off.
        plan_change_requires_refresh = plan.plan_change_significant and (
            price_deviation_bps >= policy.refresh_price_tolerance_bps
            or amount_deviation_pct >= policy.refresh_amount_tolerance_pct
        )
        needs_refresh = (
            crossing
            or age_seconds >= policy.maximum_order_lifetime_seconds
            or plan_change_requires_refresh
        )
        replace_anchor = (
            item.last_replace_at if item.last_replace_at is not None else item.created_at
        )
        replacement_elapsed = max(0.0, now_epoch - replace_anchor)
        if not needs_refresh:
            result.keeps.append(item.level_id)
            result.keep_reasons[item.level_id] = "PRICE_WITHIN_DEADBAND_KEEP"
        elif crossing:
            # Marketability is a hard maker-safety exception; it is never
            # delayed by an order-age or replacement cooldown.
            stop_unfilled(item, "stale or materially changed level", reason_code="MAKER_SAFETY")
            result.replacement_reason_counts["MAKER_SAFETY"] = (
                result.replacement_reason_counts.get("MAKER_SAFETY", 0) + 1
            )
        elif age_seconds < policy.minimum_order_lifetime_seconds:
            result.keeps.append(item.level_id)
            result.keep_reasons[item.level_id] = "MINIMUM_ORDER_LIFETIME"
        elif replacement_elapsed < policy.minimum_replace_interval_seconds:
            result.keeps.append(item.level_id)
            result.keep_reasons[item.level_id] = "REPLACEMENT_COOLDOWN"
        else:
            reason_code = (
                "MAXIMUM_ORDER_AGE"
                if age_seconds >= policy.maximum_order_lifetime_seconds
                else "MATERIAL_PLAN_CHANGE"
            )
            stop_unfilled(item, "stale or materially changed level", reason_code=reason_code)
            result.replacement_reason_counts[reason_code] = (
                result.replacement_reason_counts.get(reason_code, 0) + 1
            )

        # The mode change is intentionally observed for diagnostics only.  A
        # NORMAL/BIAS/DEFENSIVE label change does not itself cancel an order
        # when its price and amount remain inside the execution deadband.
        if mode_changed and item.level_id in result.keeps:
            result.keep_reasons[item.level_id] = "MODE_CHANGE_WITHIN_DEADBAND_KEEP"

    # Preserve action ordering: stop stale/obsolete entries first, then create
    # on a later controller tick so a replacement cannot briefly double risk.
    if result.stops:
        result.deferred_create_count = len(
            [level for level in desired_levels if level.level_id not in result.keeps]
        )
        return result

    active_count = sum(1 for item in active if item.is_active)
    active_level_count = len({item.level_id for item in active if item.is_active})
    slots = max(
        0,
        min(
            policy.max_active_executors - active_count,
            policy.max_active_grid_levels - active_level_count,
        ),
    )
    pending_buy = result.pending_buy_notional
    pending_sell = result.pending_sell_notional
    available_collateral = health.available_collateral * (ONE - policy.collateral_safety_buffer_pct)
    for desired in desired_levels:
        if desired.level_id in unfilled_by_level or desired.level_id in result.keeps:
            continue
        if slots <= 0:
            result.blocked.append(
                BlockedLevel(
                    desired.level_id,
                    "maximum active executor/grid-level cap",
                    desired.side,
                    desired.quote_amount,
                )
            )
            continue
        if desired.side is ExecutionSide.BUY:
            side_exposure = current_long + pending_buy + desired.quote_notional
        else:
            side_exposure = current_short + pending_sell + desired.quote_notional
        total_exposure = (
            current_long + current_short + pending_buy + pending_sell + desired.quote_notional
        )
        if (
            policy.max_side_position_notional is None
            or side_exposure > policy.max_side_position_notional
        ):
            result.blocked.append(
                BlockedLevel(
                    desired.level_id,
                    "would exceed side position notional limit",
                    desired.side,
                    desired.quote_amount,
                )
            )
            continue
        if (
            policy.max_total_position_notional is None
            or total_exposure > policy.max_total_position_notional
        ):
            result.blocked.append(
                BlockedLevel(
                    desired.level_id,
                    "would exceed total position notional limit",
                    desired.side,
                    desired.quote_amount,
                )
            )
            continue
        pending_after = pending_buy + pending_sell + desired.quote_notional
        if available_collateral <= ZERO or pending_after / policy.leverage > available_collateral:
            result.blocked.append(
                BlockedLevel(
                    desired.level_id,
                    "insufficient collateral after safety buffer",
                    desired.side,
                    desired.quote_amount,
                )
            )
            continue
        if environment_profile(policy.environment).is_mainnet and (
            policy.mainnet_canary_max_order_notional is None
            or desired.quote_notional > policy.mainnet_canary_max_order_notional
        ):
            result.blocked.append(
                BlockedLevel(
                    desired.level_id,
                    "mainnet canary order notional limit is missing or exceeded",
                    desired.side,
                    desired.quote_amount,
                )
            )
            continue
        result.creates.append(desired)
        slots -= 1
        if desired.side is ExecutionSide.BUY:
            pending_buy += desired.quote_notional
        else:
            pending_sell += desired.quote_notional
    result.pending_buy_notional = pending_buy
    result.pending_sell_notional = pending_sell
    result.potential_long_exposure = current_long + pending_buy
    result.potential_short_exposure = current_short + pending_sell
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class JsonlExecutionJournal:
    """Append-only execution event journal with no secret-bearing fields."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def append(self, event: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "event": event,
            **_json_safe(fields),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
