"""Deterministic Stage 5F fill lifecycle simulation.

This harness is intentionally not imported by the production controller.  It
uses Hummingbot's installed ``OrderFilledEvent`` and
``PositionExecutorConfig`` types when run inside the Hummingbot API container,
then exercises the real Stage 5 classification/reconciliation functions with
mock executor state.  Its output is simulation evidence only; it never calls
an exchange endpoint.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace

from derive_adaptive_grid.derive_adaptive_grid import DeriveAdaptiveGrid
from derive_adaptive_grid.execution_logic import (
    ActiveLevel,
    ExecutionPolicy,
    ExecutionSide,
    GridPlanView,
    PlanLevel,
    RuntimeHealth,
    TradingRuleView,
    reconcile_grid_plan,
)
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee
from hummingbot.core.event.events import OrderFilledEvent


def _quantize_price(value: Decimal) -> Decimal:
    return Decimal(round(float(f"{value:.5g}"), 6))


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def _plan() -> GridPlanView:
    return GridPlanView(
        timestamp=datetime.fromtimestamp(1_900_000_000, UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        trading_pair="BTC-USDC",
        mode="normal",
        enabled=True,
        valid=True,
        plan_version=708,
        plan_change_significant=False,
        center_price=Decimal("77508"),
        total_grid_width_pct=Decimal("0.010113110566309126"),
        buy_levels=(
            PlanLevel(
                side=ExecutionSide.BUY,
                level_index=0,
                theoretical_price=Decimal("77480"),
                quote_amount=Decimal("100"),
            ),
        ),
        sell_levels=(
            PlanLevel(
                side=ExecutionSide.SELL,
                level_index=0,
                theoretical_price=Decimal("77536"),
                quote_amount=Decimal("100"),
            ),
        ),
    )


def _health(position_notional: Decimal) -> RuntimeHealth:
    return RuntimeHealth(
        testnet_verified=True,
        connector_ready=True,
        market_data_ready=True,
        trading_rules_available=True,
        balance_verified=True,
        position_verified=True,
        best_bid=Decimal("77480"),
        best_ask=Decimal("77536"),
        position_notional=position_notional,
        available_collateral=Decimal("99964"),
        trading_rules=TradingRuleView(
            min_order_size=Decimal("0.01"),
            min_notional_size=Decimal("0"),
            min_price_increment=Decimal("0.1"),
            min_base_amount_increment=Decimal("0.0001"),
        ),
    )


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        execution_max_levels_per_side=1,
        testnet_order_scale=Decimal("9.30"),
        max_total_position_notional=Decimal("10000"),
        max_side_position_notional=Decimal("5000"),
        max_active_grid_levels=2,
        max_active_executors=2,
        minimum_order_lifetime_seconds=30,
        maximum_order_lifetime_seconds=600,
        collateral_safety_buffer_pct=Decimal("0.10"),
        leverage=Decimal("1"),
        post_only=True,
        take_profit_mode="adjacent_grid",
        take_profit_step_multiplier=Decimal("1"),
    )


def _active(
    *,
    executor_id: str,
    level_id: str,
    side: ExecutionSide,
    price: str,
    amount: str,
    filled: bool,
) -> ActiveLevel:
    price_decimal = Decimal(price)
    amount_decimal = Decimal(amount)
    return ActiveLevel(
        executor_id=executor_id,
        level_id=level_id,
        side=side,
        price=price_decimal,
        amount=amount_decimal,
        quote_notional=price_decimal * amount_decimal,
        created_at=1_900_000_000,
        is_filled=filled,
        plan_mode="normal",
    )


def main() -> None:
    plan = _plan()
    policy = _policy()
    entry_event = OrderFilledEvent(
        timestamp=1_900_000_010,
        order_id="0xsim-entry-client",
        trading_pair="BTC-USDC",
        trade_type=TradeType.BUY,
        order_type=OrderType.LIMIT_MAKER,
        price=Decimal("77480"),
        amount=Decimal("0.012"),
        trade_fee=AddedToCostTradeFee(),
        exchange_trade_id="sim-entry-trade",
        exchange_order_id="sim-entry-exchange",
        leverage=1,
        position="LONG",
    )
    tp_event = OrderFilledEvent(
        timestamp=1_900_000_020,
        order_id="0xsim-tp-client",
        trading_pair="BTC-USDC",
        trade_type=TradeType.SELL,
        order_type=OrderType.LIMIT_MAKER,
        price=Decimal("77536"),
        amount=Decimal("0.012"),
        trade_fee=AddedToCostTradeFee(),
        exchange_trade_id="sim-tp-trade",
        exchange_order_id="sim-tp-exchange",
        leverage=1,
        position="LONG",
    )

    desired_buy = reconcile_grid_plan(
        plan,
        active=[],
        health=_health(Decimal("0")),
        policy=policy,
        now_epoch=1_900_000_000,
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    ).creates[0]
    controller = object.__new__(DeriveAdaptiveGrid)
    controller.config = SimpleNamespace(
        id="derive_adaptive_grid_simulation",
        connector_name="derive_perpetual_testnet",
        trading_pair="BTC-USDC",
        leverage=1,
        stop_loss_pct=None,
        take_profit_mode="adjacent_grid",
        take_profit_pct=Decimal("0.001"),
        take_profit_step_multiplier=Decimal("1"),
        time_limit_seconds=None,
    )
    controller.market_data_provider = SimpleNamespace(time=lambda: 1_900_000_000)
    controller._executor_plan_modes = {}
    controller._pending_stop_ids = set()

    position_executor_config = controller._executor_config(desired_buy)
    fake_executor = SimpleNamespace(
        id=position_executor_config.id,
        is_active=True,
        is_trading=False,
        filled_amount_quote=Decimal("0"),
        custom_info={"level_id": "buy_0"},
        timestamp=1_900_000_000,
        config=position_executor_config,
    )
    controller.executors_info = [fake_executor]
    before_fill = controller._active_levels()
    fake_executor.is_trading = True
    fake_executor.filled_amount_quote = entry_event.price * entry_event.amount
    after_fill = controller._active_levels()

    active_after_fill = [
        _active(
            executor_id=position_executor_config.id,
            level_id="buy_0",
            side=ExecutionSide.BUY,
            price="77480",
            amount="0.012",
            filled=True,
        ),
        _active(
            executor_id="sim-sell-entry",
            level_id="sell_0",
            side=ExecutionSide.SELL,
            price="77540",
            amount="0.0119",
            filled=False,
        ),
    ]
    after_fill_reconciliation = reconcile_grid_plan(
        plan,
        active=active_after_fill,
        health=_health(entry_event.price * entry_event.amount),
        policy=policy,
        now_epoch=1_900_000_030,
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    )
    after_exit_reconciliation = reconcile_grid_plan(
        plan,
        active=[active_after_fill[1]],
        health=_health(Decimal("0")),
        policy=policy,
        now_epoch=1_900_000_030,
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    )
    expected_tp_pct = (plan.center_price - entry_event.price) / entry_event.price
    expected_tp_price = entry_event.price * (Decimal("1") + expected_tp_pct)
    output = {
        "simulation_only": True,
        "exchange_calls": 0,
        "official_event_types": [type(entry_event).__name__, type(tp_event).__name__],
        "entry_event": {
            "order_type": entry_event.order_type.name,
            "exchange_order_id": entry_event.exchange_order_id,
            "filled_amount": str(entry_event.amount),
        },
        "before_fill": {
            "active_levels": [item.level_id for item in before_fill],
            "filled_levels": [item.level_id for item in before_fill if item.is_filled],
        },
        "after_fill": {
            "active_levels": [item.level_id for item in after_fill],
            "filled_levels": [item.level_id for item in after_fill if item.is_filled],
        },
        "after_fill_reconciliation": {
            "keeps": after_fill_reconciliation.keeps,
            "creates": [item.level_id for item in after_fill_reconciliation.creates],
            "stops": [item.level_id for item in after_fill_reconciliation.stops],
        },
        "position_executor": {
            "entry_side": position_executor_config.side.name,
            "entry_price": str(position_executor_config.entry_price),
            "amount": str(position_executor_config.amount),
            "open_order_type": position_executor_config.triple_barrier_config.open_order_type.name,
            "take_profit_order_type": (
                position_executor_config.triple_barrier_config.take_profit_order_type.name
            ),
            "adjacent_grid_take_profit_pct": str(expected_tp_pct),
            "adjacent_grid_take_profit_price": str(expected_tp_price),
        },
        "exit_event": {
            "order_type": tp_event.order_type.name,
            "exchange_order_id": tp_event.exchange_order_id,
            "filled_amount": str(tp_event.amount),
            "realized_pnl_before_fees": str((tp_event.price - entry_event.price) * tp_event.amount),
        },
        "after_exit_reconciliation": {
            "filled_levels": [],
            "creates": [item.level_id for item in after_exit_reconciliation.creates],
            "duplicate_level_count": len(
                [item.level_id for item in after_exit_reconciliation.creates]
            ),
        },
    }
    assert output["simulation_only"] is True
    assert output["after_fill"]["filled_levels"] == ["buy_0"]
    assert output["after_fill_reconciliation"]["creates"] == []
    assert output["position_executor"]["open_order_type"] == OrderType.LIMIT_MAKER.name
    assert output["position_executor"]["take_profit_order_type"] == OrderType.LIMIT_MAKER.name
    assert output["after_exit_reconciliation"]["creates"] == ["buy_0"]
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
