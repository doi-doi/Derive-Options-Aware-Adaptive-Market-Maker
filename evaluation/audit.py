"""Stage 6.5 audit and robustness analysis.

This module audits the existing Stage 6 evaluator without changing the Stage
1--4 strategy or the Stage 5 execution controller.  It keeps source records
immutable, makes duplicate/conflict handling explicit, and treats every
counterfactual as an offline analysis of already-recorded inputs.
"""

# The audit report strings are deliberately human-readable evidence text.
# Keep code-style checks active while allowing those report literals.
# ruff: noqa: E501

from __future__ import annotations

import json
import math
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from derive_options_mm.grid_engine import (
    GridMode,
    GridParameterConfig,
    build_grid_plan,
    calculate_grid_width,
)
from derive_options_mm.mode_selector import (
    GridModeDecision,
    determine_candidate_mode,
)
from derive_options_mm.state_engine import (
    MarketState,
    StateEngineConfig,
    VolatilityState,
    calculate_combined_volatility_score,
    classify_volatility,
)
from integrations.hummingbot.derive_adaptive_grid.execution_logic import (
    ExecutionPolicy,
    ExecutionSide,
    GridPlanView,
    PlanLevel,
    _take_profit_pct,
)

from .baselines import StrategyVariant, static_geometric_plan
from .data_loader import (
    AsOfSeries,
    EvaluationDataset,
    EvaluationFrame,
    finite_float,
    iso_timestamp,
    parse_timestamp,
)
from .fill_models import FillModelName
from .metrics import iv_regime, summarize_replay
from .replay import ReplayConfig, ReplayResult, _adjacent_tp, run_replay

IV_FRESHNESS_THRESHOLDS = (30.0, 60.0, 120.0, 300.0)
SUBPERIOD_SECONDS = 3600.0
SUBPERIOD_WINDOWS_SECONDS = (1800.0, 3600.0)
RELATIVE_IV_LOW = 0.90
RELATIVE_IV_HIGH = 1.10
JOINT_BUCKET_THRESHOLD = 1.0
_PERCENTILES = (50.0, 90.0, 95.0)


def _json_safe(value: Any) -> Any:
    """Convert audit values to finite JSON-compatible values."""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _mean(values: Iterable[float]) -> float | None:
    finite = [number for value in values if (number := finite_float(value)) is not None]
    return statistics.mean(finite) if finite else None


def _median(values: Iterable[float]) -> float | None:
    finite = [number for value in values if (number := finite_float(value)) is not None]
    return statistics.median(finite) if finite else None


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _numeric_summary(values: Iterable[Any]) -> dict[str, Any]:
    finite = [number for value in values if (number := finite_float(value)) is not None]
    return {
        "count": len(finite),
        "mean": _mean(finite),
        "median": _median(finite),
        "p90": _percentile(finite, 90.0),
        "p95": _percentile(finite, 95.0),
        "maximum": max(finite) if finite else None,
        "minimum": min(finite) if finite else None,
        "stdev": statistics.stdev(finite) if len(finite) > 1 else 0.0,
    }


def _canonical_record(record: Mapping[str, Any]) -> str:
    return json.dumps(_json_safe(record), sort_keys=True, separators=(",", ":"), allow_nan=False)


def is_controlled_plan(record: Mapping[str, Any]) -> bool:
    """Identify explicit Stage 5 validation artifacts without guessing from mode."""

    if record.get("validation_only") is True:
        return True
    explicit_fields = (
        "validation_stage",
        "validation_reason",
        "validation_label",
        "source_kind",
        "producer",
        "artifact_kind",
    )
    text = " ".join(str(record.get(field, "")) for field in explicit_fields).lower()
    text += " " + " ".join(str(item) for item in record.get("reasons", []) or [])
    markers = (
        "stage5e",
        "stage5f",
        "stage 5e",
        "stage 5f",
        "controlled validation",
        "validation-only",
        "validation only",
        "near-touch maker-fill",
        "closest representable passive",
    )
    return any(marker in text for marker in markers)


def _validation_stage(record: Mapping[str, Any]) -> str:
    """Return an explicit or safely detected Stage 5 validation label."""

    explicit = str(record.get("validation_stage", "")).strip()
    if explicit:
        return explicit
    text = " ".join(
        str(record.get(field, ""))
        for field in ("validation_reason", "validation_label", "source_kind", "producer")
    ).lower()
    if "stage5e" in text or "stage 5e" in text:
        return "stage5e"
    if "stage5f" in text or "stage 5f" in text:
        return "stage5f"
    return "unspecified"


@dataclass(frozen=True)
class PlanDeduplication:
    """A complete duplicate/conflict ledger for one plan stream."""

    raw_records: tuple[dict[str, Any], ...]
    canonical_records: tuple[dict[str, Any], ...]
    duplicate_timestamp_count: int
    duplicate_timestamp_groups: int
    exact_duplicate_record_count: int
    exact_duplicate_groups: int
    duplicate_plan_version_count: int
    duplicate_plan_version_values: int
    conflicting_timestamp_count: int
    conflicting_record_count: int
    conflicting_extra_record_count: int
    controlled_record_count: int
    controlled_indices: tuple[int, ...]
    exact_duplicate_indices: tuple[int, ...]
    excluded_controlled_indices: tuple[int, ...]
    conflict_records: tuple[dict[str, Any], ...]
    plan_version_counts: dict[str, int]
    rule: str

    def to_record(self) -> dict[str, Any]:
        return _json_safe(
            {
                "raw_record_count": len(self.raw_records),
                "canonical_record_count": len(self.canonical_records),
                "duplicate_timestamp_count": self.duplicate_timestamp_count,
                "duplicate_timestamp_groups": self.duplicate_timestamp_groups,
                "exact_duplicate_record_count": self.exact_duplicate_record_count,
                "exact_duplicate_groups": self.exact_duplicate_groups,
                "duplicate_plan_version_count": self.duplicate_plan_version_count,
                "duplicate_plan_version_values": self.duplicate_plan_version_values,
                "conflicting_timestamp_count": self.conflicting_timestamp_count,
                "conflicting_record_count": self.conflicting_record_count,
                "conflicting_extra_record_count": self.conflicting_extra_record_count,
                "controlled_record_count": self.controlled_record_count,
                "controlled_indices": list(self.controlled_indices),
                "excluded_controlled_indices": list(self.excluded_controlled_indices),
                "plan_version_counts": self.plan_version_counts,
                "rule": self.rule,
                "conflicts": list(self.conflict_records),
            }
        )


def _timestamp_key(record: Mapping[str, Any], index: int) -> tuple[str, float | None]:
    seconds = parse_timestamp(record.get("timestamp"))
    if seconds is None:
        return f"invalid:{index}:{record.get('timestamp')}", None
    return f"{seconds:.9f}", seconds


def canonicalize_plans(records: Sequence[Mapping[str, Any]]) -> PlanDeduplication:
    """Build a production-only canonical stream and retain every conflict ledger.

    The source order is the final tie-breaker.  Within each timestamp group
    exact duplicate objects collapse to one representative; if multiple
    distinct production objects share the timestamp, the last production row
    is selected for the canonical stream and all alternatives remain in the
    conflict report.  Explicit validation-only rows are excluded from the
    canonical stream, while their source indices remain auditable.
    """

    raw = tuple(dict(record) for record in records)
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, record in enumerate(raw):
        key, _ = _timestamp_key(record, index)
        grouped[key].append((index, record))

    fingerprints = Counter(_canonical_record(record) for record in raw)
    exact_duplicate_indices: list[int] = []
    seen_fingerprints: set[str] = set()
    for index, record in enumerate(raw):
        fingerprint = _canonical_record(record)
        if fingerprint in seen_fingerprints:
            exact_duplicate_indices.append(index)
        else:
            seen_fingerprints.add(fingerprint)

    plan_versions = Counter(
        str(record.get("plan_version")) for record in raw if record.get("plan_version") is not None
    )
    controlled_indices = tuple(
        index for index, record in enumerate(raw) if is_controlled_plan(record)
    )

    conflicts: list[dict[str, Any]] = []
    selected: list[tuple[float | None, int, dict[str, Any]]] = []
    duplicate_timestamp_count = 0
    duplicate_timestamp_groups = 0
    conflicting_record_count = 0
    conflicting_extra_count = 0
    excluded_controlled: list[int] = []

    def group_sort(item: tuple[str, list[tuple[int, dict[str, Any]]]]) -> tuple[float, int]:
        key, group = item
        seconds = parse_timestamp(group[0][1].get("timestamp"))
        return (seconds if seconds is not None else float("inf"), group[0][0])

    for _key, group in sorted(grouped.items(), key=group_sort):
        if len(group) > 1:
            duplicate_timestamp_groups += 1
            duplicate_timestamp_count += len(group) - 1
        production = [(index, record) for index, record in group if not is_controlled_plan(record)]
        for index, _ in group:
            if index not in {item[0] for item in production}:
                excluded_controlled.append(index)
        if not production:
            continue
        production_fingerprints = {_canonical_record(record) for _, record in production}
        if len(production_fingerprints) > 1:
            conflicting_record_count += len(production)
            conflicting_extra_count += len(production) - 1
            conflicts.append(
                {
                    "timestamp": production[-1][1].get("timestamp"),
                    "source_indices": [index for index, _ in production],
                    "selected_source_index": production[-1][0],
                    "plan_versions": [record.get("plan_version") for _, record in production],
                    "modes": [record.get("mode") for _, record in production],
                    "fingerprint_count": len(production_fingerprints),
                    "controlled_source_indices": [
                        index for index, _ in group if is_controlled_plan(raw[index])
                    ],
                }
            )
        seconds = parse_timestamp(production[-1][1].get("timestamp"))
        selected.append((seconds, production[-1][0], production[-1][1]))

    selected.sort(key=lambda item: (item[0] if item[0] is not None else float("inf"), item[1]))
    canonical = tuple(record for _, _, record in selected)
    exact_groups = sum(count > 1 for count in fingerprints.values())
    exact_count = sum(max(0, count - 1) for count in fingerprints.values())
    duplicate_version_values = sum(count > 1 for count in plan_versions.values())
    duplicate_version_count = sum(max(0, count - 1) for count in plan_versions.values())
    return PlanDeduplication(
        raw_records=raw,
        canonical_records=canonical,
        duplicate_timestamp_count=duplicate_timestamp_count,
        duplicate_timestamp_groups=duplicate_timestamp_groups,
        exact_duplicate_record_count=exact_count,
        exact_duplicate_groups=exact_groups,
        duplicate_plan_version_count=duplicate_version_count,
        duplicate_plan_version_values=duplicate_version_values,
        conflicting_timestamp_count=len(conflicts),
        conflicting_record_count=conflicting_record_count,
        conflicting_extra_record_count=conflicting_extra_count,
        controlled_record_count=len(controlled_indices),
        controlled_indices=controlled_indices,
        exact_duplicate_indices=tuple(exact_duplicate_indices),
        excluded_controlled_indices=tuple(sorted(excluded_controlled)),
        conflict_records=tuple(conflicts),
        plan_version_counts=dict(sorted(plan_versions.items())),
        rule=(
            "sort by parsed timestamp and source index; exclude explicit validation-only rows; "
            "retain one exact duplicate; for conflicting production rows at one timestamp, "
            "select the last source row and retain every alternative in conflicts"
        ),
    )


