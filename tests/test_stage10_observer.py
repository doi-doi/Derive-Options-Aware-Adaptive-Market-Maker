"""Phase 1 bounded self-tuning observer tests."""

from __future__ import annotations

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation.replay import ReplayResult  # noqa: E402
from evaluation.self_tuning_observer import (  # noqa: E402
    UNKNOWN,
    ObserverConfig,
    PerformanceObserver,
)


def _event(timestamp: float, event: str, **fields: object) -> dict[str, object]:
    return {"timestamp_seconds": timestamp, "event": event, **fields}


def test_missing_execution_journal_keeps_live_lifecycle_unknown() -> None:
    observer = PerformanceObserver(ObserverConfig(evaluation_window_minutes=30))
    observation = observer.observe(
        [],
        state_records=[
            {"timestamp_seconds": 100.0, "trading_pair": "SOL-USDC", "inventory_ratio": 0.2},
            {"timestamp_seconds": 110.0, "trading_pair": "SOL-USDC", "inventory_ratio": -0.4},
        ],
        portfolio_records=[
            {
                "timestamp_seconds": 110.0,
                "btc_beta_equivalent_exposure": 15.0,
            }
        ],
        asset="SOL-USDC",
        event_source_status="missing",
        state_source_status="ok",
        portfolio_source_status="ok",
        end_timestamp=110.0,
    )

    window = observation.window
    assert window.orders_created is None
    assert window.fills is None
    assert window.markout_30s is None
    assert window.relationship_regime == UNKNOWN
    assert math.isclose(window.inventory_ratio_mean or 0.0, 0.3)
    assert window.inventory_ratio_max == 0.4
    assert window.portfolio_beta_exposure_max == 15.0
    assert window.metric_status["orders_created"] == UNKNOWN
    assert any("execution journal" in reason for reason in window.reasons)


def test_live_event_metrics_are_bounded_and_markout_has_no_lookahead() -> None:
    events = [
        _event(30.0, "CREATE_REQUEST", order_id="o1", quote_amount=100.0),
        _event(35.0, "ENTRY_KEEP", order_id="o1"),
        _event(
            40.0,
            "ENTRY_CANCELLED",
            order_id="o1",
            lifetime_seconds=10.0,
            reason_code="PRICE_DEVIATION",
        ),
        _event(42.0, "CREATE_REQUEST", order_id="o2", quote_amount=100.0),
        _event(
            45.0,
            "ENTRY_FILLED",
            order_id="o2",
            quote_notional=100.0,
            markout_5s_bps=1.0,
            markout_30s_bps=-5.0,
            markout_60s_bps=-8.0,
        ),
        _event(
            55.0,
            "TP_FILLED",
            order_id="tp-o2",
            quote_notional=100.0,
            gross_pnl=2.0,
            net_cycle_pnl=1.9,
            fee=0.1,
        ),
        _event(60.0, "TICK", unrealized_pnl=0.5),
    ]
    observer = PerformanceObserver(
        ObserverConfig(
            evaluation_window_minutes=30,
            minimum_order_events=1,
            minimum_fills_for_fill_metrics=1,
            minimum_completed_cycles_for_capture_metrics=1,
        )
    )
    observation = observer.observe(
        events,
        state_records=[{"timestamp_seconds": 55.0, "inventory_ratio": 0.4}],
        portfolio_records=[{"timestamp_seconds": 55.0, "btc_beta_equivalent_exposure": 12.0}],
        plan_records=[{"timestamp_seconds": 55.0, "mode": "normal"}],
        asset="SOL-USDC",
        event_source_status="ok",
        state_source_status="ok",
        portfolio_source_status="ok",
        end_timestamp=60.0,
    )

    window = observation.window
    assert window.orders_created == 2
    assert window.orders_cancelled == 1
    assert window.orders_kept == 1
    assert window.fills == 1
    assert window.completed_cycles == 1
    assert window.cancel_create_ratio == 0.5
    assert window.fill_create_ratio == 0.5
    assert window.median_order_lifetime == 6.5
    assert window.markout_5s == 1.0
    assert window.markout_30s is None
    assert window.markout_60s is None
    assert window.realized_pnl == 1.9
    assert window.unrealized_pnl == 0.5
    assert window.total_pnl == 2.4
    assert window.fees_if_known == 0.1
    assert window.metric_status["markout_30s"] == UNKNOWN


