"""Stage 8 shared-risk, relationship, portfolio, and routing tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from derive_options_mm.mode_selector import ModeSelectorConfig  # noqa: E402
from derive_options_mm.multi_asset import (  # noqa: E402
    BTCTransmissionState,
    MultiAssetConfig,
    MultiAssetCoordinator,
    PortfolioRiskGovernor,
    PortfolioRiskSettings,
    ProposedEntry,
    RollingBTCRelationshipEngine,
    pair_level_id,
)
from derive_options_mm.state_engine import StateEngineConfig  # noqa: E402

PAIRS = ("BTC-USDC", "ETH-USDC", "SOL-USDC", "HYPE-USDC")


def _config(**overrides: object) -> MultiAssetConfig:
    values: dict[str, object] = {
        "state": StateEngineConfig(
            minimum_history_samples=3,
            realized_vol_window_seconds=10,
            realized_vol_baseline_seconds=20,
            direction_return_window_seconds=10,
            direction_price_scale=0.01,
            direction_confirmation_samples=1,
        ),
        "mode": ModeSelectorConfig(
            mode_confirmation_samples=1,
            pause_recovery_samples=1,
            minimum_mode_duration_seconds=0,
        ),
        "global_options": {"minimum_history_samples": 3},
        "relationship": {
            "minimum_observations": 3,
            "window_seconds": 120,
            "short_window_seconds": 30,
            "medium_window_seconds": 60,
            "sensitivity_windows_seconds": (30, 60, 120),
        },
    }
    values.update(overrides)
    return MultiAssetConfig(**values)


def _snapshot(
    pair: str,
    timestamp: float,
    price: float,
    *,
    book: float = 0.0,
    flow: float = 0.0,
    iv: float | None = None,
    iv_age: float | None = 1.0,
    iv_available: bool | None = None,
    position: float = 0.0,
) -> dict[str, object]:
    result: dict[str, object] = {
        "timestamp": timestamp,
        "trading_pair": pair,
        "market_environment": "testnet",
        "data_valid": True,
        "best_bid": price - 0.01,
        "best_ask": price + 0.01,
        "mid_price": price,
        "spread_bps": 2.0,
        "best_bid_size": 2.0,
        "best_ask_size": 1.0,
        "depth_imbalance": book,
        "order_flow_imbalance": flow,
        "trade_data_available": True,
        "current_position": position,
        "position_notional": abs(position) * price,
        "account_data_available": True,
    }
    if iv is not None:
        result.update(
            {
                "atm_iv": iv,
                "iv_data_available": True if iv_available is None else iv_available,
                "iv_confidence": 1.0,
                "option_data_age_seconds": iv_age,
            }
        )
    else:
        result.update({"iv_data_available": False, "iv_confidence": 0.0})
    return result


def _frame(index: int, *, btc_iv: float = 0.50, btc_book: float = 0.0) -> dict[str, dict]:
    timestamp = 1_700_000_000.0 + index * 5
    btc = 100.0 + index * 0.2 + (0.05 if index % 2 else 0.0)
    return {
        "BTC-USDC": _snapshot("BTC-USDC", timestamp, btc, book=btc_book, iv=btc_iv),
        "ETH-USDC": _snapshot("ETH-USDC", timestamp, 10.0 + index * 0.02),
        "SOL-USDC": _snapshot("SOL-USDC", timestamp, 5.0 + index * 0.04),
        "HYPE-USDC": _snapshot("HYPE-USDC", timestamp, 2.0 - index * 0.01),
    }


def test_shared_btc_options_state_is_processed_once_and_reused() -> None:
    coordinator = MultiAssetCoordinator(_config())
    cycle = None
    for index in range(6):
        cycle = coordinator.update(_frame(index))

    assert cycle is not None
    assert coordinator.options_update_count == 6
    assert cycle.global_risk.source_pair == "BTC-USDC"
    assert all(
        state.global_risk_state is cycle.global_risk for state in cycle.states.values()
    )
    assert {state.trading_pair for state in cycle.states.values()} == set(PAIRS)
    assert all(state.market_environment == "testnet" for state in cycle.states.values())


def test_relationship_engine_bounds_beta_and_keeps_negative_correlation_transmission_positive(
) -> None:
    engine = RollingBTCRelationshipEngine(
        _config().relationship,
        trading_pairs=PAIRS,
    )
    states: dict[str, BTCTransmissionState] = {}
    for index in range(30):
        btc = 100.0 + index * 0.2 + (0.03 if index % 2 else 0.0)
        states = engine.update(
            {
                "BTC-USDC": btc,
                "ETH-USDC": btc * 0.1,
                "SOL-USDC": 5.0 + (0.02 if index % 2 else -0.02),
                "HYPE-USDC": 2.0 - btc * 0.001,
            },
            timestamp=1_700_000_000.0 + index * 5,
        )

    eth = states["ETH-USDC"]
    hype = states["HYPE-USDC"]
    assert eth.relationship_valid is True
    assert eth.btc_correlation == pytest.approx(1.0, abs=1e-6)
    assert eth.btc_beta == pytest.approx(1.0, rel=0.05)
    assert hype.btc_correlation is not None and hype.btc_correlation < 0
    assert hype.transmission_coefficient > 0
    assert abs(eth.btc_beta or 0) <= _config().relationship.beta_clip
    assert all(
        0 <= state.transmission_coefficient <= _config().relationship.transmission_max
        for state in states.values()
    )


def test_relationship_engine_fails_closed_on_zero_btc_variance() -> None:
    engine = RollingBTCRelationshipEngine(
        _config().relationship,
        trading_pairs=("BTC-USDC", "ETH-USDC"),
    )
    state = None
    for index in range(10):
        state = engine.update(
            {"BTC-USDC": 100.0, "ETH-USDC": 10.0 + index * 0.1},
            timestamp=1_700_000_000.0 + index * 5,
        )["ETH-USDC"]
    assert state is not None
    assert state.relationship_valid is False
    assert state.transmission_coefficient == 0
    assert "variance" in " ".join(state.reasons)


def test_stale_btc_iv_uses_local_rv_only_by_default() -> None:
    coordinator = MultiAssetCoordinator(_config())
    cycle = None
    for index in range(6):
        frame = _frame(index)
        frame["BTC-USDC"]["option_data_age_seconds"] = 60.0
        cycle = coordinator.update(frame)
    assert cycle is not None
    assert cycle.global_risk.btc_iv_available is False
    assert cycle.states["ETH-USDC"].global_iv_fallback is True
    assert cycle.states["ETH-USDC"].state_valid is True
    assert cycle.states["ETH-USDC"].transmitted_btc_iv_component is None


def test_direction_is_asset_local_and_btc_iv_does_not_create_direction() -> None:
    coordinator = MultiAssetCoordinator(_config())
    cycle = None
    for index in range(6):
        frame = _frame(index, btc_book=0.8)
        frame["ETH-USDC"]["depth_imbalance"] = -0.8
        frame["ETH-USDC"]["order_flow_imbalance"] = -0.8
        cycle = coordinator.update(frame)
    assert cycle is not None
    assert cycle.states["BTC-USDC"].direction_score is not None
    assert cycle.states["ETH-USDC"].direction_score is not None
    assert cycle.states["BTC-USDC"].direction_score > 0
    assert cycle.states["ETH-USDC"].direction_score < 0


def test_asset_pause_is_scoped_and_does_not_pause_other_markets() -> None:
    coordinator = MultiAssetCoordinator(_config())
    for index in range(6):
        coordinator.update(_frame(index))
    frame = _frame(6)
    frame.pop("SOL-USDC")
    cycle = coordinator.update(frame)

    assert cycle.states["SOL-USDC"].state_valid is False
    assert cycle.decisions["SOL-USDC"].mode.value == "pause"
    assert cycle.plans["SOL-USDC"].valid is False
    assert cycle.disabled_markets == ("SOL-USDC",)
    assert cycle.states["BTC-USDC"].state_valid is True
    assert cycle.enabled_markets == ("BTC-USDC", "ETH-USDC", "HYPE-USDC")


def test_plan_versions_are_isolated_per_pair() -> None:
    coordinator = MultiAssetCoordinator(_config())
    first_frame = _frame(0)
    for pair in ("ETH-USDC", "SOL-USDC", "HYPE-USDC"):
        base = {"ETH-USDC": 10.0, "SOL-USDC": 5.0, "HYPE-USDC": 2.0}[pair]
        first_frame[pair]["mid_price"] = base
        first_frame[pair]["best_bid"] = base - 0.01
        first_frame[pair]["best_ask"] = base + 0.01
    first = coordinator.update(first_frame)
    for index in range(1, 6):
        frame = _frame(index)
        for pair in ("ETH-USDC", "SOL-USDC", "HYPE-USDC"):
            base = {"ETH-USDC": 10.0, "SOL-USDC": 5.0, "HYPE-USDC": 2.0}[pair]
            frame[pair]["mid_price"] = base
            frame[pair]["best_bid"] = base - 0.01
            frame[pair]["best_ask"] = base + 0.01
        stable_cycle = coordinator.update(frame)
    stable_eth_version = stable_cycle.plans["ETH-USDC"].plan_version
    last_frame = _frame(6)
    for pair in ("ETH-USDC", "SOL-USDC", "HYPE-USDC"):
        base = {"ETH-USDC": 10.0, "SOL-USDC": 5.0, "HYPE-USDC": 2.0}[pair]
        last_frame[pair]["mid_price"] = base
        last_frame[pair]["best_bid"] = base - 0.01
        last_frame[pair]["best_ask"] = base + 0.01
    last = coordinator.update(last_frame)
    assert first.plans["BTC-USDC"].plan_version == 1
    assert last.plans["BTC-USDC"].plan_version > first.plans["BTC-USDC"].plan_version
    assert last.plans["ETH-USDC"].plan_version == stable_eth_version
    assert coordinator.plan_versions["SOL-USDC"] == stable_eth_version
    assert coordinator.plan_versions["HYPE-USDC"] == stable_eth_version


def test_portfolio_governor_blocks_worsening_buy_but_allows_risk_reducing_sell() -> None:
    governor = PortfolioRiskGovernor(
        PortfolioRiskSettings(
            portfolio_max_gross_notional=1_000,
            portfolio_soft_beta_exposure=100,
            portfolio_hard_beta_exposure=200,
            portfolio_max_long_beta_exposure=150,
            portfolio_max_short_beta_exposure=150,
            per_asset_max_position_notional=500,
        )
    )
    decision = governor.evaluate(
        timestamp="2026-01-01T00:00:00Z",
        positions={"ETH-USDC": 120},
        proposed_entries={
            "ETH-USDC": [
                ProposedEntry(
                    trading_pair="ETH-USDC",
                    level_id=pair_level_id("ETH-USDC", "buy", 0),
                    side="buy",
                    quote_notional=40,
                ),
                ProposedEntry(
                    trading_pair="ETH-USDC",
                    level_id=pair_level_id("ETH-USDC", "sell", 0),
                    side="sell",
                    quote_notional=40,
                ),
            ]
        },
        betas={"ETH-USDC": 1.0},
    )
    assert decision.blocked_sides["ETH-USDC"] == ["buy"]
    assert pair_level_id("ETH-USDC", "sell", 0) in decision.allowed_level_ids["ETH-USDC"]
    assert decision.global_pause_new_exposure is False


def test_portfolio_governor_accumulates_pending_and_allowed_proposals() -> None:
    governor = PortfolioRiskGovernor(
        PortfolioRiskSettings(
            portfolio_max_gross_notional=500,
            portfolio_soft_beta_exposure=150,
            portfolio_hard_beta_exposure=200,
            portfolio_max_long_beta_exposure=200,
            portfolio_max_short_beta_exposure=200,
            per_asset_max_position_notional=100,
        )
    )
    buy_zero = ProposedEntry(
        trading_pair="ETH-USDC",
        level_id=pair_level_id("ETH-USDC", "buy", 0),
        side="buy",
        quote_notional=60,
    )
    buy_one = buy_zero.model_copy(
        update={"level_id": pair_level_id("ETH-USDC", "buy", 1)}
    )
    decision = governor.evaluate(
        timestamp="2026-01-01T00:00:00Z",
        pending_entries={"ETH-USDC": {"buy": 20, "sell": 0}},
        proposed_entries={"ETH-USDC": [buy_zero, buy_one]},
        betas={"ETH-USDC": 1.0},
    )
    assert decision.gross_notional == pytest.approx(20)
    assert decision.allowed_level_ids["ETH-USDC"] == [buy_zero.level_id]
    assert decision.blocked_level_ids["ETH-USDC"] == [buy_one.level_id]


def test_pair_scoped_level_ids_are_distinct() -> None:
    assert pair_level_id("BTC-USDC", "buy", 0) != pair_level_id("ETH-USDC", "buy", 0)
    assert pair_level_id("BTC-USDC", "buy", 0) == "BTC-USDC::buy_0"