def frames_from_plans(
    dataset: EvaluationDataset,
    plan_records: Sequence[Mapping[str, Any]],
    *,
    deduplicate_timestamps: bool = False,
) -> list[EvaluationFrame]:
    """Join arbitrary plan rows to prior Stage 1--3 observations."""

    joiners = {
        name: AsOfSeries(stream.sorted_records())
        for name, stream in {
            "snapshots": dataset.snapshots,
            "states": dataset.states,
            "modes": dataset.modes,
        }.items()
    }
    ordered = sorted(
        (dict(record) for record in plan_records),
        key=lambda record: (
            parse_timestamp(record.get("timestamp"))
            if parse_timestamp(record.get("timestamp")) is not None
            else float("inf")
        ),
    )
    if deduplicate_timestamps:
        by_timestamp: dict[str, dict[str, Any]] = {}
        for record in ordered:
            timestamp = parse_timestamp(record.get("timestamp"))
            if timestamp is not None:
                by_timestamp[f"{timestamp:.9f}"] = record
        ordered = list(by_timestamp.values())
        ordered.sort(key=lambda record: parse_timestamp(record.get("timestamp")) or float("inf"))

    frames: list[EvaluationFrame] = []
    for plan in ordered:
        timestamp_seconds = parse_timestamp(plan.get("timestamp"))
        if timestamp_seconds is None:
            continue
        snapshot = joiners["snapshots"].at_or_before(timestamp_seconds)
        state = joiners["states"].at_or_before(timestamp_seconds)
        mode = joiners["modes"].at_or_before(timestamp_seconds)
        if snapshot is None or state is None or mode is None:
            continue
        pair = str(plan.get("trading_pair", "BTC-USDC"))
        if any(
            str(item.get("trading_pair", pair)) not in {pair, ""}
            for item in (snapshot, state, mode)
        ):
            continue
        frames.append(
            EvaluationFrame(
                timestamp=str(plan.get("timestamp")),
                timestamp_seconds=timestamp_seconds,
                snapshot=snapshot,
                state=state,
                mode=mode,
                plan=plan,
            )
        )
    return frames


def _iv_value_and_age(frame: EvaluationFrame) -> tuple[float | None, float | None]:
    value = finite_float(frame.state.get("atm_iv"))
    state_timestamp = parse_timestamp(frame.state.get("timestamp"))
    age = frame.timestamp_seconds - state_timestamp if state_timestamp is not None else None
    return value, age if age is None or age >= 0 else None


def relative_iv_bucket(value: Any) -> str:
    number = finite_float(value)
    if number is None:
        return "unknown"
    if number < RELATIVE_IV_LOW:
        return "low"
    if number > RELATIVE_IV_HIGH:
        return "high"
    return "normal"


def rv_iv_joint_bucket(rv_ratio: Any, iv_ratio: Any) -> str:
    rv = finite_float(rv_ratio)
    iv = finite_float(iv_ratio)
    if rv is None or iv is None:
        return "unknown"
    rv_name = "high" if rv >= JOINT_BUCKET_THRESHOLD else "low"
    iv_name = "high" if iv >= JOINT_BUCKET_THRESHOLD else "low"
    return f"rv_{rv_name}_iv_{iv_name}"


def iv_regime_audit(frames: Sequence[EvaluationFrame]) -> dict[str, Any]:
    """Check boundary labels and observed frame-level IV regime frequencies."""

    boundary_values = (0.89, 0.90, 1.0, 1.10, 1.11)
    boundary_checks = [
        {
            "iv_ratio": value,
            "relative_iv_bucket": relative_iv_bucket(value),
            "replay_iv_regime": iv_regime(value),
            "match": relative_iv_bucket(value) == iv_regime(value),
        }
        for value in boundary_values
    ]
    frame_counts = Counter(
        relative_iv_bucket(frame.state.get("iv_ratio")) for frame in frames
    )
    return _json_safe(
        {
            "relative_bucket_thresholds": {
                "low": RELATIVE_IV_LOW,
                "high": RELATIVE_IV_HIGH,
            },
            "frame_bucket_counts": dict(sorted(frame_counts.items())),
            "boundary_checks": boundary_checks,
            "pass": all(row["match"] for row in boundary_checks),
            "note": "Relative IV labels and replay entry labels use the same 0.90/1.10 boundaries; RV/IV joint buckets remain a separate 1.0-threshold diagnostic.",
        }
    )


