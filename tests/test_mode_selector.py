from __future__ import annotations

import sys
from pathlib import Path

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from derive_options_mm.mode_selector import (  # noqa: E402
    GridMode,
    ModeSelector,
    ModeSelectorConfig,
    determine_candidate_mode,
)
from derive_options_mm.state_engine import (  # noqa: E402
    DirectionState,
    InventoryState,
    MarketState,
    VolatilityState,
)


def _state(
    timestamp: float,
    *,
    volatility: VolatilityState = VolatilityState.NORMAL,
    volatility_score: float | None = 1.0,
    direction: DirectionState = DirectionState.NEUTRAL,
    direction_score: float | None = 0.0,
    inventory: InventoryState = InventoryState.NEUTRAL,
    inventory_ratio: float | None = 0.0,
    confidence: float = 0.90,
    valid: bool = True,
    iv_ratio: float | None = None,
) -> MarketState:
    return MarketState(
        timestamp=str(timestamp),
        trading_pair="BTC-USDC",
        volatility_state=volatility,
        volatility_score=volatility_score,
        direction_state=direction,
        direction_score=direction_score,
        inventory_state=inventory,
        inventory_ratio=inventory_ratio,
        iv_ratio=iv_ratio,
        confidence=confidence,
        state_valid=valid,
    )


def _config(**overrides: object) -> ModeSelectorConfig:
    values = {
        "minimum_mode_confidence": 0.75,
        "minimum_bias_confidence": 0.85,
        "critical_confidence": 0.50,
        "mode_confirmation_samples": 2,
        "minimum_mode_duration_seconds": 0,
        "pause_recovery_samples": 1,
        "pause_recovery_seconds": 0,
        "defensive_exit_confirmation_samples": 2,
    }
    values.update(overrides)
    return ModeSelectorConfig(**values)


def test_normal_mode() -> None:
    evaluation = determine_candidate_mode(_state(0), _config())
    assert evaluation.mode is GridMode.NORMAL
    assert "normal volatility" in evaluation.reasons[0]


def test_long_bias_mode() -> None:
    evaluation = determine_candidate_mode(
        _state(0, direction=DirectionState.BULLISH, direction_score=0.42),
        _config(),
    )
    assert evaluation.mode is GridMode.LONG_BIAS


def test_short_bias_mode() -> None:
    evaluation = determine_candidate_mode(
        _state(0, direction=DirectionState.BEARISH, direction_score=-0.42),
        _config(),
    )
    assert evaluation.mode is GridMode.SHORT_BIAS


def test_high_volatility_overrides_direction() -> None:
    evaluation = determine_candidate_mode(
        _state(
            0,
            volatility=VolatilityState.HIGH,
            direction=DirectionState.BULLISH,
            direction_score=0.80,
        ),
        _config(),
    )
    assert evaluation.mode is GridMode.DEFENSIVE
    assert any("volatility" in reason for reason in evaluation.reasons)


def test_extreme_volatility_pauses() -> None:
    evaluation = determine_candidate_mode(_state(0, volatility_score=3.1), _config())
    assert evaluation.mode is GridMode.PAUSE
    assert "extreme volatility" in evaluation.reasons[0]


def test_inventory_soft_limit_is_defensive() -> None:
    evaluation = determine_candidate_mode(_state(0, inventory_ratio=0.60), _config())
    assert evaluation.mode is GridMode.DEFENSIVE


def test_inventory_hard_limit_pauses() -> None:
    evaluation = determine_candidate_mode(_state(0, inventory_ratio=0.95), _config())
    assert evaluation.mode is GridMode.PAUSE
    assert "hard limit" in evaluation.reasons[0]


def test_long_bias_is_blocked_when_inventory_is_already_long() -> None:
    evaluation = determine_candidate_mode(
        _state(0, direction=DirectionState.BULLISH, direction_score=0.60, inventory_ratio=0.45),
        _config(),
    )
    assert evaluation.mode is GridMode.NORMAL
    assert "long bias blocked" in evaluation.reasons[0]


def test_short_bias_is_blocked_when_inventory_is_already_short() -> None:
    evaluation = determine_candidate_mode(
        _state(0, direction=DirectionState.BEARISH, direction_score=-0.60, inventory_ratio=-0.45),
        _config(),
    )
    assert evaluation.mode is GridMode.NORMAL
    assert "short bias blocked" in evaluation.reasons[0]


def test_invalid_state_pauses() -> None:
    evaluation = determine_candidate_mode(_state(0, valid=False), _config())
    assert evaluation.mode is GridMode.PAUSE
    assert evaluation.reasons == ("market state invalid",)


def test_low_confidence_pauses() -> None:
    evaluation = determine_candidate_mode(_state(0, confidence=0.49), _config())
    assert evaluation.mode is GridMode.PAUSE


def test_missing_ofi_does_not_force_pause() -> None:
    state = _state(0)
    evaluation = determine_candidate_mode(state, _config())
    assert evaluation.mode is GridMode.NORMAL


def test_missing_atm_iv_with_valid_rv_fallback_does_not_force_pause() -> None:
    evaluation = determine_candidate_mode(_state(0, iv_ratio=None), _config())
    assert evaluation.mode is GridMode.NORMAL


