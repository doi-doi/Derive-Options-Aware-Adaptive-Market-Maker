from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from derive_options_mm.grid_engine import (  # noqa: E402
    GridMode,
    GridParameterConfig,
    GridParameterEngine,
    calculate_base_reference,
    calculate_final_center,
    calculate_volatility_width_multiplier,
    generate_geometric_distances,
    plan_change_significant,
    validate_grid_plan,
)


def _snapshot(**overrides: object) -> dict:
    value = {
        "timestamp": "2026-08-24T00:00:00Z",
        "trading_pair": "BTC-USDC",
        "data_valid": True,
        "best_bid": 77000.0,
        "best_ask": 77010.0,
        "mid_price": 77005.0,
        "spread_bps": 1.2986,
        "best_bid_size": 2.0,
        "best_ask_size": 1.0,
    }
    value.update(overrides)
    return value


def _state(
    *,
    volatility_score: float = 1.0,
    direction_state: str = "neutral",
    direction_score: float | None = 0.0,
    inventory_ratio: float | None = 0.0,
    valid: bool = True,
    confidence: float = 0.925,
    timestamp: str = "2026-08-24T00:00:00Z",
) -> dict:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "volatility_state": "high" if volatility_score >= 1.5 else "normal",
        "volatility_score": volatility_score,
        "direction_state": direction_state,
        "direction_score": direction_score,
        "inventory_state": (
            "long" if inventory_ratio and inventory_ratio > 0.1
            else "short" if inventory_ratio and inventory_ratio < -0.1
            else "neutral"
        ),
        "inventory_ratio": inventory_ratio,
        "confidence": confidence,
        "state_valid": valid,
        "reasons": [],
    }


def _decision(
    mode: str = "normal",
    *,
    direction_score: float | None = 0.0,
    direction_state: str = "neutral",
    inventory_ratio: float | None = 0.0,
    volatility_score: float = 1.0,
    valid: bool = True,
    confidence: float = 0.925,
    timestamp: str = "2026-08-24T00:00:00Z",
) -> dict:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "mode": mode,
        "previous_mode": None,
        "transition_occurred": False,
        "volatility_state": "high" if volatility_score >= 1.5 else "normal",
        "volatility_score": volatility_score,
        "direction_state": direction_state,
        "direction_score": direction_score,
        "inventory_state": (
            "long" if inventory_ratio and inventory_ratio > 0.1
            else "short" if inventory_ratio and inventory_ratio < -0.1
            else "neutral"
        ),
        "inventory_ratio": inventory_ratio,
        "confidence": confidence,
        "valid": valid,
        "reasons": ["test mode reason"],
        "recommended_profile": "standard" if mode == "normal" else mode,
    }


def _plan(
    mode: str = "normal",
    *,
    direction_score: float | None = 0.0,
    direction_state: str = "neutral",
    inventory_ratio: float | None = 0.0,
    volatility_score: float = 1.0,
    snapshot: dict | None = None,
    state_valid: bool = True,
    decision_valid: bool = True,
    config: GridParameterConfig | None = None,
):
    state = _state(
        volatility_score=volatility_score,
        direction_state=direction_state,
        direction_score=direction_score,
        inventory_ratio=inventory_ratio,
        valid=state_valid,
    )
    decision = _decision(
        mode,
        direction_score=direction_score,
        direction_state=direction_state,
        inventory_ratio=inventory_ratio,
        volatility_score=volatility_score,
        valid=decision_valid,
    )
    from derive_options_mm.grid_engine import build_grid_plan

    return build_grid_plan(snapshot or _snapshot(), state, decision, config)


def test_normal_plan_is_balanced_and_has_five_levels_per_side() -> None:
    plan = _plan()

    assert plan.valid is True
    assert plan.enabled is True
    assert plan.mode is GridMode.NORMAL
    assert plan.buy_levels_count == 5
    assert plan.sell_levels_count == 5
    assert plan.buy_allocation_pct == Decimal("0.5")
    assert plan.sell_allocation_pct == Decimal("0.5")


def test_defensive_plan_is_wider_smaller_and_has_fewer_levels() -> None:
    normal = _plan()
    defensive = _plan("defensive", volatility_score=1.6)

    assert defensive.valid is True
    assert defensive.total_grid_width_pct > normal.total_grid_width_pct
    assert defensive.buy_levels_count == 3
    assert defensive.sell_levels_count == 3
    assert defensive.effective_quote_amount == Decimal("500.0")
    assert defensive.inner_distance_bps == Decimal("7.5")


def test_long_bias_has_upward_center_and_buy_bias() -> None:
    plan = _plan("long_bias", direction_state="bullish", direction_score=0.5)

    assert plan.center_price > plan.reference_price
    assert plan.directional_adjustment == Decimal("10.0")
    assert plan.buy_allocation_pct == Decimal("0.60")
    assert plan.sell_allocation_pct == Decimal("0.40")


