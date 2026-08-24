"""Closed-loop, deterministic maker replay for Stage 6.

This module is intentionally separate from the Hummingbot controller.  It
reuses only the pure Stage 2--4 calculations, models orders as resting maker
quotes, and records every simulated lifecycle decision.  The replay has no
exchange or account API dependency.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from derive_options_mm.grid_engine import GridParameterConfig
from derive_options_mm.mode_selector import ModeSelector, ModeSelectorConfig
from derive_options_mm.state_engine import StateEngine, StateEngineConfig

from .baselines import StrategyVariant, static_geometric_plan
from .data_loader import finite_float, iso_timestamp, parse_timestamp
from .fill_models import FillModelName, bbo_fill_condition


def _model_value(model: FillModelName | str) -> FillModelName:
    return model if isinstance(model, FillModelName) else FillModelName(str(model))


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class ReplayConfig:
    """Fixed replay assumptions shared by every strategy variant."""

    # 9.30 is the smallest tested Stage 5 scale that met the observed
    # 0.01-BTC Derive testnet minimum near the captured price.  It scales the
    # unchanged theoretical quote allocations only for offline comparison.
    order_scale: Decimal = Decimal("9.30")
    min_order_size: Decimal = Decimal("0.01")
    amount_increment: Decimal = Decimal("0.0001")
    price_increment: Decimal = Decimal("0.1")
    min_notional_size: Decimal = Decimal("0")
    maker_fee_bps: Decimal = Decimal("0")
    maker_adverse_fill_buffer_bps: Decimal = Decimal("0")
    max_total_position_notional: Decimal = Decimal("10000")
    max_side_position_notional: Decimal = Decimal("5000")
    max_active_levels: int = 10
    minimum_order_lifetime_seconds: float = 30.0
    maximum_order_lifetime_seconds: float = 600.0
    refresh_price_tolerance_bps: Decimal = Decimal("5")
    refresh_amount_tolerance_pct: Decimal = Decimal("0.05")
    initial_capital: Decimal = Decimal("10000")
    markout_horizons_seconds: tuple[int, ...] = (5, 30, 60)

    def to_record(self) -> dict[str, Any]:
        return _json_safe(self.__dict__)


@dataclass
class ReplayOrder:
    """One resting simulated maker entry."""

    order_id: str
    level_id: str
    side: str
    price: Decimal
    amount: Decimal
    quote_notional: Decimal
    theoretical_price: Decimal
    created_at_seconds: float
    created_index: int
    plan_version: int
    mode: str
    take_profit_price: Decimal
    take_profit_level_id: str


@dataclass
class ReplayPosition:
    """One filled entry whose native adjacent-grid TP remains managed."""

    position_id: str
    level_id: str
    entry_side: str
    entry_price: Decimal
    amount: Decimal
    entry_quote: Decimal
    entry_time_seconds: float
    entry_index: int
    entry_mode: str
    entry_iv_regime: str
    tp_price: Decimal
    tp_level_id: str
    entry_fee: Decimal


@dataclass
class ReplayResult:
    """Raw events and tick state for one strategy/fill-model combination."""

    strategy: str
    fill_model: str
    events: list[dict[str, Any]] = field(default_factory=list)
    ticks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def all_events(self) -> list[dict[str, Any]]:
        return [*self.events, *self.ticks]


def _quantize(value: Decimal, increment: Decimal, rounding: str) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=rounding) * increment


def _quantize_amount(value: Decimal, config: ReplayConfig) -> Decimal:
    return _quantize(value, config.amount_increment, ROUND_DOWN)


def _quantize_price(value: Decimal, config: ReplayConfig) -> Decimal:
    return _quantize(value, config.price_increment, ROUND_HALF_UP)


def _level_id(level: Mapping[str, Any]) -> str:
    return f"{str(level.get('side', '')).lower()}_{int(level.get('level_index', 0))}"


def _levels(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        values = plan.get(f"{side}_levels", [])
        if not isinstance(values, Sequence):
            continue
        for level in values:
            if isinstance(level, Mapping):
                result.append(dict(level))
    return result


def _adjacent_tp(
    *,
    level: Mapping[str, Any],
    plan: Mapping[str, Any],
    entry_price: Decimal,
    config: ReplayConfig,
) -> tuple[Decimal, str]:
    side = str(level.get("side", "")).lower()
    index = int(level.get("level_index", 0))
    source = plan.get("buy_levels" if side == "buy" else "sell_levels", [])
    target: Decimal | None = None
    target_level_id = "center"
    center = _decimal(plan.get("center_price"))
    if index > 0 and isinstance(source, Sequence):
        for candidate in source:
            if (
                isinstance(candidate, Mapping)
                and int(candidate.get("level_index", -1)) == index - 1
            ):
                target = _decimal(candidate.get("theoretical_price"))
                target_level_id = _level_id(candidate)
                break
    if target is None:
        target = center
    if target is None or target <= 0:
        target = entry_price * (Decimal("1.001") if side == "buy" else Decimal("0.999"))
    if side == "buy" and target <= entry_price:
        target = entry_price * Decimal("1.001")
    if side == "sell" and target >= entry_price:
        target = entry_price * Decimal("0.999")
    return _quantize_price(target, config), target_level_id


def _iv_regime(iv_ratio: Any) -> str:
    value = finite_float(iv_ratio)
    if value is None:
        return "unknown"
    if value < 0.90:
        return "low"
    if value > 1.10:
        return "high"
    return "normal"


class ReplayEngine:
    """Replay one strategy with one explicit fill model."""

    def __init__(
        self,
        snapshots: Sequence[Mapping[str, Any]],
        *,
        evaluation_start_seconds: float,
        evaluation_end_seconds: float,
        strategy: StrategyVariant,
        fill_model: FillModelName,
        grid_config: GridParameterConfig | None = None,
        replay_config: ReplayConfig | None = None,
    ) -> None:
        self.snapshots = [dict(snapshot) for snapshot in snapshots]
        self.evaluation_start_seconds = evaluation_start_seconds
        self.evaluation_end_seconds = evaluation_end_seconds
        self.strategy = strategy
        self.fill_model = fill_model
        self.grid_config = grid_config or GridParameterConfig()
        self.config = replay_config or ReplayConfig()
        self._times = [parse_timestamp(snapshot.get("timestamp")) for snapshot in self.snapshots]
        self._mids = [finite_float(snapshot.get("mid_price")) for snapshot in self.snapshots]

    def _snapshot_with_inventory(
        self, snapshot: Mapping[str, Any], position_base: Decimal
    ) -> dict[str, Any]:
        result = dict(snapshot)
        mid = _decimal(snapshot.get("mid_price"))
        result["account_data_available"] = True
        result["current_position"] = float(position_base)
        result["position_notional"] = float(abs(position_base) * mid) if mid and mid > 0 else 0.0
        return result

    def _adaptive_plan(
        self,
        snapshot: Mapping[str, Any],
        state_engine: StateEngine,
        mode_selector: ModeSelector,
        plan_engine: Any,
    ) -> tuple[dict[str, Any], Any, Any]:
        state = state_engine.update(snapshot)
        decision = mode_selector.update(state)
        plan = plan_engine.build(snapshot, state, decision)
        return plan.to_record(), state, decision

    def _plan_for(
        self,
        snapshot: Mapping[str, Any],
        *,
        tick_index: int,
        state_engine: StateEngine,
        mode_selector: ModeSelector,
        plan_engine: Any,
    ) -> tuple[dict[str, Any], Any, Any]:
        if self.strategy is StrategyVariant.STATIC:
            plan = static_geometric_plan(
                snapshot,
                config=self.grid_config,
                plan_version=tick_index,
            )
            # Static quotes still get a state observation for common regime and
            # markout reporting; the observation does not affect its plan.
            state = state_engine.update(snapshot)
            decision = mode_selector.update(state)
            return plan, state, decision
        return self._adaptive_plan(snapshot, state_engine, mode_selector, plan_engine)

    def _desired_order(
        self,
        level: Mapping[str, Any],
        plan: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        now_seconds: float,
        tick_index: int,
    ) -> tuple[ReplayOrder | None, str | None]:
        side = str(level.get("side", "")).lower()
        theoretical = _decimal(level.get("theoretical_price"))
        quote_amount = _decimal(level.get("quote_amount"))
        best_bid = _decimal(snapshot.get("best_bid"))
        best_ask = _decimal(snapshot.get("best_ask"))
        if side not in {"buy", "sell"} or theoretical is None or quote_amount is None:
            return None, "invalid theoretical level"
        price = _quantize_price(theoretical, self.config)
        if side == "buy" and best_ask is not None and price >= best_ask:
            price = _quantize_price(best_ask - self.config.price_increment, self.config)
        if side == "sell" and best_bid is not None and price <= best_bid:
            price = _quantize_price(best_bid + self.config.price_increment, self.config)
        if price <= 0:
            return None, "invalid maker price"
        if side == "buy" and best_ask is not None and price >= best_ask:
            return None, "buy price is not passive"
        if side == "sell" and best_bid is not None and price <= best_bid:
            return None, "sell price is not passive"
        scaled_quote = quote_amount * self.config.order_scale
        amount = _quantize_amount(scaled_quote / price, self.config)
        if amount < self.config.min_order_size:
            return None, "amount below observed Derive minimum"
        quote_notional = amount * price
        if quote_notional < self.config.min_notional_size:
            return None, "notional below configured minimum"
        tp_price, tp_level_id = _adjacent_tp(
            level=level,
            plan=plan,
            entry_price=price,
            config=self.config,
        )
        mode = str(plan.get("mode", "normal"))
        return (
            ReplayOrder(
                order_id=f"{self.strategy.value}:{self.fill_model.value}:{tick_index}:{_level_id(level)}",
                level_id=_level_id(level),
                side=side,
                price=price,
                amount=amount,
                quote_notional=quote_notional,
                theoretical_price=theoretical,
                created_at_seconds=now_seconds,
                created_index=tick_index,
                plan_version=int(plan.get("plan_version", tick_index)),
                mode=mode,
                take_profit_price=tp_price,
                take_profit_level_id=tp_level_id,
            ),
            None,
        )

    def _event(
        self,
        events: list[dict[str, Any]],
        *,
        timestamp: str,
        timestamp_seconds: float,
        event: str,
        **fields: Any,
    ) -> None:
        events.append(
            _json_safe(
                {
                    "timestamp": timestamp,
                    "timestamp_seconds": timestamp_seconds,
                    "strategy": self.strategy.value,
                    "fill_model": self.fill_model.value,
                    "event": event,
                    **fields,
                }
            )
        )

    def _markout(
        self, timestamp_seconds: float, entry_side: str, entry_price: Decimal
    ) -> dict[str, float | None]:
        values: dict[str, float | None] = {}
        for horizon in self.config.markout_horizons_seconds:
            target = timestamp_seconds + horizon
            index = bisect_left(
                [value if value is not None else float("inf") for value in self._times],
                target,
            )
            mid = self._mids[index] if index < len(self._mids) else None
            if mid is None or entry_price <= 0:
                values[f"markout_{horizon}s_bps"] = None
                continue
            signed = (mid - float(entry_price)) / float(entry_price) * 10_000
            values[f"markout_{horizon}s_bps"] = signed if entry_side == "buy" else -signed
        return values

    def _unrealized(self, positions: Mapping[str, ReplayPosition], mid: Decimal | None) -> Decimal:
        if mid is None or mid <= 0:
            return Decimal(0)
        total = Decimal(0)
        for position in positions.values():
            if position.entry_side == "buy":
                total += (mid - position.entry_price) * position.amount
            else:
                total += (position.entry_price - mid) * position.amount
        return total

    def run(self) -> ReplayResult:
        state_config = StateEngineConfig(
            iv_weight=Decimal("0")
            if self.strategy is StrategyVariant.RV_ONLY
            else StateEngineConfig().iv_weight,
            max_position_notional=100_000.0,
        )
        state_engine = StateEngine(state_config)
        mode_selector = ModeSelector(ModeSelectorConfig())
        from derive_options_mm.grid_engine import GridParameterEngine

        plan_engine = GridParameterEngine(self.grid_config)
        result = ReplayResult(self.strategy.value, self.fill_model.value)
        orders: dict[str, ReplayOrder] = {}
        positions: dict[str, ReplayPosition] = {}
        position_base = Decimal(0)
        realized_gross = Decimal(0)
        fees = Decimal(0)
        high_water = Decimal(0)
        last_tick_seconds: float | None = None
        previous_plan: Mapping[str, Any] | None = None
        last_iv_ratio: Any = None

        for tick_index, raw_snapshot in enumerate(self.snapshots):
            timestamp_seconds = self._times[tick_index]
            if timestamp_seconds is None:
                continue
            raw_timestamp = str(raw_snapshot.get("timestamp", iso_timestamp(timestamp_seconds)))
            snapshot = dict(raw_snapshot)

            if timestamp_seconds < self.evaluation_start_seconds:
                # Warm all stateful Stage 2--4 components without creating
                # orders.  This preserves history and mode hysteresis while
                # keeping the measured window free of warm-up trades.
                replay_snapshot = self._snapshot_with_inventory(snapshot, position_base)
                try:
                    warm_plan, warm_state, _ = self._plan_for(
                        replay_snapshot,
                        tick_index=tick_index,
                        state_engine=state_engine,
                        mode_selector=mode_selector,
                        plan_engine=plan_engine,
                    )
                    last_iv_ratio = getattr(warm_state, "iv_ratio", None)
                    previous_plan = warm_plan
                except (ArithmeticError, TypeError, ValueError):
                    pass
                continue
            if timestamp_seconds > self.evaluation_end_seconds:
                break

            # First process evidence that existed after creation.  This is the
            # key no-same-timestamp look-ahead guard.
            for level_id, order in list(orders.items()):
                if timestamp_seconds <= order.created_at_seconds:
                    continue
                decision = bbo_fill_condition(
                    side=order.side,
                    order_price=float(order.price),
                    snapshot=snapshot,
                    model=self.fill_model,
                )
                if not decision.filled:
                    continue
                fill_price = order.price
                adverse = self.config.maker_adverse_fill_buffer_bps / Decimal(10_000)
                if self.config.maker_adverse_fill_buffer_bps:
                    fill_price = order.price * (
                        Decimal(1) + adverse if order.side == "buy" else Decimal(1) - adverse
                    )
                entry_fee = fill_price * order.amount * self.config.maker_fee_bps / Decimal(10_000)
                iv_regime = _iv_regime(last_iv_ratio)
                self._event(
                    result.events,
                    timestamp=raw_timestamp,
                    timestamp_seconds=timestamp_seconds,
                    event="ENTRY_FILLED",
                    order_id=order.order_id,
                    level_id=order.level_id,
                    side=order.side,
                    price=fill_price,
                    amount=order.amount,
                    quote_notional=fill_price * order.amount,
                    fee=entry_fee,
                    plan_version=order.plan_version,
                    mode=order.mode,
                    entry_iv_regime=iv_regime,
                    fill_evidence=decision.to_record(),
                    **self._markout(timestamp_seconds, order.side, fill_price),
                )
                position = ReplayPosition(
                    position_id=f"{order.order_id}:position",
                    level_id=order.level_id,
                    entry_side=order.side,
                    entry_price=fill_price,
                    amount=order.amount,
                    entry_quote=fill_price * order.amount,
                    entry_time_seconds=timestamp_seconds,
                    entry_index=tick_index,
                    entry_mode=order.mode,
                    entry_iv_regime=iv_regime,
                    tp_price=order.take_profit_price,
                    tp_level_id=order.take_profit_level_id,
                    entry_fee=entry_fee,
                )
                positions[level_id] = position
                position_base += order.amount if order.side == "buy" else -order.amount
                fees += entry_fee
                del orders[level_id]
                self._event(
                    result.events,
                    timestamp=raw_timestamp,
                    timestamp_seconds=timestamp_seconds,
                    event="TP_CREATED",
                    position_id=position.position_id,
                    level_id=position.level_id,
                    side="sell" if order.side == "buy" else "buy",
                    price=position.tp_price,
                    amount=position.amount,
                    quote_notional=position.tp_price * position.amount,
                    tp_level_id=position.tp_level_id,
                    order_type="LIMIT_MAKER",
                )

            for position_id, position in list(positions.items()):
                if timestamp_seconds <= position.entry_time_seconds:
                    continue
                tp_side = "sell" if position.entry_side == "buy" else "buy"
                decision = bbo_fill_condition(
                    side=tp_side,
                    order_price=float(position.tp_price),
                    snapshot=snapshot,
                    model=self.fill_model,
                )
                if not decision.filled:
                    continue
                exit_price = position.tp_price
                gross = (
                    (exit_price - position.entry_price) * position.amount
                    if position.entry_side == "buy"
                    else (position.entry_price - exit_price) * position.amount
                )
                exit_fee = (
                    exit_price * position.amount * self.config.maker_fee_bps / Decimal(10_000)
                )
                realized_gross += gross
                fees += exit_fee
                position_base -= (
                    position.amount if position.entry_side == "buy" else -position.amount
                )
                self._event(
                    result.events,
                    timestamp=raw_timestamp,
                    timestamp_seconds=timestamp_seconds,
                    event="TP_FILLED",
                    position_id=position.position_id,
                    level_id=position.level_id,
                    side=tp_side,
                    price=exit_price,
                    amount=position.amount,
                    gross_pnl=gross,
                    fee=exit_fee,
                    quote_notional=exit_price * position.amount,
                    entry_iv_regime=position.entry_iv_regime,
                    net_cycle_pnl=gross - position.entry_fee - exit_fee,
                    holding_time_seconds=timestamp_seconds - position.entry_time_seconds,
                    fill_evidence=decision.to_record(),
                )
                del positions[position_id]

            # Re-run the existing deterministic state/mode/grid chain after
            # fills so simulated inventory feeds the next plan.
            replay_snapshot = self._snapshot_with_inventory(snapshot, position_base)
            try:
                plan, state, decision = self._plan_for(
                    replay_snapshot,
                    tick_index=tick_index,
                    state_engine=state_engine,
                    mode_selector=mode_selector,
                    plan_engine=plan_engine,
                )
            except (ArithmeticError, TypeError, ValueError) as exc:
                result.warnings.append(
                    f"tick {tick_index} strategy chain failed: {type(exc).__name__}"
                )
                plan = {
                    "enabled": False,
                    "valid": False,
                    "mode": "pause",
                    "buy_levels": [],
                    "sell_levels": [],
                    "plan_version": tick_index,
                }
                state = None
                decision = None

            last_iv_ratio = getattr(state, "iv_ratio", None)
            desired: dict[str, ReplayOrder] = {}
            for level in _levels(plan):
                order, reason = self._desired_order(
                    level,
                    plan,
                    snapshot,
                    timestamp_seconds,
                    tick_index,
                )
                if order is None:
                    self._event(
                        result.events,
                        timestamp=raw_timestamp,
                        timestamp_seconds=timestamp_seconds,
                        event="ENTRY_BLOCKED",
                        level_id=_level_id(level),
                        reason=reason or "unavailable",
                    )
                    continue
                desired[order.level_id] = order

            cancellations_this_tick: set[str] = set()
            for level_id, order in list(orders.items()):
                desired_order = desired.get(level_id)
                age = max(0.0, timestamp_seconds - order.created_at_seconds)
                if (
                    not bool(plan.get("enabled", False))
                    or str(plan.get("mode", "pause")) == "pause"
                ):
                    self._event(
                        result.events,
                        timestamp=raw_timestamp,
                        timestamp_seconds=timestamp_seconds,
                        event="ENTRY_CANCELLED",
                        order_id=order.order_id,
                        level_id=level_id,
                        reason="PAUSE or invalid plan",
                        lifetime_seconds=age,
                    )
                    del orders[level_id]
                    cancellations_this_tick.add(level_id)
                    continue
                if desired_order is None:
                    self._event(
                        result.events,
                        timestamp=raw_timestamp,
                        timestamp_seconds=timestamp_seconds,
                        event="ENTRY_CANCELLED",
                        order_id=order.order_id,
                        level_id=level_id,
                        reason="level no longer desired",
                        lifetime_seconds=age,
                    )
                    del orders[level_id]
                    cancellations_this_tick.add(level_id)
                    continue
                price_deviation = (
                    abs(order.price - desired_order.price) / order.price * Decimal(10_000)
                    if order.price > 0
                    else Decimal("Infinity")
                )
                amount_deviation = (
                    abs(order.quote_notional - desired_order.quote_notional) / order.quote_notional
                    if order.quote_notional > 0
                    else Decimal("Infinity")
                )
                crossing = (
                    order.side == "buy"
                    and _decimal(snapshot.get("best_ask")) is not None
                    and order.price >= _decimal(snapshot.get("best_ask"))
                ) or (
                    order.side == "sell"
                    and _decimal(snapshot.get("best_bid")) is not None
                    and order.price <= _decimal(snapshot.get("best_bid"))
                )
                needs_refresh = (
                    str(order.mode) != str(plan.get("mode", ""))
                    or age >= self.config.maximum_order_lifetime_seconds
                    or price_deviation > self.config.refresh_price_tolerance_bps
                    or amount_deviation > self.config.refresh_amount_tolerance_pct
                    or crossing
                )
                if needs_refresh and age >= self.config.minimum_order_lifetime_seconds:
                    self._event(
                        result.events,
                        timestamp=raw_timestamp,
                        timestamp_seconds=timestamp_seconds,
                        event="ENTRY_CANCELLED",
                        order_id=order.order_id,
                        level_id=level_id,
                        reason="stale or materially changed level",
                        lifetime_seconds=age,
                        price_deviation_bps=price_deviation,
                    )
                    del orders[level_id]
                    cancellations_this_tick.add(level_id)
                else:
                    self._event(
                        result.events,
                        timestamp=raw_timestamp,
                        timestamp_seconds=timestamp_seconds,
                        event="ENTRY_KEEP",
                        order_id=order.order_id,
                        level_id=level_id,
                        lifetime_seconds=age,
                    )

            completed_levels_this_tick = {
                event["level_id"]
                for event in result.events
                if event.get("event") == "TP_FILLED"
                and event.get("timestamp_seconds") == timestamp_seconds
            }
            occupied_levels = set(orders) | {position.level_id for position in positions.values()}
            occupied_levels.update(completed_levels_this_tick)
            if not cancellations_this_tick:
                pending_notional = sum(
                    (order.quote_notional for order in orders.values()), Decimal(0)
                )
                current_notional = abs(position_base) * (
                    _decimal(snapshot.get("mid_price")) or Decimal(0)
                )
                for level_id, order in sorted(desired.items()):
                    if level_id in occupied_levels:
                        continue
                    if len(orders) + len(positions) >= self.config.max_active_levels:
                        self._event(
                            result.events,
                            timestamp=raw_timestamp,
                            timestamp_seconds=timestamp_seconds,
                            event="ENTRY_BLOCKED",
                            level_id=level_id,
                            reason="maximum active replay levels",
                        )
                        continue
                    if (
                        current_notional + pending_notional + order.quote_notional
                        > self.config.max_total_position_notional
                    ):
                        self._event(
                            result.events,
                            timestamp=raw_timestamp,
                            timestamp_seconds=timestamp_seconds,
                            event="ENTRY_BLOCKED",
                            level_id=level_id,
                            reason="maximum total replay notional",
                        )
                        continue
                    side_exposure = current_notional + pending_notional + order.quote_notional
                    if side_exposure > self.config.max_side_position_notional:
                        self._event(
                            result.events,
                            timestamp=raw_timestamp,
                            timestamp_seconds=timestamp_seconds,
                            event="ENTRY_BLOCKED",
                            level_id=level_id,
                            reason="maximum side replay notional",
                        )
                        continue
                    orders[level_id] = order
                    occupied_levels.add(level_id)
                    pending_notional += order.quote_notional
                    self._event(
                        result.events,
                        timestamp=raw_timestamp,
                        timestamp_seconds=timestamp_seconds,
                        event="ENTRY_CREATED",
                        order_id=order.order_id,
                        level_id=level_id,
                        side=order.side,
                        price=order.price,
                        amount=order.amount,
                        quote_notional=order.quote_notional,
                        plan_version=order.plan_version,
                        mode=order.mode,
                        order_type="LIMIT_MAKER",
                        mid_price=_decimal(snapshot.get("mid_price")),
                        best_bid=_decimal(snapshot.get("best_bid")),
                        best_ask=_decimal(snapshot.get("best_ask")),
                    )
            else:
                self._event(
                    result.events,
                    timestamp=raw_timestamp,
                    timestamp_seconds=timestamp_seconds,
                    event="REPLACEMENT_DEFERRED",
                    canceled_levels=sorted(cancellations_this_tick),
                )

            mid = _decimal(snapshot.get("mid_price"))
            pending_entry_notional = sum(
                (order.quote_notional for order in orders.values()), Decimal(0)
            )
            position_notional = abs(position_base) * (mid or Decimal(0))
            deployed_notional = position_notional + pending_entry_notional
            unrealized = self._unrealized(positions, mid)
            net_pnl = realized_gross - fees + unrealized
            high_water = max(high_water, net_pnl)
            drawdown = high_water - net_pnl
            tick = {
                "timestamp": raw_timestamp,
                "timestamp_seconds": timestamp_seconds,
                "strategy": self.strategy.value,
                "fill_model": self.fill_model.value,
                "mid_price": mid,
                "best_bid": _decimal(snapshot.get("best_bid")),
                "best_ask": _decimal(snapshot.get("best_ask")),
                "state_valid": getattr(state, "state_valid", None),
                "volatility_score": getattr(state, "volatility_score", None),
                "volatility_state": getattr(
                    getattr(state, "volatility_state", None), "value", None
                ),
                "realized_volatility_ratio": getattr(state, "realized_volatility_ratio", None),
                "iv_ratio": getattr(state, "iv_ratio", None),
                "direction_score": getattr(state, "direction_score", None),
                "direction_state": getattr(getattr(state, "direction_state", None), "value", None),
                "inventory_ratio": getattr(state, "inventory_ratio", None),
                "inventory_state": getattr(getattr(state, "inventory_state", None), "value", None),
                "mode": plan.get("mode"),
                "plan_version": plan.get("plan_version"),
                "reference_price": plan.get("reference_price"),
                "center_price": plan.get("center_price"),
                "grid_width_pct": plan.get("total_grid_width_pct"),
                "center_shift_bps": plan.get("center_shift_bps"),
                "buy_allocation_pct": plan.get("buy_allocation_pct"),
                "sell_allocation_pct": plan.get("sell_allocation_pct"),
                "active_entry_count": len(orders),
                "open_position_count": len(positions),
                "position_base": position_base,
                "pending_entry_notional": pending_entry_notional,
                "position_notional": position_notional,
                "deployed_notional": deployed_notional,
                "realized_gross_pnl": realized_gross,
                "fees": fees,
                "unrealized_pnl": unrealized,
                "net_pnl": net_pnl,
                "drawdown": drawdown,
                "last_plan_version": previous_plan.get("plan_version") if previous_plan else None,
            }
            result.ticks.append(_json_safe(tick))
            previous_plan = plan
            last_tick_seconds = timestamp_seconds

        if positions:
            result.warnings.append(
                f"{len(positions)} simulated positions remained open at replay end"
            )
        if last_tick_seconds is None:
            result.warnings.append("no replay ticks were available in the evaluation window")
        return result


def run_replay(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    evaluation_start_seconds: float,
    evaluation_end_seconds: float,
    strategies: Sequence[StrategyVariant] = tuple(StrategyVariant),
    fill_models: Sequence[FillModelName] = (
        FillModelName.CONSERVATIVE_CROSS_THROUGH,
        FillModelName.TOUCH_OPTIMISTIC,
    ),
    grid_config: Any = None,
    replay_config: ReplayConfig | None = None,
) -> list[ReplayResult]:
    """Run every fair strategy/fill-model combination deterministically."""

    results: list[ReplayResult] = []
    for strategy in strategies:
        for fill_model in fill_models:
            results.append(
                ReplayEngine(
                    snapshots,
                    evaluation_start_seconds=evaluation_start_seconds,
                    evaluation_end_seconds=evaluation_end_seconds,
                    strategy=strategy,
                    fill_model=fill_model,
                    grid_config=grid_config,
                    replay_config=replay_config,
                ).run()
            )
    return results


__all__ = [
    "ReplayConfig",
    "ReplayEngine",
    "ReplayOrder",
    "ReplayPosition",
    "ReplayResult",
    "run_replay",
]
