"""Focused regression tests for Stage 12C shadow observability."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from derive_options_mm.shadow import (  # noqa: E402
    ShadowConfig,
    ShadowExecutionEngine,
    ShadowMarketFrame,
)
from derive_options_mm.stage12c import (  # noqa: E402
    CANCEL_TAXONOMY,
    FILL_ELIGIBILITY_STATUSES,
    LIFECYCLE_EVENTS,
    REPLACEMENT_DEVIATION_BUCKETS,
    RESTING_LIFETIME_BUCKETS,
    RiskEpisodeTracker,
    calculate_trade_coverage,
    classify_cancel_reason,
    replacement_deviation_bucket,
    resting_lifetime_bucket,
)
from integrations.hummingbot.derive_adaptive_grid.execution_logic import (  # noqa: E402
    TradingRuleView,
)


def test_cancel_taxonomy_is_exact_and_unknown_is_not_other() -> None:
    assert CANCEL_TAXONOMY == (
        "PRICE_DEVIATION",
        "AMOUNT_DEVIATION",
        "MODE_CHANGE",
        "PLAN_LEVEL_REMOVED",
        "PLAN_DISABLED",
        "PLAN_STALE",
        "MAX_AGE",
        "MIN_LIFETIME_SAFETY_OVERRIDE",
        "REPLACEMENT_COOLDOWN_OVERRIDE",
        "POST_ONLY_SAFETY",
        "WOULD_CROSS_MARKET",
        "STALE_MARKET_DATA",
        "STALE_GLOBAL_RISK",
        "ACCOUNT_STATE_INVALID",
        "ASSET_INVENTORY_RISK",
        "PORTFOLIO_GROSS_RISK",
        "PORTFOLIO_BETA_RISK",
        "DRAWDOWN_RISK",
        "PAUSE",
        "CONFIG_CHANGE",
        "SESSION_SHUTDOWN",
        "MANUAL_STOP",
        "EXECUTOR_STATE_CHANGE",
        "FILL_TRANSITION",
        "UNKNOWN_INTERNAL",
    )
    assert classify_cancel_reason("unrecognised internal condition") == "UNKNOWN_INTERNAL"
    assert "OTHER" not in CANCEL_TAXONOMY
    assert set(FILL_ELIGIBILITY_STATUSES) == {
        "TRADED_THROUGH_FILLED",
        "TRADE_THROUGH_OBSERVED_NO_FILL",
        "TOUCHED_FILLED",
        "TOUCHED_NOT_TRADED_THROUGH",
        "NEVER_REACHED_PRICE",
        "INSUFFICIENT_TRADE_EVIDENCE",
    }
    assert set(LIFECYCLE_EVENTS) == {
        "ORDER_CREATED",
        "ORDER_RESTING",
        "ORDER_KEEP",
        "ORDER_REPLACE_DEFERRED",
        "ORDER_CANCEL_REQUESTED",
        "ORDER_CANCELLED",
        "ORDER_FILLED",
        "ORDER_TP_CREATED",
        "ORDER_COMPLETE",
    }


def test_stage12c_distribution_buckets_use_the_requested_boundaries() -> None:
    assert REPLACEMENT_DEVIATION_BUCKETS == (
        "<2bps",
        "2-5bps",
        "5-8bps",
        "8-12bps",
        "12-20bps",
        "20+bps",
        "UNKNOWN",
    )
    assert [
        replacement_deviation_bucket(value)
        for value in (1, 2, 4.999, 5, 7.999, 8, 11.999, 12, 20, 20.001, None)
    ] == [
        "<2bps",
        "2-5bps",
        "2-5bps",
        "5-8bps",
        "5-8bps",
        "8-12bps",
        "8-12bps",
        "12-20bps",
        "12-20bps",
        "20+bps",
        "UNKNOWN",
    ]
    assert RESTING_LIFETIME_BUCKETS == ("<5s", "5-30s", "30-60s", "1-2m", "2-5m", "5-15m", ">15m")
    assert [
        resting_lifetime_bucket(value)
        for value in (4.999, 5, 29.999, 30, 59.999, 60, 119.999, 120, 299.999, 300, 900, 900.001)
    ] == [
        "<5s",
        "5-30s",
        "5-30s",
        "30-60s",
        "30-60s",
        "1-2m",
        "1-2m",
        "2-5m",
        "2-5m",
        "5-15m",
        "5-15m",
        ">15m",
    ]


def test_risk_episodes_separate_raw_blocks_from_continuous_duration() -> None:
    tracker = RiskEpisodeTracker(continuity_gap_seconds=10.0)
    tracker.record_check(3)
    tracker.record(
        100.0,
        reason="portfolio beta risk",
        trading_pair="BTC-USDC",
        level_id="buy_0",
        side="buy",
        assets=["BTC-USDC"],
        context={"candidate": "buy_0"},
    )
    tracker.record(
        105.0,
        reason="portfolio beta risk",
        trading_pair="BTC-USDC",
        level_id="buy_0",
        side="buy",
    )
    tracker.record(
        120.0,
        reason="portfolio beta risk",
        trading_pair="BTC-USDC",
        level_id="buy_0",
        side="buy",
    )

    rows = tracker.rows(130.0)
    assert tracker.raw_blocks_total == 3
    assert tracker.risk_checks_total == 3
    assert len(rows) == 2
    assert [row["raw_block_count"] for row in rows] == [2, 1]
    assert [row["blocked_seconds"] for row in rows] == [5.0, 10.0]
    summary = tracker.summary(130.0)
    assert summary == [
        {
            "reason": "PORTFOLIO_BETA_RISK",
            "raw_blocks": 3,
            "unique_episodes": 2,
            "blocked_seconds": 15.0,
            "assets": ["BTC-USDC"],
        }
    ]


def test_trade_coverage_reports_gaps_without_implying_full_trade_history() -> None:
    coverage = calculate_trade_coverage(
        [0.0, 10.0, 10.0],
        start_timestamp=0.0,
        end_timestamp=20.0,
        sample_interval_seconds=5.0,
        trade_count=3,
        evidence_minutes=1,
    )
    assert coverage["expected_duration_seconds"] == 20.0
    assert coverage["covered_duration_seconds"] == 10.0
    assert coverage["coverage_pct"] == 50.0
    assert coverage["trade_count"] == 3
    assert coverage["gap_count"] == 2
    assert coverage["max_gap_seconds"] == 5.0
    assert coverage["median_gap_seconds"] == 5.0


def test_order_lifecycle_uses_resting_start_and_persists_decision_context(
    tmp_path: Path,
) -> None:
    config = ShadowConfig(
        enabled=True,
        fee_model="unknown",
        event_path=str(tmp_path / "events.jsonl"),
        sqlite_path=str(tmp_path / "shadow.sqlite3"),
        report_root=str(tmp_path / "reports"),
    )
    engine = ShadowExecutionEngine(config, session_id="stage12c-lifecycle")
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=99.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=98.0,
        best_ask=100.0,
    )
    engine.cancel_order(
        order.shadow_order_id,
        timestamp=103.0,
        reason="grid price moved",
        reason_code="PRICE_DEVIATION",
        market_mid=100.0,
        market_best_bid=99.9,
        market_best_ask=100.1,
        decision_context={
            "old_price": 99.0,
            "new_desired_price": 98.5,
            "old_amount": 0.1,
            "new_desired_amount": 0.11,
            "amount_deviation_pct": 0.1,
            "old_mode": "normal",
            "new_mode": "defensive",
            "old_plan_version": 4,
            "new_plan_version": 5,
            "old_level_present": True,
            "new_level_present": True,
            "risk_state": "NORMAL",
            "inventory_ratio": 0.2,
            "portfolio_gross_exposure": 100.0,
            "portfolio_beta_exposure": 80.0,
            "minimum_order_lifetime_seconds": 60.0,
            "replacement_cooldown_seconds": 30.0,
            "time_since_last_replace_seconds": 100.0,
            "cooldown_remaining_seconds": 0.0,
            "decision_path": "test",
        },
    )

    assert order.lifecycle_state == "CANCELLED_AFTER_RESTING"
    assert order.resting_start_epoch == 100.0
    assert order.terminal_epoch == 103.0
    assert order.cancel_reason_category == "PRICE_DEVIATION"
    assert order.cancel_market_best_bid == 99.9
    assert order.cancel_market_best_ask == 100.1
    assert '"new_plan_version": 5' in (order.cancel_reason_detail or "")
    cancelled = next(
        event for event in engine.lifecycle_events if event["event"] == "ORDER_CANCELLED"
    )
    assert cancelled["resting_lifetime_seconds"] == 3.0
    assert [
        event["event"]
        for event in engine.lifecycle_events
        if event.get("shadow_order_id") == order.shadow_order_id
    ] == [
        "ORDER_CREATED",
        "ORDER_RESTING",
        "ORDER_CANCEL_REQUESTED",
        "ORDER_CANCELLED",
    ]


def test_crossed_maker_order_is_excluded_from_resting_lifetime(tmp_path: Path) -> None:
    config = ShadowConfig(
        enabled=True,
        event_path=str(tmp_path / "events.jsonl"),
        sqlite_path=str(tmp_path / "shadow.sqlite3"),
        report_root=str(tmp_path / "reports"),
    )
    engine = ShadowExecutionEngine(config, session_id="stage12c-rejected")
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=100.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=99.0,
        best_ask=100.0,
    )
    metrics = engine.metrics(now=110.0)
    assert order.lifecycle_state == "NEVER_RESTED_REJECTED"
    assert metrics["resting_lifetime_sample_count"] == 0
    assert metrics["resting_lifetime_excluded_never_rested"] == 1
    assert metrics["lifecycle_state_counts"]["NEVER_RESTED_REJECTED"] == 1


def test_material_plan_replacement_keeps_quantized_desired_level_context(
    tmp_path: Path,
) -> None:
    config = ShadowConfig(
        enabled=True,
        minimum_order_lifetime_seconds=0.0,
        minimum_replace_interval_seconds=0.0,
        event_path=str(tmp_path / "events.jsonl"),
        sqlite_path=str(tmp_path / "shadow.sqlite3"),
        report_root=str(tmp_path / "reports"),
    )
    engine = ShadowExecutionEngine(config, session_id="stage12c-replace")

    def plan(timestamp: float, price: float, version: int) -> dict[str, object]:
        return {
            "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat(),
            "trading_pair": "ETH-USDC",
            "mode": "normal",
            "enabled": True,
            "valid": True,
            "plan_version": version,
            "plan_change_significant": version > 1,
            "center_price": 100.0,
            "total_grid_width_pct": 0.1,
            "buy_levels": [
                {
                    "side": "buy",
                    "level_index": 0,
                    "theoretical_price": price,
                    "quote_amount": 10.0,
                }
            ],
            "sell_levels": [
                {
                    "side": "sell",
                    "level_index": 0,
                    "theoretical_price": 101.0,
                    "quote_amount": 10.0,
                }
            ],
        }

    frame = ShadowMarketFrame(
        timestamp=1000.0,
        trading_pair="ETH-USDC",
        environment="mainnet",
        best_bid=98.0,
        best_ask=100.0,
        rule=TradingRuleView(
            min_order_size=Decimal("0.01"),
            min_base_amount_increment=Decimal("0.01"),
            min_price_increment=Decimal("0.01"),
        ),
    )
    first = engine.reconcile_pair(plan(1000.0, 97.0, 1), frame=frame)
    assert len(first.creates) == 2
    buy_order = next(order for order in engine.orders.values() if order.level_id == "buy_0")

    moved = ShadowMarketFrame(
        timestamp=1001.0,
        trading_pair="ETH-USDC",
        environment="mainnet",
        best_bid=98.0,
        best_ask=100.0,
        rule=frame.rule,
    )
    second = engine.reconcile_pair(plan(1001.0, 96.0, 2), frame=moved)
    assert any(stop.executor_id == buy_order.shadow_order_id for stop in second.stops)
    assert buy_order.cancel_reason_category == "PRICE_DEVIATION"
    assert buy_order.new_desired_price == Decimal("96.00")
    assert buy_order.new_desired_amount == Decimal("0.10")
