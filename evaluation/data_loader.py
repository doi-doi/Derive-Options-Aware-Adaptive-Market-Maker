"""Deterministic, point-in-time-safe loaders for the Stage 1--4 JSONL streams.

The loader treats the Condor files as append-only event streams.  It records
quality metadata before any analysis, sorts only an in-memory copy by event
timestamp, and exposes an as-of join that never selects a future observation.
No source file is modified or silently repaired.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def finite_float(value: Any) -> float | None:
    """Return a finite float or ``None`` for missing/non-numeric values."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_timestamp(value: Any) -> float | None:
    """Parse numeric, ISO-8601, and UTC ``Z`` timestamps into epoch seconds."""

    numeric = finite_float(value)
    if numeric is not None:
        # Derive/Hummingbot timestamps can be expressed in milliseconds.
        return numeric / 1000.0 if numeric > 100_000_000_000 else numeric
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def iso_timestamp(seconds: float | None) -> str | None:
    """Format epoch seconds as a stable UTC ISO timestamp."""

    if seconds is None or not math.isfinite(seconds):
        return None
    return (
        datetime.fromtimestamp(seconds, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


@dataclass(frozen=True)
class StreamQuality:
    """Quality and provenance summary for one JSONL input."""

    name: str
    path: str
    bytes_read: int
    sha256: str
    records: int
    invalid_records: int
    timestamp_records: int
    start_timestamp: str | None
    end_timestamp: str | None
    duration_seconds: float | None
    median_sampling_interval_seconds: float | None
    max_sampling_gap_seconds: float | None
    sampling_gap_count: int
    duplicate_timestamp_count: int
    out_of_order_count: int
    stale_record_count: int
    missing_rates: dict[str, float]
    availability_rates: dict[str, float]
    value_counts: dict[str, dict[str, int]]
    source_changed_during_read: bool = False

    @property
    def duration_hours(self) -> float | None:
        return None if self.duration_seconds is None else self.duration_seconds / 3600.0

    def to_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "bytes_read": self.bytes_read,
            "sha256": self.sha256,
            "records": self.records,
            "invalid_records": self.invalid_records,
            "timestamp_records": self.timestamp_records,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "duration_seconds": self.duration_seconds,
            "duration_hours": self.duration_hours,
            "median_sampling_interval_seconds": self.median_sampling_interval_seconds,
            "max_sampling_gap_seconds": self.max_sampling_gap_seconds,
            "sampling_gap_count": self.sampling_gap_count,
            "duplicate_timestamp_count": self.duplicate_timestamp_count,
            "out_of_order_count": self.out_of_order_count,
            "stale_record_count": self.stale_record_count,
            "missing_rates": self.missing_rates,
            "availability_rates": self.availability_rates,
            "value_counts": self.value_counts,
            "source_changed_during_read": self.source_changed_during_read,
        }


@dataclass(frozen=True)
class JsonlStream:
    """Records plus the quality manifest for one input stream."""

    name: str
    path: Path
    records: tuple[dict[str, Any], ...]
    quality: StreamQuality

    def sorted_records(self) -> list[dict[str, Any]]:
        """Return a timestamp-ordered in-memory copy, preserving file order on ties."""

        return [
            record
            for _, record in sorted(
                enumerate(self.records),
                key=lambda item: (
                    parse_timestamp(item[1].get("timestamp"))
                    if parse_timestamp(item[1].get("timestamp")) is not None
                    else float("inf"),
                    item[0],
                ),
            )
        ]


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _quality_for(
    name: str,
    path: Path,
    raw: bytes,
    records: Sequence[dict[str, Any]],
    invalid_records: int,
    source_changed: bool,
) -> StreamQuality:
    timestamps = [parse_timestamp(record.get("timestamp")) for record in records]
    valid_timestamps = [value for value in timestamps if value is not None]
    sorted_timestamps = sorted(valid_timestamps)
    intervals = [
        later - earlier
        for earlier, later in zip(sorted_timestamps, sorted_timestamps[1:], strict=False)
        if later >= earlier
    ]
    median_interval = statistics.median(intervals) if intervals else None
    gap_threshold = median_interval * 3 if median_interval and median_interval > 0 else None
    gap_count = (
        sum(interval > gap_threshold for interval in intervals) if gap_threshold is not None else 0
    )
    max_gap = max(intervals) if intervals else None
    duplicate_count = len(valid_timestamps) - len(set(valid_timestamps))
    out_of_order = sum(
        later < earlier
        for earlier, later in zip(valid_timestamps, valid_timestamps[1:], strict=False)
    )

    candidate_fields = {
        "best_bid",
        "best_ask",
        "mid_price",
        "spread_bps",
        "atm_iv",
        "iv_ratio",
        "current_position",
        "position_notional",
        "inventory_ratio",
        "volatility_score",
        "mode",
        "plan_version",
        "plan_change_significant",
        "data_valid",
        "account_data_available",
        "iv_data_available",
        "trade_data_available",
    }
    present_fields = {
        field_name for record in records for field_name in record if field_name in candidate_fields
    }
    missing_rates = {
        field_name: round(
            sum(_is_missing(record.get(field_name)) for record in records) / len(records) * 100,
            6,
        )
        for field_name in sorted(present_fields)
    }
    availability_fields = {
        "data_valid",
        "account_data_available",
        "iv_data_available",
        "trade_data_available",
    }
    availability_rates = {
        field_name: round(
            sum(record.get(field_name) is True for record in records) / len(records) * 100,
            6,
        )
        for field_name in sorted(availability_fields & present_fields)
    }
    value_counts: dict[str, dict[str, int]] = {}
    for field_name in ("mode", "volatility_state", "direction_state", "inventory_state"):
        counts = Counter(
            str(record[field_name])
            for record in records
            if field_name in record and not _is_missing(record[field_name])
        )
        if counts:
            value_counts[field_name] = dict(sorted(counts.items()))

    # Stage 1's configured maximum accepted age is 15 seconds.  Other streams
    # do not carry an equivalent tracker-age field, so their stale count stays 0.
    stale_count = sum(
        (finite_float(record.get("data_age_seconds")) or 0.0) > 15.0 for record in records
    )
    return StreamQuality(
        name=name,
        path=str(path),
        bytes_read=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        records=len(records),
        invalid_records=invalid_records,
        timestamp_records=len(valid_timestamps),
        start_timestamp=iso_timestamp(min(valid_timestamps)) if valid_timestamps else None,
        end_timestamp=iso_timestamp(max(valid_timestamps)) if valid_timestamps else None,
        duration_seconds=(max(valid_timestamps) - min(valid_timestamps))
        if valid_timestamps
        else None,
        median_sampling_interval_seconds=median_interval,
        max_sampling_gap_seconds=max_gap,
        sampling_gap_count=gap_count,
        duplicate_timestamp_count=duplicate_count,
        out_of_order_count=out_of_order,
        stale_record_count=stale_count,
        missing_rates=missing_rates,
        availability_rates=availability_rates,
        value_counts=value_counts,
        source_changed_during_read=source_changed,
    )


def load_jsonl(path: str | Path, name: str) -> JsonlStream:
    """Read one JSONL stream and retain all malformed/provenance information."""

    resolved = Path(path).expanduser().resolve()
    try:
        before = resolved.stat()
        raw = resolved.read_bytes()
        after = resolved.stat()
    except OSError as exc:
        raise FileNotFoundError(f"Unable to read {resolved}: {exc}") from exc

    records: list[dict[str, Any]] = []
    invalid_records = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid_records += 1
            continue
        if not isinstance(value, dict):
            invalid_records += 1
            continue
        records.append(value)
    source_changed = (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    )
    quality = _quality_for(name, resolved, raw, records, invalid_records, source_changed)
    return JsonlStream(name=name, path=resolved, records=tuple(records), quality=quality)


@dataclass(frozen=True)
class EvaluationFrame:
    """One point-in-time-safe plan frame with preceding inputs attached."""

    timestamp: str
    timestamp_seconds: float
    snapshot: dict[str, Any]
    state: dict[str, Any]
    mode: dict[str, Any]
    plan: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        plan = self.plan
        state = self.state
        mode = self.mode
        snapshot = self.snapshot
        return {
            "timestamp": self.timestamp,
            "timestamp_seconds": self.timestamp_seconds,
            "best_bid": snapshot.get("best_bid"),
            "best_ask": snapshot.get("best_ask"),
            "mid_price": snapshot.get("mid_price"),
            "spread_bps": snapshot.get("spread_bps"),
            "microprice": snapshot.get("microprice"),
            "book_imbalance": state.get("book_imbalance", snapshot.get("depth_imbalance")),
            "order_flow_imbalance": state.get(
                "order_flow_imbalance", snapshot.get("order_flow_imbalance")
            ),
            "atm_iv": state.get("atm_iv", snapshot.get("atm_iv")),
            "iv_ratio": state.get("iv_ratio"),
            "iv_change": state.get("iv_change"),
            "volatility_score": state.get("volatility_score"),
            "volatility_state": state.get("volatility_state"),
            "realized_volatility_ratio": state.get("realized_volatility_ratio"),
            "direction_score": state.get("direction_score"),
            "direction_state": state.get("direction_state"),
            "inventory_ratio": state.get("inventory_ratio"),
            "inventory_state": state.get("inventory_state"),
            "mode": mode.get("mode"),
            "plan_version": plan.get("plan_version"),
            "reference_price": plan.get("reference_price"),
            "center_price": plan.get("center_price"),
            "center_shift_bps": plan.get("center_shift_bps"),
            "grid_width_pct": plan.get("total_grid_width_pct"),
            "half_grid_width_pct": plan.get("half_grid_width_pct"),
            "inner_distance_bps": plan.get("inner_distance_bps"),
            "buy_allocation_pct": plan.get("buy_allocation_pct"),
            "sell_allocation_pct": plan.get("sell_allocation_pct"),
            "buy_levels": plan.get("buy_levels", []),
            "sell_levels": plan.get("sell_levels", []),
            "buy_levels_count": plan.get("buy_levels_count", 0),
            "sell_levels_count": plan.get("sell_levels_count", 0),
            "enabled": plan.get("enabled"),
            "valid": plan.get("valid"),
        }


@dataclass
class EvaluationDataset:
    """All Stage 1--4 streams and their deterministic provenance."""

    snapshots: JsonlStream
    states: JsonlStream
    modes: JsonlStream
    plans: JsonlStream
    trades: JsonlStream | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def qualities(self) -> dict[str, StreamQuality]:
        result = {
            "snapshots": self.snapshots.quality,
            "states": self.states.quality,
            "modes": self.modes.quality,
            "plans": self.plans.quality,
        }
        if self.trades is not None:
            result["trades"] = self.trades.quality
        return result

    @property
    def common_start_seconds(self) -> float | None:
        starts = [parse_timestamp(stream.start_timestamp) for stream in self.qualities.values()]
        starts = [value for value in starts if value is not None]
        return max(starts) if starts else None

    @property
    def common_end_seconds(self) -> float | None:
        ends = [parse_timestamp(stream.end_timestamp) for stream in self.qualities.values()]
        ends = [value for value in ends if value is not None]
        return min(ends) if ends else None

    @property
    def common_duration_seconds(self) -> float | None:
        start = self.common_start_seconds
        end = self.common_end_seconds
        return max(0.0, end - start) if start is not None and end is not None else None

    def sorted_snapshots(self) -> list[dict[str, Any]]:
        return self.snapshots.sorted_records()

    def plan_frames(self) -> list[EvaluationFrame]:
        """As-of join deduplicated plan timestamps to prior Stage 1--3 records."""

        streams = {
            "snapshots": self.snapshots.sorted_records(),
            "states": self.states.sorted_records(),
            "modes": self.modes.sorted_records(),
        }
        joiners = {name: AsOfSeries(records) for name, records in streams.items()}
        plan_records = self.plans.sorted_records()
        deduped_plans: list[dict[str, Any]] = []
        last_timestamp: float | None = None
        for plan in plan_records:
            timestamp = parse_timestamp(plan.get("timestamp"))
            if timestamp is None:
                continue
            if last_timestamp is not None and timestamp == last_timestamp:
                deduped_plans[-1] = plan
            else:
                deduped_plans.append(plan)
                last_timestamp = timestamp

        frames: list[EvaluationFrame] = []
        for plan in deduped_plans:
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

    def replay_snapshots(self) -> list[dict[str, Any]]:
        """Return snapshots in the common Stage 1--4 coverage window."""

        start = self.common_start_seconds
        end = self.common_end_seconds
        records = self.sorted_snapshots()
        if start is None or end is None:
            return []
        return [
            record
            for record in records
            if (
                (timestamp := parse_timestamp(record.get("timestamp"))) is not None
                and start <= timestamp <= end
            )
        ]

    def manifest(self) -> dict[str, Any]:
        return {
            "streams": {name: quality.to_record() for name, quality in self.qualities.items()},
            "common_start_timestamp": iso_timestamp(self.common_start_seconds),
            "common_end_timestamp": iso_timestamp(self.common_end_seconds),
            "common_duration_seconds": self.common_duration_seconds,
            "warnings": list(self.warnings),
        }


class AsOfSeries:
    """Efficient latest-record-at-or-before lookup with no forward fill."""

    def __init__(self, records: Sequence[dict[str, Any]]):
        self.records = list(records)
        self.times = [
            parse_timestamp(record.get("timestamp"))
            if parse_timestamp(record.get("timestamp")) is not None
            else float("inf")
            for record in self.records
        ]

    def at_or_before(self, timestamp_seconds: float) -> dict[str, Any] | None:
        index = bisect_right(self.times, timestamp_seconds) - 1
        if index < 0 or self.times[index] == float("inf"):
            return None
        return self.records[index]


def load_dataset(
    *,
    snapshots_path: str | Path,
    states_path: str | Path,
    modes_path: str | Path,
    plans_path: str | Path,
    trades_path: str | Path | None = None,
    trading_pair: str = "BTC-USDC",
) -> EvaluationDataset:
    """Load all sources and add explicit coverage warnings."""

    streams = EvaluationDataset(
        snapshots=load_jsonl(snapshots_path, "snapshots"),
        states=load_jsonl(states_path, "states"),
        modes=load_jsonl(modes_path, "modes"),
        plans=load_jsonl(plans_path, "plans"),
        trades=load_jsonl(trades_path, "trades") if trades_path else None,
    )
    for name, stream in streams.qualities.items():
        if stream.source_changed_during_read:
            streams.warnings.append(f"{name} source changed while it was being read")
        if stream.invalid_records:
            streams.warnings.append(
                f"{name} contained {stream.invalid_records} malformed/non-object records"
            )
    if streams.trades is None:
        streams.warnings.append("no raw trade stream supplied; BBO fill models are used")
    if not streams.plan_frames():
        streams.warnings.append("no complete as-of Stage 1--4 plan frames were available")
    source_streams = {
        "snapshots": streams.snapshots,
        "states": streams.states,
        "modes": streams.modes,
        "plans": streams.plans,
    }
    if streams.trades is not None:
        source_streams["trades"] = streams.trades
    for name, stream in source_streams.items():
        pairs = {
            str(record.get("trading_pair"))
            for record in stream.records
            if record.get("trading_pair") is not None
        }
        if pairs and trading_pair not in pairs:
            streams.warnings.append(f"{name} contains no records for {trading_pair}")
    return streams


__all__ = [
    "AsOfSeries",
    "EvaluationDataset",
    "EvaluationFrame",
    "JsonlStream",
    "StreamQuality",
    "finite_float",
    "iso_timestamp",
    "load_dataset",
    "load_jsonl",
    "parse_timestamp",
]
