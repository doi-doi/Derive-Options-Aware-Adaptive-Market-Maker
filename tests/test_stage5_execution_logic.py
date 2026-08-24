from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

INTEGRATION_ROOT = Path(__file__).parents[1] / "integrations" / "hummingbot"
if str(INTEGRATION_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT))

from derive_adaptive_grid.execution_logic import (  # noqa: E402
    ActiveLevel,
    ExecutionPolicy,
    ExecutionSide,
    GridPlanView,
    JsonlExecutionJournal,
    RuntimeHealth,
    TradingRuleView,
    parse_grid_plan,
    quantize_level,
    reconcile_grid_plan,
)

NOW = 1_900_000_000.0


def _timestamp(epoch: float = NOW) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _level(side: str, index: int, price: str, quote: str = "100") -> dict:
    return {
        "side": side,
        "level_index": index,
        "theoretical_price": price,
        "quote_amount": quote,
    }


def _record(
    *,
    timestamp: str | None = None,
    mode: str = "normal",
    enabled: bool = True,
    valid: bool = True,
    buys: list[dict] | None = None,
    sells: list[dict] | None = None,
) -> dict:
    return {
        "timestamp": timestamp or _timestamp(),
        "trading_pair": "BTC-USDC",
        "mode": mode,
        "enabled": enabled,
        "valid": valid,
        "plan_version": 7,
        "plan_change_significant": False,
        "center_price": "100",
        "total_grid_width_pct": "0.04",
        "buy_levels": buys if buys is not None else [_level("buy", 0, "99")],
        "sell_levels": sells if sells is not None else [_level("sell", 0, "101")],
    }


def _plan(**kwargs: object) -> GridPlanView:
    return parse_grid_plan(_record(**kwargs))


def _rules(*, min_order_size: str = "0.001", min_notional_size: str = "0") -> TradingRuleView:
    return TradingRuleView(
        min_order_size=Decimal(min_order_size),
        min_notional_size=Decimal(min_notional_size),
        min_price_increment=Decimal("0.1"),
        min_base_amount_increment=Decimal("0.001"),
    )


def _health(
    *,
    available: str = "10000",
    position: str = "0",
    testnet: bool = True,
    reason: str = "",
    rules: TradingRuleView | None = None,
) -> RuntimeHealth:
    return RuntimeHealth(
        testnet_verified=testnet,
        connector_ready=True,
        market_data_ready=True,
        trading_rules_available=rules is not None or rules is None,
        balance_verified=True,
        position_verified=True,
        best_bid=Decimal("99.5"),
        best_ask=Decimal("100.5"),
        position_notional=Decimal(position),
        available_collateral=Decimal(available),
        trading_rules=rules or _rules(),
        reason=reason,
    )


def _policy(**kwargs: object) -> ExecutionPolicy:
    values = {
        "execution_max_levels_per_side": 2,
        "testnet_order_scale": Decimal("1"),
        "max_total_position_notional": Decimal("1000"),
        "max_side_position_notional": Decimal("1000"),
        "max_active_grid_levels": 4,
        "max_active_executors": 4,
        "minimum_order_lifetime_seconds": 30.0,
        "maximum_order_lifetime_seconds": 600.0,
    }
    values.update(kwargs)
    return ExecutionPolicy(**values)


def _quantize_price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_DOWN)


def _reconcile(
    plan: GridPlanView | None,
    *,
    active: list[ActiveLevel] | None = None,
    health: RuntimeHealth | None = None,
    policy: ExecutionPolicy | None = None,
    now: float = NOW,
):
    return reconcile_grid_plan(
        plan,
        active=active or [],
        health=health or _health(),
        policy=policy or _policy(),
        now_epoch=now,
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    )


def _active(
    level_id: str,
    side: ExecutionSide,
    *,
    executor_id: str | None = None,
    price: str = "99",
    amount: str = "1",
    created_at: float = NOW,
    filled: bool = False,
    mode: str = "normal",
) -> ActiveLevel:
    price_decimal = Decimal(price)
    amount_decimal = Decimal(amount)
    return ActiveLevel(
        executor_id=executor_id or f"executor-{level_id}",
        level_id=level_id,
        side=side,
        price=price_decimal,
        amount=amount_decimal,
        quote_notional=price_decimal * amount_decimal,
        created_at=created_at,
        is_filled=filled,
        plan_mode=mode,
    )


