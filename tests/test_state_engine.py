from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

STAGE1_PATH = Path(__file__).parents[1] / "integrations" / "condor" / "derive_market_snapshot.py"
stage1_spec = importlib.util.spec_from_file_location(
    "derive_market_snapshot_for_state_test", STAGE1_PATH
)
assert stage1_spec and stage1_spec.loader
stage1_module = importlib.util.module_from_spec(stage1_spec)
sys.modules[stage1_spec.name] = stage1_module
stage1_spec.loader.exec_module(stage1_module)

from derive_options_mm.state_engine import (  # noqa: E402
    DirectionState,
    InventoryState,
    StateEngine,
    StateEngineConfig,
    VolatilityState,
    calculate_combined_volatility_score,
    calculate_confidence,
    calculate_direction_score,
    calculate_inventory_ratio,
    calculate_iv_regime,
    calculate_log_return,
    calculate_normalized_volatility,
    calculate_price_signal,
    calculate_realized_volatility,
    classify_direction,
    classify_inventory,
    classify_volatility,
    format_state_summary,
)


def _config(**overrides) -> StateEngineConfig:
    values = {
        "history_window_seconds": 120.0,
        "minimum_history_samples": 2,
        "realized_vol_window_seconds": 5.0,
        "realized_vol_baseline_seconds": 10.0,
        "direction_return_window_seconds": 5.0,
        "direction_confirmation_samples": 2,
        "max_position_notional": 1_000.0,
    }
    values.update(overrides)
    return StateEngineConfig(**values)


def _snapshot(
    timestamp: float,
    *,
    price: float = 100.0,
    valid: bool = True,
    book: float | None = 0.4,
    flow: float | None = 0.2,
    iv: float | None = None,
    option_expiry: str | None = None,
    atm_strike: float | None = None,
    iv_confidence: float | None = None,
    option_errors: list[str] | None = None,
    position: float | None = 0.0,
    position_notional: float | None = 0.0,
    account_available: bool = True,
    errors: list[str] | None = None,
) -> dict:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "mid_price": price,
        "depth_imbalance": book,
        "top_level_imbalance": None,
        "order_flow_imbalance": flow,
        "trade_data_available": flow is not None,
        "atm_iv": iv,
        "atm_call_iv": iv,
        "atm_put_iv": iv,
        "iv_confidence": iv_confidence if iv_confidence is not None else (1.0 if iv else 0.0),
        "iv_data_available": iv is not None,
        "option_expiry": option_expiry,
        "option_expiry_dte": 7.0 if option_expiry else None,
        "atm_strike": atm_strike,
        "atm_distance_pct": 0.01 if atm_strike else None,
        "option_data_age_seconds": 1.0 if iv is not None else None,
        "option_data_source": "test" if iv is not None else None,
        "option_data_errors": option_errors or [],
        "current_position": position,
        "position_notional": position_notional,
        "account_data_available": account_available,
        "data_valid": valid,
        "validation_errors": errors or [],
    }


def test_log_return_and_zero_volatility_series() -> None:
    assert calculate_log_return(100.0, 110.0) == pytest.approx(math.log(1.1))
    assert calculate_realized_volatility([0.0, 0.0, 0.0]) == 0.0


def test_realized_volatility_and_normalization() -> None:
    realized = calculate_realized_volatility([0.1, -0.1])
    assert realized == pytest.approx(0.1)
    assert calculate_normalized_volatility(0.2, 0.1) == pytest.approx(2.0)
    assert calculate_normalized_volatility(0.0, 0.0) == pytest.approx(1.0)


def test_iv_regime_and_combined_score() -> None:
    assert calculate_iv_regime(0.6, [0.4, 0.5, 0.5]) == pytest.approx(1.2)
    assert calculate_iv_regime(None, [0.4, 0.5]) is None
    assert calculate_combined_volatility_score(
        2.0,
        1.0,
        realized_vol_weight=0.75,
        iv_weight=0.25,
    ) == pytest.approx(1.75)
    assert calculate_combined_volatility_score(
        2.0,
        None,
        realized_vol_weight=0.75,
        iv_weight=0.25,
    ) == pytest.approx(2.0)


def test_volatility_entry_and_hysteresis_exit() -> None:
    assert (
        classify_volatility(
            1.50,
            VolatilityState.NORMAL,
            enter_threshold=1.50,
            exit_threshold=1.25,
        )
        is VolatilityState.HIGH
    )
    assert (
        classify_volatility(
            1.30,
            VolatilityState.HIGH,
            enter_threshold=1.50,
            exit_threshold=1.25,
        )
        is VolatilityState.HIGH
    )
    assert (
        classify_volatility(
            1.20,
            VolatilityState.HIGH,
            enter_threshold=1.50,
            exit_threshold=1.25,
        )
        is VolatilityState.NORMAL
    )