def iv_coverage_audit(
    dataset: EvaluationDataset,
    frames: Sequence[EvaluationFrame],
    *,
    thresholds: Sequence[float] = IV_FRESHNESS_THRESHOLDS,
) -> dict[str, Any]:
    """Measure raw, state, common-window, and carried-IV freshness."""

    def coverage(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
        values = [finite_float(record.get(field)) for record in records]
        available = sum(value is not None for value in values)
        return {
            "records": len(records),
            "available_records": available,
            "coverage_pct": available / len(records) * 100 if records else 0.0,
            "missing_records": len(records) - available,
        }

    ages: list[float] = []
    iv_values: list[float] = []
    for frame in frames:
        value, age = _iv_value_and_age(frame)
        if value is not None:
            iv_values.append(value)
            if age is not None:
                ages.append(age)

    freshness: dict[str, Any] = {}
    for threshold in thresholds:
        fresh = stale = missing = rv_fallback = 0
        effective = Counter()
        for frame in frames:
            value, age = _iv_value_and_age(frame)
            rv_available = finite_float(frame.state.get("realized_volatility_ratio")) is not None
            if value is None:
                missing += 1
                if rv_available:
                    rv_fallback += 1
                    effective["missing_iv_rv_fallback"] += 1
                else:
                    effective["missing_iv_no_rv"] += 1
            elif age is None or age <= threshold:
                fresh += 1
                effective["fresh_iv"] += 1
            else:
                stale += 1
                if rv_available:
                    rv_fallback += 1
                    effective["stale_iv_rv_fallback"] += 1
                else:
                    effective["stale_iv_no_rv"] += 1
        freshness[str(int(threshold))] = {
            "threshold_seconds": threshold,
            "fresh_iv_frames": fresh,
            "stale_iv_frames": stale,
            "missing_iv_frames": missing,
            "rv_fallback_frames": rv_fallback,
            "effective_source_counts": dict(sorted(effective.items())),
        }

    option_ages = [
        age
        for record in dataset.snapshots.records
        if (age := finite_float(record.get("option_data_age_seconds"))) is not None
    ]
    return _json_safe(
        {
            "definitions": {
                "snapshot_atm_iv": "finite snapshot.atm_iv value",
                "state_atm_iv": "finite as-of state.atm_iv value",
                "carried_iv_age": "plan frame timestamp minus the selected state timestamp",
                "fresh_iv": "finite state ATM IV with carried age <= threshold",
                "stale_iv": "finite state ATM IV with carried age > threshold",
                "rv_fallback": "missing or stale IV while realized_volatility_ratio is finite",
                "missing_iv": "state ATM IV is unavailable, regardless of RV fallback",
            },
            "snapshot_coverage": coverage(dataset.snapshots.records, "atm_iv"),
            "state_coverage": coverage(dataset.states.records, "atm_iv"),
            "common_window_coverage": coverage([frame.state for frame in frames], "atm_iv"),
            "asof_carried_iv_coverage": {
                "frames": len(frames),
                "finite_iv_frames": len(iv_values),
                "coverage_pct": len(iv_values) / len(frames) * 100 if frames else 0.0,
                "age_seconds": _numeric_summary(ages),
            },
            "raw_option_data_age_seconds": _numeric_summary(option_ages),
            "freshness_sensitivity": freshness,
        }
    )


def _component_row(
    frame: EvaluationFrame,
    *,
    state_config: StateEngineConfig,
    grid_config: GridParameterConfig,
) -> dict[str, Any]:
    state = frame.state
    rv_ratio = finite_float(state.get("realized_volatility_ratio"))
    iv_ratio = finite_float(state.get("iv_ratio"))
    score = finite_float(state.get("volatility_score"))
    rv_weight = state_config.realized_vol_weight if rv_ratio is not None else 0.0
    iv_weight = state_config.iv_weight if iv_ratio is not None else 0.0
    total_weight = rv_weight + iv_weight
    rv_effective_weight = rv_weight / total_weight if total_weight else 0.0
    iv_effective_weight = iv_weight / total_weight if total_weight else 0.0
    rv_contribution = rv_ratio * rv_effective_weight if rv_ratio is not None else None
    iv_contribution = iv_ratio * iv_effective_weight if iv_ratio is not None else None
    expected_score = calculate_combined_volatility_score(
        rv_ratio,
        iv_ratio,
        realized_vol_weight=state_config.realized_vol_weight,
        iv_weight=state_config.iv_weight,
    )
    actual_mode = str(frame.mode.get("mode", "pause"))
    try:
        expected_width, expected_vol_multiplier, expected_mode_multiplier = calculate_grid_width(
            expected_score,
            GridMode(actual_mode),
            grid_config,
        )
        expected_width_value = float(expected_width)
        expected_vol_value = float(expected_vol_multiplier)
        expected_mode_value = float(expected_mode_multiplier)
    except (TypeError, ValueError, ArithmeticError):
        expected_width_value = expected_vol_value = expected_mode_value = None
    actual_width = finite_float(frame.plan.get("total_grid_width_pct"))
    recorded_volatility_multiplier = finite_float(
        frame.plan.get("volatility_width_multiplier")
    )
    recorded_mode_multiplier = finite_float(frame.plan.get("mode_width_multiplier"))
    plan_enabled = bool(frame.plan.get("enabled", False))
    if not plan_enabled or actual_mode == GridMode.PAUSE.value:
        expected_recorded_width_value = 0.0
    elif (
        recorded_volatility_multiplier is not None
        and recorded_mode_multiplier is not None
        and grid_config.base_grid_width_pct > 0
    ):
        raw_recorded_width = (
            float(grid_config.base_grid_width_pct)
            * recorded_volatility_multiplier
            * recorded_mode_multiplier
        )
        expected_recorded_width_value = min(
            float(grid_config.max_grid_width_pct),
            max(float(grid_config.min_grid_width_pct), raw_recorded_width),
        )
    else:
        expected_recorded_width_value = None
    return {
        "timestamp": frame.timestamp,
        "rv_ratio": rv_ratio,
        "rv_weight": rv_effective_weight,
        "rv_contribution": rv_contribution,
        "iv_ratio": iv_ratio,
        "iv_weight": iv_effective_weight,
        "iv_contribution": iv_contribution,
        "combined_volatility_score": score,
        "expected_combined_volatility_score": expected_score,
        "combined_score_error": (
            abs(score - expected_score)
            if score is not None and expected_score is not None
            else None
        ),
        "expected_volatility_width_multiplier": expected_vol_value,
        "recorded_volatility_width_multiplier": recorded_volatility_multiplier,
        "recorded_mode_width_multiplier": recorded_mode_multiplier,
        "expected_mode_width_multiplier": expected_mode_value,
        "expected_grid_width": expected_width_value,
        "expected_grid_width_from_recorded_multipliers": expected_recorded_width_value,
        "recorded_grid_width": actual_width,
        "grid_width_error": (
            abs(actual_width - expected_width_value)
            if actual_width is not None and expected_width_value is not None
            else None
        ),
        "recorded_grid_width_formula_error": (
            abs(actual_width - expected_recorded_width_value)
            if actual_width is not None and expected_recorded_width_value is not None
            else None
        ),
        "final_width_multiplier": (
            actual_width / float(grid_config.base_grid_width_pct)
            if actual_width is not None and grid_config.base_grid_width_pct > 0
            else None
        ),
        "mode": actual_mode,
        "volatility_state": state.get("volatility_state"),
        "direction_state": state.get("direction_state"),
    }


def volatility_decomposition(
    frames: Sequence[EvaluationFrame],
    *,
    state_config: StateEngineConfig | None = None,
    grid_config: GridParameterConfig | None = None,
) -> dict[str, Any]:
    """Audit Stage 2 score and Stage 4 width formulas frame by frame."""

    state_cfg = state_config or StateEngineConfig()
    grid_cfg = grid_config or GridParameterConfig()
    rows = [_component_row(frame, state_config=state_cfg, grid_config=grid_cfg) for frame in frames]
    rv_contributions = [
        value for row in rows if (value := finite_float(row.get("rv_contribution"))) is not None
    ]
    iv_contributions = [
        value for row in rows if (value := finite_float(row.get("iv_contribution"))) is not None
    ]
    rv_variance = statistics.pvariance(rv_contributions) if len(rv_contributions) > 1 else 0.0
    iv_variance = statistics.pvariance(iv_contributions) if len(iv_contributions) > 1 else 0.0
    variance_total = rv_variance + iv_variance
    score_errors = [
        value
        for row in rows
        if (value := finite_float(row.get("combined_score_error"))) is not None
    ]
    asof_width_errors = [
        value for row in rows if (value := finite_float(row.get("grid_width_error"))) is not None
    ]
    recorded_width_errors = [
        value
        for row in rows
        if (value := finite_float(row.get("recorded_grid_width_formula_error"))) is not None
    ]
    return _json_safe(
        {
            "rows": rows,
            "summary": {
                "frames": len(rows),
                "mean_absolute_rv_contribution": _mean(abs(value) for value in rv_contributions),
                "mean_absolute_iv_contribution": _mean(abs(value) for value in iv_contributions),
                "rv_contribution_variance": rv_variance,
                "iv_contribution_variance": iv_variance,
                "rv_variance_share": rv_variance / variance_total if variance_total else 0.0,
                "iv_variance_share": iv_variance / variance_total if variance_total else 0.0,
                "max_combined_score_error": max(score_errors) if score_errors else None,
                "max_grid_width_error": max(recorded_width_errors)
                if recorded_width_errors
                else None,
                "max_asof_grid_width_error": max(asof_width_errors)
                if asof_width_errors
                else None,
                "formula_score_pass": max(score_errors, default=0.0) <= 1e-9,
                "formula_width_pass": max(recorded_width_errors, default=0.0) <= 1e-9,
                "asof_input_width_mismatch_frames": sum(
                    value > 1e-9 for value in asof_width_errors
                ),
            },
        }
    )


def _mode_profile_name(mode: str) -> str:
    return {
        "normal": "standard",
        "defensive": "risk_reduced",
        "long_bias": "long_bias",
        "short_bias": "short_bias",
        "pause": "disabled",
    }.get(mode, "disabled")


def _counterfactual_state(frame: EvaluationFrame, rv_score: float | None) -> MarketState:
    state = MarketState.model_validate(frame.state)
    if rv_score is None:
        return state.model_copy(
            update={
                "volatility_score": None,
                "iv_ratio": None,
                "atm_iv": None,
                "volatility_state": VolatilityState.INITIALIZING,
                "state_valid": False,
            }
        )
    volatility_state = classify_volatility(
        rv_score,
        VolatilityState.NORMAL,
        enter_threshold=StateEngineConfig().high_vol_enter_threshold,
        exit_threshold=StateEngineConfig().high_vol_exit_threshold,
    )
    return state.model_copy(
        update={
            "volatility_score": rv_score,
            "iv_ratio": None,
            "atm_iv": None,
            "iv_change": None,
            "volatility_state": volatility_state,
        }
    )


def _candidate_decision(
    frame: EvaluationFrame,
    state: MarketState,
    mode: GridMode,
    reasons: Sequence[str],
) -> GridModeDecision:
    raw = dict(frame.mode)
    raw.update(
        {
            "timestamp": frame.timestamp,
            "trading_pair": state.trading_pair,
            "mode": mode.value,
            "previous_mode": None,
            "transition_occurred": False,
            "volatility_state": state.volatility_state.value,
            "volatility_score": state.volatility_score,
            "direction_state": state.direction_state.value,
            "direction_score": state.direction_score,
            "inventory_state": state.inventory_state.value,
            "inventory_ratio": state.inventory_ratio,
            "confidence": state.confidence,
            "valid": state.state_valid,
            "recommended_profile": _mode_profile_name(mode.value),
            "reasons": list(reasons),
        }
    )
    return GridModeDecision.model_validate(raw)


def counterfactual_iv_impact(
    frames: Sequence[EvaluationFrame],
    *,
    grid_config: GridParameterConfig | None = None,
    state_config: StateEngineConfig | None = None,
) -> dict[str, Any]:
    """Compare full and RV-only volatility/geometry decisions per frame."""

    grid_cfg = grid_config or GridParameterConfig()
    state_cfg = state_config or StateEngineConfig()
    rows: list[dict[str, Any]] = []
    for frame in frames:
        rv_ratio = finite_float(frame.state.get("realized_volatility_ratio"))
        iv_ratio = finite_float(frame.state.get("iv_ratio"))
        full_score = calculate_combined_volatility_score(
            rv_ratio,
            iv_ratio,
            realized_vol_weight=state_cfg.realized_vol_weight,
            iv_weight=state_cfg.iv_weight,
        )
        rv_score = calculate_combined_volatility_score(
            rv_ratio,
            None,
            realized_vol_weight=state_cfg.realized_vol_weight,
            iv_weight=state_cfg.iv_weight,
        )
        try:
            full_state = MarketState.model_validate(frame.state)
            full_candidate = determine_candidate_mode(full_state)
            rv_state = _counterfactual_state(frame, rv_score)
            rv_candidate = determine_candidate_mode(rv_state)
            full_decision = _candidate_decision(
                frame, full_state, full_candidate.mode, full_candidate.reasons
            )
            rv_decision = _candidate_decision(
                frame, rv_state, rv_candidate.mode, rv_candidate.reasons
            )
            full_plan = build_grid_plan(frame.snapshot, full_state, full_decision, grid_cfg)
            rv_plan = build_grid_plan(frame.snapshot, rv_state, rv_decision, grid_cfg)
            full_width = float(full_plan.total_grid_width_pct)
            rv_width = float(rv_plan.total_grid_width_pct)
            full_capital = float(full_plan.effective_quote_amount)
            rv_capital = float(rv_plan.effective_quote_amount)
            full_levels = full_plan.buy_levels_count + full_plan.sell_levels_count
            rv_levels = rv_plan.buy_levels_count + rv_plan.sell_levels_count
        except (TypeError, ValueError, ArithmeticError) as exc:
            full_width = rv_width = full_capital = rv_capital = None
            full_levels = rv_levels = None
            full_candidate = rv_candidate = None
            rows.append(
                {
                    "timestamp": frame.timestamp,
                    "error": f"{type(exc).__name__}: {exc}",
                    "full_score": full_score,
                    "rv_only_score": rv_score,
                }
            )
            continue
        rows.append(
            {
                "timestamp": frame.timestamp,
                "rv_ratio": rv_ratio,
                "iv_ratio": iv_ratio,
                "full_score": full_score,
                "rv_only_score": rv_score,
                "delta_volatility_score": (
                    full_score - rv_score
                    if full_score is not None and rv_score is not None
                    else None
                ),
                "full_width": full_width,
                "rv_only_width": rv_width,
                "delta_grid_width": (
                    full_width - rv_width
                    if full_width is not None and rv_width is not None
                    else None
                ),
                "full_mode": full_candidate.mode.value if full_candidate else None,
                "rv_only_mode": rv_candidate.mode.value if rv_candidate else None,
                "full_volatility_state": full_state.volatility_state.value,
                "rv_only_volatility_state": rv_state.volatility_state.value,
                "delta_capital": (
                    full_capital - rv_capital
                    if full_capital is not None and rv_capital is not None
                    else None
                ),
                "full_capital": full_capital,
                "rv_only_capital": rv_capital,
                "delta_level_count": (
                    full_levels - rv_levels
                    if full_levels is not None and rv_levels is not None
                    else None
                ),
                "full_level_count": full_levels,
                "rv_only_level_count": rv_levels,
            }
        )

    def delta_values(field: str) -> list[float]:
        return [value for row in rows if (value := finite_float(row.get(field))) is not None]

    score_values = delta_values("delta_volatility_score")
    width_values = delta_values("delta_grid_width")
    capital_values = delta_values("delta_capital")
    levels_values = delta_values("delta_level_count")
    score_threshold_counts = {
        f"greater_than_{threshold:g}_pct": sum(
            abs(delta) > threshold / 100.0 * abs(finite_float(row.get("rv_only_score")) or 1.0)
            for row in rows
            if (delta := finite_float(row.get("delta_volatility_score"))) is not None
        )
        for threshold in (1.0, 5.0, 10.0)
    }
    width_gt_5 = sum(
        abs(delta) > 0.05 * abs(finite_float(row.get("rv_only_width")) or 1.0)
        for row in rows
        if (delta := finite_float(row.get("delta_grid_width"))) is not None
    )
    return _json_safe(
        {
            "definitions": {
                "full": "stateless candidate using recorded RV and IV ratios",
                "rv_only": "stateless candidate using the same RV ratio with IV removed",
                "mode": "candidate mode from the same ModeSelector thresholds; hysteresis is not re-run",
                "capital": "effective theoretical quote amount from the candidate Stage 4 plan",
            },
            "rows": rows,
            "summary": {
                "frames": len(rows),
                "score": _numeric_summary(score_values),
                "grid_width": _numeric_summary(width_values),
                "capital": _numeric_summary(capital_values),
                "level_count": _numeric_summary(levels_values),
                "score_change_threshold_counts": score_threshold_counts,
                "frames_iv_changes_volatility_state": sum(
                    row.get("full_volatility_state") is not None
                    and row.get("full_volatility_state")
                    != row.get("rv_only_volatility_state")
                    for row in rows
                ),
                "frames_iv_changes_candidate_mode": sum(
                    row.get("full_mode") is not None
                    and row.get("full_mode") != row.get("rv_only_mode")
                    for row in rows
                ),
                "frames_iv_changes_grid_width_gt_5_pct": width_gt_5,
                "frames_iv_changes_level_count": sum(
                    row.get("full_level_count") != row.get("rv_only_level_count")
                    for row in rows
                    if row.get("full_level_count") is not None
                ),
            },
        }
    )


@dataclass
class PositionLedger:
    """Signed weighted-average position ledger used by accounting audits."""

    net_amount: Decimal = Decimal(0)
    average_entry_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)

    def apply(
        self, side: str, amount: Decimal | float | str, price: Decimal | float | str
    ) -> Decimal:
        quantity = _decimal(amount, Decimal(0)) or Decimal(0)
        trade_price = _decimal(price, Decimal(0)) or Decimal(0)
        if quantity <= 0 or trade_price <= 0:
            raise ValueError("position trade amount and price must be positive")
        signed = quantity if str(side).lower() == "buy" else -quantity
        if self.net_amount == 0 or self.net_amount * signed > 0:
            total_cost = abs(self.net_amount) * self.average_entry_price
            self.net_amount += signed
            self.average_entry_price = (
                (total_cost + quantity * trade_price) / abs(self.net_amount)
                if self.net_amount != 0
                else Decimal(0)
            )
            return Decimal(0)
        close_quantity = min(abs(self.net_amount), abs(signed))
        if self.net_amount > 0:
            realized = (trade_price - self.average_entry_price) * close_quantity
        else:
            realized = (self.average_entry_price - trade_price) * close_quantity
        self.realized_pnl += realized
        remaining = signed + self.net_amount
        if remaining == 0:
            self.net_amount = Decimal(0)
            self.average_entry_price = Decimal(0)
        elif self.net_amount * remaining > 0:
            self.net_amount = remaining
        else:
            self.net_amount = remaining
            self.average_entry_price = trade_price
        return realized

    def mark_to_market(self, mark_price: Decimal | float | str) -> Decimal:
        mark = _decimal(mark_price, Decimal(0)) or Decimal(0)
        if self.net_amount == 0 or mark <= 0:
            return Decimal(0)
        return (mark - self.average_entry_price) * self.net_amount

    def close_at(self, mark_price: Decimal | float | str) -> Decimal:
        if self.net_amount > 0:
            return self.apply("sell", self.net_amount, mark_price)
        if self.net_amount < 0:
            return self.apply("buy", abs(self.net_amount), mark_price)
        return Decimal(0)

    def to_record(self, mark_price: Decimal | float | str | None = None) -> dict[str, Any]:
        mark = _decimal(mark_price) if mark_price is not None else None
        return _json_safe(
            {
                "net_amount": self.net_amount,
                "average_entry_price": self.average_entry_price,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.mark_to_market(mark) if mark is not None else None,
            }
        )