def test_live_controller_lifecycle_uses_confirmed_stop_and_executor_ids() -> None:
    events = [
        _event(100.0, "CREATE_REQUEST", level_id="buy_0"),
        _event(101.0, "CREATE_SUCCESS", executor_id="exec-1", level_id="buy_0"),
        _event(110.0, "STOP_REQUEST", executor_id="exec-1", reason="stale"),
        _event(112.0, "STOP_SUCCESS", executor_id="exec-1", close_type="STOPPED"),
    ]
    observation = PerformanceObserver(
        ObserverConfig(evaluation_window_minutes=30, minimum_order_events=1)
    ).observe(events, event_source_status="ok", end_timestamp=112.0)

    assert observation.window.orders_created == 1
    assert observation.window.orders_cancelled == 1
    assert observation.window.mean_order_lifetime == 11.0


def test_replay_observation_reuses_existing_metrics_and_is_labeled() -> None:
    result = ReplayResult(
        strategy="synthetic",
        fill_model="conservative_cross_through",
        events=[
            _event(10.0, "ENTRY_CREATED", order_id="entry-1", quote_notional=100.0),
            _event(
                20.0,
                "ENTRY_FILLED",
                order_id="entry-1",
                side="buy",
                price=100.0,
                amount=1.0,
                quote_notional=100.0,
                markout_5s_bps=1.0,
                markout_30s_bps=2.0,
                markout_60s_bps=3.0,
                fee=0.1,
            ),
            _event(
                40.0,
                "TP_FILLED",
                order_id="tp-1",
                quote_notional=100.0,
                gross_pnl=2.0,
                net_cycle_pnl=1.9,
                fee=0.1,
            ),
        ],
        ticks=[
            {
                "timestamp_seconds": 10.0,
                "mid_price": 100.0,
                "position_base": 0.0,
                "inventory_ratio": 0.0,
                "unrealized_pnl": 0.0,
                "fees": 0.0,
                "drawdown": 0.0,
                "mode": "normal",
            },
            {
                "timestamp_seconds": 50.0,
                "mid_price": 101.0,
                "position_base": 0.0,
                "inventory_ratio": 0.0,
                "unrealized_pnl": 0.0,
                "fees": 0.2,
                "drawdown": 1.0,
                "mode": "normal",
            },
        ],
    )
    window = PerformanceObserver().observe_replay(result).window

    assert window.evidence_source == "SHADOW_REPLAY"
    assert window.orders_created == 1
    assert window.fills == 1
    assert window.completed_cycles == 1
    assert window.realized_pnl == 1.8
    assert window.markout_5s == 1.0
    assert window.markout_30s == 2.0
    assert window.markout_60s is None
    assert any("replay evidence" in reason for reason in window.reasons)