def test_short_bias_has_downward_center_and_sell_bias() -> None:
    plan = _plan("short_bias", direction_state="bearish", direction_score=-0.5)

    assert plan.center_price < plan.reference_price
    assert plan.directional_adjustment == Decimal("-10.0")
    assert plan.buy_allocation_pct == Decimal("0.40")
    assert plan.sell_allocation_pct == Decimal("0.60")


def test_pause_produces_no_levels_and_zero_effective_quote() -> None:
    plan = _plan("pause")

    assert plan.enabled is False
    assert plan.valid is False
    assert plan.buy_levels == []
    assert plan.sell_levels == []
    assert plan.effective_quote_amount == 0


def test_positive_direction_moves_center_upward() -> None:
    long_plan = _plan("long_bias", direction_state="bullish", direction_score=0.4)
    assert long_plan.center_price > _plan().center_price


def test_negative_direction_moves_center_downward() -> None:
    short_plan = _plan("short_bias", direction_state="bearish", direction_score=-0.4)
    assert short_plan.center_price < _plan().center_price


def test_long_inventory_moves_center_downward() -> None:
    plan = _plan(inventory_ratio=0.5)

    assert plan.center_price < plan.reference_price
    assert plan.inventory_adjustment == Decimal("-15.0")


def test_short_inventory_moves_center_upward() -> None:
    plan = _plan(inventory_ratio=-0.5)

    assert plan.center_price > plan.reference_price
    assert plan.inventory_adjustment == Decimal("15.0")


def test_inventory_overrides_directional_center_and_allocation_bias() -> None:
    plan = _plan(
        "long_bias",
        direction_state="bullish",
        direction_score=0.5,
        inventory_ratio=0.7,
    )

    assert plan.directional_adjustment == Decimal("10.0")
    assert plan.inventory_adjustment == Decimal("-21.0")
    assert plan.center_shift_bps == Decimal("-11.0")
    assert plan.buy_allocation_pct == Decimal("0.425")
    assert plan.sell_allocation_pct == Decimal("0.575")


def test_total_center_shift_is_clamped() -> None:
    config = GridParameterConfig()
    center, total = calculate_final_center(
        Decimal("100"),
        Decimal("50"),
        Decimal("30"),
        config,
    )

    assert total == config.max_total_center_shift_bps
    assert center == Decimal("100.4")


def test_low_volatility_narrows_grid_and_high_volatility_widens_grid() -> None:
    low = _plan(volatility_score=0.8)
    normal = _plan(volatility_score=1.0)
    high = _plan(volatility_score=1.3)

    assert low.total_grid_width_pct < normal.total_grid_width_pct < high.total_grid_width_pct


def test_volatility_multiplier_is_clamped() -> None:
    assert calculate_volatility_width_multiplier(0.1) == Decimal("0.75")
    assert calculate_volatility_width_multiplier(10) == Decimal("2.0")


def test_defensive_width_multiplier_is_applied() -> None:
    normal = _plan(volatility_score=1.0)
    defensive = _plan("defensive", volatility_score=1.0)

    assert defensive.total_grid_width_pct == normal.total_grid_width_pct * Decimal("1.5")


def test_defensive_size_is_reduced() -> None:
    assert _plan("defensive").effective_quote_amount == Decimal("500.0")


def test_level_counts_are_mode_specific() -> None:
    assert len(_plan().buy_levels) == 5
    assert len(_plan("defensive").buy_levels) == 3
    assert len(_plan("long_bias", direction_state="bullish", direction_score=0.4).buy_levels) == 5


def test_geometric_distances_have_constant_ratio() -> None:
    distances = generate_geometric_distances(Decimal("0.0005"), Decimal("0.005"), 5)
    ratios = [right / left for left, right in zip(distances, distances[1:], strict=False)]

    assert len(ratios) == 4
    assert all(abs(ratio - ratios[0]) < Decimal("1e-24") for ratio in ratios)


def test_buy_levels_are_below_center_and_progressively_lower() -> None:
    plan = _plan()
    prices = [level.theoretical_price for level in plan.buy_levels]

    assert all(price < plan.center_price for price in prices)
    assert all(left > right for left, right in zip(prices, prices[1:], strict=False))


def test_sell_levels_are_above_center_and_progressively_higher() -> None:
    plan = _plan()
    prices = [level.theoretical_price for level in plan.sell_levels]

    assert all(price > plan.center_price for price in prices)
    assert all(left < right for left, right in zip(prices, prices[1:], strict=False))


def test_theoretical_levels_have_no_duplicate_prices() -> None:
    plan = _plan()
    prices = [level.theoretical_price for level in plan.buy_levels + plan.sell_levels]

    assert len(prices) == len(set(prices))
    assert validate_grid_plan(plan) == ()


def test_long_bias_buy_weighting() -> None:
    plan = _plan("long_bias", direction_state="bullish", direction_score=0.4)

    assert plan.buy_allocation_pct > plan.sell_allocation_pct


def test_short_bias_sell_weighting() -> None:
    plan = _plan("short_bias", direction_state="bearish", direction_score=-0.4)

    assert plan.sell_allocation_pct > plan.buy_allocation_pct


