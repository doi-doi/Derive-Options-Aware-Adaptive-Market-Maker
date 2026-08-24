"""Focused Stage 6 evaluation and replay regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from evaluation.baselines import StrategyVariant, static_geometric_plan
from evaluation.data_loader import AsOfSeries, EvaluationFrame, parse_timestamp
from evaluation.fill_models import (
    FillModelName,
    bbo_fill_condition,
    first_future_bbo_fill,
)
from evaluation.metrics import plan_stability, summarize_replay
from evaluation.replay import ReplayConfig, ReplayEngine, ReplayResult


def _snapshot(index: int, *, bid: float = 99.9, ask: float = 100.1) -> dict[str, object]:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index * 5)
    return {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "trading_pair": "BTC-USDC",
        "data_valid": True,
        "account_data_available": True,
        "current_position": 0.0,
        "position_notional": 0.0,
        "best_bid": bid,
        "best_ask": ask,
        "best_bid_size": 1.0,
        "best_ask_size": 1.0,
        "mid_price": (bid + ask) / 2,
        "spread_bps": (ask - bid) / ((bid + ask) / 2) * 10_000,
        "depth_imbalance": 0.0,
        "top_level_imbalance": 0.0,
        "order_flow_imbalance": 0.0,
        "trade_data_available": True,
        "atm_iv": 0.5,
        "iv_data_available": True,
        "iv_confidence": 1.0,
    }


def test_asof_series_never_returns_a_future_record() -> None:
    series = AsOfSeries(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "value": "old"},
            {"timestamp": "2026-01-01T00:00:10Z", "value": "future"},
        ]
    )
    assert series.at_or_before(1_767_225_599) is None
    assert series.at_or_before(1_767_225_600)["value"] == "old"


def test_fill_models_keep_conservative_and_touch_separate() -> None:
    snapshot = {
        "timestamp": "2026-01-01T00:00:10Z",
        "best_bid": 99.0,
        "best_ask": 99.5,
    }
    assert not bbo_fill_condition(
        side="buy",
        order_price=99.5,
        snapshot=snapshot,
        model=FillModelName.CONSERVATIVE_CROSS_THROUGH,
    ).filled
    assert bbo_fill_condition(
        side="buy",
        order_price=99.5,
        snapshot=snapshot,
        model=FillModelName.TOUCH_OPTIMISTIC,
    ).filled
    assert bbo_fill_condition(
        side="sell",
        order_price=99.0,
        snapshot=snapshot,
        model=FillModelName.TOUCH_OPTIMISTIC,
    ).filled


def test_same_timestamp_cannot_fill_a_resting_order() -> None:
    evidence = first_future_bbo_fill(
        side="buy",
        order_price=100.0,
        created_at_seconds=1_000.0,
        future_snapshots=[
            {"timestamp": 1_000.0, "best_bid": 99.0, "best_ask": 99.0},
            {"timestamp": 1_005.0, "best_bid": 99.0, "best_ask": 99.0},
        ],
        model=FillModelName.CONSERVATIVE_CROSS_THROUGH,
    )
    assert evidence is not None
    assert evidence[0] == 1


def test_static_baseline_is_fixed_width_five_levels_and_fifty_fifty() -> None:
    plan = static_geometric_plan(_snapshot(0))
    assert plan["enabled"] is True
    assert plan["buy_levels_count"] == 5
    assert plan["sell_levels_count"] == 5
    assert plan["buy_allocation_pct"] == 0.5
    assert plan["sell_allocation_pct"] == 0.5
    assert plan["total_grid_width_pct"] == 0.01


def test_closed_loop_replay_records_fill_tp_inventory_and_repopulation() -> None:
    snapshots = [_snapshot(index) for index in range(65)]
    snapshots[61] = _snapshot(61, bid=98.8, ask=98.9)
    snapshots[62] = _snapshot(62, bid=101.1, ask=101.2)
    start = parse_timestamp(snapshots[60]["timestamp"])
    end = parse_timestamp(snapshots[64]["timestamp"])
    assert start is not None and end is not None
    engine = ReplayEngine(
        snapshots,
        evaluation_start_seconds=start,
        evaluation_end_seconds=end,
        strategy=StrategyVariant.STATIC,
        fill_model=FillModelName.TOUCH_OPTIMISTIC,
        replay_config=ReplayConfig(
            order_scale=Decimal("9.30"),
            maker_fee_bps=Decimal("1"),
        ),
    )
    result = engine.run()
    event_names = [event["event"] for event in result.events]
    assert "ENTRY_FILLED" in event_names
    assert "TP_CREATED" in event_names
    assert "TP_FILLED" in event_names
    entry_index = event_names.index("ENTRY_FILLED")
    tp_index = event_names.index("TP_FILLED")
    entry = result.events[entry_index]
    exit_event = result.events[tp_index]
    tp_created = next(event for event in result.events if event["event"] == "TP_CREATED")
    assert tp_created["order_type"] == "LIMIT_MAKER"
    assert any(
        float(tick["position_base"]) != 0.0
        for tick in result.ticks
        if tick["timestamp_seconds"] >= entry["timestamp_seconds"]
        and tick["timestamp_seconds"] <= exit_event["timestamp_seconds"]
    )
    same_tick_repopulation = [
        event
        for event in result.events
        if event["event"] == "ENTRY_CREATED"
        and event["level_id"] == exit_event["level_id"]
        and event["timestamp_seconds"] == exit_event["timestamp_seconds"]
    ]
    assert same_tick_repopulation == []


def test_replay_metrics_include_fees_and_drawdown() -> None:
    result = ReplayResult(
        strategy=StrategyVariant.STATIC.value,
        fill_model=FillModelName.CONSERVATIVE_CROSS_THROUGH.value,
        events=[
            {
                "event": "ENTRY_CREATED",
                "order_id": "o1",
                "timestamp_seconds": 1.0,
                "price": 100.0,
                "mid_price": 100.0,
                "best_bid": 99.0,
                "best_ask": 101.0,
                "side": "buy",
                "quote_notional": 100.0,
            },
            {
                "event": "ENTRY_FILLED",
                "order_id": "o1",
                "timestamp_seconds": 2.0,
                "side": "buy",
                "price": 100.0,
                "amount": 1.0,
                "quote_notional": 100.0,
                "fee": 0.01,
                "entry_iv_regime": "normal",
                "mode": "normal",
            },
            {
                "event": "TP_FILLED",
                "timestamp_seconds": 3.0,
                "side": "sell",
                "price": 101.0,
                "amount": 1.0,
                "quote_notional": 101.0,
                "gross_pnl": 1.0,
                "fee": 0.0101,
                "net_cycle_pnl": 0.9799,
                "holding_time_seconds": 1.0,
                "entry_iv_regime": "normal",
            },
        ],
        ticks=[
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "position_base": 0,
                "net_pnl": 0,
                "drawdown": 0,
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "position_base": 1,
                "net_pnl": -0.01,
                "drawdown": 0.01,
            },
            {
                "timestamp": "2026-01-01T00:00:03Z",
                "position_base": 0,
                "net_pnl": 0.9799,
                "drawdown": 0,
            },
        ],
    )
    summary = summarize_replay(result)
    assert summary["entry_fills"] == 1
    assert summary["completed_grid_cycles"] == 1
    assert summary["fees"] == 0.0201
    assert summary["maximum_drawdown"] == 0.01
    assert summary["performance_by_iv_regime"][0]["iv_regime"] == "normal"


def test_plan_stability_emits_new_keep_refresh_and_removed() -> None:
    def frame(second: int, price: float, levels: list[dict[str, object]]) -> EvaluationFrame:
        timestamp = f"2026-01-01T00:00:{second:02d}Z"
        return EvaluationFrame(
            timestamp=timestamp,
            timestamp_seconds=second,
            snapshot={"timestamp": timestamp},
            state={},
            mode={"mode": "normal"},
            plan={"buy_levels": levels, "sell_levels": [], "total_grid_width_pct": 0.01},
        )

    levels = [
        {"side": "buy", "level_index": 0, "theoretical_price": 99.0, "quote_amount": 100.0}
    ]
    moved = [{"side": "buy", "level_index": 0, "theoretical_price": 100.0, "quote_amount": 100.0}]
    result = plan_stability(
        [frame(0, 99.0, levels), frame(5, 99.0, levels), frame(40, 100.0, moved)]
    )
    assert result["actions"]["new"] == 1
    assert result["actions"]["keep"] == 1
    assert result["actions"]["refresh"] == 1
