"""Bounded, read-only readers for Condor's append-only JSONL streams."""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from derive_options_mm.state_engine import parse_timestamp


@dataclass(frozen=True)
class StreamRead:
    """One safe read result; malformed or incomplete lines never abort a page."""

    name: str
    path: Path
    records: tuple[dict[str, Any], ...] = ()
    malformed_lines: int = 0
    partial_trailing_line: bool = False
    status: str = "ok"
    error: str | None = None
    mtime_ns: int | None = None
    size_bytes: int = 0

    @property
    def latest(self) -> dict[str, Any] | None:
        return self.records[-1] if self.records else None

    @property
    def latest_timestamp(self) -> float | None:
        return parse_timestamp(self.latest.get("timestamp")) if self.latest else None

    def age_seconds(self, now: float | None = None) -> float | None:
        timestamp = self.latest_timestamp
        if timestamp is None:
            return None
        return max(0.0, (time.time() if now is None else now) - timestamp)


@dataclass(frozen=True)
class ChurnSummary:
    """Derived order-churn counters from the optional execution journal."""

    available: bool = False
    window_seconds: float = 3600.0
    keep_count: int = 0
    refresh_count: int = 0
    safety_cancel_count: int = 0
    orders_created: int = 0
    orders_cancelled: int = 0
    fills: int = 0
    cancel_create_ratio: float | None = None
    cancels_per_hour: float | None = None
    median_order_lifetime: float | None = None
    average_order_lifetime: float | None = None
    replacement_reason_counts: dict[str, int] = field(default_factory=dict)
    recent_events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Latest-per-stream view used by the dashboard pages."""

    streams: dict[str, StreamRead]
    latest_by_asset: dict[str, dict[str, dict[str, Any]]]
    global_risk: dict[str, Any] | None
    portfolio_risk: dict[str, Any] | None
    churn: ChurnSummary

    def stream_age(self, name: str, now: float | None = None) -> float | None:
        stream = self.streams.get(name)
        return stream.age_seconds(now) if stream else None


class JsonlTailReader:
    """Read complete recent JSONL records with a small mtime/size cache."""

    def __init__(self, *, max_records: int = 2_000, max_bytes: int = 4_000_000) -> None:
        self.max_records = max(1, max_records)
        self.max_bytes = max(1024, max_bytes)
        self._cache: dict[Path, tuple[tuple[int, int, int], StreamRead]] = {}

    def read(self, name: str, path: str | Path) -> StreamRead:
        target = Path(path).expanduser()
        try:
            stat = target.stat()
        except FileNotFoundError:
            return StreamRead(name=name, path=target, status="missing", error="file not found")
        except OSError as exc:
            return StreamRead(name=name, path=target, status="unavailable", error=str(exc))

        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = self._cache.get(target)
        if cached and cached[0] == signature:
            return cached[1]

        try:
            raw, partial = self._read_tail(target, stat.st_size)
        except OSError as exc:
            result = StreamRead(
                name=name,
                path=target,
                status="unavailable",
                error=str(exc),
                mtime_ns=stat.st_mtime_ns,
                size_bytes=stat.st_size,
            )
            self._cache[target] = (signature, result)
            return result

        records: list[dict[str, Any]] = []
        malformed = 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                malformed += 1
        records = records[-self.max_records :]
        result = StreamRead(
            name=name,
            path=target,
            records=tuple(records),
            malformed_lines=malformed,
            partial_trailing_line=partial,
            status="ok",
            mtime_ns=stat.st_mtime_ns,
            size_bytes=stat.st_size,
        )
        self._cache[target] = (signature, result)
        return result

    def _read_tail(self, path: Path, size_bytes: int) -> tuple[bytes, bool]:
        with path.open("rb") as handle:
            offset = max(0, size_bytes - self.max_bytes)
            handle.seek(offset)
            if offset:
                handle.readline()
            raw = handle.read()
        partial = bool(raw) and not raw.endswith(b"\n")
        if partial:
            raw = raw.rsplit(b"\n", 1)[0] + b"\n"
        return raw, partial


def latest_by_asset(
    records: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Select the newest record per trading pair without assuming file order."""

    latest: dict[str, tuple[float, int, dict[str, Any]]] = {}
    for index, record in enumerate(records):
        pair = record.get("trading_pair") or record.get("pair")
        if not pair:
            continue
        timestamp = parse_timestamp(record.get("timestamp"))
        sort_timestamp = timestamp if timestamp is not None else float(index)
        current = latest.get(str(pair))
        if current is None or (sort_timestamp, index) >= (current[0], current[1]):
            latest[str(pair)] = (sort_timestamp, index, record)
    return {pair: value[2] for pair, value in latest.items()}