def test_phase_a_volume_metrics_use_fills_and_time_weighted_risk_only() -> None:
    events = [
        _event(0.0, "CREATE_REQUEST", order_id="entry-1", level_id="buy_0", quote_amount=100.0),
        _event(0.0, "CREATE_REQUEST", order_id="entry-2", level_id="sell_0", quote_amount=100.0),
        _event(2.0, "ENTRY_KEEP", order_id="entry-1", level_id="buy_0"),
        _event(
            5.0,
            "ENTRY_CANCELLED",
            order_id="entry-1",
            level_id="buy_0",
            lifetime_seconds=5.0,
        ),
        _event(
            10.0,
            "ENTRY_FILLED",
            order_id="entry-2",
            level_id="sell_0",
            side="buy",
            quote_notional=100.0,
            fee=0.1,
            markout_5s_bps=1.0,
            markout_30s_bps=2.0,
            markout_60s_bps=3.0,
        ),
        _event(
            70.0,
            "TP_FILLED",
            position_id="entry-2:position",
            level_id="sell_0",
            side="sell",
            quote_notional=102.0,
            gross_pnl=2.0,
            net_cycle_pnl=1.8,
            fee=0.1,
        ),
    ]
    states = [
        _event(0.0, "TICK", deployed_notional=0.0, position_notional=0.0, position_base=0.0),
        _event(10.0, "TICK", deployed_notional=120.0, position_notional=20.0, position_base=0.2),
        _event(70.0, "TICK", deployed_notional=0.0, position_notional=0.0, position_base=0.0),
    ]
    portfolio = [
        _event(0.0, "RISK", gross_notional=100.0, btc_beta_equivalent_exposure=10.0),
        _event(10.0, "RISK", gross_notional=120.0, btc_beta_equivalent_exposure=12.0),
        _event(70.0, "RISK", gross_notional=0.0, btc_beta_equivalent_exposure=0.0),
    ]
    observation = PerformanceObserver(
        ObserverConfig(
            evaluation_window_minutes=30,
            minimum_order_events=1,
            minimum_fills_for_fill_metrics=1,
            minimum_completed_cycles_for_capture_metrics=1,
        )
    ).observe(
        events,
        state_records=states,
        portfolio_records=portfolio,
        asset="ALL",
        event_source_status="ok",
        state_source_status="ok",
        portfolio_source_status="ok",
        end_timestamp=70.0,
    )

    metrics = observation.volume_efficiency
    assert metrics is not None
    assert metrics.executed_buy_notional == 100.0
    assert metrics.executed_sell_notional == 102.0
    assert metrics.executed_total_notional == 202.0
    assert metrics.orders_created == 2
    assert metrics.orders_cancelled == 1
    assert metrics.fill_create_ratio == 0.5
    assert metrics.cancel_create_ratio == 0.5
    assert metrics.completed_cycles == 1
    assert metrics.mean_cycle_duration_seconds == 60.0
    assert metrics.median_quote_lifetime == 7.5
    assert metrics.markout_30s == 2.0
    assert metrics.realized_grid_capture == 2.0
    assert metrics.fees == 0.2
    assert metrics.average_gross_exposure is not None
    assert metrics.average_gross_exposure > 100.0
    assert metrics.average_absolute_inventory is not None
    assert metrics.average_absolute_inventory > 0.0
    assert metrics.volume_per_average_gross_exposure is not None


def test_phase_a_marks_incomplete_fill_notional_unknown_and_ignores_messages() -> None:
    events = [
        _event(0.0, "CREATE_REQUEST", order_id="entry-1", quote_amount=1000.0),
        _event(1.0, "CREATE_SUCCESS", order_id="entry-1", quote_amount=1000.0),
        _event(2.0, "STOP_REQUEST", order_id="entry-1"),
        _event(3.0, "STOP_SUCCESS", order_id="entry-1"),
        _event(4.0, "SELF_TRADE", quote_notional=999999.0),
        _event(5.0, "ENTRY_FILLED", order_id="entry-2", side="buy"),
        _event(
            6.0,
            "POSITION_EXITED",
            executor_id="entry-2",
            close_type="TAKE_PROFIT",
            net_pnl_quote=1.0,
        ),
    ]
    metrics = PerformanceObserver(
        ObserverConfig(evaluation_window_minutes=30, minimum_order_events=1)
    ).observe(
        events,
        event_source_status="ok",
        end_timestamp=6.0,
    ).volume_efficiency

    assert metrics is not None
    assert metrics.orders_created == 1
    assert metrics.orders_cancelled == 1
    assert metrics.executed_fill_count == 2
    assert metrics.executed_total_notional is None
    assert metrics.missing_fill_notional_count == 2
    assert metrics.completed_cycles == 1
    assert metrics.metric_status["executed_total_notional"] == UNKNOWN
    assert any("lack executed notional" in reason for reason in metrics.reasons)


def test_phase_a_markout_respects_elapsed_horizon() -> None:
    events = [
        _event(0.0, "CREATE_REQUEST", order_id="entry-1", level_id="buy_0"),
        _event(
            10.0,
            "ENTRY_FILLED",
            order_id="entry-1",
            level_id="buy_0",
            side="buy",
            quote_notional=100.0,
            markout_5s_bps=1.0,
            markout_30s_bps=2.0,
            markout_60s_bps=3.0,
        ),
    ]
    metrics = PerformanceObserver(
        ObserverConfig(evaluation_window_minutes=30, minimum_order_events=1)
    ).observe(events, event_source_status="ok", end_timestamp=30.0).volume_efficiency

    assert metrics is not None
    assert metrics.markout_5s == 1.0
    assert metrics.markout_30s is None
    assert metrics.markout_60s is None
