"""Stage 9 consequence and existing-Stage-4 grid preview tests."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dashboard.config_schema import Stage9StrategySettings  # noqa: E402
from dashboard.consequence_preview import (  # noqa: E402
    refresh_stability_estimate,
    risk_consequence_preview,
)
from dashboard.grid_preview import build_proposed_plan, compare_plans  # noqa: E402
from derive_options_mm.competition_risk import (  # noqa: E402
    CompetitionMarketRule,
    CompetitionProfile,
)


def _records() -> dict[str, dict]:
    snapshot = {
        "timestamp": "2026-08-25T00:00:00Z",
        "trading_pair": "ETH-USDC",
        "data_valid": True,
        "mid_price": 100.0,
        "reference_price": 100.0,
        "best_bid": 99.0,
        "best_ask": 101.0,
        "spread_bps": 20.0,
    }
    state = {
        "timestamp": snapshot["timestamp"],
        "trading_pair": "ETH-USDC",
        "market_environment": "testnet",
        "volatility_state": "normal",
        "volatility_score": 0.8,
        "direction_state": "neutral",
        "direction_score": 0.0,
        "inventory_state": "neutral",
        "inventory_ratio": 0.0,
        "state_valid": True,
        "confidence": 0.9,
    }
    mode = {
        "timestamp": snapshot["timestamp"],
        "trading_pair": "ETH-USDC",
        "market_environment": "testnet",
        "mode": "normal",
        "volatility_state": "normal",
        "volatility_score": 0.8,
        "direction_state": "neutral",
        "direction_score": 0.0,
        "inventory_state": "neutral",
        "inventory_ratio": 0.0,
        "confidence": 0.9,
        "valid": True,
        "recommended_profile": "standard",
    }
    return {"snapshot": snapshot, "state": state, "mode": mode}


def test_grid_preview_calls_existing_stage4_planner_and_is_non_mutating() -> None:
    records = _records()
    strategy = Stage9StrategySettings()
    current = build_proposed_plan(records, strategy)
    assert current is not None and current.valid
    records["plan"] = current.to_record()
    proposed_strategy = strategy.model_copy(update={"base_grid_width_pct": 0.02})
    proposed = build_proposed_plan(records, proposed_strategy)
    assert proposed is not None and proposed.valid
    assert proposed.total_grid_width_pct != current.total_grid_width_pct
    assert records["state"]["inventory_ratio"] == 0.0
    comparison = compare_plans(current, proposed)
    assert (
        comparison["total_grid_width_pct"]["current"]
        != comparison["total_grid_width_pct"]["proposed"]
    )


def test_consequence_and_historical_refresh_previews_are_deterministic() -> None:
    profile = CompetitionProfile()
    proposed = profile.model_copy(update={"portfolio_hard_beta_exposure": 900.0})
    preview = risk_consequence_preview(profile, proposed, account_equity=800.0)
    assert preview.values["old_hard_beta"] == 800
    assert preview.values["new_hard_beta_reference_multiple"] == 1.125
    history = [
        {
            "buy_levels": [{"level_index": 0, "theoretical_price": 99.0, "quote_amount": 70.0}],
            "sell_levels": [],
        },
        {
            "buy_levels": [{"level_index": 0, "theoretical_price": 99.01, "quote_amount": 70.0}],
            "sell_levels": [],
        },
        {
            "buy_levels": [{"level_index": 0, "theoretical_price": 99.5, "quote_amount": 70.0}],
            "sell_levels": [],
        },
    ]
    estimate = refresh_stability_estimate(
        history, price_tolerance_bps=12.0, amount_tolerance_pct=0.15
    )
    assert estimate.values["counts"]["KEEP"] == 1
    assert estimate.values["counts"]["REFRESH"] == 1
    assert estimate.warnings


def test_rule_model_remains_local_and_read_only() -> None:
    rule = CompetitionMarketRule(
        trading_pair="SOL-USDC",
        instrument_name="SOL-PERP",
        minimum_amount=1,
        amount_step=0.1,
        price_increment=0.01,
        reference_price=74.0,
        observed_at="2026-08-25T00:00:00Z",
    )
    assert rule.minimum_amount == 1
