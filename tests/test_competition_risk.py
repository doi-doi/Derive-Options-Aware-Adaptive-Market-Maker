"""Competition profile, exchange-minimum, drawdown, and portfolio-risk tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
INTEGRATION_ROOT = PROJECT_ROOT / "integrations" / "hummingbot"
for path in (SRC_ROOT, INTEGRATION_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from derive_adaptive_grid.execution_logic import (  # noqa: E402
    ActiveLevel,
    ExecutionPolicy,
    ExecutionSide,
    RuntimeHealth,
    TradingRuleView,
    parse_grid_plan,
    reconcile_grid_plan,
)

from derive_options_mm.competition_risk import (  # noqa: E402
    CompetitionCandidate,
    CompetitionMarketRule,
    CompetitionProfile,
    CompetitionRiskGovernor,
    CompetitionRiskStage,
    assess_order_sizing,
    load_competition_profile,
)

NOW = 1_900_000_000.0


def _profile(**overrides: object) -> CompetitionProfile:
    values = CompetitionProfile().model_dump(mode="python")
    values.update(overrides)
    return CompetitionProfile(**values)


def _rule(pair: str, minimum: float, step: float, price: float) -> CompetitionMarketRule:
    asset = pair.split("-", 1)[0]
    return CompetitionMarketRule(
        trading_pair=pair,
        instrument_name=f"{asset}-PERP",
        minimum_amount=minimum,
        amount_step=step,
        price_increment=0.01,
        reference_price=price,
        observed_at="2026-08-25T00:00:00Z",
    )


def _candidate(
    pair: str, side: str, quote: float, level: str | None = None
) -> CompetitionCandidate:
    return CompetitionCandidate(
        trading_pair=pair,
        level_id=level or f"{pair}::{side}_0",
        side=side,
        quote_notional=quote,
    )


def _timestamp(epoch: float = NOW) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _record(*, mode: str = "normal", enabled: bool = True) -> dict:
    return {
        "timestamp": _timestamp(),
        "trading_pair": "ETH-USDC",
        "mode": mode,
        "enabled": enabled,
        "valid": True,
        "plan_version": 1,
        "center_price": "100",
        "total_grid_width_pct": "0.04",
        "buy_levels": [
            {"side": "buy", "level_index": 0, "theoretical_price": "99", "quote_amount": "70"}
        ],
        "sell_levels": [
            {"side": "sell", "level_index": 0, "theoretical_price": "101", "quote_amount": "70"}
        ],
    }


def _health() -> RuntimeHealth:
    return RuntimeHealth(
        testnet_verified=True,
        connector_ready=True,
        market_data_ready=True,
        trading_rules_available=True,
        balance_verified=True,
        position_verified=True,
        best_bid=99.5,
        best_ask=100.5,
        available_collateral=800,
        trading_rules=TradingRuleView(
            min_order_size=0.001,
            min_price_increment=0.1,
            min_base_amount_increment=0.001,
        ),
    )


def test_committed_800_profile_loads_with_safe_defaults() -> None:
    path = PROJECT_ROOT / "configs" / "competition_800_usdc.yml"
    profile = load_competition_profile(path)

    assert profile.starting_equity_reference == 800
    assert profile.collateral_reserve_pct == pytest.approx(0.20)
    assert profile.leverage == 2
    assert profile.portfolio_soft_gross_notional == 900
    assert profile.portfolio_max_gross_notional == 1100
    assert profile.portfolio_soft_beta_exposure == 600
    assert profile.portfolio_hard_beta_exposure == 800
    assert profile.asset_limits["ETH-USDC"].max_position_notional == 280
    assert profile.asset_limits["SOL-USDC"].max_position_notional == 280
    assert profile.asset_limits["HYPE-USDC"].max_position_notional == 220
    assert profile.execution_enabled is False
    assert profile.allow_mainnet_trading is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("leverage", 3, "leverage"),
        ("portfolio_max_gross_notional", 900, "gross"),
        ("portfolio_hard_beta_exposure", 600, "beta"),
        ("target_order_notional", 101, "target"),
        ("collateral_reserve_pct", 1, "reserve"),
        ("competition_hard_drawdown_quote", 0, "drawdown"),
        ("minimum_replace_interval_seconds", -1, "replacement"),
        ("refresh_price_tolerance_bps", 0, "tolerance"),
    ],
)
def test_profile_rejects_unsafe_limits(field: str, value: object, message: str) -> None:
    values = CompetitionProfile().model_dump(mode="python")
    values[field] = value
    with pytest.raises(ValueError):
        CompetitionProfile(**values)


def test_exchange_minimum_sizing_keeps_the_100_usdc_budget() -> None:
    btc = assess_order_sizing(_rule("BTC-USDC", 0.01, 0.0001, 79_020))
    eth = assess_order_sizing(_rule("ETH-USDC", 0.1, 0.01, 2_474))
    sol = assess_order_sizing(_rule("SOL-USDC", 1, 0.1, 74.07))
    hype = assess_order_sizing(_rule("HYPE-USDC", 10, 1, 80.017))

    assert btc.eligible is False
    assert btc.reason == "MIN_ORDER_EXCEEDS_BUDGET"
    assert eth.eligible is False
    assert sol.eligible is True
    assert sol.minimum_valid_notional == pytest.approx(74.07)
    assert hype.eligible is False


def test_pending_orders_count_and_create_budget_are_deterministic() -> None:
    governor = CompetitionRiskGovernor()
    decision = governor.evaluate(
        timestamp="2026-08-25T00:00:00Z",
        pending_entries={"ETH-USDC": {"buy": 70, "count": 1}},
        proposed_entries={
            "ETH-USDC": [_candidate("ETH-USDC", "buy", 70, "ETH-USDC::buy_0")],
        },
        betas={"ETH-USDC": 1},
        available_collateral=800,
    )

    assert decision.exposure.pending_order_count == 1
    assert decision.exposure.gross_notional == pytest.approx(70)
    assert decision.risk_increasing_creates == 1
    assert decision.blocked_level_ids == {}


def test_two_risk_create_cycle_cap_blocks_the_third_candidate() -> None:
    governor = CompetitionRiskGovernor()
    decision = governor.evaluate(
        timestamp="2026-08-25T00:00:00Z",
        proposed_entries={
            "ETH-USDC": [_candidate("ETH-USDC", "buy", 70)],
            "SOL-USDC": [_candidate("SOL-USDC", "buy", 70)],
            "HYPE-USDC": [_candidate("HYPE-USDC", "buy", 70)],
        },
    )

    assert decision.risk_increasing_creates == 2
    assert decision.risk_create_cap_triggered is True
    assert "HYPE-USDC::buy_0" in decision.blocked_reasons


def test_long_beta_blocks_buy_but_allows_risk_reducing_sell() -> None:
    governor = CompetitionRiskGovernor()
    decision = governor.evaluate(
        timestamp="2026-08-25T00:00:00Z",
        positions={"ETH-USDC": 180, "SOL-USDC": 240, "HYPE-USDC": 280},
        proposed_entries={
            "ETH-USDC": [
                _candidate("ETH-USDC", "buy", 70, "ETH-USDC::buy_0"),
                _candidate("ETH-USDC", "sell", 70, "ETH-USDC::sell_0"),
            ]
        },
        betas={"ETH-USDC": 1, "SOL-USDC": 1, "HYPE-USDC": 1},
    )

    assert decision.exposure.btc_beta_equivalent_exposure == pytest.approx(700)
    assert decision.blocked_reasons["ETH-USDC::buy_0"] == "PORTFOLIO_SOFT_BETA_LONG"
    assert "ETH-USDC::sell_0" in decision.allowed_level_ids["ETH-USDC"]


def test_short_beta_has_symmetric_behavior() -> None:
    governor = CompetitionRiskGovernor()
    decision = governor.evaluate(
        timestamp="2026-08-25T00:00:00Z",
        positions={"ETH-USDC": -180, "SOL-USDC": -240, "HYPE-USDC": -280},
        proposed_entries={
            "ETH-USDC": [
                _candidate("ETH-USDC", "sell", 70, "ETH-USDC::sell_0"),
                _candidate("ETH-USDC", "buy", 70, "ETH-USDC::buy_0"),
            ]
        },
    )

    assert decision.exposure.btc_beta_equivalent_exposure == pytest.approx(-700)
    assert decision.blocked_reasons["ETH-USDC::sell_0"] == "PORTFOLIO_SOFT_BETA_SHORT"
    assert "ETH-USDC::buy_0" in decision.allowed_level_ids["ETH-USDC"]


def test_drawdown_ladder_and_hard_stop_latch() -> None:
    governor = CompetitionRiskGovernor()
    assert governor.equity_state(760).risk_stage is CompetitionRiskStage.CAUTION
    assert governor.equity_state(740).risk_stage is CompetitionRiskStage.REDUCE
    assert governor.equity_state(720).risk_stage is CompetitionRiskStage.DEFENSIVE
    hard = governor.equity_state(699)
    assert hard.risk_stage is CompetitionRiskStage.HARD_STOP_NEW_RISK
    assert hard.risk_capacity_multiplier == 0

    recovered = governor.equity_state(800)
    assert recovered.risk_stage is CompetitionRiskStage.HARD_STOP_NEW_RISK
    with pytest.raises(PermissionError):
        governor.reset_session(800)


def test_hard_stop_allows_risk_reduction_but_not_new_entries() -> None:
    governor = CompetitionRiskGovernor()
    decision = governor.evaluate(
        timestamp="2026-08-25T00:00:00Z",
        current_equity=699,
        positions={"ETH-USDC": 100},
        proposed_entries={
            "ETH-USDC": [
                _candidate("ETH-USDC", "buy", 70, "ETH-USDC::buy_0"),
                _candidate("ETH-USDC", "sell", 70, "ETH-USDC::sell_0"),
            ]
        },
    )

    assert decision.global_pause_new_exposure is True
    assert decision.blocked_reasons["ETH-USDC::buy_0"] == "COMPETITION_HARD_STOP"
    assert "ETH-USDC::sell_0" in decision.allowed_level_ids["ETH-USDC"]


def test_competition_churn_policy_keeps_small_moves_and_mode_changes() -> None:
    plan = parse_grid_plan(_record(mode="defensive"), expected_pair="ETH-USDC")
    active = [
        ActiveLevel(
            executor_id="eth-buy",
            level_id="buy_0",
            side=ExecutionSide.BUY,
            price=99,
            amount=70 / 99,
            quote_notional=70,
            created_at=NOW - 300,
            is_filled=False,
            plan_mode="normal",
            last_replace_at=NOW - 300,
        )
    ]
    policy = ExecutionPolicy(
        execution_max_levels_per_side=1,
        testnet_order_scale=1,
        max_total_position_notional=1_100,
        max_side_position_notional=280,
        max_active_grid_levels=2,
        max_active_executors=2,
        minimum_order_lifetime_seconds=120,
        minimum_replace_interval_seconds=60,
        maximum_order_lifetime_seconds=900,
        refresh_price_tolerance_bps=12,
        refresh_amount_tolerance_pct=0.15,
    )

    def price(value: float) -> float:
        return value

    result = reconcile_grid_plan(
        plan,
        active=active,
        health=_health(),
        policy=policy,
        now_epoch=NOW,
        quantize_price=price,
        quantize_amount=price,
    )

    assert result.stops == []
    assert result.keeps == ["buy_0"]
    assert result.keep_reasons["buy_0"] == "MODE_CHANGE_WITHIN_DEADBAND_KEEP"


def test_pause_cancels_unfilled_and_keeps_filled_position_management() -> None:
    plan = parse_grid_plan(_record(mode="pause", enabled=False), expected_pair="ETH-USDC")
    active = [
        ActiveLevel("buy", "buy_0", ExecutionSide.BUY, 99, 1, 99, NOW, False),
        ActiveLevel("sell", "sell_0", ExecutionSide.SELL, 101, 1, 101, NOW, True),
    ]
    result = reconcile_grid_plan(
        plan,
        active=active,
        health=_health(),
        policy=ExecutionPolicy(),
        now_epoch=NOW,
        quantize_price=lambda value: value,
        quantize_amount=lambda value: value,
    )

    assert [item.executor_id for item in result.stops] == ["buy"]
    assert result.keeps == ["sell_0"]
