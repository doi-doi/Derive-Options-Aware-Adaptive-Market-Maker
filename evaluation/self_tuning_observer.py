"""Phase 1 bounded self-tuning observer.

This module only observes existing event and state records.  It deliberately
does not diagnose, propose, mutate configuration, or touch an exchange.

The observer keeps unavailable metrics as ``None`` and exposes a status map so
callers can render them as ``UNKNOWN`` without manufacturing evidence.  Replay
observations reuse :func:`evaluation.metrics.summarize_replay`; they are always
labelled as shadow/offline evidence and are never mixed with live observations.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .data_loader import finite_float, parse_timestamp
from .metrics import summarize_replay
from .replay import ReplayResult

UNKNOWN = "UNKNOWN"

_CREATE_EVENTS = frozenset({"CREATE_REQUEST", "CREATE", "ORDER_CREATED", "ENTRY_CREATED"})
_CREATE_FALLBACK_EVENTS = frozenset({"CREATE_SUCCESS", "CREATED"})
_CANCEL_EVENTS = frozenset(
    {"STOP_SUCCESS", "CANCEL", "CANCELLED", "ORDER_CANCELLED", "ENTRY_CANCELLED"}
)
_KEEP_EVENTS = frozenset({"KEEP", "ENTRY_KEEP"})
_REFRESH_EVENTS = frozenset({"REFRESH", "REPLACE", "ENTRY_REFRESH", "REPLACEMENT"})
_FILL_EVENTS = frozenset({"FILL", "FILLED", "ORDER_FILLED", "ENTRY_FILLED"})
_CYCLE_EVENTS = frozenset({"TP_FILLED", "POSITION_EXITED", "CYCLE_COMPLETED"})
_ENTRY_FILL_EVENTS = frozenset({"ENTRY_FILLED", "ENTRY_FILL"})
_EXIT_FILL_EVENTS = frozenset({"TP_FILLED", "EXIT_FILLED", "EXIT_FILL"})
_GENERIC_FILL_EVENTS = frozenset({"FILL", "FILLED", "ORDER_FILLED"})


@dataclass(frozen=True)
class VolumeEfficiencyMetrics:
    """Phase A measurements reconstructed from existing event/state records.

    This object deliberately contains measurements only.  It has no strategy
    mutation, order-generation, or optimization method.  ``None`` means the
    source contract did not support a reliable reconstruction; it is never a
    manufactured zero.
    """

    window_start: str | None = None
    window_end: str | None = None
    observed_duration_seconds: float | None = None
    trading_pair: str = "ALL"
    evidence_source: str = "LIVE_OBSERVED"
    executed_buy_notional: float | None = None
    executed_sell_notional: float | None = None
    executed_total_notional: float | None = None
    executed_fill_count: int | None = None
    missing_fill_notional_count: int = 0
    average_gross_exposure: float | None = None
    average_absolute_inventory: float | None = None
    average_absolute_inventory_base: float | None = None
    average_margin_used: float | None = None
    average_beta_risk_used: float | None = None
    max_gross_exposure: float | None = None
    max_inventory: float | None = None
    max_beta_exposure: float | None = None
    exposure_time_notional_seconds: float | None = None
    inventory_exposure_time_notional_seconds: float | None = None
    completed_cycles: int | None = None
    cycles_per_hour: float | None = None
    median_cycle_duration_seconds: float | None = None
    mean_cycle_duration_seconds: float | None = None
    capital_time_efficiency: float | None = None
    volume_per_average_gross_exposure: float | None = None
    volume_per_average_inventory: float | None = None
    volume_per_average_margin: float | None = None
    volume_per_beta_risk: float | None = None
    orders_created: int | None = None
    orders_cancelled: int | None = None
    orders_kept: int | None = None
    fill_create_ratio: float | None = None
    cancel_create_ratio: float | None = None
    keep_ratio: float | None = None
    median_quote_lifetime: float | None = None
    mean_quote_lifetime: float | None = None
    realized_grid_capture: float | None = None
    fees: float | None = None
    inventory_unrealized: float | None = None
    total_pnl: float | None = None
    markout_5s: float | None = None
    markout_30s: float | None = None
    markout_60s: float | None = None
    adverse_selection_rate: float | None = None
    drawdown: float | None = None
    confidence: str = UNKNOWN
    sample_size: int = 0
    reasons: tuple[str, ...] = ()
    metric_status: dict[str, str] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["reasons"] = list(self.reasons)
        record["metric_status"] = dict(sorted(self.metric_status.items()))
        return record


@dataclass(frozen=True)
class ObserverConfig:
    """Operator-defined observation thresholds; no values are self-modified."""

    evaluation_window_minutes: int = 30
    minimum_order_events: int = 20
    minimum_fills_for_fill_metrics: int = 5
    minimum_completed_cycles_for_capture_metrics: int = 3
    markout_horizons_seconds: tuple[int, ...] = (5, 30, 60)

    def __post_init__(self) -> None:
        if self.evaluation_window_minutes <= 0:
            raise ValueError("evaluation_window_minutes must be positive")
        if self.minimum_order_events < 0:
            raise ValueError("minimum_order_events must be non-negative")
        if self.minimum_fills_for_fill_metrics < 0:
            raise ValueError("minimum_fills_for_fill_metrics must be non-negative")
        if self.minimum_completed_cycles_for_capture_metrics < 0:
            raise ValueError("minimum_completed_cycles_for_capture_metrics must be non-negative")
        if not self.markout_horizons_seconds or any(
            horizon <= 0 for horizon in self.markout_horizons_seconds
        ):
            raise ValueError("markout_horizons_seconds must contain positive horizons")

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PerformanceWindow:
    """One bounded observation window with explicit evidence status."""

    start_timestamp: str | None
    end_timestamp: str | None
    asset: str
    mode: str
    global_volatility_regime: str
    relationship_regime: str
    orders_created: int | None = None
    orders_cancelled: int | None = None
    orders_kept: int | None = None
    orders_refreshed: int | None = None
    safety_cancels: int | None = None
    fills: int | None = None
    completed_cycles: int | None = None
    cancel_create_ratio: float | None = None
    keep_ratio: float | None = None
    fill_create_ratio: float | None = None
    median_order_lifetime: float | None = None
    mean_order_lifetime: float | None = None
    maker_capture_quote: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    total_pnl: float | None = None
    markout_5s: float | None = None
    markout_30s: float | None = None
    markout_60s: float | None = None
    adverse_markout_rate: float | None = None
    inventory_ratio_mean: float | None = None
    inventory_ratio_max: float | None = None
    portfolio_beta_exposure_mean: float | None = None
    portfolio_beta_exposure_max: float | None = None
    drawdown: float | None = None
    turnover: float | None = None
    fees_if_known: float | None = None
    confidence: str = UNKNOWN
    sample_count: int = 0
    reasons: tuple[str, ...] = ()
    evidence_source: str = "LIVE_OBSERVED"
    metric_status: dict[str, str] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["reasons"] = list(self.reasons)
        record["metric_status"] = dict(sorted(self.metric_status.items()))
        return record


@dataclass(frozen=True)
class PerformanceObservation:
    """Window plus source health needed to explain what was measurable."""

    window: PerformanceWindow
    source_status: dict[str, str]
    source_record_counts: dict[str, int]
    volume_efficiency: VolumeEfficiencyMetrics | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "window": self.window.to_record(),
            "volume_efficiency": (
                self.volume_efficiency.to_record() if self.volume_efficiency is not None else None
            ),
            "source_status": dict(sorted(self.source_status.items())),
            "source_record_counts": dict(sorted(self.source_record_counts.items())),
        }


def _event_name(record: Mapping[str, Any]) -> str:
    return str(record.get("event", "")).strip().upper()


def _timestamp_seconds(record: Mapping[str, Any]) -> float | None:
    direct = finite_float(record.get("timestamp_seconds"))
    if direct is not None:
        return direct
    return parse_timestamp(record.get("timestamp"))


def _asset_value(record: Mapping[str, Any]) -> str | None:
    value = record.get("trading_pair") or record.get("pair") or record.get("asset")
    return str(value) if value else None


def _event_identifier(record: Mapping[str, Any]) -> str | None:
    value = record.get("order_id") or record.get("executor_id")
    return str(value) if value else None


def _in_scope(record: Mapping[str, Any], *, start: float, end: float, asset: str) -> bool:
    timestamp = _timestamp_seconds(record)
    if timestamp is None or timestamp < start or timestamp > end:
        return False
    if asset.upper() in {"ALL", "*"}:
        return True
    record_asset = _asset_value(record)
    return record_asset is None or record_asset == asset


def _scoped(
    records: Sequence[Mapping[str, Any]], *, start: float, end: float, asset: str
) -> list[Mapping[str, Any]]:
    return [record for record in records if _in_scope(record, start=start, end=end, asset=asset)]


def _bounds(
    records: Sequence[Sequence[Mapping[str, Any]]],
    config: ObserverConfig,
    end_timestamp: float | None,
) -> tuple[float | None, float | None]:
    timestamps = [
        timestamp
        for group in records
        for record in group
        if (timestamp := _timestamp_seconds(record)) is not None
    ]
    end = end_timestamp if end_timestamp is not None else (max(timestamps) if timestamps else None)
    if end is None:
        return None, None
    return end - config.evaluation_window_minutes * 60, end


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _dominant(records: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> str:
    values = [
        str(value)
        for record in records
        for key in keys
        if (value := record.get(key)) not in (None, "", "unknown", "UNKNOWN")
    ]
    return Counter(values).most_common(1)[0][0] if values else UNKNOWN


def _values(records: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[float]:
    values: list[float] = []
    for record in records:
        for key in keys:
            if key in record and record.get(key) is not None:
                value = finite_float(record.get(key))
                if value is not None:
                    values.append(value)
                break
    return values


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _event_rows(records: Sequence[Mapping[str, Any]], names: set[str]) -> list[Mapping[str, Any]]:
    return [record for record in records if _event_name(record) in names]


def _explicit_lifetimes(
    records: Sequence[Mapping[str, Any]], terminal_rows: Sequence[Mapping[str, Any]]
) -> list[float]:
    values = _values(
        terminal_rows,
        ("lifetime_seconds", "order_lifetime_seconds", "age_seconds"),
    )
    created = {
        identifier: _timestamp_seconds(record)
        for record in records
        if _event_name(record) in _CREATE_EVENTS | _CREATE_FALLBACK_EVENTS
        and (identifier := _event_identifier(record))
        and _timestamp_seconds(record) is not None
    }
    for record in terminal_rows:
        identifier = _event_identifier(record)
        created_at = created.get(identifier) if identifier else None
        timestamp = _timestamp_seconds(record)
        explicit_lifetime = any(
            record.get(key) is not None
            for key in ("lifetime_seconds", "order_lifetime_seconds", "age_seconds")
        )
        if not explicit_lifetime and created_at is not None and timestamp is not None:
            lifetime = timestamp - created_at
            if lifetime >= 0:
                values.append(lifetime)
    return values


def _markouts(
    fills: Sequence[Mapping[str, Any]], horizon: int, end_timestamp: float
) -> list[float]:
    values: list[float] = []
    key = f"markout_{horizon}s_bps"
    for record in fills:
        timestamp = _timestamp_seconds(record)
        value = finite_float(record.get(key))
        if timestamp is None or value is None or timestamp + horizon > end_timestamp:
            continue
        values.append(value)
    return values


def _numeric_sum(records: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> float | None:
    values = _values(records, keys)
    return sum(values) if values else None


def _metric_status(window: PerformanceWindow, **values: Any) -> PerformanceWindow:
    status = {key: ("AVAILABLE" if value is not None else UNKNOWN) for key, value in values.items()}
    return replace(window, metric_status=status)


def _first_numeric(record: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = finite_float(record.get(key))
        if value is not None:
            return value
    return None


def _event_side(record: Mapping[str, Any]) -> str | None:
    value = record.get("side") or record.get("order_side") or record.get("exit_side")
    if value is not None:
        normalized = str(value).strip().lower()
        if normalized in {"buy", "sell"}:
            return normalized
    level_id = str(record.get("level_id", "")).strip().lower()
    if level_id.startswith(("buy_", "sell_")):
        return level_id.split("_", 1)[0]
    return None


def _fill_role(record: Mapping[str, Any]) -> str | None:
    event = _event_name(record)
    if event in _ENTRY_FILL_EVENTS:
        return "entry"
    if event in _EXIT_FILL_EVENTS:
        return "exit"
    if event == "POSITION_EXITED":
        close_type = str(record.get("close_type", "")).upper()
        return "exit" if close_type in {"TAKE_PROFIT", "COMPLETED", "TP"} else None
    if event in _GENERIC_FILL_EVENTS:
        role = str(record.get("fill_role") or record.get("role") or "").lower()
        return role if role in {"entry", "exit"} else None
    return None


def _executed_fill_rows(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return actual fill events, never create/cancel/replace messages."""

    return [record for record in records if _fill_role(record) is not None]