def replay_position_accounting(result: ReplayResult) -> dict[str, Any]:
    """Reconstruct replay inventory independently from its tick bookkeeping."""

    ledger = PositionLedger()
    for event in sorted(
        result.events,
        key=lambda row: finite_float(row.get("timestamp_seconds")) or 0.0,
    ):
        if event.get("event") == "ENTRY_FILLED":
            ledger.apply(str(event.get("side")), event.get("amount"), event.get("price"))
        elif event.get("event") == "TP_FILLED":
            ledger.apply(str(event.get("side")), event.get("amount"), event.get("price"))
    final_tick = result.ticks[-1] if result.ticks else {}
    mark = _decimal(final_tick.get("mid_price"), Decimal(0)) or Decimal(0)
    best_bid = _decimal(final_tick.get("best_bid"), mark) or mark
    best_ask = _decimal(final_tick.get("best_ask"), mark) or mark
    liquidation_mark = best_bid if ledger.net_amount > 0 else best_ask
    reconstructed_unrealized = ledger.mark_to_market(mark)
    recorded_inventory = _decimal(final_tick.get("position_base"), Decimal(0)) or Decimal(0)
    recorded_unrealized = _decimal(final_tick.get("unrealized_pnl"), Decimal(0)) or Decimal(0)
    recorded_realized_gross = sum(
        (_decimal(event.get("gross_pnl"), Decimal(0)) or Decimal(0))
        for event in result.events
        if event.get("event") == "TP_FILLED"
    )
    liquidation_increment = ledger.mark_to_market(liquidation_mark)
    weighted_total_before_fees = ledger.realized_pnl + reconstructed_unrealized
    recorded_lot_total_before_fees = recorded_realized_gross + recorded_unrealized
    return _json_safe(
        {
            "ending_inventory_base": ledger.net_amount,
            "average_entry_cost": ledger.average_entry_price if ledger.net_amount else None,
            "ending_mark_price": mark,
            "unrealized_pnl": reconstructed_unrealized,
            "weighted_ledger_unrealized_pnl": reconstructed_unrealized,
            "recorded_unrealized_pnl": recorded_unrealized,
            "unrealized_formula_error": abs(reconstructed_unrealized - recorded_unrealized),
            "unrealized_model_difference": abs(reconstructed_unrealized - recorded_unrealized),
            "recorded_inventory_error": abs(ledger.net_amount - recorded_inventory),
            "realized_pnl_from_position_ledger": ledger.realized_pnl,
            "recorded_lot_realized_gross_pnl": recorded_realized_gross,
            "weighted_ledger_total_pnl_before_fees": weighted_total_before_fees,
            "recorded_lot_total_pnl_before_fees": recorded_lot_total_before_fees,
            "total_pnl_model_difference": abs(
                weighted_total_before_fees - recorded_lot_total_before_fees
            ),
            "position_accounting_total_pass": abs(
                weighted_total_before_fees - recorded_lot_total_before_fees
            )
            <= Decimal("1e-8"),
            "liquidation_mark_price": liquidation_mark,
            "liquidation_at_end_incremental_pnl": liquidation_increment,
            "liquidation_at_end_hypothetical_total_pnl": ledger.realized_pnl
            + liquidation_increment,
            "mark_sign_check": {
                "long_positive_if_mark_above_cost": (
                    ledger.net_amount <= 0
                    or mark <= ledger.average_entry_price
                    or reconstructed_unrealized >= 0
                ),
                "short_positive_if_mark_below_cost": (
                    ledger.net_amount >= 0
                    or mark >= ledger.average_entry_price
                    or reconstructed_unrealized >= 0
                ),
            },
        }
    )


def _asof_iv_snapshots(
    snapshots: Sequence[Mapping[str, Any]],
    max_age_seconds: float,
) -> list[dict[str, Any]]:
    """Carry only prior observed IV while its as-of age is within a rule."""

    result: list[dict[str, Any]] = []
    last_iv: float | None = None
    last_iv_time: float | None = None
    for raw in sorted(
        (dict(snapshot) for snapshot in snapshots),
        key=lambda row: parse_timestamp(row.get("timestamp")) or float("inf"),
    ):
        timestamp = parse_timestamp(raw.get("timestamp"))
        current_iv = finite_float(raw.get("atm_iv"))
        if raw.get("iv_data_available") is True and current_iv is not None and current_iv > 0:
            last_iv = current_iv
            last_iv_time = timestamp
        carried_age = (
            timestamp - last_iv_time if timestamp is not None and last_iv_time is not None else None
        )
        if last_iv is not None and carried_age is not None and 0 <= carried_age <= max_age_seconds:
            raw["atm_iv"] = last_iv
            raw["iv_data_available"] = True
            raw["option_data_age_seconds"] = carried_age
        else:
            raw["atm_iv"] = None
            raw["iv_data_available"] = False
        result.append(raw)
    return result


def replay_behavior_summary(result: ReplayResult) -> dict[str, Any]:
    """Summarize mode/geometry metrics from replay ticks for sensitivity runs."""

    ticks = result.ticks
    mode_seconds: Counter[str] = Counter()
    widths: list[float] = []
    defensive = 0.0
    for previous, current in zip(ticks, ticks[1:], strict=False):
        current_time = parse_timestamp(current.get("timestamp"))
        previous_time = parse_timestamp(previous.get("timestamp"))
        duration = (
            max(0.0, current_time - previous_time)
            if current_time is not None and previous_time is not None
            else 0.0
        )
        mode = str(previous.get("mode", "unknown"))
        mode_seconds[mode] += duration
        if mode == "defensive":
            defensive += duration
    widths = [
        value for tick in ticks if (value := finite_float(tick.get("grid_width_pct"))) is not None
    ]
    total_seconds = sum(mode_seconds.values())
    return _json_safe(
        {
            "mode_time_seconds": dict(sorted(mode_seconds.items())),
            "mode_time_pct": {
                mode: value / total_seconds * 100 if total_seconds else 0.0
                for mode, value in sorted(mode_seconds.items())
            },
            "average_grid_width_pct": _mean(widths),
            "defensive_duration_seconds": defensive,
            "defensive_time_pct": defensive / total_seconds * 100 if total_seconds else 0.0,
            "ticks": len(ticks),
        }
    )


def _event_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("event"),
        event.get("level_id"),
        event.get("timestamp_seconds"),
        event.get("side"),
    )