def test_parser_copies_stage4_levels_without_recalculating() -> None:
    plan = _plan(
        buys=[_level("buy", 0, "98.1234", "37.5")],
        sells=[_level("sell", 0, "101.9876", "62.5")],
    )

    assert plan.buy_levels[0].theoretical_price == Decimal("98.1234")
    assert plan.buy_levels[0].quote_amount == Decimal("37.5")
    assert plan.sell_levels[0].level_id == "sell_0"


def test_parser_rejects_wrong_pair_and_duplicate_level_ids() -> None:
    with pytest.raises(ValueError, match="does not match"):
        parse_grid_plan({**_record(), "trading_pair": "ETH-USDC"})

    duplicate = [_level("buy", 0, "99"), _level("buy", 0, "98")]
    with pytest.raises(ValueError, match="duplicate"):
        parse_grid_plan(_record(buys=duplicate))


def test_quantization_adjusts_crossing_buy_to_a_maker_price() -> None:
    plan = _plan(buys=[_level("buy", 0, "101")], sells=[])
    desired, reason = quantize_level(
        plan.buy_levels[0],
        plan=plan,
        policy=_policy(),
        rules=_rules(),
        best_bid=Decimal("99.5"),
        best_ask=Decimal("100.5"),
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    )

    assert reason is None
    assert desired is not None
    assert desired.price == Decimal("100.4")
    assert desired.maker_price_adjusted is True


def test_quantization_adjusts_crossing_sell_to_a_maker_price() -> None:
    plan = _plan(buys=[], sells=[_level("sell", 0, "99")])
    desired, reason = quantize_level(
        plan.sell_levels[0],
        plan=plan,
        policy=_policy(),
        rules=_rules(),
        best_bid=Decimal("99.5"),
        best_ask=Decimal("100.5"),
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    )

    assert reason is None
    assert desired is not None
    assert desired.price == Decimal("99.6")
    assert desired.maker_price_adjusted is True


def test_quantization_blocks_exchange_minimums() -> None:
    plan = _plan(buys=[_level("buy", 0, "99", "1")], sells=[])
    desired, reason = quantize_level(
        plan.buy_levels[0],
        plan=plan,
        policy=_policy(),
        rules=_rules(min_order_size="1", min_notional_size="200"),
        best_bid=Decimal("99.5"),
        best_ask=Decimal("100.5"),
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    )

    assert desired is None
    assert reason == "amount below exchange minimum"


def test_normal_plan_creates_one_entry_per_side_under_rollout_cap() -> None:
    result = _reconcile(_plan(), policy=_policy(execution_max_levels_per_side=1))

    assert [item.level_id for item in result.creates] == ["buy_0", "sell_0"]
    assert result.stops == []
    assert result.pause_reason == ""
    assert result.potential_long_exposure > Decimal("0")
    assert result.potential_short_exposure > Decimal("0")


def test_default_scale_can_block_a_small_stage4_order_at_testnet_minimum() -> None:
    result = _reconcile(
        _plan(buys=[_level("buy", 0, "99", "100")], sells=[]),
        policy=_policy(testnet_order_scale=Decimal("0.05")),
        health=_health(rules=_rules(min_order_size="0.1")),
    )

    assert result.creates == []
    assert result.blocked[0].reason == "amount below exchange minimum"


def test_pause_stops_unfilled_but_keeps_filled_exposure() -> None:
    active = [
        _active("buy_0", ExecutionSide.BUY, filled=False),
        _active("sell_0", ExecutionSide.SELL, price="101", filled=True),
    ]
    result = _reconcile(
        _plan(mode="pause", enabled=False),
        active=active,
        policy=_policy(cancel_orders_on_pause=True),
    )

    assert result.pause_reason == "GridPlan PAUSE"
    assert [stop.executor_id for stop in result.stops] == ["executor-buy_0"]
    assert result.keeps == ["sell_0"]
    assert result.creates == []