def _fill_notional(record: Mapping[str, Any]) -> float | None:
    value = _first_numeric(
        record,
        (
            "executed_notional",
            "quote_notional",
            "quote_amount",
            "filled_quote",
            "amount_quote",
            "notional",
        ),
    )
    if value is not None:
        return abs(value)
    price = _first_numeric(record, ("fill_price", "price", "executed_price"))
    amount = _first_numeric(record, ("filled_amount", "amount", "base_amount"))
    if price is not None and amount is not None and price > 0 and amount != 0:
        return abs(price * amount)
    return None


def _lifecycle_keys(record: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in (
        "position_id",
        "order_id",
        "executor_id",
        "entry_order_id",
        "entry_executor_id",
        "cycle_id",
        "level_id",
    ):
        value = record.get(key)
        if value in (None, ""):
            continue
        text = str(value)
        values.append(text)
        if text.endswith(":position"):
            values.append(text[: -len(":position")])
    return tuple(dict.fromkeys(values))


def _unique_lifecycle_rows(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Deduplicate request/terminal aliases without merging real fill events."""

    result: list[Mapping[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for record in records:
        timestamp = _timestamp_seconds(record)
        keys = _lifecycle_keys(record)
        identity = (keys[0],) if keys else (
            timestamp,
            record.get("level_id"),
            record.get("price"),
            record.get("amount"),
        )
        key = (*identity, timestamp)
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _order_rows(
    records: Sequence[Mapping[str, Any]],
    names: frozenset[str],
    fallback: frozenset[str] = frozenset(),
) -> list[Mapping[str, Any]]:
    primary = _unique_lifecycle_rows([record for record in records if _event_name(record) in names])
    if primary or not fallback:
        return primary
    return _unique_lifecycle_rows([record for record in records if _event_name(record) in fallback])


def _cycle_rows(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rows = [
        record
        for record in records
        if _event_name(record) in _CYCLE_EVENTS
        and (
            _event_name(record) != "POSITION_EXITED"
            or str(record.get("close_type", "")).upper()
            in {"TAKE_PROFIT", "COMPLETED", "TP"}
        )
    ]
    rows.extend(record for record in records if _fill_role(record) == "exit")
    return _unique_lifecycle_rows(rows)


def _cycle_matches(
    entries: Sequence[Mapping[str, Any]], exits: Sequence[Mapping[str, Any]]
) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any], float]], int]:
    """Pair exits with entries using explicit lifecycle IDs, then level FIFO."""

    used: set[int] = set()
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any], float]] = []
    unassociated = 0
    ordered_entries = sorted(
        enumerate(entries),
        key=lambda item: (_timestamp_seconds(item[1]) or float("inf"), item[0]),
    )
    for exit_record in sorted(exits, key=lambda row: _timestamp_seconds(row) or float("inf")):
        exit_keys = set(_lifecycle_keys(exit_record))
        candidate: tuple[int, Mapping[str, Any]] | None = None
        for index, entry_record in ordered_entries:
            if index in used:
                continue
            if exit_keys.intersection(_lifecycle_keys(entry_record)):
                candidate = (index, entry_record)
                break
        if candidate is None:
            exit_level = exit_record.get("level_id")
            for index, entry_record in ordered_entries:
                if index in used or exit_level in (None, ""):
                    continue
                if entry_record.get("level_id") == exit_level:
                    candidate = (index, entry_record)
                    break
        if candidate is None:
            unassociated += 1
            continue
        entry_index, entry_record = candidate
        entry_time = _timestamp_seconds(entry_record)
        exit_time = _timestamp_seconds(exit_record)
        if entry_time is None or exit_time is None or exit_time < entry_time:
            unassociated += 1
            continue
        duration = _first_numeric(
            exit_record,
            ("holding_time_seconds", "cycle_duration_seconds", "duration_seconds"),
        )
        duration = exit_time - entry_time if duration is None else duration
        if duration < 0:
            unassociated += 1
            continue
        used.add(entry_index)
        pairs.append((entry_record, exit_record, duration))
    return pairs, unassociated


def _series_rows(
    records: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
    *,
    absolute: bool = False,
) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for record in records:
        timestamp = _timestamp_seconds(record)
        value = _first_numeric(record, keys)
        if timestamp is None or value is None:
            continue
        rows.append((timestamp, abs(value) if absolute else value))
    return sorted(rows)


def _aggregate_series_rows(
    records: Sequence[Mapping[str, Any]], keys: Sequence[str], *, absolute: bool = False
) -> list[tuple[float, float]]:
    values: dict[float, float] = {}
    for timestamp, value in _series_rows(records, keys, absolute=absolute):
        values[timestamp] = values.get(timestamp, 0.0) + value
    return sorted(values.items())


def _series_stats(
    rows: Sequence[tuple[float, float]],
) -> tuple[float | None, float | None, float | None]:
    """Return left-hold time average, maximum, and value*time integral."""

    if not rows:
        return None, None, None
    maximum = max(value for _, value in rows)
    if len(rows) == 1:
        return rows[0][1], maximum, None
    duration = rows[-1][0] - rows[0][0]
    if duration <= 0:
        return rows[-1][1], maximum, None
    integral = sum(
        value * max(0.0, next_timestamp - timestamp)
        for (timestamp, value), (next_timestamp, _) in zip(rows, rows[1:], strict=False)
    )
    return integral / duration, maximum, integral


def _latest_numeric(records: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> float | None:
    candidates = [
        (_timestamp_seconds(record) or float(index), index, value)
        for index, record in enumerate(records)
        if (value := _first_numeric(record, keys)) is not None
    ]
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _metric_values_status(metrics: VolumeEfficiencyMetrics) -> VolumeEfficiencyMetrics:
    values = metrics.to_record()
    metadata = {
        "window_start",
        "window_end",
        "observed_duration_seconds",
        "trading_pair",
        "evidence_source",
        "missing_fill_notional_count",
        "confidence",
        "sample_size",
        "reasons",
        "metric_status",
    }
    status = {
        key: ("AVAILABLE" if value is not None else UNKNOWN)
        for key, value in values.items()
        if key not in metadata
    }
    return replace(metrics, metric_status=status)


def _build_volume_efficiency(
    events: Sequence[Mapping[str, Any]],
    state_records: Sequence[Mapping[str, Any]],
    portfolio_records: Sequence[Mapping[str, Any]],
    *,
    asset: str,
    start: float | None,
    end: float | None,
    event_source_status: str,
    state_source_status: str,
    portfolio_source_status: str,
    evidence_source: str,
    minimum_order_events: int,
    markout_horizons_seconds: Sequence[int],
) -> VolumeEfficiencyMetrics:
    """Build Phase A metrics from already bounded records."""

    scoped_events = list(events)
    scoped_states = list(state_records)
    scoped_portfolio = list(portfolio_records)
    all_timestamps = [
        timestamp
        for group in (scoped_events, scoped_states, scoped_portfolio)
        for record in group
        if (timestamp := _timestamp_seconds(record)) is not None
    ]
    observed_start = min(all_timestamps) if all_timestamps else None
    observed_end = max(all_timestamps) if all_timestamps else None
    observed_duration = (
        observed_end - observed_start
        if observed_start is not None
        and observed_end is not None
        and observed_end >= observed_start
        else None
    )
    reasons: list[str] = []
    event_available = event_source_status == "ok"
    if not event_available:
        reasons.append(
            "execution journal is missing or unavailable; executed volume, cycles, and "
            "order-lifecycle ratios remain UNKNOWN"
        )
    elif len(scoped_events) < minimum_order_events:
        reasons.append(f"execution sample below minimum_order_events={minimum_order_events}")
    if state_source_status != "ok":
        reasons.append("state stream is missing or unavailable; inventory exposure remains UNKNOWN")
    if portfolio_source_status != "ok":
        reasons.append(
            "portfolio risk stream is missing or unavailable; beta exposure remains UNKNOWN"
        )

    creates = (
        _order_rows(scoped_events, _CREATE_EVENTS, _CREATE_FALLBACK_EVENTS)
        if event_available
        else []
    )
    cancels = _order_rows(scoped_events, _CANCEL_EVENTS) if event_available else []
    keeps = _order_rows(scoped_events, _KEEP_EVENTS) if event_available else []
    entry_fills = (
        [record for record in _executed_fill_rows(scoped_events) if _fill_role(record) == "entry"]
        if event_available
        else []
    )
    fill_rows = _executed_fill_rows(scoped_events) if event_available else []
    cycle_exits = _cycle_rows(scoped_events) if event_available else []
    cycles, unassociated_exits = _cycle_matches(entry_fills, cycle_exits)
    if event_available and unassociated_exits:
        reasons.append(
            f"{unassociated_exits} exit lifecycle record(s) could not be associated with an entry"
        )

    notionals = [_fill_notional(record) for record in fill_rows]
    missing_fill_notional = sum(value is None for value in notionals)
    if missing_fill_notional:
        reasons.append(
            f"{missing_fill_notional} actual fill record(s) lack executed notional or price*amount"
        )
    volume_reliable = event_available and missing_fill_notional == 0
    if volume_reliable:
        buy_volume = sum(
            value
            for record, value in zip(fill_rows, notionals, strict=True)
            if _event_side(record) == "buy" and value is not None
        )
        sell_volume = sum(
            value
            for record, value in zip(fill_rows, notionals, strict=True)
            if _event_side(record) == "sell" and value is not None
        )
        total_volume = sum(value for value in notionals if value is not None)
        executed_fill_count: int | None = len(fill_rows)
    else:
        buy_volume = sell_volume = total_volume = None
        executed_fill_count = len(fill_rows) if event_available else None

    # Prefer portfolio gross exposure for the combined view; per-asset
    # portfolio maps are supported when the stream provides them.
    asset_portfolio_records = scoped_portfolio
    if asset.upper() not in {"ALL", "*"}:
        mapped_rows = []
        for record in scoped_portfolio:
            mapping = record.get("per_asset_exposure")
            if not isinstance(mapping, Mapping) or asset not in mapping:
                continue
            value = finite_float(mapping[asset])
            timestamp = _timestamp_seconds(record)
            if value is not None and timestamp is not None:
                mapped_rows.append({"timestamp_seconds": timestamp, "gross_notional": value})
        if mapped_rows:
            asset_portfolio_records = mapped_rows
    if asset.upper() in {"ALL", "*"} and scoped_portfolio:
        gross_rows = _series_rows(
            scoped_portfolio,
            ("gross_notional", "gross_exposure", "deployed_notional"),
            absolute=True,
        )
    elif asset_portfolio_records and any(
        _first_numeric(record, ("gross_notional", "gross_exposure", "deployed_notional"))
        is not None
        for record in asset_portfolio_records
    ):
        gross_rows = _series_rows(
            asset_portfolio_records,
            ("gross_notional", "gross_exposure", "deployed_notional"),
            absolute=True,
        )
    else:
        gross_rows = _series_rows(
            scoped_states,
            ("deployed_notional", "gross_notional", "gross_exposure", "position_notional"),
            absolute=True,
        )
    inventory_rows = (
        _aggregate_series_rows(
            scoped_states,
            ("inventory_notional", "absolute_inventory_notional", "position_notional"),
            absolute=True,
        )
        if asset.upper() in {"ALL", "*"}
        else _series_rows(
            scoped_states,
            ("inventory_notional", "absolute_inventory_notional", "position_notional"),
            absolute=True,
        )
    )
    inventory_base_rows = (
        _aggregate_series_rows(
            scoped_states,
            ("position_base", "current_position", "inventory_base"),
            absolute=True,
        )
        if asset.upper() in {"ALL", "*"}
        else _series_rows(
            scoped_states,
            ("position_base", "current_position", "inventory_base"),
            absolute=True,
        )
    )
    margin_rows = _series_rows(
        scoped_portfolio or scoped_states,
        ("margin_used", "used_margin", "deployed_margin"),
        absolute=True,
    )
    beta_rows = _series_rows(
        scoped_portfolio if asset.upper() in {"ALL", "*"} else scoped_states,
        ("btc_beta_equivalent_exposure", "portfolio_beta_exposure", "beta_exposure"),
        absolute=True,
    )
    average_gross, max_gross, gross_integral = _series_stats(gross_rows)
    average_inventory, max_inventory, inventory_integral = _series_stats(inventory_rows)
    average_inventory_base, _, _ = _series_stats(inventory_base_rows)
    average_margin, _, _ = _series_stats(margin_rows)
    average_beta, max_beta, _ = _series_stats(beta_rows)

    lifetimes = (
        _explicit_lifetimes(scoped_events, [*cancels, *entry_fills]) if event_available else []
    )
    cycle_durations = [duration for _, _, duration in cycles]
    markout_end = end if end is not None else (observed_end or 0.0)
    markouts = {
        horizon: (_markouts(entry_fills, horizon, markout_end) if event_available else [])
        for horizon in markout_horizons_seconds
    }
    if event_available and entry_fills:
        for horizon, values in markouts.items():
            if not values:
                reasons.append(f"{horizon}-second markout is UNKNOWN until its horizon elapses")
    markout_30_values = markouts.get(30, [])
    adverse_rate = (
        sum(value < 0 for value in markout_30_values) / len(markout_30_values)
        if markout_30_values
        else None
    )

    cycle_exit_records = [exit_record for _, exit_record, _ in cycles]
    realized_capture_values = _values(
        cycle_exit_records, ("gross_pnl", "realized_capture", "grid_capture")
    )
    realized_values = _values(
        cycle_exit_records, ("net_cycle_pnl", "net_pnl_quote", "realized_pnl")
    )
    fees = _numeric_sum(fill_rows, ("fee", "fees")) if fill_rows else None
    if fees is None:
        fees = _latest_numeric(scoped_states, ("fees", "fee_total", "cumulative_fees"))
    inventory_unrealized = _latest_numeric(
        [*scoped_states, *scoped_events],
        ("unrealized_pnl", "unrealized_pnl_quote", "unrealized"),
    )
    realized_pnl = sum(realized_values) if realized_values else None
    total_pnl = (
        realized_pnl + inventory_unrealized
        if realized_pnl is not None and inventory_unrealized is not None
        else _latest_numeric(scoped_states, ("total_pnl", "net_pnl"))
    )
    drawdown_values = _values(
        [*scoped_states, *scoped_portfolio, *scoped_events],
        ("drawdown", "drawdown_quote", "portfolio_drawdown"),
    )

    cycles_known = event_available and not unassociated_exits
    completed_cycles = len(cycles) if cycles_known else None
    cycle_hours = (
        observed_duration / 3600.0
        if observed_duration and observed_duration > 0
        else None
    )
    cycles_per_hour = completed_cycles / cycle_hours if cycles_known and cycle_hours else None
    fill_create_ratio = len(entry_fills) / len(creates) if event_available and creates else None
    cancel_create_ratio = len(cancels) / len(creates) if event_available and creates else None
    refreshes = _order_rows(scoped_events, _REFRESH_EVENTS) if event_available else []
    keep_total = len(keeps) + len(refreshes)
    keep_ratio = len(keeps) / keep_total if event_available and keep_total else None
    inventory_exposure_hours = (
        inventory_integral / 3600.0 if inventory_integral is not None else None
    )

    def ratio(numerator: float | None, denominator: float | None) -> float | None:
        return (
            numerator / denominator
            if numerator is not None and denominator is not None and denominator > 0
            else None
        )

    metrics = VolumeEfficiencyMetrics(
        window_start=_iso(start),
        window_end=_iso(end),
        observed_duration_seconds=observed_duration,
        trading_pair=asset,
        evidence_source=evidence_source,
        executed_buy_notional=buy_volume,
        executed_sell_notional=sell_volume,
        executed_total_notional=total_volume,
        executed_fill_count=executed_fill_count,
        missing_fill_notional_count=missing_fill_notional,
        average_gross_exposure=average_gross,
        average_absolute_inventory=average_inventory,
        average_absolute_inventory_base=average_inventory_base,
        average_margin_used=average_margin,
        average_beta_risk_used=average_beta,
        max_gross_exposure=max_gross,
        max_inventory=max_inventory,
        max_beta_exposure=max_beta,
        exposure_time_notional_seconds=gross_integral,
        inventory_exposure_time_notional_seconds=inventory_integral,
        completed_cycles=completed_cycles,
        cycles_per_hour=cycles_per_hour,
        median_cycle_duration_seconds=_median(cycle_durations),
        mean_cycle_duration_seconds=_mean(cycle_durations),
        capital_time_efficiency=ratio(total_volume, inventory_exposure_hours),
        volume_per_average_gross_exposure=ratio(total_volume, average_gross),
        volume_per_average_inventory=ratio(total_volume, average_inventory),
        volume_per_average_margin=ratio(total_volume, average_margin),
        volume_per_beta_risk=ratio(total_volume, average_beta),
        orders_created=len(creates) if event_available else None,
        orders_cancelled=len(cancels) if event_available else None,
        orders_kept=len(keeps) if event_available else None,
        fill_create_ratio=fill_create_ratio,
        cancel_create_ratio=cancel_create_ratio,
        keep_ratio=keep_ratio,
        median_quote_lifetime=_median(lifetimes),
        mean_quote_lifetime=_mean(lifetimes),
        realized_grid_capture=sum(realized_capture_values) if realized_capture_values else None,
        fees=fees,
        inventory_unrealized=inventory_unrealized,
        total_pnl=total_pnl,
        markout_5s=_mean(markouts.get(5, [])),
        markout_30s=_mean(markouts.get(30, [])),
        markout_60s=_mean(markouts.get(60, [])),
        adverse_selection_rate=adverse_rate,
        drawdown=max(drawdown_values) if drawdown_values else None,
        confidence=("MEDIUM" if event_available else ("LOW" if all_timestamps else UNKNOWN)),
        sample_size=len(scoped_events) + len(scoped_states) + len(scoped_portfolio),
        reasons=tuple(dict.fromkeys(reasons)),
    )
    return _metric_values_status(metrics)


class PerformanceObserver:
    """Aggregate one bounded window from existing records only."""

    def __init__(self, config: ObserverConfig | None = None):
        self.config = config or ObserverConfig()

    def observe(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        state_records: Sequence[Mapping[str, Any]] = (),
        portfolio_records: Sequence[Mapping[str, Any]] = (),
        relationship_records: Sequence[Mapping[str, Any]] = (),
        plan_records: Sequence[Mapping[str, Any]] = (),
        asset: str = "ALL",
        event_source_status: str = "missing",
        state_source_status: str = "missing",
        portfolio_source_status: str = "missing",
        relationship_source_status: str = "missing",
        evidence_source: str = "LIVE_OBSERVED",
        end_timestamp: float | None = None,
    ) -> PerformanceObservation:
        start, end = _bounds(
            [events, state_records, portfolio_records, relationship_records, plan_records],
            self.config,
            end_timestamp,
        )
        if start is None or end is None:
            window = PerformanceWindow(
                None,
                None,
                asset,
                UNKNOWN,
                UNKNOWN,
                UNKNOWN,
                evidence_source=evidence_source,
                reasons=("no timestamped records were available",),
            )
            volume_efficiency = _build_volume_efficiency(
                (),
                (),
                (),
                asset=asset,
                start=None,
                end=None,
                event_source_status=event_source_status,
                state_source_status=state_source_status,
                portfolio_source_status=portfolio_source_status,
                evidence_source=evidence_source,
                minimum_order_events=self.config.minimum_order_events,
                markout_horizons_seconds=self.config.markout_horizons_seconds,
            )
            return PerformanceObservation(
                window=window,
                source_status={
                    "execution_journal": event_source_status,
                    "state": state_source_status,
                    "portfolio_risk": portfolio_source_status,
                    "relationship": relationship_source_status,
                },
                source_record_counts={
                    "execution_journal": len(events),
                    "state": len(state_records),
                    "portfolio_risk": len(portfolio_records),
                    "relationship": len(relationship_records),
                    "plan": len(plan_records),
                },
                volume_efficiency=volume_efficiency,
            )

        scoped_events = _scoped(events, start=start, end=end, asset=asset)
        scoped_states = _scoped(state_records, start=start, end=end, asset=asset)
        scoped_portfolio = _scoped(portfolio_records, start=start, end=end, asset="ALL")
        scoped_relationship = _scoped(relationship_records, start=start, end=end, asset=asset)
        scoped_plans = _scoped(plan_records, start=start, end=end, asset=asset)
        event_source_available = event_source_status == "ok"
        volume_efficiency = _build_volume_efficiency(
            scoped_events,
            scoped_states,
            scoped_portfolio,
            asset=asset,
            start=start,
            end=end,
            event_source_status=event_source_status,
            state_source_status=state_source_status,
            portfolio_source_status=portfolio_source_status,
            evidence_source=evidence_source,
            minimum_order_events=self.config.minimum_order_events,
            markout_horizons_seconds=self.config.markout_horizons_seconds,
        )

        creates = _event_rows(scoped_events, set(_CREATE_EVENTS))
        if not creates:
            creates = _event_rows(scoped_events, set(_CREATE_FALLBACK_EVENTS))
        cancels = _event_rows(scoped_events, set(_CANCEL_EVENTS))
        keeps = _event_rows(scoped_events, set(_KEEP_EVENTS))
        refreshes = _event_rows(scoped_events, set(_REFRESH_EVENTS))
        fills = _event_rows(scoped_events, set(_FILL_EVENTS))
        cycles = _event_rows(scoped_events, set(_CYCLE_EVENTS))
        safety = [
            record
            for record in scoped_events
            if _event_name(record) in {"SAFETY_CANCEL", "MAKER_SAFETY"}
            or str(record.get("reason_code", "")).upper() == "MAKER_SAFETY"
        ]

        orders_created = len(creates) if event_source_available else None
        orders_cancelled = len(cancels) if event_source_available else None
        orders_kept = len(keeps) if event_source_available else None
        orders_refreshed = len(refreshes) if event_source_available else None
        safety_cancels = len(safety) if event_source_available else None
        fill_count = len(fills) if event_source_available else None
        cycle_count = len(cycles) if event_source_available else None
        lifetime_rows = [*cancels, *fills]
        lifetimes = (
            _explicit_lifetimes(scoped_events, lifetime_rows) if event_source_available else []
        )

        cancel_create_ratio = (
            orders_cancelled / orders_created
            if orders_cancelled is not None and orders_created
            else None
        )
        keep_total = (orders_kept or 0) + (orders_refreshed or 0)
        keep_ratio = (orders_kept / keep_total) if orders_kept is not None and keep_total else None
        fill_create_ratio = (
            fill_count / orders_created if fill_count is not None and orders_created else None
        )

        capture_rows = cycles
        capture_values = _values(
            capture_rows,
            ("gross_pnl", "net_cycle_pnl", "net_pnl_quote", "realized_pnl"),
        )
        realized_values = _values(capture_rows, ("net_cycle_pnl", "net_pnl_quote", "realized_pnl"))
        fees = _numeric_sum(scoped_events, ("fee", "fees")) if event_source_available else None
        maker_capture = sum(capture_values) if capture_values else None
        realized = sum(realized_values) if realized_values else None
        if realized is None and maker_capture is not None and fees is not None:
            realized = maker_capture - fees

        unrealized_values = _values(
            scoped_events,
            ("unrealized_pnl", "unrealized_pnl_quote", "unrealized"),
        )
        unrealized = unrealized_values[-1] if unrealized_values else None
        total = realized + unrealized if realized is not None and unrealized is not None else None

        markouts = {
            horizon: _markouts(fills, horizon, end) if event_source_available else []
            for horizon in self.config.markout_horizons_seconds
        }
        markout_5s = _mean(markouts.get(5, []))
        markout_30s = _mean(markouts.get(30, []))
        markout_60s = _mean(markouts.get(60, []))
        markout_30_values = markouts.get(30, [])
        adverse_rate = (
            sum(value < 0 for value in markout_30_values) / len(markout_30_values)
            if markout_30_values
            else None
        )

        inventory_values = [abs(value) for value in _values(scoped_states, ("inventory_ratio",))]
        beta_values = [
            abs(value)
            for value in _values(
                scoped_portfolio,
                (
                    "btc_beta_equivalent_exposure",
                    "portfolio_beta_exposure",
                    "beta_exposure",
                ),
            )
        ]
        drawdown_values = _values(
            [*scoped_events, *scoped_portfolio],
            ("drawdown", "drawdown_quote", "portfolio_drawdown"),
        )
        turnover = _numeric_sum(
            [
                record
                for record in scoped_events
                if _event_name(record) in _FILL_EVENTS | _CYCLE_EVENTS
            ],
            ("quote_notional", "quote_amount", "turnover"),
        )
        if turnover is None:
            cumulative_turnover = _values(scoped_events, ("turnover",))
            turnover = cumulative_turnover[-1] if cumulative_turnover else None

        mode = _dominant(scoped_plans or scoped_events, ("mode", "grid_mode"))
        volatility = _dominant(
            scoped_states,
            ("global_risk_regime", "global_risk_state", "volatility_state"),
        )
        relationship = _dominant(
            [
                *scoped_relationship,
                *[
                    record
                    for record in scoped_states
                    if "relationship_regime" in record or "relationship_state" in record
                ],
            ],
            ("relationship_regime", "relationship_state"),
        )

        reasons: list[str] = []
        if not event_source_available:
            reasons.append(
                "execution journal is missing or unavailable; order lifecycle metrics "
                "remain UNKNOWN"
            )
        elif len(scoped_events) < self.config.minimum_order_events:
            reasons.append(
                f"execution sample below minimum_order_events={self.config.minimum_order_events}"
            )
        if state_source_status != "ok":
            reasons.append("state stream is unavailable; inventory metrics remain UNKNOWN")
        if portfolio_source_status != "ok":
            reasons.append("portfolio risk stream is unavailable; beta metrics remain UNKNOWN")
        if relationship_source_status != "ok":
            reasons.append(
                "relationship stream is unavailable; relationship regime remains UNKNOWN"
            )
        if (
            event_source_available
            and (fill_count or 0) < self.config.minimum_fills_for_fill_metrics
        ):
            reasons.append(
                "fill-dependent metrics require "
                f"minimum_fills_for_fill_metrics={self.config.minimum_fills_for_fill_metrics}"
            )
        if (
            event_source_available
            and (cycle_count or 0) < self.config.minimum_completed_cycles_for_capture_metrics
        ):
            reasons.append(
                "capture metrics require "
                f"minimum_completed_cycles_for_capture_metrics={self.config.minimum_completed_cycles_for_capture_metrics}"
            )
        for horizon, values in markouts.items():
            if fills and not values:
                reasons.append(f"{horizon}-second markout is UNKNOWN until its horizon elapses")
        if not reasons:
            reasons.append("observation window contains sufficient timestamped source records")

        confidence = UNKNOWN
        if event_source_available:
            confidence = (
                "MEDIUM" if len(scoped_events) >= self.config.minimum_order_events else "LOW"
            )
        elif scoped_states or scoped_portfolio:
            confidence = "LOW"

        window = PerformanceWindow(
            start_timestamp=_iso(start),
            end_timestamp=_iso(end),
            asset=asset,
            mode=mode,
            global_volatility_regime=volatility,
            relationship_regime=relationship,
            orders_created=orders_created,
            orders_cancelled=orders_cancelled,
            orders_kept=orders_kept,
            orders_refreshed=orders_refreshed,
            safety_cancels=safety_cancels,
            fills=fill_count,
            completed_cycles=cycle_count,
            cancel_create_ratio=cancel_create_ratio,
            keep_ratio=keep_ratio,
            fill_create_ratio=fill_create_ratio,
            median_order_lifetime=_median(lifetimes),
            mean_order_lifetime=_mean(lifetimes),
            maker_capture_quote=maker_capture,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=total,
            markout_5s=markout_5s,
            markout_30s=markout_30s,
            markout_60s=markout_60s,
            adverse_markout_rate=adverse_rate,
            inventory_ratio_mean=_mean(inventory_values),
            inventory_ratio_max=max(inventory_values) if inventory_values else None,
            portfolio_beta_exposure_mean=_mean(beta_values),
            portfolio_beta_exposure_max=max(beta_values) if beta_values else None,
            drawdown=max(drawdown_values) if drawdown_values else None,
            turnover=turnover,
            fees_if_known=fees,
            confidence=confidence,
            sample_count=len(scoped_events),
            reasons=tuple(dict.fromkeys(reasons)),
            evidence_source=evidence_source,
        )
        window = _metric_status(
            window,
            orders_created=orders_created,
            orders_cancelled=orders_cancelled,
            orders_kept=orders_kept,
            orders_refreshed=orders_refreshed,
            fills=fill_count,
            completed_cycles=cycle_count,
            median_order_lifetime=window.median_order_lifetime,
            maker_capture_quote=maker_capture,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=total,
            markout_5s=markout_5s,
            markout_30s=markout_30s,
            markout_60s=markout_60s,
            inventory_ratio_mean=window.inventory_ratio_mean,
            portfolio_beta_exposure_mean=window.portfolio_beta_exposure_mean,
            drawdown=window.drawdown,
            turnover=turnover,
            fees_if_known=fees,
        )
        return PerformanceObservation(
            window=window,
            source_status={
                "execution_journal": event_source_status,
                "state": state_source_status,
                "portfolio_risk": portfolio_source_status,
                "relationship": relationship_source_status,
            },
            source_record_counts={
                "execution_journal": len(events),
                "state": len(state_records),
                "portfolio_risk": len(portfolio_records),
                "relationship": len(relationship_records),
                "plan": len(plan_records),
            },
            volume_efficiency=volume_efficiency,
        )

    def observe_replay(
        self,
        result: ReplayResult,
        *,
        asset: str = "ALL",
    ) -> PerformanceObservation:
        """Adapt existing replay summaries without presenting them as live data."""

        summary = summarize_replay(result)
        ticks = result.ticks
        observation = self.observe(
            result.events,
            state_records=ticks,
            plan_records=ticks,
            asset=asset,
            event_source_status="ok",
            state_source_status="ok",
            portfolio_source_status="missing",
            evidence_source="SHADOW_REPLAY",
        )
        window = replace(
            observation.window,
            orders_created=summary.get("entry_creates"),
            orders_cancelled=summary.get("entry_cancels"),
            orders_kept=summary.get("keep_count"),
            orders_refreshed=summary.get("refresh_count"),
            fills=summary.get("entry_fills"),
            completed_cycles=summary.get("completed_grid_cycles"),
            cancel_create_ratio=summary.get("cancel_create_ratio"),
            fill_create_ratio=summary.get("fills_per_created_entry"),
            median_order_lifetime=summary.get("median_quote_lifetime_before_cancel_or_fill"),
            mean_order_lifetime=summary.get("average_quote_lifetime_before_cancel_or_fill"),
            maker_capture_quote=summary.get("gross_realized_pnl"),
            realized_pnl=summary.get("net_realized_pnl"),
            unrealized_pnl=summary.get("unrealized_pnl_end"),
            total_pnl=summary.get("total_pnl"),
            # Keep the observer's elapsed-horizon checks.  The existing replay
            # summary is reused for completed metrics, but a supplied markout
            # field cannot make a future horizon available early.
            markout_5s=observation.window.markout_5s,
            markout_30s=observation.window.markout_30s,
            markout_60s=observation.window.markout_60s,
            inventory_ratio_max=summary.get("maximum_inventory_ratio"),
            drawdown=summary.get("maximum_drawdown"),
            turnover=summary.get("turnover"),
            fees_if_known=summary.get("fees"),
            metric_status={
                key: ("AVAILABLE" if value is not None else UNKNOWN)
                for key, value in {
                    "orders_created": summary.get("entry_creates"),
                    "orders_cancelled": summary.get("entry_cancels"),
                    "fills": summary.get("entry_fills"),
                    "completed_cycles": summary.get("completed_grid_cycles"),
                    "realized_pnl": summary.get("net_realized_pnl"),
                    "unrealized_pnl": summary.get("unrealized_pnl_end"),
                    "markout_5s": observation.window.markout_5s,
                    "markout_30s": observation.window.markout_30s,
                    "markout_60s": observation.window.markout_60s,
                }.items()
            },
            reasons=(
                "existing evaluation.metrics.summarize_replay supplied replay metrics",
                "replay evidence is not live exchange evidence",
                *observation.window.reasons,
            ),
        )
        return replace(observation, window=window)


__all__ = [
    "UNKNOWN",
    "ObserverConfig",
    "PerformanceObservation",
    "PerformanceObserver",
    "PerformanceWindow",
    "VolumeEfficiencyMetrics",
]
