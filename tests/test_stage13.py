"""Stage 13 bounded stability controls and audit tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).parents[1] / "src"
INTEGRATION_PATH = Path(__file__).parents[1] / "integrations" / "hummingbot"
for path in (SRC_PATH, INTEGRATION_PATH):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from derive_adaptive_grid.execution_logic import (  # noqa: E402
    ExecutionPolicy,
    RuntimeHealth,
    TradingRuleView,
    parse_grid_plan,
    reconcile_grid_plan,
)

from derive_options_mm.mode_selector import (  # noqa: E402
    GridMode,
    ModeSelector,
    ModeSelectorConfig,
)
from derive_options_mm.multi_asset import (  # noqa: E402
    PortfolioRiskGovernor,
    PortfolioRiskSettings,
    ProposedEntry,
    pair_level_id,
)
from derive_options_mm.stage13 import (  # noqa: E402
    Stage13StabilityConfig,
    build_create_decision_reconciliation,
    build_order_funnel,
    build_pause_hysteresis,
    build_quote_survival,
    effective_asset_status,
)
from derive_options_mm.state_engine import (  # noqa: E402
    DirectionState,
    InventoryState,
    MarketState,
    VolatilityState,
)


def _state(timestamp: float, *, extreme: bool = False, valid: bool = True) -> MarketState:
    return MarketState(
        timestamp=str(timestamp),
        trading_pair="BTC-USDC",
        volatility_state=VolatilityState.NORMAL,
        volatility_score=3.1 if extreme else 1.0,
        direction_state=DirectionState.NEUTRAL,
        direction_score=0.0,
        inventory_state=InventoryState.NEUTRAL,
        inventory_ratio=0.0,
        confidence=0.9,
        state_valid=valid,
    )


def test_stage13_requires_bounded_nonzero_hysteresis() -> None:
    with pytest.raises(ValueError):
        Stage13StabilityConfig(
            enabled=True,
            regime_pause_entry_confirm_seconds=4,
        )
    config = Stage13StabilityConfig(
        enabled=True,
        regime_pause_entry_confirm_seconds=5,
        regime_pause_exit_confirm_seconds=30,
    )
    assert config.regime_pause_entry_confirm_seconds == 5


def test_strategy_pause_is_delayed_but_data_pause_is_immediate() -> None:
    selector = ModeSelector(
        ModeSelectorConfig(
            mode_confirmation_samples=1,
            pause_recovery_samples=1,
            strategy_pause_entry_confirm_seconds=10,
            strategy_pause_exit_confirm_seconds=10,
        )
    )
    assert selector.update(_state(0)).mode is GridMode.NORMAL
    pending = selector.update(_state(1, extreme=True))
    assert pending.mode is GridMode.NORMAL
    assert pending.pause_candidate_active is True
    assert pending.pause_confirmed is False
    confirmed = selector.update(_state(11, extreme=True))
    assert confirmed.mode is GridMode.PAUSE
    assert confirmed.transition_occurred is True
    immediate = selector.update(_state(12, valid=False))
    assert immediate.mode is GridMode.PAUSE
    assert immediate.pause_active_category == "DATA_CRITICAL"


def test_incremental_pending_risk_audit_covers_create_keep_resize_and_release() -> None:
    governor = PortfolioRiskGovernor(
        PortfolioRiskSettings(
            portfolio_max_gross_notional=200,
            portfolio_soft_beta_exposure=140,
            portfolio_hard_beta_exposure=150,
            portfolio_max_long_beta_exposure=150,
            portfolio_max_short_beta_exposure=150,
            per_asset_max_position_notional=150,
            max_active_executors_per_asset=3,
            max_active_executors_portfolio=3,
        )
    )
    buy0 = ProposedEntry(
        trading_pair="SOL-USDC",
        level_id=pair_level_id("SOL-USDC", "buy", 0),
        side="buy",
        quote_notional=80,
    )
    buy1 = ProposedEntry(
        trading_pair="SOL-USDC",
        level_id=pair_level_id("SOL-USDC", "buy", 1),
        side="buy",
        quote_notional=50,
    )
    decision = governor.evaluate(
        timestamp="2026-01-01T00:00:00Z",
        pending_entries={"SOL-USDC": {"buy": 80, "sell": 40, "count": 2}},
        proposed_entries={"SOL-USDC": [buy0, buy1]},
        existing_entries={
                "SOL-USDC": {
                "SOL-USDC::buy_0": {"notional": 80, "side": "buy"},
                "SOL-USDC::sell_0": {"notional": 40, "side": "sell"},
            }
        },
        use_incremental_pending_exposure=True,
        betas={"SOL-USDC": 1.0},
    )
    actions = {row["level_id"]: row["action"] for row in decision.risk_delta_audit}
    deltas = {row["level_id"]: row["notional_delta"] for row in decision.risk_delta_audit}
    assert actions["SOL-USDC::buy_0"] == "KEEP"
    assert deltas["SOL-USDC::buy_0"] == pytest.approx(0)
    assert actions["SOL-USDC::buy_1"] == "CREATE"
    assert actions["SOL-USDC::sell_0"] == "CANCEL_RELEASE"
    assert deltas["SOL-USDC::sell_0"] == pytest.approx(-40)
    assert all(row["allowed"] for row in decision.risk_delta_audit)

    resize_up = governor.evaluate(
        timestamp="2026-01-01T00:00:05Z",
        pending_entries={"SOL-USDC": {"buy": 80, "sell": 0, "count": 1}},
        proposed_entries={"SOL-USDC": [buy0.model_copy(update={"quote_notional": 100})]},
        existing_entries={"SOL-USDC": {"SOL-USDC::buy_0": {"notional": 80, "side": "buy"}}},
        use_incremental_pending_exposure=True,
        betas={"SOL-USDC": 1.0},
    )
    resize_down = governor.evaluate(
        timestamp="2026-01-01T00:00:10Z",
        pending_entries={"SOL-USDC": {"buy": 100, "sell": 0, "count": 1}},
        proposed_entries={"SOL-USDC": [buy0.model_copy(update={"quote_notional": 60})]},
        existing_entries={"SOL-USDC": {"SOL-USDC::buy_0": {"notional": 100, "side": "buy"}}},
        use_incremental_pending_exposure=True,
        betas={"SOL-USDC": 1.0},
    )
    assert resize_up.risk_delta_audit[0]["action"] == "RESIZE_UP"
    assert resize_up.risk_delta_audit[0]["notional_delta"] == pytest.approx(20)
    assert resize_down.risk_delta_audit[0]["action"] == "RESIZE_DOWN"
    assert resize_down.risk_delta_audit[0]["notional_delta"] == pytest.approx(-40)


def test_signal_only_status_is_explicit_and_not_fallback_routed() -> None:
    config = Stage13StabilityConfig(
        enabled=True,
        asset_execution_status={
            "BTC-USDC": "SIGNAL_ONLY",
            "ETH-USDC": "SIGNAL_ONLY_MIN_SIZE",
        },
    )
    statuses = effective_asset_status(
        config,
        ("BTC-USDC", "ETH-USDC", "SOL-USDC"),
        ("BTC-USDC", "ETH-USDC", "SOL-USDC"),
    )
    assert statuses == {
        "BTC-USDC": "SIGNAL_ONLY",
        "ETH-USDC": "SIGNAL_ONLY_MIN_SIZE",
        "SOL-USDC": "EXECUTION_ENABLED",
    }


def test_pre_create_gate_blocks_before_create_and_survival_excludes_same_frame() -> None:
    plan = parse_grid_plan(
        {
            "timestamp": "1970-01-01T00:01:40Z",
            "trading_pair": "SOL-USDC",
            "mode": "normal",
            "enabled": True,
            "valid": True,
            "plan_version": 1,
            "plan_change_significant": False,
            "center_price": "100",
            "total_grid_width_pct": "0.04",
            "buy_levels": [
                {
                    "side": "buy",
                    "level_index": 0,
                    "theoretical_price": "98",
                    "quote_amount": "10",
                }
            ],
            "sell_levels": [],
        },
        expected_pair="SOL-USDC",
    )
    health = RuntimeHealth(
        testnet_verified=False,
        connector_ready=True,
        market_data_ready=True,
        trading_rules_available=True,
        balance_verified=True,
        position_verified=True,
        best_bid=99,
        best_ask=101,
        available_collateral=1000,
        trading_rules=TradingRuleView(),
        environment="mainnet",
        execution_mode="SHADOW",
        environment_verified=True,
        environment_consistent=True,
    )
    result = reconcile_grid_plan(
        plan,
        active=(),
        health=health,
        policy=ExecutionPolicy(
            testnet_order_scale=1,
            environment="mainnet",
            execution_mode="SHADOW",
        ),
        now_epoch=100,
        quantize_price=lambda value: value,
        quantize_amount=lambda value: value,
        final_create_gate=lambda _desired: (
            False,
            "PRE_CREATE_RISK_BLOCK",
            "final gate test block",
        ),
    )
    assert result.creates == []
    assert result.blocked[0].reason_code == "PRE_CREATE_RISK_BLOCK"
    rows = build_create_decision_reconciliation(
        [
            {
                "planned_action": "CREATE_DECISION",
                "raw_planned_action": "CREATE_DECISION",
                "asset_execution_status": "SIGNAL_ONLY",
                "order_instantiated": False,
            },
            {
                "planned_action": "ORDER_INSTANTIATED",
                "raw_planned_action": "CREATE_DECISION",
                "order_instantiated": True,
            },
        ]
    )
    assert rows["counts"]["SIGNAL_ONLY"] == 1
    assert rows["counts"]["INSTANTIATED"] == 1
    assert rows["unknown_internal"] == 0


def test_quote_survival_excludes_same_frame_and_counts_open_order() -> None:
    result = build_quote_survival(
        [
            {
                "shadow_order_id": "same",
                "is_exit": False,
                "created_epoch": 100,
                "resting_start_epoch": 100,
                "terminal_epoch": 100,
                "same_cycle_create_cancel": True,
            },
            {
                "shadow_order_id": "open",
                "is_exit": False,
                "created_epoch": 100,
                "resting_start_epoch": 100,
                "terminal_epoch": None,
            },
        ],
        end_timestamp=110,
    )
    assert result["same_frame_excluded"] == 1
    assert result["evidence_sample_count"] == 1
    assert result["counts"]["stayed_resting_ge_5s"] == 1


def test_order_funnel_counts_blocked_raw_create_decisions() -> None:
    rows = [
        {
            "trading_pair": "SOL-USDC",
            "level_id": "SOL-USDC::buy_0",
            "timestamp": "2026-01-01T00:00:00Z",
            "candidate_grid_level": True,
            "raw_planned_action": "BLOCKED",
            "planned_action": "BLOCKED",
        },
        {
            "trading_pair": "SOL-USDC",
            "level_id": "SOL-USDC::sell_0",
            "timestamp": "2026-01-01T00:00:00Z",
            "candidate_grid_level": True,
            "raw_planned_action": "SIGNAL_ONLY",
            "planned_action": "SIGNAL_ONLY",
        },
    ]
    funnel = {row["stage"]: row["count"] for row in build_order_funnel([], rows)}
    assert funnel["candidate_grid_levels"] == 2
    assert funnel["create_decisions"] == 2


def test_pause_hysteresis_reports_bounded_strategy_episode() -> None:
    result = build_pause_hysteresis(
        [
            {
                "timestamp": "1970-01-01T00:00:00Z",
                "decisions": {"SOL-USDC": {"mode": "normal"}},
                "plans": {},
            },
            {
                "timestamp": "1970-01-01T00:00:02Z",
                "decisions": {
                    "SOL-USDC": {
                        "mode": "normal",
                        "pause_candidate_active": True,
                        "pause_candidate_category": "STRATEGY_REGIME",
                        "pause_candidate_reason": "transient regime",
                        "pause_confirmed": False,
                    }
                },
                "plans": {},
            },
            {
                "timestamp": "1970-01-01T00:00:04Z",
                "decisions": {"SOL-USDC": {"mode": "normal"}},
                "plans": {},
            },
        ]
    )
    assert result["strategy_regime_pause_episodes"] == 1
    assert result["transient_le_5s"] == 1
