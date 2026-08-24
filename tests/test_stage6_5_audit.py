"""Focused Stage 6.5 audit-contract regression tests."""

from __future__ import annotations

from decimal import Decimal

from derive_options_mm.grid_engine import GridParameterConfig, calculate_grid_width
from evaluation.audit import (
    PositionLedger,
    _asof_iv_snapshots,
    _validate_no_lookahead,
    canonicalize_plans,
    inventory_feedback_audit,
    tp_parity_audit,
    volatility_decomposition,
)
from evaluation.data_loader import EvaluationFrame, parse_timestamp
from evaluation.fill_models import FillModelName
from evaluation.replay import ReplayResult


def _plan(timestamp: str, *, version: int = 1, **extra: object) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "plan_version": version,
        "mode": "normal",
        "enabled": True,
        "valid": True,
        **extra,
    }


def _frame(
    *,
    timestamp: str = "2026-01-01T00:00:10Z",
    state_timestamp: str | None = None,
    plan: dict[str, object] | None = None,
) -> EvaluationFrame:
    return EvaluationFrame(
        timestamp=timestamp,
        timestamp_seconds=parse_timestamp(timestamp) or 10.0,
        snapshot={"timestamp": timestamp, "mid_price": 100.0},
        state={
            "timestamp": state_timestamp or timestamp,
            "trading_pair": "BTC-USDC",
            "realized_volatility_ratio": 0.8,
            "iv_ratio": 1.4,
            "volatility_score": 0.95,
            "volatility_state": "normal",
            "direction_state": "neutral",
        },
        mode={"timestamp": timestamp, "mode": "normal"},
        plan=plan or _plan(timestamp),
    )


def test_canonical_plan_stream_collapses_duplicates_and_keeps_conflicts() -> None:
    timestamp = "2026-01-01T00:00:10Z"
    first = _plan(timestamp, version=1)
    exact = dict(first)
    conflict = _plan(timestamp, version=2, mode="defensive")

    result = canonicalize_plans([first, exact, conflict])

    assert len(result.canonical_records) == 1
    assert result.canonical_records[0]["plan_version"] == 2
    assert result.duplicate_timestamp_count == 2
    assert result.exact_duplicate_record_count == 1
    assert result.conflicting_timestamp_count == 1
    assert result.conflicting_extra_record_count == 2
    assert result.conflict_records[0]["source_indices"] == [0, 1, 2]


def test_controlled_validation_rows_are_excluded_but_remain_auditable() -> None:
    production = _plan("2026-01-01T00:00:10Z")
    controlled = _plan(
        "2026-01-01T00:00:20Z",
        validation_only=True,
        validation_stage="stage5e",
    )

    result = canonicalize_plans([production, controlled])

    assert len(result.canonical_records) == 1
    assert result.controlled_record_count == 1
    assert result.excluded_controlled_indices == (1,)
    assert result.canonical_records[0]["timestamp"] == production["timestamp"]


def test_recorded_width_formula_is_separate_from_asof_input_mismatch() -> None:
    config = GridParameterConfig()
    width, _, _ = calculate_grid_width(0.75, "defensive", config)
    timestamp = "2026-01-01T00:00:10Z"
    frame = _frame(
        timestamp=timestamp,
        plan=_plan(
            timestamp,
            mode="defensive",
            volatility_width_multiplier=0.75,
            mode_width_multiplier=1.5,
            total_grid_width_pct=float(width),
        ),
    )
    frame.state["volatility_score"] = 1.8
    frame.mode["mode"] = "defensive"

    result = volatility_decomposition([frame])

    assert result["summary"]["formula_width_pass"] is True
    assert result["summary"]["max_grid_width_error"] == 0.0
    assert result["summary"]["asof_input_width_mismatch_frames"] == 1
    assert result["rows"][0]["grid_width_error"] > 0


def test_iv_asof_carry_respects_the_requested_age_limit() -> None:
    snapshots = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "atm_iv": 0.8,
            "iv_data_available": True,
        },
        {
            "timestamp": "2026-01-01T00:00:31Z",
            "atm_iv": None,
            "iv_data_available": False,
        },
    ]

    short = _asof_iv_snapshots(snapshots, 30.0)
    long = _asof_iv_snapshots(snapshots, 60.0)

    assert short[1]["iv_data_available"] is False
    assert short[1]["atm_iv"] is None
    assert long[1]["iv_data_available"] is True
    assert long[1]["atm_iv"] == 0.8
    assert long[1]["option_data_age_seconds"] == 31.0


def test_position_ledger_handles_weighted_additions_crossing_and_signs() -> None:
    ledger = PositionLedger()
    ledger.apply("buy", "2", "100")
    ledger.apply("buy", "1", "110")
    assert ledger.average_entry_price == Decimal("103.3333333333333333333333333")

    ledger.apply("sell", "1", "120")
    assert ledger.realized_pnl == Decimal("16.6666666666666666666666667")
    ledger.apply("sell", "3", "90")
    assert ledger.net_amount == Decimal("-1")
    assert ledger.average_entry_price == Decimal("90")
    assert abs(float(ledger.realized_pnl) + 10.0) < 1e-12
    assert ledger.mark_to_market("80") == Decimal("10")


def test_inventory_feedback_aggregates_entry_and_tp_at_one_timestamp() -> None:
    result = ReplayResult(
        strategy="iv_adaptive_grid",
        fill_model=FillModelName.CONSERVATIVE_CROSS_THROUGH.value,
        events=[
            {
                "event": "ENTRY_FILLED",
                "timestamp": "2026-01-01T00:00:02Z",
                "timestamp_seconds": 2.0,
                "side": "buy",
                "level_id": "buy_0",
                "amount": 1.0,
            },
            {
                "event": "TP_FILLED",
                "timestamp": "2026-01-01T00:00:02Z",
                "timestamp_seconds": 2.0,
                "side": "sell",
                "level_id": "sell_0",
                "amount": 0.5,
            },
        ],
        ticks=[
            {"timestamp_seconds": 1.0, "position_base": 0.0},
            {
                "timestamp_seconds": 2.0,
                "position_base": 0.5,
                "center_price": 100.0,
                "buy_allocation_pct": 0.5,
                "sell_allocation_pct": 0.5,
            },
        ],
    )

    audit = inventory_feedback_audit({("iv", "conservative"): result})

    assert audit["pass"] is True
    assert audit["checks"][0]["expected_inventory_delta"] == 0.5
    assert audit["checks"][0]["same_timestamp_fill_count"] == 2


def test_tp_parity_and_lookahead_audits_are_explicit() -> None:
    timestamp = "2026-01-01T00:00:10Z"
    plan = _plan(
        timestamp,
        center_price=100.0,
        total_grid_width_pct=0.01,
        buy_levels=[
            {"side": "buy", "level_index": 0, "theoretical_price": 99.0, "quote_amount": 100.0},
            {"side": "buy", "level_index": 1, "theoretical_price": 98.0, "quote_amount": 100.0},
        ],
        sell_levels=[
            {"side": "sell", "level_index": 0, "theoretical_price": 101.0, "quote_amount": 100.0},
            {"side": "sell", "level_index": 1, "theoretical_price": 102.0, "quote_amount": 100.0},
        ],
    )
    frame = _frame(plan=plan)
    parity = tp_parity_audit([frame], sample_limit=4)
    lookahead = _validate_no_lookahead(
        [_frame(state_timestamp="2026-01-01T00:00:11Z")]
    )

    assert parity["pass"] is True
    assert parity["sample_count"] == 4
    assert lookahead["pass"] is False
    assert [row["input"] for row in lookahead["violations"]] == ["state"]