def test_direction_score_with_book_flow_and_price() -> None:
    score = calculate_direction_score(
        0.5,
        0.25,
        calculate_price_signal(0.001, 0.002),
        book_weight=0.45,
        flow_weight=0.30,
        price_weight=0.25,
    )
    assert score == pytest.approx(0.425)


def test_direction_score_renormalizes_when_ofi_is_unavailable() -> None:
    score = calculate_direction_score(
        0.5,
        None,
        0.25,
        book_weight=0.45,
        flow_weight=0.30,
        price_weight=0.25,
    )
    assert score == pytest.approx((0.5 * 0.45 + 0.25 * 0.25) / 0.70)


def test_direction_classification() -> None:
    assert (
        classify_direction(0.3, bullish_threshold=0.25, bearish_threshold=-0.25)
        is DirectionState.BULLISH
    )
    assert (
        classify_direction(-0.3, bullish_threshold=0.25, bearish_threshold=-0.25)
        is DirectionState.BEARISH
    )
    assert (
        classify_direction(0.1, bullish_threshold=0.25, bearish_threshold=-0.25)
        is DirectionState.NEUTRAL
    )


def test_direction_confirmation_logic() -> None:
    engine = StateEngine(_config())
    first = engine.update(_snapshot(0.0, book=0.8, flow=0.7))
    second = engine.update(_snapshot(5.0, book=0.8, flow=0.7))
    third = engine.update(_snapshot(10.0, book=0.8, flow=0.7))
    assert first.direction_state is DirectionState.INITIALIZING
    assert second.direction_state is DirectionState.INITIALIZING
    assert third.direction_state is DirectionState.BULLISH


def test_inventory_long_short_neutral_and_unknown() -> None:
    long_ratio, long_notional = calculate_inventory_ratio(2.0, 200.0, 100.0, 1_000.0)
    short_ratio, short_notional = calculate_inventory_ratio(-2.0, 200.0, 100.0, 1_000.0)
    neutral_ratio, _ = calculate_inventory_ratio(0.0, 0.0, 100.0, 1_000.0)
    unavailable_ratio, _ = calculate_inventory_ratio(None, 200.0, 100.0, 1_000.0)
    assert (long_ratio, long_notional) == pytest.approx((0.2, 200.0))
    assert (short_ratio, short_notional) == pytest.approx((-0.2, -200.0))
    assert neutral_ratio == 0.0
    assert unavailable_ratio is None
    assert classify_inventory(0.2, 0.1) is InventoryState.LONG
    assert classify_inventory(-0.2, 0.1) is InventoryState.SHORT
    assert classify_inventory(0.05, 0.1) is InventoryState.NEUTRAL
    assert classify_inventory(None, 0.1) is InventoryState.UNKNOWN


def test_engine_inventory_unavailable_is_not_neutral() -> None:
    engine = StateEngine(_config())
    state = engine.update(_snapshot(0.0, account_available=False, position=None))
    assert state.inventory_state is InventoryState.UNKNOWN
    assert state.inventory_ratio is None


def test_engine_accepts_the_actual_stage1_market_snapshot_model() -> None:
    snapshot = stage1_module.MarketSnapshot(
        timestamp="2026-08-23T00:00:00.000Z",
        connector="derive_perpetual",
        trading_pair="BTC-USDC",
        book_depth_levels=5,
        mid_price=100.0,
        depth_imbalance=0.4,
        order_flow_imbalance=0.2,
        trade_data_available=True,
        current_position=0.0,
        position_notional=0.0,
        account_data_available=True,
        data_valid=True,
    )
    state = StateEngine(_config()).update(snapshot)
    assert state.trading_pair == "BTC-USDC"
    assert state.inventory_state is InventoryState.NEUTRAL


def test_insufficient_history_and_invalid_snapshot_are_fail_closed() -> None:
    engine = StateEngine(_config(minimum_history_samples=3))
    initializing = engine.update(_snapshot(0.0))
    invalid = engine.update(
        _snapshot(5.0, valid=False, errors=["stale order-book tracker snapshot"])
    )
    assert initializing.state_valid is False
    assert initializing.volatility_state is VolatilityState.INITIALIZING
    assert "initializing" in initializing.reasons[0]
    assert invalid.state_valid is False
    assert "stale order-book tracker snapshot" in invalid.reasons
    assert engine.history_size == 1