def fill_model_audit(
    results: Mapping[tuple[str, str], ReplayResult],
) -> list[dict[str, Any]]:
    """Compare conservative and touch fill identities and evidence reasons."""

    rows: list[dict[str, Any]] = []
    for strategy in StrategyVariant:
        conservative = results.get((strategy.value, FillModelName.CONSERVATIVE_CROSS_THROUGH.value))
        touch = results.get((strategy.value, FillModelName.TOUCH_OPTIMISTIC.value))
        if conservative is None or touch is None:
            continue
        conservative_events = {
            _event_key(event): event
            for event in conservative.events
            if event.get("event") in {"ENTRY_FILLED", "TP_FILLED"}
        }
        touch_events = {
            _event_key(event): event
            for event in touch.events
            if event.get("event") in {"ENTRY_FILLED", "TP_FILLED"}
        }
        both = sorted(set(conservative_events) & set(touch_events), key=str)
        touch_only = sorted(set(touch_events) - set(conservative_events), key=str)
        conservative_only = sorted(set(conservative_events) - set(touch_events), key=str)
        rows.append(
            _json_safe(
                {
                    "strategy": strategy.value,
                    "conservative_fill_count": len(conservative_events),
                    "touch_fill_count": len(touch_events),
                    "fills_satisfying_both_models": len(both),
                    "touch_only_fills": len(touch_only),
                    "conservative_only_fills": len(conservative_only),
                    "touch_only_examples": [touch_events[key] for key in touch_only[:5]],
                    "conservative_examples": [conservative_events[key] for key in both[:3]],
                    "condition_implementation": {
                        "conservative_buy": "future best_ask < resting buy",
                        "conservative_sell": "future best_bid > resting sell",
                        "touch_buy": "future best_ask <= resting buy",
                        "touch_sell": "future best_bid >= resting sell",
                    },
                    "touch_is_distinct": True,
                }
            )
        )
    return rows


def replay_timeline_audit(
    results: Mapping[tuple[str, str], ReplayResult],
    *,
    sample_size: int = 3,
    seed: int = 20260824,
) -> dict[str, Any]:
    """Validate event order and retain deterministic completed-cycle timelines."""

    rng = random.Random(seed)
    timelines: list[dict[str, Any]] = []
    ordering_checks: list[dict[str, Any]] = []
    for _key, result in sorted(results.items()):
        events = result.events
        by_order: dict[str, dict[str, Any]] = {}
        by_position: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("event") == "ENTRY_CREATED":
                by_order[str(event.get("order_id"))] = event
            elif event.get("event") == "TP_CREATED":
                by_position[str(event.get("position_id"))] = event
        violations: list[str] = []
        completed = []
        for event in events:
            name = event.get("event")
            timestamp = finite_float(event.get("timestamp_seconds"))
            if name == "ENTRY_FILLED":
                created = by_order.get(str(event.get("order_id")))
                created_timestamp = (
                    finite_float(created.get("timestamp_seconds")) if created else None
                )
                if (
                    created is None
                    or timestamp is None
                    or created_timestamp is None
                    or timestamp <= created_timestamp
                ):
                    violations.append(
                        f"entry fill lacks strictly earlier create: {event.get('order_id')}"
                    )
            if name == "TP_FILLED":
                created = by_position.get(str(event.get("position_id")))
                created_timestamp = (
                    finite_float(created.get("timestamp_seconds")) if created else None
                )
                if (
                    created is None
                    or timestamp is None
                    or created_timestamp is None
                    or timestamp <= created_timestamp
                ):
                    violations.append(
                        f"TP fill lacks strictly earlier TP create: {event.get('position_id')}"
                    )
            if name == "ENTRY_FILLED":
                position_id = f"{event.get('order_id')}:position"
                exits = [
                    candidate
                    for candidate in events
                    if candidate.get("event") == "TP_FILLED"
                    and candidate.get("position_id") == position_id
                ]
                if exits:
                    completed.append((event, exits[0]))
        rng.shuffle(completed)
        for entry, exit_event in completed[:sample_size]:
            created_index = next(
                (
                    index
                    for index, event in enumerate(events)
                    if event.get("event") == "ENTRY_CREATED"
                    and event.get("order_id") == entry.get("order_id")
                ),
                None,
            )
            exit_index = next(
                (index for index, event in enumerate(events) if event is exit_event),
                None,
            )
            if created_index is None or exit_index is None:
                continue
            segment = events[created_index : exit_index + 1]
            timeline = []
            for event in segment:
                tick = min(
                    result.ticks,
                    key=lambda row: abs(
                        (finite_float(row.get("timestamp_seconds")) or 0.0)
                        - (finite_float(event.get("timestamp_seconds")) or 0.0)
                    ),
                    default={},
                )
                timeline.append(
                    {
                        "timestamp": event.get("timestamp"),
                        "market_bbo": {
                            "best_bid": tick.get("best_bid"),
                            "best_ask": tick.get("best_ask"),
                            "mid_price": tick.get("mid_price"),
                        },
                        "state": {
                            "volatility_score": tick.get("volatility_score"),
                            "volatility_state": tick.get("volatility_state"),
                            "inventory_ratio": tick.get("inventory_ratio"),
                            "direction_state": tick.get("direction_state"),
                        },
                        "mode": tick.get("mode"),
                        "plan_version": tick.get("plan_version"),
                        "event": event,
                    }
                )
            timelines.append(
                {
                    "strategy": result.strategy,
                    "fill_model": result.fill_model,
                    "entry_order_id": entry.get("order_id"),
                    "entry_level_id": entry.get("level_id"),
                    "events": timeline,
                }
            )
        ordering_checks.append(
            {
                "strategy": result.strategy,
                "fill_model": result.fill_model,
                "completed_cycles": len(completed),
                "violations": violations,
                "pass": not violations,
            }
        )
    return _json_safe(
        {
            "seed": seed,
            "sample_size": sample_size,
            "ordering_checks": ordering_checks,
            "timelines": timelines,
            "pass": all(item["pass"] for item in ordering_checks),
        }
    )


def tp_parity_audit(
    frames: Sequence[EvaluationFrame],
    *,
    sample_limit: int = 48,
) -> dict[str, Any]:
    """Compare Stage 5 adjacent-grid TP math with the replay TP helper."""

    policy = ExecutionPolicy(
        take_profit_mode="adjacent_grid",
        take_profit_step_multiplier=Decimal("1"),
    )
    replay_config = ReplayConfig()
    rows: list[dict[str, Any]] = []
    for frame in frames:
        if len(rows) >= sample_limit:
            break
        try:
            plan_view = GridPlanView(
                timestamp=str(frame.plan.get("timestamp", frame.timestamp)),
                trading_pair=str(frame.plan.get("trading_pair", "BTC-USDC")),
                mode=str(frame.plan.get("mode", "normal")),
                enabled=bool(frame.plan.get("enabled", False)),
                valid=bool(frame.plan.get("valid", False)),
                plan_version=int(frame.plan.get("plan_version", 0)),
                plan_change_significant=bool(frame.plan.get("plan_change_significant", False)),
                center_price=_decimal(frame.plan.get("center_price")),
                total_grid_width_pct=_decimal(frame.plan.get("total_grid_width_pct"), Decimal(0))
                or Decimal(0),
                buy_levels=tuple(
                    PlanLevel(
                        side=ExecutionSide.BUY,
                        level_index=int(level.get("level_index", 0)),
                        theoretical_price=_decimal(level.get("theoretical_price"), Decimal(0))
                        or Decimal(0),
                        quote_amount=_decimal(level.get("quote_amount"), Decimal(0))
                        or Decimal(0),
                    )
                    for level in frame.plan.get("buy_levels", [])
                    if isinstance(level, Mapping)
                ),
                sell_levels=tuple(
                    PlanLevel(
                        side=ExecutionSide.SELL,
                        level_index=int(level.get("level_index", 0)),
                        theoretical_price=_decimal(level.get("theoretical_price"), Decimal(0))
                        or Decimal(0),
                        quote_amount=_decimal(level.get("quote_amount"), Decimal(0))
                        or Decimal(0),
                    )
                    for level in frame.plan.get("sell_levels", [])
                    if isinstance(level, Mapping)
                ),
            )
            for level in (*plan_view.buy_levels, *plan_view.sell_levels):
                if len(rows) >= sample_limit:
                    break
                entry_price = (level.theoretical_price / replay_config.price_increment).to_integral_value() * replay_config.price_increment
                if entry_price <= 0:
                    continue
                levels = plan_view.buy_levels if level.side is ExecutionSide.BUY else plan_view.sell_levels
                stage5_pct = _take_profit_pct(
                    level,
                    levels,
                    plan_view.center_price,
                    policy,
                    entry_price,
                )
                stage5_target = entry_price * (
                    Decimal(1) + stage5_pct
                    if level.side is ExecutionSide.BUY
                    else Decimal(1) - stage5_pct
                )
                replay_target, replay_level_id = _adjacent_tp(
                    level={
                        "side": level.side.value,
                        "level_index": level.level_index,
                        "theoretical_price": level.theoretical_price,
                    },
                    plan=frame.plan,
                    entry_price=entry_price,
                    config=replay_config,
                )
                error = abs(stage5_target - replay_target)
                rows.append(
                    _json_safe(
                        {
                            "timestamp": frame.timestamp,
                            "level_id": level.level_id,
                            "entry_price": entry_price,
                            "stage5_take_profit_pct": stage5_pct,
                            "stage5_target_price": stage5_target,
                            "stage6_target_price": replay_target,
                            "stage6_target_level_id": replay_level_id,
                            "absolute_price_error": error,
                            "pass": error <= replay_config.price_increment,
                        }
                    )
                )
        except (TypeError, ValueError, ArithmeticError) as exc:
            rows.append({"timestamp": frame.timestamp, "error": f"{type(exc).__name__}: {exc}"})
    errors = [
        finite_float(row.get("absolute_price_error"))
        for row in rows
        if finite_float(row.get("absolute_price_error")) is not None
    ]
    return _json_safe(
        {
            "samples": rows,
            "sample_count": len(rows),
            "max_absolute_price_error": max(errors) if errors else None,
            "pass": all(row.get("pass", False) for row in rows),
            "rule": "Stage 5 adjacent-grid previous-level/center target with one-step multiplier; exchange tick tolerance is 0.1",
        }
    )