def _latest_global_risk(state_records: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for index, record in enumerate(state_records):
        value = record.get("global_risk_state")
        if not isinstance(value, dict):
            if "global_risk_regime" in record or "global_risk_score" in record:
                value = record
            else:
                continue
        timestamp = parse_timestamp(value.get("timestamp") or record.get("timestamp"))
        candidates.append((timestamp if timestamp is not None else float(index), index, value))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def summarize_churn(
    stream: StreamRead,
    *,
    now: float | None = None,
    window_seconds: float = 3600.0,
) -> ChurnSummary:
    """Aggregate journal events without requiring an execution connection."""

    if stream.status != "ok" or not stream.records:
        return ChurnSummary()
    current_time = time.time() if now is None else now
    recent: list[dict[str, Any]] = []
    for record in stream.records:
        timestamp = parse_timestamp(record.get("timestamp"))
        if timestamp is None or current_time - timestamp <= window_seconds:
            recent.append(record)
    if not recent:
        return ChurnSummary(available=True, window_seconds=window_seconds)

    event_counts = Counter(str(record.get("event", "")).upper() for record in recent)
    reason_counts: Counter[str] = Counter()
    lifetimes: list[float] = []
    for record in recent:
        event = str(record.get("event", "")).upper()
        reason = (
            record.get("reason_code") or record.get("replacement_reason") or record.get("reason")
        )
        if reason and event in {
            "STOP",
            "CANCEL",
            "CANCELLED",
            "REPLACE",
            "REFRESH",
            "SAFETY_CANCEL",
        }:
            reason_counts[str(reason)] += 1
        lifetime = (
            _finite(record.get("lifetime_seconds"))
            or _finite(record.get("order_lifetime_seconds"))
            or _finite(record.get("age_seconds"))
        )
        if lifetime is not None and lifetime >= 0:
            lifetimes.append(lifetime)
    created = sum(
        event_counts[event]
        for event in ("CREATE", "CREATED", "CREATE_SUCCESS", "ORDER_CREATED", "ENTRY_CREATED")
    )
    cancelled = sum(
        event_counts[event]
        for event in (
            "CANCEL",
            "CANCELLED",
            "ORDER_CANCELLED",
            "STOP",
            "STOP_SUCCESS",
            "SAFETY_CANCEL",
        )
    )
    fills = sum(event_counts[event] for event in ("FILL", "FILLED", "ORDER_FILLED", "ENTRY_FILLED"))
    keep = event_counts["KEEP"]
    refresh = event_counts["REFRESH"] + event_counts["REPLACE"]
    safety = event_counts["SAFETY_CANCEL"] + reason_counts.get("MAKER_SAFETY", 0)
    return ChurnSummary(
        available=True,
        window_seconds=window_seconds,
        keep_count=keep,
        refresh_count=refresh,
        safety_cancel_count=safety,
        orders_created=created,
        orders_cancelled=cancelled,
        fills=fills,
        cancel_create_ratio=(cancelled / created if created else None),
        cancels_per_hour=cancelled * 3600.0 / window_seconds,
        median_order_lifetime=statistics.median(lifetimes) if lifetimes else None,
        average_order_lifetime=statistics.fmean(lifetimes) if lifetimes else None,
        replacement_reason_counts=dict(reason_counts),
        recent_events=tuple(recent[-100:]),
    )


def read_runtime(paths: dict[str, Path], reader: JsonlTailReader | None = None) -> RuntimeSnapshot:
    """Read all configured streams safely; missing streams are expected."""

    tail_reader = reader or JsonlTailReader()
    streams = {name: tail_reader.read(name, path) for name, path in paths.items()}
    stream_asset_sources = {
        name: latest_by_asset(stream.records)
        for name, stream in streams.items()
        if name != "execution_journal"
    }
    pairs = sorted({pair for values in stream_asset_sources.values() for pair in values})
    by_asset: dict[str, dict[str, dict[str, Any]]] = {
        pair: {
            name: values[pair] for name, values in stream_asset_sources.items() if pair in values
        }
        for pair in pairs
    }
    state_stream = streams.get("state", StreamRead("state", Path("")))
    portfolio_stream = streams.get("portfolio_risk", StreamRead("portfolio_risk", Path("")))
    return RuntimeSnapshot(
        streams=streams,
        latest_by_asset=by_asset,
        global_risk=_latest_global_risk(state_stream.records),
        portfolio_risk=portfolio_stream.latest,
        churn=summarize_churn(
            streams.get("execution_journal", StreamRead("execution_journal", Path("")))
        ),
    )


__all__ = [
    "ChurnSummary",
    "JsonlTailReader",
    "RuntimeSnapshot",
    "StreamRead",
    "latest_by_asset",
    "read_runtime",
    "summarize_churn",
]