def test_engine_uses_iv_when_available_but_falls_back_to_realized_vol() -> None:
    engine = StateEngine(_config())
    for index in range(8):
        state = engine.update(
            _snapshot(
                index * 5.0,
                price=100.0 + index * 0.1,
                iv=0.5 if index >= 2 else None,
            )
        )
    assert state.realized_volatility is not None
    assert state.iv_ratio is not None
    fallback_engine = StateEngine(_config())
    for index in range(8):
        fallback = fallback_engine.update(
            _snapshot(index * 5.0, price=100.0 + index * 0.1)
        )
    assert fallback.realized_volatility is not None
    assert fallback.iv_ratio is None
    assert fallback.volatility_score == pytest.approx(fallback.realized_volatility_ratio)


def test_iv_history_warms_separately_and_exposes_raw_iv_before_ratio() -> None:
    engine = StateEngine(_config(iv_minimum_samples=2))
    first = engine.update(_snapshot(0.0, iv=0.50, option_expiry="2026-08-28", atm_strike=100))
    second = engine.update(
        _snapshot(5.0, price=100.1, iv=0.60, option_expiry="2026-08-28", atm_strike=100)
    )
    third = engine.update(
        _snapshot(10.0, price=100.2, iv=0.70, option_expiry="2026-08-28", atm_strike=100)
    )

    assert first.atm_iv == 0.50
    assert second.atm_iv == 0.60
    assert second.iv_ratio is None
    assert second.iv_history_samples == 1
    assert second.iv_history_ready is False
    assert any("prior ATM IV observations" in reason for reason in second.reasons)
    assert third.iv_history_ready is True
    assert third.iv_history_samples == 2
    assert third.iv_ratio == pytest.approx(0.70 / 0.55)
    assert third.iv_change == pytest.approx(0.10)
    assert third.option_expiry == "2026-08-28"
    assert third.atm_strike == 100


def test_iv_missing_keeps_direction_path_independent_and_reports_errors() -> None:
    config = _config(iv_minimum_samples=1)
    with_iv = StateEngine(config)
    without_iv = StateEngine(config)
    for index in range(5):
        timestamp = index * 5.0
        common = {
            "timestamp": timestamp,
            "price": 100.0 + index * 0.1,
            "book": 0.6,
            "flow": 0.4,
        }
        state_with_iv = with_iv.update(
            _snapshot(**common, iv=0.8, option_errors=["put ticker missing"])
        )
        state_without_iv = without_iv.update(_snapshot(**common))

    assert state_with_iv.direction_score == pytest.approx(state_without_iv.direction_score)
    assert state_with_iv.direction_state is state_without_iv.direction_state
    assert state_with_iv.atm_iv == 0.8
    assert "options: put ticker missing" in state_with_iv.reasons
    assert state_without_iv.iv_ratio is None


def test_state_summary_formats_iv_as_percent_and_includes_selection() -> None:
    engine = StateEngine(_config(iv_minimum_samples=1))
    state = engine.update(
        _snapshot(
            0.0,
            iv=0.54,
            option_expiry="2026-08-28",
            atm_strike=100,
        )
    )
    summary = format_state_summary(state)

    assert "ATM IV: 54.00%" in summary
    assert "IV Ratio: initializing" in summary
    assert "IV Expiry: 2026-08-28" in summary
    assert "ATM Strike: 100" in summary


def test_confidence_reflects_optional_data_completeness() -> None:
    assert calculate_confidence(
        snapshot_valid=True,
        history_ready=True,
        realized_volatility_available=True,
        book_available=True,
        flow_available=True,
        iv_available=True,
    ) == 1.0
    assert calculate_confidence(
        snapshot_valid=True,
        history_ready=True,
        realized_volatility_available=True,
        book_available=True,
        flow_available=False,
        iv_available=False,
    ) == pytest.approx(0.85)
    assert calculate_confidence(
        snapshot_valid=False,
        history_ready=False,
        realized_volatility_available=False,
        book_available=False,
        flow_available=False,
        iv_available=False,
    ) == 0.0


def test_state_summary_is_deterministic_and_has_no_grid_fields() -> None:
    engine = StateEngine(_config())
    state = engine.update(_snapshot(0.0))
    summary = state.model_dump(mode="json")
    assert "reasons" in summary
    assert not any("grid" in key.lower() for key in summary)