def inventory_feedback_audit(
    results: Mapping[tuple[str, str], ReplayResult],
) -> dict[str, Any]:
    """Verify same-timestamp lifecycle fills reach the next state decision."""

    checks: list[dict[str, Any]] = []
    for _key, result in sorted(results.items()):
        ticks = sorted(
            result.ticks,
            key=lambda row: finite_float(row.get("timestamp_seconds")) or 0.0,
        )
        fills_by_timestamp: dict[float, list[dict[str, Any]]] = defaultdict(list)
        for event in result.events:
            if event.get("event") not in {"ENTRY_FILLED", "TP_FILLED"}:
                continue
            event_time = finite_float(event.get("timestamp_seconds"))
            if event_time is not None:
                fills_by_timestamp[event_time].append(event)
        for event_time, fills in sorted(fills_by_timestamp.items()):
            tick = next(
                (
                    row
                    for row in ticks
                    if abs((finite_float(row.get("timestamp_seconds")) or 0.0) - event_time)
                    <= 1e-9
                ),
                None,
            )
            prior = next(
                (
                    row
                    for row in reversed(ticks)
                    if (finite_float(row.get("timestamp_seconds")) or 0.0) < event_time
                ),
                None,
            )
            expected_delta = sum(
                (
                    (finite_float(fill.get("amount")) or 0.0)
                    if str(fill.get("side")) == "buy"
                    else -(finite_float(fill.get("amount")) or 0.0)
                )
                for fill in fills
            )
            observed_delta = (
                (finite_float(tick.get("position_base")) or 0.0)
                - (finite_float(prior.get("position_base")) or 0.0)
                if tick is not None and prior is not None
                else None
            )
            passed = observed_delta is not None and abs(observed_delta - expected_delta) <= 1e-9
            for event in fills:
                if event.get("event") != "ENTRY_FILLED":
                    continue
                checks.append(
                    {
                        "strategy": result.strategy,
                        "fill_model": result.fill_model,
                        "timestamp": event.get("timestamp"),
                        "level_id": event.get("level_id"),
                        "expected_inventory_delta": expected_delta,
                        "observed_inventory_delta": observed_delta,
                        "same_timestamp_fill_count": len(fills),
                        "same_timestamp_events": [fill.get("event") for fill in fills],
                        "inventory_updated_before_next_plan": passed,
                        "next_center_price": tick.get("center_price") if tick else None,
                        "next_buy_allocation": tick.get("buy_allocation_pct") if tick else None,
                        "next_sell_allocation": tick.get("sell_allocation_pct") if tick else None,
                    }
                )
    return _json_safe(
        {
            "checks": checks,
            "examples": checks[:12],
            "pass": all(check["inventory_updated_before_next_plan"] for check in checks),
            "note": "The replay snapshot replaces live account inventory before State -> Mode -> GridPlan is called; all ENTRY_FILLED and TP_FILLED events at one timestamp are aggregated before comparison.",
        }
    )


def _base_result_map(results: Sequence[ReplayResult]) -> dict[tuple[str, str], ReplayResult]:
    return {(result.strategy, result.fill_model): result for result in results}


def replay_summaries(results: Mapping[tuple[str, str], ReplayResult]) -> list[dict[str, Any]]:
    return [summarize_replay(results[key]) for key in sorted(results)]


def _run_results(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    start: float,
    end: float,
    replay_config: ReplayConfig,
    strategies: Sequence[StrategyVariant] = tuple(StrategyVariant),
    fill_models: Sequence[FillModelName] = (
        FillModelName.CONSERVATIVE_CROSS_THROUGH,
        FillModelName.TOUCH_OPTIMISTIC,
    ),
) -> dict[tuple[str, str], ReplayResult]:
    return _base_result_map(
        run_replay(
            snapshots,
            evaluation_start_seconds=start,
            evaluation_end_seconds=end,
            strategies=strategies,
            fill_models=fill_models,
            grid_config=GridParameterConfig(),
            replay_config=replay_config,
        )
    )


def _subperiods(
    start: float, end: float, window_seconds: float = SUBPERIOD_SECONDS
) -> list[tuple[float, float, int]]:
    rows = []
    index = 0
    cursor = start
    while cursor < end:
        window_end = min(end, cursor + window_seconds)
        rows.append((cursor, window_end, index))
        index += 1
        cursor = window_end
    return rows