def test_stale_invalid_and_unverified_testnet_plans_fail_closed() -> None:
    stale = _reconcile(
        _plan(timestamp=_timestamp(NOW - 100)),
        policy=_policy(stale_plan_timeout_seconds=30),
    )
    invalid = _reconcile(_plan(valid=False))
    unverified = _reconcile(_plan(), health=_health(testnet=False))

    assert stale.pause_reason.startswith("GridPlan stale")
    assert invalid.pause_reason == "GridPlan invalid"
    assert unverified.pause_reason == "connector/account health unavailable"
    assert stale.creates == invalid.creates == unverified.creates == []


def test_position_unavailable_and_manual_kill_switch_fail_closed() -> None:
    unavailable = RuntimeHealth(
        testnet_verified=True,
        connector_ready=True,
        market_data_ready=True,
        trading_rules_available=True,
        balance_verified=True,
        position_verified=False,
        best_bid=Decimal("99.5"),
        best_ask=Decimal("100.5"),
        available_collateral=Decimal("10000"),
        trading_rules=_rules(),
    )
    unavailable_result = _reconcile(_plan(), health=unavailable)
    kill_result = _reconcile(_plan(), policy=_policy(manual_kill_switch=True))

    assert unavailable_result.pause_reason == "connector/account health unavailable"
    assert kill_result.pause_reason == "manual_kill_switch"
    assert unavailable_result.creates == kill_result.creates == []


def test_order_error_pause_reason_blocks_new_entries() -> None:
    result = _reconcile(
        _plan(),
        policy=_policy(forced_pause_reason="order_error_pause"),
    )

    assert result.pause_reason == "order_error_pause"
    assert result.creates == []


def test_duplicate_active_level_is_stopped_before_any_replacement() -> None:
    active = [
        _active("buy_0", ExecutionSide.BUY, executor_id="first"),
        _active("buy_0", ExecutionSide.BUY, executor_id="duplicate"),
    ]
    result = _reconcile(_plan(sells=[]), active=active)

    assert [stop.executor_id for stop in result.stops] == ["duplicate"]
    assert result.creates == []
    assert result.deferred_create_count == 0


def test_material_change_stops_after_minimum_lifetime_and_defers_create() -> None:
    active = [_active("buy_0", ExecutionSide.BUY, price="98", created_at=NOW - 60)]
    result = _reconcile(_plan(sells=[]), active=active, now=NOW)

    assert result.stops[0].reason == "stale or materially changed level"
    assert result.creates == []
    assert result.deferred_create_count == 1


def test_maximum_lifetime_refreshes_even_when_price_is_unchanged() -> None:
    active = [_active("buy_0", ExecutionSide.BUY, created_at=NOW - 601)]
    result = _reconcile(
        _plan(sells=[]),
        active=active,
        policy=_policy(maximum_order_lifetime_seconds=600),
        now=NOW,
    )

    assert result.stops[0].reason == "stale or materially changed level"


def test_insignificant_quantized_price_movement_keeps_existing_level() -> None:
    active = [_active("buy_0", ExecutionSide.BUY, price="99", created_at=NOW - 60)]
    plan = _plan(buys=[_level("buy", 0, "99.04")], sells=[])
    result = _reconcile(plan, active=active, now=NOW)

    assert result.keeps == ["buy_0"]
    assert result.stops == []
    assert result.creates == []


def test_recent_material_change_is_kept_to_protect_queue_position() -> None:
    active = [_active("buy_0", ExecutionSide.BUY, price="98", created_at=NOW - 1)]
    result = _reconcile(_plan(sells=[]), active=active, now=NOW)

    assert result.stops == []
    assert result.keeps == ["buy_0"]
    assert result.creates == []


def test_filled_entry_is_never_repriced_by_a_new_grid_plan() -> None:
    active = [_active("buy_0", ExecutionSide.BUY, price="80", created_at=NOW - 1000, filled=True)]
    changed_plan = _plan(buys=[_level("buy", 0, "95")], sells=[])
    result = _reconcile(changed_plan, active=active, now=NOW)

    assert result.keeps == ["buy_0"]
    assert result.stops == []
    assert result.creates == []


def test_active_executor_and_grid_caps_block_new_levels() -> None:
    plan = _plan(
        buys=[_level("buy", 0, "99"), _level("buy", 1, "98")],
        sells=[_level("sell", 0, "101"), _level("sell", 1, "102")],
    )
    result = _reconcile(
        plan,
        policy=_policy(
            execution_max_levels_per_side=2,
            max_active_executors=1,
            max_active_grid_levels=1,
        ),
    )

    assert len(result.creates) == 1
    assert len(result.blocked) == 3
    assert all("cap" in item.reason for item in result.blocked)