def test_elevated_iv_ratio_is_defensive() -> None:
    evaluation = determine_candidate_mode(_state(0, iv_ratio=1.30), _config())
    assert evaluation.mode is GridMode.DEFENSIVE


def test_selector_confirms_directional_transition() -> None:
    selector = ModeSelector(_config())
    assert selector.update(_state(0)).mode is GridMode.NORMAL
    first_candidate = selector.update(
        _state(1, direction=DirectionState.BULLISH, direction_score=0.50)
    )
    second_candidate = selector.update(
        _state(2, direction=DirectionState.BULLISH, direction_score=0.50)
    )
    assert first_candidate.mode is GridMode.NORMAL
    assert "pending" in " ".join(first_candidate.reasons)
    assert second_candidate.mode is GridMode.LONG_BIAS
    assert second_candidate.transition_occurred is True


def test_minimum_mode_duration_delays_transition() -> None:
    selector = ModeSelector(
        _config(mode_confirmation_samples=1, minimum_mode_duration_seconds=10)
    )
    selector.update(_state(0))
    selector.update(_state(1))
    early = selector.update(
        _state(2, direction=DirectionState.BULLISH, direction_score=0.50)
    )
    late = selector.update(
        _state(11, direction=DirectionState.BULLISH, direction_score=0.50)
    )
    assert early.mode is GridMode.NORMAL
    assert "minimum mode duration" in " ".join(early.reasons)
    assert late.mode is GridMode.LONG_BIAS


def test_pause_is_immediate_even_when_mode_duration_is_active() -> None:
    selector = ModeSelector(_config(minimum_mode_duration_seconds=100))
    selector.update(_state(0))
    selector.update(_state(1))
    paused = selector.update(_state(2, valid=False))
    assert paused.mode is GridMode.PAUSE
    assert paused.transition_occurred is True


def test_pause_recovery_is_delayed() -> None:
    selector = ModeSelector(
        _config(
            pause_recovery_samples=3,
            mode_confirmation_samples=1,
            minimum_mode_duration_seconds=0,
        )
    )
    assert selector.update(_state(0, valid=False)).mode is GridMode.PAUSE
    assert selector.update(_state(1)).mode is GridMode.PAUSE
    assert selector.update(_state(2)).mode is GridMode.PAUSE
    recovered = selector.update(_state(3))
    assert recovered.mode is GridMode.NORMAL
    assert recovered.transition_occurred is True


def test_pause_recovery_accepts_confirmed_directional_mode() -> None:
    selector = ModeSelector(
        _config(
            pause_recovery_samples=3,
            mode_confirmation_samples=1,
            minimum_mode_duration_seconds=0,
        )
    )
    assert selector.update(_state(0, valid=False)).mode is GridMode.PAUSE
    first = selector.update(
        _state(1, direction=DirectionState.BULLISH, direction_score=0.50)
    )
    second = selector.update(
        _state(2, direction=DirectionState.BULLISH, direction_score=0.50)
    )
    recovered = selector.update(
        _state(3, direction=DirectionState.BULLISH, direction_score=0.50)
    )

    assert first.mode is GridMode.PAUSE
    assert second.mode is GridMode.PAUSE
    assert recovered.mode is GridMode.LONG_BIAS
    assert recovered.transition_occurred is True


def test_pause_recovery_accepts_defensive_candidate() -> None:
    selector = ModeSelector(
        _config(
            pause_recovery_samples=1,
            mode_confirmation_samples=1,
            minimum_mode_duration_seconds=0,
        )
    )
    assert selector.update(_state(0, valid=False)).mode is GridMode.PAUSE
    decision = selector.update(
        _state(1, volatility=VolatilityState.HIGH, direction=DirectionState.BULLISH)
    )

    assert decision.mode is GridMode.DEFENSIVE
    assert decision.transition_occurred is True


def test_defensive_exit_requires_confirmation() -> None:
    selector = ModeSelector(
        _config(
            mode_confirmation_samples=1,
            defensive_exit_confirmation_samples=2,
        )
    )
    selector.update(_state(0))
    selector.update(_state(1))
    selector.update(_state(2, volatility=VolatilityState.HIGH))
    defensive = selector.update(_state(3, volatility=VolatilityState.HIGH))
    assert defensive.mode is GridMode.DEFENSIVE
    first_safe = selector.update(_state(4))
    second_safe = selector.update(_state(5))
    assert first_safe.mode is GridMode.DEFENSIVE
    assert second_safe.mode is GridMode.NORMAL


def test_malformed_mapping_pauses() -> None:
    decision = ModeSelector(_config()).update(
        {"timestamp": "10", "trading_pair": "BTC-USDC", "confidence": "bad"}
    )
    assert decision.mode is GridMode.PAUSE
    assert decision.valid is False
    assert "malformed" in decision.reasons[0]


def test_out_of_order_state_pauses() -> None:
    selector = ModeSelector(_config())
    selector.update(_state(10))
    decision = selector.update(_state(9))
    assert decision.mode is GridMode.PAUSE
    assert "not newer" in " ".join(decision.reasons)


def test_selector_emits_only_symbolic_profile() -> None:
    decision = ModeSelector(_config()).update(_state(0, valid=False))
    assert decision.recommended_profile == "disabled"
    for forbidden_field in ("grid_width", "grid_levels", "order_size", "buy_weight"):
        assert not hasattr(decision, forbidden_field)
