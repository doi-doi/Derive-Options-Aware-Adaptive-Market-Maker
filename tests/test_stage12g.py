"""Stage 12G lifecycle, suppression, and reservation diagnostics."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).parents[1] / "src"
INTEGRATION_PATH = Path(__file__).parents[1] / "integrations" / "hummingbot"
for path in (SRC_PATH, INTEGRATION_PATH):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from derive_adaptive_grid.execution_logic import TradingRuleView  # noqa: E402

from derive_options_mm.multi_asset import (  # noqa: E402
    PortfolioRiskGovernor,
    PortfolioRiskSettings,
)
from derive_options_mm.shadow import (  # noqa: E402
    ShadowConfig,
    ShadowExecutionEngine,
    ShadowMarketFrame,
    ShadowOrderStatus,
)
from derive_options_mm.stage12g import (  # noqa: E402
    build_level_return_analysis,
    build_min_exchange_size_audit,
    build_order_funnel,
    build_order_state_transitions,
    build_pause_episode_breakdown,
    build_plan_invalid_transitions,
    build_resting_lifetime,
    build_zero_lifetime_root_causes,
)


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _config(tmp_path: Path, **overrides: object) -> ShadowConfig:
    values: dict[str, object] = {
        "enabled": True,
        "event_path": str(tmp_path / "events.jsonl"),
        "sqlite_path": str(tmp_path / "shadow.sqlite3"),
        "report_root": str(tmp_path / "reports"),
    }
    values.update(overrides)
    return ShadowConfig(**values)


def _frame(
    timestamp: float,
    *,
    trading_pair: str = "ETH-USDC",
    best_bid: float = 98.0,
    best_ask: float = 100.0,
    min_order_size: str = "0.01",
    min_notional_size: str = "0",
) -> ShadowMarketFrame:
    return ShadowMarketFrame(
        timestamp=timestamp,
        trading_pair=trading_pair,
        environment="mainnet",
        best_bid=best_bid,
        best_ask=best_ask,
        rule=TradingRuleView(
            min_order_size=Decimal(min_order_size),
            min_notional_size=Decimal(min_notional_size),
            min_price_increment=Decimal("0.01"),
            min_base_amount_increment=Decimal("0.01"),
        ),
    )


def _plan(
    timestamp: float,
    *,
    pair: str = "ETH-USDC",
    enabled: bool = True,
    valid: bool = True,
    mode: str = "normal",
    version: int = 1,
    buys: list[dict[str, object]] | None = None,
    sells: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "timestamp": _timestamp(timestamp),
        "trading_pair": pair,
        "mode": mode,
        "enabled": enabled,
        "valid": valid,
        "plan_version": version,
        "plan_change_significant": False,
        "center_price": "99",
        "total_grid_width_pct": "0.04",
        "buy_levels": buys
        if buys is not None
        else [
            {
                "side": "buy",
                "level_index": 0,
                "theoretical_price": "98",
                "quote_amount": "10",
            }
        ],
        "sell_levels": sells
        if sells is not None
        else [
            {
                "side": "sell",
                "level_index": 0,
                "theoretical_price": "100.5",
                "quote_amount": "10",
            }
        ],
    }


def _plan_row(
    epoch: float,
    *,
    valid: bool,
    desired_level_ids: list[str],
    reason: str = "NONE",
    pair: str = "ETH-USDC",
    removed_level_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "timestamp": _timestamp(epoch),
        "timestamp_epoch": epoch,
        "trading_pair": pair,
        "plan_version": int(epoch) + 1,
        "plan_valid": valid,
        "is_paused": not valid,
        "primary_reason": reason,
        "reason_category": reason,
        "reason": reason,
        "pause_reason": reason if not valid else None,
        "desired_level_ids": desired_level_ids,
        "removed_level_ids": removed_level_ids or [],
        "added_level_ids": [],
        "stop_count": len(removed_level_ids or []),
    }


def test_shadow_resting_state_machine_needs_no_exchange_ack(tmp_path: Path) -> None:
    engine = ShadowExecutionEngine(_config(tmp_path), session_id="state-machine")
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=98.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=97.0,
        best_ask=99.0,
    )

    transitions = build_order_state_transitions([order.to_record()], list(engine.lifecycle_events))

    assert order.status is ShadowOrderStatus.RESTING
    assert order.lifecycle_state_sequence == ["CREATED", "VALIDATED", "RESTING"]
    assert [(row["from_state"], row["to_state"]) for row in transitions] == [
        ("CREATED", "VALIDATED"),
        ("VALIDATED", "RESTING"),
    ]
    assert all(row["transition_allowed"] for row in transitions)
    assert engine.real_exchange_mutation_calls == 0


def test_same_frame_plan_removal_is_traced_as_resting_cancel(tmp_path: Path) -> None:
    engine = ShadowExecutionEngine(_config(tmp_path), session_id="same-frame")
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=98.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=97.0,
        best_ask=99.0,
    )
    engine.cancel_order(
        order.shadow_order_id,
        timestamp=100.0,
        reason="level removed",
        reason_code="PLAN_LEVEL_REMOVED",
        decision_context={
            "plan_valid": False,
            "plan_enabled": False,
            "new_level_present": False,
            "new_mode": "pause",
        },
    )

    zero_rows = build_zero_lifetime_root_causes([order.to_record()])

    assert order.lifecycle_state_sequence == [
        "CREATED",
        "VALIDATED",
        "RESTING",
        "CANCELLED_AFTER_RESTING",
    ]
    assert zero_rows[0]["zero_lifetime_root_cause"] == "RECONCILIATION_CANCEL_SAME_FRAME"
    assert zero_rows[0]["same_cycle_create_cancel"] is True


def test_immediate_pause_cancels_unfilled_entries_without_creating_more(tmp_path: Path) -> None:
    engine = ShadowExecutionEngine(_config(tmp_path), session_id="pause")
    normal = _plan(100.0)
    engine.reconcile_pair(normal, frame=_frame(100.0), cycle_id="cycle-1")
    assert len(engine.orders) == 2

    paused = _plan(101.0, enabled=False, mode="pause", version=2)
    result = engine.reconcile_pair(paused, frame=_frame(101.0), cycle_id="cycle-2")

    assert result.pause_reason == "GridPlan PAUSE"
    assert result.creates == []
    assert all(order.status is ShadowOrderStatus.CANCELLED for order in engine.orders.values())
    assert len(engine.orders) == 2


def test_keep_preserves_resting_timestamp_and_does_not_duplicate(tmp_path: Path) -> None:
    engine = ShadowExecutionEngine(_config(tmp_path), session_id="keep")
    plan = _plan(100.0)
    engine.reconcile_pair(plan, frame=_frame(100.0), cycle_id="cycle-1")
    initial = {
        order.level_id: order.resting_start_timestamp
        for order in engine.orders.values()
        if not order.is_exit
    }

    engine.reconcile_pair(plan, frame=_frame(105.0), cycle_id="cycle-2")
    keep_rows = [
        row for row in engine.order_eligibility_audit if row.get("planned_action") == "KEEP"
    ]

    assert len(engine.orders) == 2
    assert len(keep_rows) == 2
    assert all(
        row["resting_start_timestamp_before"] == row["resting_start_timestamp_after"]
        for row in keep_rows
    )
    assert {
        order.level_id: order.resting_start_timestamp
        for order in engine.orders.values()
        if not order.is_exit
    } == initial


def test_pending_reservation_is_not_counted_as_an_active_executor_twice() -> None:
    governor = PortfolioRiskGovernor(
        PortfolioRiskSettings(
            portfolio_max_gross_notional=500,
            portfolio_soft_beta_exposure=150,
            portfolio_hard_beta_exposure=200,
            portfolio_max_long_beta_exposure=200,
            portfolio_max_short_beta_exposure=200,
            per_asset_max_position_notional=200,
        )
    )
    decision = governor.evaluate(
        timestamp="2026-01-01T00:00:00Z",
        pending_entries={
            "ETH-USDC": {"buy": 70, "sell": 0, "count": 1},
            "SOL-USDC": {"buy": 70, "sell": 0, "count": 1},
        },
        active_executors={"ETH-USDC": 0, "SOL-USDC": 0},
        proposed_entries={},
    )

    assert decision.pending_executor_count == 2
    assert decision.active_executor_input_count == 0
    assert decision.active_pending_executor_overlap_count == 0
    assert decision.pre_proposal_active_executors == 2
    assert decision.active_executors == 2
    assert decision.gross_notional == pytest.approx(140)


def test_plan_invalid_transition_and_level_return_are_measured() -> None:
    rows = build_plan_invalid_transitions(
        [
            _plan_row(100.0, valid=True, desired_level_ids=["buy_0"]),
            _plan_row(
                110.0,
                valid=False,
                desired_level_ids=[],
                reason="STRATEGY_REGIME",
                removed_level_ids=["buy_0"],
            ),
            _plan_row(112.0, valid=True, desired_level_ids=["buy_0"]),
        ]
    )
    levels = build_level_return_analysis(rows, end_timestamp=120.0)

    assert [row["transition"] for row in rows] == [
        "INITIAL_VALID",
        "VALID_TO_INVALID",
        "INVALID_TO_VALID",
    ]
    assert len(levels) == 1
    assert levels[0]["same_level_returned"] is True
    assert levels[0]["absence_duration_seconds"] == pytest.approx(2.0)
    assert levels[0]["return_within_5s"] is True


def test_pause_episode_breakdown_deduplicates_asset_rows_and_adds_portfolio_scope() -> None:
    rows = []
    for pair in ("ETH-USDC", "SOL-USDC"):
        for epoch in (100.0, 105.0, 110.0, 115.0):
            rows.append(
                _plan_row(
                    epoch,
                    valid=False,
                    desired_level_ids=[],
                    reason="PORTFOLIO_RISK",
                    pair=pair,
                    removed_level_ids=["buy_0", "sell_0"],
                )
            )
        rows.append(
            _plan_row(
                116.0,
                valid=True,
                desired_level_ids=["buy_0", "sell_0"],
                pair=pair,
            )
        )

    episodes = build_pause_episode_breakdown(
        rows,
        start_timestamp=100.0,
        end_timestamp=120.0,
        continuity_gap_seconds=15.0,
    )
    asset = [row for row in episodes if row["pause_scope"] == "ASSET"]
    portfolio = [row for row in episodes if row["pause_scope"] == "PORTFOLIO"]

    assert len(asset) == 2
    assert len(portfolio) == 1
    assert sum(row["raw_pause_observation_count"] for row in asset) == 8
    assert portfolio[0]["affected_level_count"] == 4
    assert all(row["transient_pause_oscillation"] for row in asset)


def test_minimum_size_audit_reports_below_rule_without_changing_sizing() -> None:
    frame = _frame(
        100.0,
        min_order_size="0.1",
        min_notional_size="10",
    )
    eligibility = [
        {
            "trading_pair": "ETH-USDC",
            "desired_notional": 5.0,
            "quantized_price": 100.0,
            "quantized_amount": 0.05,
            "blocked_reason": "amount below exchange minimum",
        }
    ]

    rows = build_min_exchange_size_audit([frame], eligibility, {"enabled_markets": ("ETH-USDC",)})
    eth = next(row for row in rows if row["asset"] == "ETH-USDC")

    assert eth["minimum_amount"] == pytest.approx(0.1)
    assert eth["minimum_notional"] == pytest.approx(10.0)
    assert eth["status"] == "CURRENT_SIZE_BELOW_MINIMUM"
    assert eth["executable"] is False


def test_funnel_and_resting_lifetime_keep_zero_lifetime_out_of_sample() -> None:
    orders = [
        {
            "shadow_order_id": "zero",
            "trading_pair": "ETH-USDC",
            "level_id": "buy_0",
            "side": "buy",
            "status": "CANCELLED",
            "is_exit": False,
            "created_timestamp": _timestamp(100.0),
            "resting_start_timestamp": _timestamp(100.0),
            "terminal_timestamp": _timestamp(100.0),
            "lifecycle_state_sequence": [
                "CREATED",
                "VALIDATED",
                "RESTING",
                "CANCELLED_AFTER_RESTING",
            ],
            "same_cycle_create_cancel": True,
            "terminal_reason": "RECONCILIATION_CANCEL_SAME_FRAME",
            "create_terminal_latency_ms": 0.0,
        },
        {
            "shadow_order_id": "measured",
            "trading_pair": "ETH-USDC",
            "level_id": "sell_0",
            "side": "sell",
            "status": "CANCELLED",
            "is_exit": False,
            "created_timestamp": _timestamp(100.0),
            "resting_start_timestamp": _timestamp(100.0),
            "terminal_timestamp": _timestamp(110.0),
            "lifecycle_state_sequence": [
                "CREATED",
                "VALIDATED",
                "RESTING",
                "CANCELLED_AFTER_RESTING",
            ],
            "same_cycle_create_cancel": False,
            "terminal_reason": "MAXIMUM_ORDER_AGE",
            "create_terminal_latency_ms": 10_000.0,
        },
    ]
    funnel = build_order_funnel(orders, [{"candidate_grid_level": True, "risk_allowed": True}])
    _lifetime_rows, lifetime = build_resting_lifetime(orders, end_timestamp=120.0)

    assert next(row["count"] for row in funnel if row["stage"] == "entered_resting") == 2
    assert next(row["count"] for row in funnel if row["stage"] == "stayed_resting_ge_1s") == 1
    assert lifetime["resting_orders"] == 2
    assert lifetime["evidence_sample_count"] == 1
    assert lifetime["excluded_zero_or_same_frame"] == 1