def test_position_and_pending_exposure_are_counted_before_creation() -> None:
    buy_plan = _plan(buys=[_level("buy", 0, "99")], sells=[])
    position_block = _reconcile(
        buy_plan,
        health=_health(position="950"),
        policy=_policy(max_side_position_notional=Decimal("1000")),
    )
    pending_block = _reconcile(
        _plan(
            buys=[_level("buy", 0, "99"), _level("buy", 1, "98")],
            sells=[],
        ),
        active=[_active("buy_0", ExecutionSide.BUY, price="99")],
        policy=_policy(max_side_position_notional=Decimal("150")),
    )

    assert position_block.creates == []
    assert "side position notional" in position_block.blocked[0].reason
    assert pending_block.creates == []
    assert "side position notional" in pending_block.blocked[0].reason


def test_long_position_blocks_buy_but_keeps_safe_sell_side() -> None:
    result = _reconcile(
        _plan(),
        health=_health(position="950"),
        policy=_policy(
            max_side_position_notional=Decimal("1000"), max_total_position_notional=Decimal("2000")
        ),
    )

    assert [item.level_id for item in result.creates] == ["sell_0"]
    assert result.blocked[0].level_id == "buy_0"


def test_short_position_blocks_sell_but_keeps_safe_buy_side() -> None:
    result = _reconcile(
        _plan(),
        health=_health(position="-950"),
        policy=_policy(
            max_side_position_notional=Decimal("1000"), max_total_position_notional=Decimal("2000")
        ),
    )

    assert [item.level_id for item in result.creates] == ["buy_0"]
    assert result.blocked[0].level_id == "sell_0"


def test_pending_sell_exposure_is_included_in_side_limit() -> None:
    plan = _plan(buys=[], sells=[_level("sell", 0, "101"), _level("sell", 1, "102")])
    result = _reconcile(
        plan,
        active=[_active("sell_0", ExecutionSide.SELL, price="101")],
        policy=_policy(max_side_position_notional=Decimal("150")),
    )

    assert result.pending_sell_notional == Decimal("101")
    assert result.creates == []
    assert "side position notional" in result.blocked[0].reason


def test_defensive_plan_removes_unfilled_levels_not_in_new_plan() -> None:
    active = [_active("buy_4", ExecutionSide.BUY, price="95", mode="normal")]
    defensive = _plan(
        mode="defensive",
        buys=[_level("buy", 0, "98")],
        sells=[_level("sell", 0, "102")],
    )
    result = _reconcile(defensive, active=active, now=NOW)

    assert result.stops[0].reason == "level no longer desired"
    assert result.creates == []


def test_collateral_safety_buffer_blocks_creation() -> None:
    result = _reconcile(
        _plan(buys=[_level("buy", 0, "99")], sells=[]),
        health=_health(available="100"),
        policy=_policy(collateral_safety_buffer_pct=Decimal("0.10")),
    )

    assert result.creates == []
    assert result.blocked[0].reason == "insufficient collateral after safety buffer"


def test_adjacent_grid_take_profit_is_derived_from_stage4_neighbors() -> None:
    plan = _plan(
        buys=[_level("buy", 0, "99"), _level("buy", 1, "98")],
        sells=[],
    )
    desired, reason = quantize_level(
        plan.buy_levels[1],
        plan=plan,
        policy=_policy(),
        rules=_rules(),
        best_bid=Decimal("97"),
        best_ask=Decimal("100.5"),
        quantize_price=_quantize_price,
        quantize_amount=_quantize_amount,
    )

    assert reason is None
    assert desired is not None
    assert desired.take_profit_pct == (Decimal("99") - Decimal("98")) / Decimal("98")


def test_journal_is_append_only_jsonl_and_keeps_decimal_values(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    journal = JsonlExecutionJournal(path)
    journal.append("CREATE_REQUEST", level_id="buy_0", price=Decimal("99.1"))
    journal.append("PAUSE", reason="stale", nested={"amount": Decimal("1.25")})

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == ["CREATE_REQUEST", "PAUSE"]
    assert records[0]["price"] == 99.1
    assert records[1]["nested"]["amount"] == 1.25