def subperiod_audit(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    start: float,
    end: float,
    replay_config: ReplayConfig,
    window_seconds: float = SUBPERIOD_SECONDS,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window_start, window_end, index in _subperiods(start, end, window_seconds):
        results = _run_results(
            snapshots,
            start=window_start,
            end=window_end,
            replay_config=replay_config,
            fill_models=(FillModelName.CONSERVATIVE_CROSS_THROUGH,),
        )
        for _key, result in sorted(results.items()):
            summary = summarize_replay(result)
            rows.append(
                _json_safe(
                    {
                        "window_index": index,
                        "window_seconds": window_seconds,
                        "window_label": f"{int(window_seconds // 60)}m",
                        "window_start": iso_timestamp(window_start),
                        "window_end": iso_timestamp(window_end),
                        "strategy": result.strategy,
                        "fill_model": result.fill_model,
                        "entry_fills": summary["entry_fills"],
                        "completed_cycles": summary["completed_grid_cycles"],
                        "total_pnl": summary["total_pnl"],
                        "net_realized_pnl": summary["net_realized_pnl"],
                        "unrealized_pnl_end": summary["unrealized_pnl_end"],
                        "maximum_drawdown": summary["maximum_drawdown"],
                        "maximum_absolute_inventory_base": summary[
                            "maximum_absolute_inventory_base"
                        ],
                    }
                )
            )
    return rows


def rolling_comparison(results: Mapping[tuple[str, str], ReplayResult]) -> list[dict[str, Any]]:
    """Align conservative replay ticks and emit IV-minus-baseline differences."""

    selected = {
        strategy: results.get((strategy.value, FillModelName.CONSERVATIVE_CROSS_THROUGH.value))
        for strategy in StrategyVariant
    }
    if any(result is None for result in selected.values()):
        return []
    by_strategy = {
        strategy: {
            finite_float(tick.get("timestamp_seconds")): tick
            for tick in result.ticks
            if finite_float(tick.get("timestamp_seconds")) is not None
        }
        for strategy, result in selected.items()
        if result is not None
    }
    timestamps = sorted(set.intersection(*(set(rows) for rows in by_strategy.values())))
    rows = []

    def tick_difference(
        tick_rows: Mapping[StrategyVariant, Mapping[str, Any]],
        field: str,
        baseline: StrategyVariant,
    ) -> float | None:
        iv = finite_float(tick_rows[StrategyVariant.IV_ADAPTIVE].get(field))
        base = finite_float(tick_rows[baseline].get(field))
        return iv - base if iv is not None and base is not None else None

    for timestamp in timestamps:
        ticks = {strategy: by_strategy[strategy][timestamp] for strategy in StrategyVariant}

        rows.append(
            _json_safe(
                {
                    "timestamp": iso_timestamp(timestamp),
                    "iv_minus_rv_pnl": tick_difference(ticks, "net_pnl", StrategyVariant.RV_ONLY),
                    "iv_minus_static_pnl": tick_difference(ticks, "net_pnl", StrategyVariant.STATIC),
                    "iv_minus_rv_inventory": tick_difference(
                        ticks, "position_base", StrategyVariant.RV_ONLY
                    ),
                    "iv_minus_static_inventory": tick_difference(
                        ticks,
                        "position_base", StrategyVariant.STATIC
                    ),
                    "iv_minus_rv_drawdown": tick_difference(ticks, "drawdown", StrategyVariant.RV_ONLY),
                    "iv_minus_static_drawdown": tick_difference(
                        ticks, "drawdown", StrategyVariant.STATIC
                    ),
                }
            )
        )
    return rows


def scale_sensitivity(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    start: float,
    end: float,
    base_config: ReplayConfig,
    scales: Sequence[Decimal] = (Decimal("1.0"), Decimal("9.30")),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scale in scales:
        config = base_config.__class__(**{**base_config.__dict__, "order_scale": scale})
        results = _run_results(
            snapshots,
            start=start,
            end=end,
            replay_config=config,
            fill_models=(FillModelName.CONSERVATIVE_CROSS_THROUGH,),
        )
        for _key, result in sorted(results.items()):
            summary = summarize_replay(result)
            created = len(
                [event for event in result.events if event.get("event") == "ENTRY_CREATED"]
            )
            blocked_min = len(
                [
                    event
                    for event in result.events
                    if event.get("event") == "ENTRY_BLOCKED"
                    and "minimum" in str(event.get("reason", ""))
                ]
            )
            deployed = [
                finite_float(tick.get("deployed_notional"))
                for tick in result.ticks
                if finite_float(tick.get("deployed_notional")) is not None
            ]
            rows.append(
                _json_safe(
                    {
                        "notional_basis": (
                            "native_stage4_theoretical"
                            if scale == Decimal("1.0")
                            else "testnet_minimum_normalized"
                        ),
                        "order_scale": scale,
                        "strategy": result.strategy,
                        "fill_model": result.fill_model,
                        "entry_creates": created,
                        "entry_fills": summary["entry_fills"],
                        "minimum_order_blocks": blocked_min,
                        "fill_eligibility_pct": (
                            summary["entry_fills"] / created * 100 if created else 0.0
                        ),
                        "skipped_order_pct": (
                            blocked_min / (blocked_min + created) * 100
                            if blocked_min + created
                            else 0.0
                        ),
                        "total_pnl": summary["total_pnl"],
                        "maximum_drawdown": summary["maximum_drawdown"],
                        "maximum_inventory_base": summary["maximum_absolute_inventory_base"],
                        "average_deployed_notional": _mean(deployed),
                        "maximum_deployed_notional": max(deployed) if deployed else 0.0,
                        "maximum_deployed_capital_pct": (
                            max(deployed) / float(config.initial_capital) * 100
                            if deployed and config.initial_capital > 0
                            else 0.0
                        ),
                    }
                )
            )
    return rows


def fee_sensitivity(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    start: float,
    end: float,
    base_config: ReplayConfig,
    fee_bps_values: Sequence[Decimal] = (
        Decimal("-1"),
        Decimal("0"),
        Decimal("1"),
        Decimal("2"),
    ),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fee_bps in fee_bps_values:
        config = base_config.__class__(**{**base_config.__dict__, "maker_fee_bps": fee_bps})
        results = _run_results(
            snapshots,
            start=start,
            end=end,
            replay_config=config,
            fill_models=(FillModelName.CONSERVATIVE_CROSS_THROUGH,),
        )
        for _key, result in sorted(results.items()):
            summary = summarize_replay(result)
            rows.append(
                _json_safe(
                    {
                        "fee_assumption": "hypothetical maker fee/rebate",
                        "maker_fee_bps": fee_bps,
                        "strategy": result.strategy,
                        "fill_model": result.fill_model,
                        "fees": summary["fees"],
                        "net_realized_pnl": summary["net_realized_pnl"],
                        "total_pnl": summary["total_pnl"],
                        "maximum_drawdown": summary["maximum_drawdown"],
                    }
                )
            )
    return rows


def _validate_no_lookahead(frames: Sequence[EvaluationFrame]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for frame in frames:
        for name, record in (
            ("snapshot", frame.snapshot),
            ("state", frame.state),
            ("mode", frame.mode),
            ("plan", frame.plan),
        ):
            timestamp = parse_timestamp(record.get("timestamp"))
            if timestamp is not None and timestamp > frame.timestamp_seconds + 1e-9:
                violations.append(
                    {
                        "frame_timestamp": frame.timestamp,
                        "input": name,
                        "input_timestamp": record.get("timestamp"),
                    }
                )
    return {
        "frame_count": len(frames),
        "violations": violations,
        "pass": not violations,
        "rules": [
            "all as-of joined inputs have timestamp <= plan frame timestamp",
            "orders are evaluated against only snapshots with timestamp strictly after creation",
            "same-timestamp BBO cannot fill a newly created order",
        ],
    }


def _plan_fairness_audit(
    frames: Sequence[EvaluationFrame],
    results: Mapping[tuple[str, str], ReplayResult],
    replay_config: ReplayConfig,
    grid_config: GridParameterConfig,
) -> dict[str, Any]:
    static_rows = []
    for frame in frames:
        plan = static_geometric_plan(frame.snapshot, config=grid_config)
        static_rows.append(
            {
                "width": finite_float(plan.get("total_grid_width_pct")),
                "levels": (plan.get("buy_levels_count", 0), plan.get("sell_levels_count", 0)),
                "buy_allocation": finite_float(plan.get("buy_allocation_pct")),
                "sell_allocation": finite_float(plan.get("sell_allocation_pct")),
            }
        )
    replay_configs = {
        field: getattr(replay_config, field)
        for field in (
            "order_scale",
            "min_order_size",
            "amount_increment",
            "price_increment",
            "maker_fee_bps",
            "max_total_position_notional",
            "max_side_position_notional",
            "minimum_order_lifetime_seconds",
            "maximum_order_lifetime_seconds",
        )
    }
    strategies = {
        strategy.value: {
            "replay_config": replay_configs,
            "fill_models": [
                model.value for model in FillModelName if model is not FillModelName.TRADE_BASED
            ],
            "tp_lifecycle": "same ReplayEngine adjacent-grid TP and order-lifetime/reconciliation path",
            "reference_policy": (
                "recenter every replay tick around current microprice/reference; "
                "same KEEP/refresh/cancel thresholds as adaptive variants"
            )
            if strategy is StrategyVariant.STATIC
            else "recomputed by existing State -> Mode -> GridPlan chain; same reconciliation path",
        }
        for strategy in StrategyVariant
    }
    widths = [row["width"] for row in static_rows if row["width"] is not None]
    return _json_safe(
        {
            "shared_assumptions": replay_configs,
            "stage4_grid_config": grid_config.model_dump(mode="json"),
            "static_plan_geometry_observed": {
                "frame_count": len(static_rows),
                "fixed_base_width_pct": float(grid_config.base_grid_width_pct),
                "recorded_normal_width_summary": _numeric_summary(widths),
                "static_baseline_definition": "fixed Stage 4 base width, five levels per side, 50/50 allocation",
            },
            "static_reference_behavior": (
                "recentered continuously around the current snapshot reference; "
                "not anchored until refresh"
            ),
            "fairness_verdict": (
                "all strategy variants share capital, scale, minimums, tick, fee, exposure, "
                "fill model, TP lifecycle, lifetime, timestamps, and reconciliation code; "
                "intended differences are geometry and adaptive state use"
            ),
            "strategies": strategies,
            "replay_summary_keys": sorted(f"{key[0]}::{key[1]}" for key in results),
            "normal_frames": sum(
                str(frame.mode.get("mode")) == "normal" for frame in frames
            ),
        }
    )


def _pnl_decomposition_rows(
    results: Mapping[tuple[str, str], ReplayResult],
) -> list[dict[str, Any]]:
    rows = []
    for _key, result in sorted(results.items()):
        summary = summarize_replay(result)
        accounting = replay_position_accounting(result)
        realized_gross = finite_float(summary.get("gross_realized_pnl")) or 0.0
        fees = finite_float(summary.get("fees")) or 0.0
        realized_net = finite_float(summary.get("net_realized_pnl")) or 0.0
        unrealized = finite_float(summary.get("unrealized_pnl_end")) or 0.0
        total = finite_float(summary.get("total_pnl")) or 0.0
        rows.append(
            _json_safe(
                {
                    "strategy": result.strategy,
                    "fill_model": result.fill_model,
                    "realized_grid_capture_gross": realized_gross,
                    "fees": fees,
                    "realized_pnl_after_fees": realized_net,
                    "open_position_unrealized_pnl": unrealized,
                    "ending_inventory_base": accounting.get("ending_inventory_base"),
                    "ending_mark_price": accounting.get("ending_mark_price"),
                    "total_pnl": total,
                    "formula": "total_pnl = realized_grid_capture_gross - fees + open_position_unrealized_pnl",
                    "formula_error": abs(total - (realized_gross - fees + unrealized)),
                    "accounting_formula_pass": abs(total - (realized_gross - fees + unrealized))
                    <= 1e-8,
                    "average_entry_cost": accounting.get("average_entry_cost"),
                    "liquidation_at_end_hypothetical_total_pnl": accounting.get(
                        "liquidation_at_end_hypothetical_total_pnl"
                    ),
                }
            )
        )
    return rows


def build_iv_ablation_table(
    frames: Sequence[EvaluationFrame],
    results: Mapping[tuple[str, str], ReplayResult],
    counterfactual: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the central RV-only versus IV-aware comparison table."""

    rows = volatility_decomposition(frames)["rows"]
    cf_rows = list((counterfactual or {}).get("rows", []))
    summary_by_key = {
        key: summarize_replay(result)
        for key, result in results.items()
        if result.fill_model == FillModelName.CONSERVATIVE_CROSS_THROUGH.value
    }
    frame_stats = {
        "volatility_score": (
            _mean(row.get("rv_ratio") for row in rows),
            _mean(row.get("combined_volatility_score") for row in rows),
        ),
        "average_grid_width": (
            _mean(
                row.get("rv_only_width")
                for row in cf_rows
                if finite_float(row.get("rv_only_width")) is not None
            ),
            _mean(
                row.get("full_width")
                for row in cf_rows
                if finite_float(row.get("full_width")) is not None
            ),
        ),
        "defensive_time": (
            sum(1 for row in cf_rows if row.get("rv_only_mode") == "defensive")
            / len(cf_rows)
            * 100
            if cf_rows
            else 0.0,
            sum(1 for row in cf_rows if row.get("full_mode") == "defensive")
            / len(cf_rows)
            * 100
            if cf_rows
            else 0.0,
        ),
    }
    metrics = [
        ("volatility_score", "frame_mean", frame_stats["volatility_score"]),
        ("average_grid_width", "frame_mean", frame_stats["average_grid_width"]),
        ("defensive_time_pct", "frame_percentage", frame_stats["defensive_time"]),
    ]
    rv_summary = summary_by_key.get(
        (StrategyVariant.RV_ONLY.value, FillModelName.CONSERVATIVE_CROSS_THROUGH.value),
        {},
    )
    iv_summary = summary_by_key.get(
        (StrategyVariant.IV_ADAPTIVE.value, FillModelName.CONSERVATIVE_CROSS_THROUGH.value),
        {},
    )
    for name, metric_field in (
        ("entry_fills", "entry_fills"),
        ("cycles", "completed_grid_cycles"),
        ("realized_pnl_after_fees", "net_realized_pnl"),
        ("unrealized_pnl", "unrealized_pnl_end"),
        ("total_pnl", "total_pnl"),
        ("max_drawdown", "maximum_drawdown"),
        ("max_inventory", "maximum_absolute_inventory_base"),
        ("markout_30s_bps", "markout"),
        ("cancel_create_ratio", "cancel_create_ratio"),
    ):
        if metric_field == "markout":
            rv_value = rv_summary.get(metric_field, {}).get("30s", {}).get("mean_bps")
            iv_value = iv_summary.get(metric_field, {}).get("30s", {}).get("mean_bps")
        else:
            rv_value = rv_summary.get(metric_field)
            iv_value = iv_summary.get(metric_field)
        metrics.append((name, metric_field, (rv_value, iv_value)))
    output = []
    for metric, basis, values in metrics:
        rv_value, iv_value = values
        rv_number = finite_float(rv_value)
        iv_number = finite_float(iv_value)
        output.append(
            _json_safe(
                {
                    "metric": metric,
                    "basis": basis,
                    "rv_only": rv_value,
                    "iv_aware": iv_value,
                    "delta_iv_minus_rv": iv_number - rv_number
                    if iv_number is not None and rv_number is not None
                    else None,
                }
            )
        )
    return output


@dataclass
class Stage65Audit:
    """In-memory audit bundle consumed by the report writer."""

    analysis: dict[str, Any]
    base_results: dict[tuple[str, str], ReplayResult]
    staleness_results: dict[str, ReplayResult]
    timeline_audit: dict[str, Any]
    canonical_plans: list[dict[str, Any]] = field(default_factory=list)
    raw_frames: list[EvaluationFrame] = field(default_factory=list)
    canonical_frames: list[EvaluationFrame] = field(default_factory=list)


def run_stage65_audit(
    dataset: EvaluationDataset,
    *,
    validation_plan_paths: Sequence[str | Path] = (),
) -> Stage65Audit:
    """Run the complete bounded Stage 6.5 audit over one loaded dataset."""

    dedup = canonicalize_plans(dataset.plans.records)
    raw_frames = frames_from_plans(dataset, dedup.raw_records)
    stage6_frames = dataset.plan_frames()
    canonical_frames = frames_from_plans(dataset, dedup.canonical_records)
    if not canonical_frames:
        raise ValueError("canonical plan stream produced no complete Stage 1--4 frames")
    start = dataset.common_start_seconds
    end = dataset.common_end_seconds
    if start is None or end is None or end <= start:
        raise ValueError("no common Stage 1--4 evaluation window available")
    replay_config = ReplayConfig()
    base_results = _run_results(
        dataset.sorted_snapshots(),
        start=start,
        end=end,
        replay_config=replay_config,
    )
    staleness_results: dict[str, ReplayResult] = {}
    for threshold in IV_FRESHNESS_THRESHOLDS:
        masked = _asof_iv_snapshots(dataset.sorted_snapshots(), threshold)
        threshold_results = _run_results(
            masked,
            start=start,
            end=end,
            replay_config=replay_config,
            strategies=(StrategyVariant.IV_ADAPTIVE,),
            fill_models=(FillModelName.CONSERVATIVE_CROSS_THROUGH,),
        )
        result = threshold_results.get(
            (StrategyVariant.IV_ADAPTIVE.value, FillModelName.CONSERVATIVE_CROSS_THROUGH.value)
        )
        if result is not None:
            staleness_results[str(int(threshold))] = result

    state_config = StateEngineConfig()
    grid_config = GridParameterConfig()
    decomposition = volatility_decomposition(
        canonical_frames, state_config=state_config, grid_config=grid_config
    )
    counterfactual = counterfactual_iv_impact(
        canonical_frames,
        grid_config=grid_config,
        state_config=state_config,
    )
    base_summaries = replay_summaries(base_results)
    coverage = iv_coverage_audit(dataset, canonical_frames)
    accounting = [
        _json_safe(
            {
                **summarize_replay(result),
                **replay_position_accounting(result),
                **replay_behavior_summary(result),
            }
        )
        for _, result in sorted(base_results.items())
    ]
    staleness_rows = []
    for threshold, result in sorted(staleness_results.items(), key=lambda item: int(item[0])):
        summary = summarize_replay(result)
        freshness = coverage.get("freshness_sensitivity", {}).get(str(threshold), {})
        staleness_rows.append(
            _json_safe(
                {
                    "threshold_seconds": int(threshold),
                    "strategy": result.strategy,
                    "fill_model": result.fill_model,
                    **replay_behavior_summary(result),
                    "fresh_iv_frames": freshness.get("fresh_iv_frames"),
                    "stale_iv_frames": freshness.get("stale_iv_frames"),
                    "missing_iv_frames": freshness.get("missing_iv_frames"),
                    "rv_fallback_frames": freshness.get("rv_fallback_frames"),
                    "entry_fills": summary["entry_fills"],
                    "completed_cycles": summary["completed_grid_cycles"],
                    "total_pnl": summary["total_pnl"],
                    "net_realized_pnl": summary["net_realized_pnl"],
                    "unrealized_pnl_end": summary["unrealized_pnl_end"],
                    "maximum_drawdown": summary["maximum_drawdown"],
                }
            )
        )
    raw_vs_canonical = {
        "raw_stream_rows": len(dedup.raw_records),
        "raw_complete_frames_including_duplicates": len(raw_frames),
        "stage6_timestamp_collapsed_frames": len(stage6_frames),
        "canonical_production_rows": len(dedup.canonical_records),
        "canonical_complete_frames": len(canonical_frames),
        "frame_count_delta_raw_minus_canonical": len(raw_frames) - len(canonical_frames),
        "stage6_vs_canonical_frame_count_delta": len(stage6_frames) - len(canonical_frames),
        "recorded_behavior_comparison": {
            "raw_mode_counts": dict(
                Counter(str(frame.mode.get("mode", "unknown")) for frame in raw_frames)
            ),
            "canonical_mode_counts": dict(
                Counter(str(frame.mode.get("mode", "unknown")) for frame in canonical_frames)
            ),
            "raw_mean_width": _mean(
                finite_float(frame.plan.get("total_grid_width_pct"))
                for frame in raw_frames
                if finite_float(frame.plan.get("total_grid_width_pct")) is not None
            ),
            "canonical_mean_width": _mean(
                finite_float(frame.plan.get("total_grid_width_pct"))
                for frame in canonical_frames
                if finite_float(frame.plan.get("total_grid_width_pct")) is not None
            ),
        },
        "replay_comparison": {
            "headline_pnl_change": 0.0,
            "explanation": (
                "Stage 6 ReplayEngine is snapshot-driven and recomputes State -> Mode -> GridPlan; "
                "recorded plan-row deduplication changes recorded-plan behavior counts, not replay fills"
            ),
        },
    }
    validation_artifacts = []
    for path_value in validation_plan_paths:
        path = Path(path_value).expanduser().resolve()
        if not path.exists():
            validation_artifacts.append({"path": str(path), "exists": False, "records": 0})
            continue
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
        validation_artifacts.append(
            {
                "path": str(path),
                "exists": True,
                "records": len(records),
                "controlled_records": sum(is_controlled_plan(record) for record in records),
                "validation_stages": dict(
                    Counter(
                        _validation_stage(record) for record in records
                    )
                ),
                "first_timestamp": records[0].get("timestamp") if records else None,
                "last_timestamp": records[-1].get("timestamp") if records else None,
            }
        )
    lookahead = _validate_no_lookahead(canonical_frames)
    timeline = replay_timeline_audit(base_results)
    tp_parity = tp_parity_audit(canonical_frames)
    inventory_feedback = inventory_feedback_audit(base_results)
    fill_models = fill_model_audit(base_results)
    fairness = _plan_fairness_audit(canonical_frames, base_results, replay_config, grid_config)
    pnl_rows = _pnl_decomposition_rows(base_results)
    scale_rows = scale_sensitivity(
        dataset.sorted_snapshots(), start=start, end=end, base_config=replay_config
    )
    fee_rows = fee_sensitivity(
        dataset.sorted_snapshots(), start=start, end=end, base_config=replay_config
    )
    subperiod_rows = [
        row
        for window_seconds in SUBPERIOD_WINDOWS_SECONDS
        for row in subperiod_audit(
            dataset.sorted_snapshots(),
            start=start,
            end=end,
            replay_config=replay_config,
            window_seconds=window_seconds,
        )
    ]
    rolling_rows = rolling_comparison(base_results)
    ablation_rows = build_iv_ablation_table(canonical_frames, base_results, counterfactual)
    analysis = _json_safe(
        {
            "audit_verdict": {
                "status": "conditionally_presentable",
                "summary": (
                    "Stage 6 is internally auditable and look-ahead-safe under its stated BBO replay "
                    "assumptions; the short sample, conflicting plan timestamps, missing raw trade stream, "
                    "and hypothetical fees prevent live profitability or queue-quality claims"
                ),
                "strategy_parameters_changed": False,
                "live_execution_modified": False,
                "mainnet_enabled": False,
            },
            "dataset": dataset.manifest(),
            "deduplication": dedup.to_record(),
            "raw_vs_canonical": raw_vs_canonical,
            "dataset_contamination": {
                "canonical_source_records_flagged_controlled": dedup.controlled_record_count,
                "excluded_controlled_records": len(dedup.excluded_controlled_indices),
                "external_validation_artifacts": validation_artifacts,
                "verdict": (
                    "no explicit controlled validation markers were present in the canonical Condor plan file"
                    if dedup.controlled_record_count == 0
                    else "controlled markers were excluded from the canonical production stream"
                ),
            },
            "iv_coverage": coverage,
            "volatility_decomposition": decomposition,
            "counterfactual_iv_impact": counterfactual,
            "iv_regime_audit": iv_regime_audit(canonical_frames),
            "iv_regime_definitions": {
                "relative_iv_bucket": {
                    "low": f"iv_ratio < {RELATIVE_IV_LOW}",
                    "normal": f"{RELATIVE_IV_LOW} <= iv_ratio <= {RELATIVE_IV_HIGH}",
                    "high": f"iv_ratio > {RELATIVE_IV_HIGH}",
                },
                "rv_iv_joint_bucket": {
                    "rv_high": f"realized_volatility_ratio >= {JOINT_BUCKET_THRESHOLD}",
                    "rv_low": f"realized_volatility_ratio < {JOINT_BUCKET_THRESHOLD}",
                    "iv_high": f"iv_ratio >= {JOINT_BUCKET_THRESHOLD}",
                    "iv_low": f"iv_ratio < {JOINT_BUCKET_THRESHOLD}",
                    "note": "joint buckets intentionally use a different threshold and are not relative_iv_bucket labels",
                },
            },
            "lookahead_audit": lookahead,
            "replay_timeline_audit": timeline,
            "tp_parity_audit": tp_parity,
            "inventory_feedback_audit": inventory_feedback,
            "fill_model_audit": fill_models,
            "baseline_fairness": fairness,
            "pnl_decomposition": pnl_rows,
            "position_accounting": accounting,
            "iv_staleness_sensitivity": staleness_rows,
            "scale_sensitivity": scale_rows,
            "fee_sensitivity": fee_rows,
            "subperiod_results": subperiod_rows,
            "rolling_comparison": rolling_rows,
            "iv_ablation": ablation_rows,
            "base_replay_summaries": base_summaries,
            "canonical_frame_count": len(canonical_frames),
            "stage6_frame_count": len(stage6_frames),
            "common_window": {
                "start": iso_timestamp(start),
                "end": iso_timestamp(end),
                "duration_seconds": end - start,
            },
            "limitations": [
                "no raw public trade-by-trade stream was supplied; BBO fills are not queue-aware",
                "partial fills are not modeled by Stage 6 ReplayEngine",
                "maker fee schedule was not locally verified; fee sensitivity is hypothetical",
                "the common window is short and is not a statistical validation sample",
                "recorded plan timestamp conflicts are preserved in the conflict ledger; canonical selection is a deterministic audit view",
                "ReplayEngine marks per-lot positions while PositionLedger is weighted-net; component unrealized marks can differ even when total pre-fee PnL reconciles",
            ],
        }
    )
    return Stage65Audit(
        analysis=analysis,
        base_results=base_results,
        staleness_results=staleness_results,
        timeline_audit=timeline,
        canonical_plans=[dict(record) for record in dedup.canonical_records],
        raw_frames=raw_frames,
        canonical_frames=canonical_frames,
    )


__all__ = [
    "IV_FRESHNESS_THRESHOLDS",
    "SUBPERIOD_WINDOWS_SECONDS",
    "PlanDeduplication",
    "PositionLedger",
    "Stage65Audit",
    "canonicalize_plans",
    "counterfactual_iv_impact",
    "frames_from_plans",
    "is_controlled_plan",
    "iv_coverage_audit",
    "iv_regime_audit",
    "inventory_feedback_audit",
    "relative_iv_bucket",
    "replay_position_accounting",
    "run_stage65_audit",
    "rv_iv_joint_bucket",
    "tp_parity_audit",
    "volatility_decomposition",
]