def test_long_inventory_reduces_buy_allocation() -> None:
    neutral = _plan("long_bias", direction_state="bullish", direction_score=0.4)
    long = _plan("long_bias", direction_state="bullish", direction_score=0.4, inventory_ratio=0.5)

    assert long.buy_allocation_pct < neutral.buy_allocation_pct


def test_short_inventory_reduces_sell_allocation() -> None:
    neutral = _plan("short_bias", direction_state="bearish", direction_score=-0.4)
    short = _plan(
        "short_bias",
        direction_state="bearish",
        direction_score=-0.4,
        inventory_ratio=-0.5,
    )

    assert short.sell_allocation_pct < neutral.sell_allocation_pct


def test_allocation_is_clamped_to_side_limits() -> None:
    config = GridParameterConfig(
        max_allocation_bias=Decimal("1"),
        inventory_allocation_strength=Decimal("1"),
    )
    plan = _plan(
        "long_bias",
        direction_state="bullish",
        direction_score=0.4,
        inventory_ratio=-1.0,
        config=config,
    )

    assert plan.buy_allocation_pct == Decimal("0.90")
    assert plan.sell_allocation_pct == Decimal("0.10")


def test_buy_and_sell_allocations_sum_to_one() -> None:
    for mode, direction, score in (
        ("normal", "neutral", 0.0),
        ("defensive", "neutral", 0.0),
        ("long_bias", "bullish", 0.5),
        ("short_bias", "bearish", -0.5),
    ):
        plan = _plan(mode, direction_state=direction, direction_score=score)
        assert plan.buy_allocation_pct + plan.sell_allocation_pct == Decimal("1")


def test_invalid_snapshot_disables_plan() -> None:
    plan = _plan(snapshot=_snapshot(data_valid=False))

    assert plan.enabled is False
    assert plan.valid is False
    assert "MarketSnapshot invalid" in plan.reasons[0]


def test_invalid_state_disables_plan() -> None:
    plan = _plan(state_valid=False)

    assert plan.enabled is False
    assert plan.valid is False
    assert "MarketState invalid" in plan.reasons[0]


def test_invalid_mode_decision_disables_plan() -> None:
    plan = _plan(decision_valid=False)

    assert plan.enabled is False
    assert plan.valid is False
    assert "GridModeDecision invalid" in plan.reasons[0]


def test_malformed_reference_price_disables_plan() -> None:
    plan = _plan(snapshot=_snapshot(mid_price=0, best_bid=0, best_ask=0))

    assert plan.enabled is False
    assert plan.valid is False
    assert "reference price unavailable" in plan.reasons[0]


def test_grid_width_smaller_than_inner_gap_disables_plan() -> None:
    config = GridParameterConfig(
        base_grid_width_pct=Decimal("0.001"),
        min_grid_width_pct=Decimal("0.001"),
        max_grid_width_pct=Decimal("0.010"),
        configured_min_inner_distance_bps=Decimal("6"),
        maker_safety_buffer_bps=Decimal("0"),
    )
    plan = _plan(config=config)

    assert plan.enabled is False
    assert plan.valid is False
    assert "grid width is smaller than inner distance" in plan.reasons[0]


def test_quote_allocations_total_effective_amount() -> None:
    plan = _plan("short_bias", direction_state="bearish", direction_score=-0.4)
    buy_total = sum((level.quote_amount for level in plan.buy_levels), Decimal("0"))
    sell_total = sum((level.quote_amount for level in plan.sell_levels), Decimal("0"))

    assert buy_total == plan.effective_quote_amount * plan.buy_allocation_pct
    assert sell_total == plan.effective_quote_amount * plan.sell_allocation_pct
    assert buy_total + sell_total == plan.effective_quote_amount


def test_reference_prefers_microprice_then_mid() -> None:
    derived = calculate_base_reference(_snapshot())
    explicit = calculate_base_reference(_snapshot(microprice=77008))
    fallback = calculate_base_reference(
        _snapshot(best_bid_size=0, best_ask_size=0, microprice=None)
    )

    assert derived == Decimal("77006.66666666666666666666667")
    assert explicit == Decimal("77008")
    assert fallback == Decimal("77005")


def test_plan_version_only_increments_for_material_changes() -> None:
    engine = GridParameterEngine()
    first = engine.build(_snapshot(), _state(), _decision())
    same = engine.build(_snapshot(), _state(), _decision())
    changed = engine.build(
        _snapshot(),
        _state(volatility_score=1.4),
        _decision(volatility_score=1.4),
    )

    assert first.plan_version == 1
    assert first.plan_change_significant is True
    assert same.plan_version == 1
    assert same.plan_change_significant is False
    assert changed.plan_version == 2
    assert changed.plan_change_significant is True


def test_plan_change_thresholds_ignore_small_center_moves() -> None:
    first = _plan()
    second = _plan(inventory_ratio=0.001)

    assert plan_change_significant(second, first) is False
