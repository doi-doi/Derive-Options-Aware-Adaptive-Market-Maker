"""Behavioral, replay, risk, and execution metrics for Stage 6."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .data_loader import EvaluationFrame, finite_float, parse_timestamp
from .replay import ReplayResult


def _events(result: ReplayResult, name: str) -> list[dict[str, Any]]:
    return [event for event in result.events if event.get("event") == name]


def _sum_numeric(rows: Iterable[Mapping[str, Any]], field_name: str) -> float:
    return sum(finite_float(row.get(field_name)) or 0.0 for row in rows)


def _mean(values: Iterable[float]) -> float | None:
    values = [value for value in values if math.isfinite(value)]
    return statistics.mean(values) if values else None


def _median(values: Iterable[float]) -> float | None:
    values = [value for value in values if math.isfinite(value)]
    return statistics.median(values) if values else None


def _time_weighted_distribution(
    records: Sequence[Mapping[str, Any]], field_name: str
) -> list[dict[str, Any]]:
    rows = []
    for index, record in enumerate(records):
        timestamp = parse_timestamp(record.get("timestamp"))
        if timestamp is not None:
            rows.append((timestamp, index, record))
    rows.sort(key=lambda item: (item[0], item[1]))
    if not rows:
        return []
    intervals = [
        later[0] - earlier[0]
        for earlier, later in zip(rows, rows[1:], strict=False)
        if later[0] >= earlier[0]
    ]
    default_interval = statistics.median(intervals) if intervals else 0.0
    durations: Counter[str] = Counter()
    total = 0.0
    for index, (timestamp, _, record) in enumerate(rows):
        next_timestamp = (
            rows[index + 1][0] if index + 1 < len(rows) else timestamp + default_interval
        )
        duration = max(0.0, next_timestamp - timestamp)
        value = str(record.get(field_name, "unknown"))
        durations[value] += duration
        total += duration
    return [
        {
            "value": value,
            "duration_seconds": duration,
            "percentage": duration / total * 100 if total else 0.0,
            "records": sum(
                1 for _, _, record in rows if str(record.get(field_name, "unknown")) == value
            ),
        }
        for value, duration in sorted(durations.items())
    ]


def categorical_distribution(
    records: Sequence[Mapping[str, Any]], field_name: str
) -> list[dict[str, Any]]:
    """Return both record-count and time-weighted distributions."""

    counts = Counter(str(record.get(field_name, "unknown")) for record in records)
    total = sum(counts.values())
    weighted = {row["value"]: row for row in _time_weighted_distribution(records, field_name)}
    return [
        {
            "value": value,
            "records": count,
            "record_percentage": count / total * 100 if total else 0.0,
            "duration_seconds": weighted.get(value, {}).get("duration_seconds", 0.0),
            "time_percentage": weighted.get(value, {}).get("percentage", 0.0),
        }
        for value, count in sorted(counts.items())
    ]


def transition_matrix(
    records: Sequence[Mapping[str, Any]], field_name: str = "mode"
) -> dict[str, dict[str, int]]:
    ordered = sorted(
        (
            (parse_timestamp(record.get("timestamp")), index, record)
            for index, record in enumerate(records)
            if parse_timestamp(record.get("timestamp")) is not None
        ),
        key=lambda item: (item[0], item[1]),
    )
    matrix: Counter[tuple[str, str]] = Counter()
    for previous, current in zip(ordered, ordered[1:], strict=False):
        from_value = str(previous[2].get(field_name, "unknown"))
        to_value = str(current[2].get(field_name, "unknown"))
        if from_value != to_value:
            matrix[(from_value, to_value)] += 1
    values = sorted({value for pair in matrix for value in pair})
    return {
        from_value: {to_value: matrix[(from_value, to_value)] for to_value in values}
        for from_value in values
    }


def numeric_summary(records: Sequence[Mapping[str, Any]], field_name: str) -> dict[str, Any]:
    values = [finite_float(record.get(field_name)) for record in records]
    values = [value for value in values if value is not None]
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def iv_regime(value: Any) -> str:
    number = finite_float(value)
    if number is None:
        return "unknown"
    if number < 0.90:
        return "low"
    if number > 1.10:
        return "high"
    return "normal"


def _mode_and_iv_stats(
    frames: Sequence[EvaluationFrame],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mode_groups: dict[str, list[EvaluationFrame]] = defaultdict(list)
    iv_groups: dict[str, list[EvaluationFrame]] = defaultdict(list)
    for frame in frames:
        mode_groups[str(frame.mode.get("mode", "unknown"))].append(frame)
        iv_groups[iv_regime(frame.state.get("iv_ratio"))].append(frame)

    def summarize(group: Sequence[EvaluationFrame], key: str, value: str) -> dict[str, Any]:
        widths = [finite_float(frame.plan.get("total_grid_width_pct")) for frame in group]
        modes = Counter(str(frame.mode.get("mode", "unknown")) for frame in group)
        levels = [
            (finite_float(frame.plan.get("buy_levels_count")) or 0)
            + (finite_float(frame.plan.get("sell_levels_count")) or 0)
            for frame in group
        ]
        capital = [finite_float(frame.plan.get("effective_quote_amount")) for frame in group]
        capital = [item for item in capital if item is not None]
        return {
            key: value,
            "records": len(group),
            "average_grid_width_pct": _mean(item for item in widths if item is not None),
            "average_level_count_total": _mean(levels),
            "average_effective_quote_amount": _mean(capital),
            "mode_distribution": dict(sorted(modes.items())),
        }

    return (
        [summarize(group, "mode", value) for value, group in sorted(mode_groups.items())],
        [summarize(group, "iv_regime", value) for value, group in sorted(iv_groups.items())],
    )


def geometry_statistics(frames: Sequence[EvaluationFrame]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for frame in frames:
        width = finite_float(frame.plan.get("total_grid_width_pct"))
        half = finite_float(frame.plan.get("half_grid_width_pct"))
        inner = finite_float(frame.plan.get("inner_distance_bps"))
        center = finite_float(frame.plan.get("center_price"))
        levels = [
            level
            for level in [*frame.plan.get("buy_levels", []), *frame.plan.get("sell_levels", [])]
            if isinstance(level, Mapping)
        ]
        distances = [finite_float(level.get("distance_from_center_bps")) for level in levels]
        distances = [distance for distance in distances if distance is not None]
        rows.append(
            {
                "timestamp": frame.timestamp,
                "mode": frame.mode.get("mode"),
                "volatility_state": frame.state.get("volatility_state"),
                "iv_regime": iv_regime(frame.state.get("iv_ratio")),
                "direction_state": frame.state.get("direction_state"),
                "half_width_pct": half,
                "full_width_pct": width,
                "inner_distance_bps": inner,
                "outer_distance_bps": max(distances) if distances else None,
                "level_count": len(levels),
                "average_distance_bps": _mean(distances),
                "effective_quote_amount": finite_float(frame.plan.get("effective_quote_amount")),
                "buy_allocation_pct": finite_float(frame.plan.get("buy_allocation_pct")),
                "sell_allocation_pct": finite_float(frame.plan.get("sell_allocation_pct")),
                "center_price": center,
            }
        )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for field_name in ("mode", "volatility_state", "iv_regime", "direction_state"):
            grouped[f"by_{field_name}:{row.get(field_name)}"].append(row)
    grouped_summary = {
        key: {
            "records": len(group),
            "average_full_width_pct": numeric_summary(group, "full_width_pct").get("mean"),
            "average_inner_distance_bps": numeric_summary(group, "inner_distance_bps").get("mean"),
            "average_level_count": numeric_summary(group, "level_count").get("mean"),
            "average_buy_allocation_pct": numeric_summary(
                group, "buy_allocation_pct"
            ).get("mean"),
            "average_sell_allocation_pct": numeric_summary(
                group, "sell_allocation_pct"
            ).get("mean"),
        }
        for key, group in sorted(grouped.items())
    }
    return {
        "records": rows,
        "summary": numeric_summary(rows, "full_width_pct"),
        "by_bucket": grouped_summary,
    }


def _group_replay_events(result: ReplayResult, field_name: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in _events(result, "ENTRY_FILLED"):
        groups[str(event.get(field_name, "unknown"))].append(event)
    rows = []
    output_field = "iv_regime" if field_name == "entry_iv_regime" else field_name
    for value, entries in sorted(groups.items()):
        exits = [
            event
            for event in _events(result, "TP_FILLED")
            if str(event.get(field_name, "unknown")) == value
        ]
        rows.append(
            {
                output_field: value,
                "entry_fills": len(entries),
                "exit_fills": len(exits),
                "gross_pnl": _sum_numeric(exits, "gross_pnl"),
                "net_cycle_pnl": _sum_numeric(exits, "net_cycle_pnl"),
                "filled_quote_volume": _sum_numeric(entries, "quote_notional")
                + _sum_numeric(exits, "quote_notional"),
                "mean_markout_30s_bps": _mean(
                    finite_float(event.get("markout_30s_bps"))
                    for event in entries
                    if finite_float(event.get("markout_30s_bps")) is not None
                ),
                "mean_holding_time_seconds": _mean(
                    finite_float(event.get("holding_time_seconds"))
                    for event in exits
                    if finite_float(event.get("holding_time_seconds")) is not None
                ),
            }
        )
    return rows


def summarize_replay(result: ReplayResult) -> dict[str, Any]:
    """Calculate all core, risk, execution, markout, and grouped metrics."""

    entries = _events(result, "ENTRY_FILLED")
    exits = _events(result, "TP_FILLED")
    creates = _events(result, "ENTRY_CREATED")
    cancels = _events(result, "ENTRY_CANCELLED")
    keeps = _events(result, "ENTRY_KEEP")
    blocks = _events(result, "ENTRY_BLOCKED")
    ticks = result.ticks
    final_tick = ticks[-1] if ticks else {}
    gross = _sum_numeric(exits, "gross_pnl")
    fees = _sum_numeric(exits, "fee") + _sum_numeric(entries, "fee")
    # Entry fees are not attached to ENTRY_FILLED in older event records; the
    # tick's final fee total remains authoritative when available.
    fees = finite_float(final_tick.get("fees")) if final_tick.get("fees") is not None else fees
    fees = fees or 0.0
    unrealized = finite_float(final_tick.get("unrealized_pnl")) or 0.0
    net_realized = gross - fees
    total_pnl = net_realized + unrealized
    inventory_values = [abs(finite_float(tick.get("position_base")) or 0.0) for tick in ticks]
    inventory_ratios = [
        abs(finite_float(tick.get("inventory_ratio")))
        for tick in ticks
        if finite_float(tick.get("inventory_ratio")) is not None
    ]
    drawdowns = [finite_float(tick.get("drawdown")) or 0.0 for tick in ticks]
    entry_quotes = _sum_numeric(entries, "quote_notional")
    exit_quotes = _sum_numeric(exits, "quote_notional")
    turnover = entry_quotes + exit_quotes
    holding_times = [
        finite_float(event.get("holding_time_seconds"))
        for event in exits
        if finite_float(event.get("holding_time_seconds")) is not None
    ]
    profitable = [finite_float(event.get("net_cycle_pnl")) or 0.0 for event in exits]
    hard_blocks = [event for event in blocks if "maximum" in str(event.get("reason", ""))]
    mode_groups = _group_replay_events(result, "mode")
    iv_groups = _group_replay_events(result, "entry_iv_regime")
    markouts = {}
    for horizon in (5, 30, 60):
        values = [
            finite_float(event.get(f"markout_{horizon}s_bps"))
            for event in entries
            if finite_float(event.get(f"markout_{horizon}s_bps")) is not None
        ]
        markouts[f"{horizon}s"] = {
            "count": len(values),
            "mean_bps": _mean(values),
            "median_bps": _median(values),
        }
    return {
        "strategy": result.strategy,
        "fill_model": result.fill_model,
        "warnings": list(result.warnings),
        "entry_fills": len(entries),
        "completed_grid_cycles": len(exits),
        "buy_fills": sum(str(event.get("side")) == "buy" for event in entries),
        "sell_fills": sum(str(event.get("side")) == "sell" for event in entries),
        "filled_quote_volume": entry_quotes + exit_quotes,
        "turnover": turnover,
        "gross_realized_pnl": gross,
        "fees": fees,
        "net_realized_pnl": net_realized,
        "unrealized_pnl_end": unrealized,
        "total_pnl": total_pnl,
        "maximum_absolute_inventory_base": max(inventory_values) if inventory_values else 0.0,
        "average_absolute_inventory_base": _mean(inventory_values) or 0.0,
        "maximum_inventory_ratio": max(inventory_ratios) if inventory_ratios else None,
        "maximum_drawdown": max(drawdowns) if drawdowns else 0.0,
        "average_holding_time_seconds": _mean(holding_times),
        "median_holding_time_seconds": _median(holding_times),
        "profitable_cycle_percentage": (
            sum(value > 0 for value in profitable) / len(profitable) * 100 if profitable else None
        ),
        "grid_capture_per_completed_cycle": _mean(profitable),
        "pnl_per_filled_quote_volume": net_realized / turnover if turnover else None,
        "max_long_exposure_base": max(
            [max(0.0, finite_float(tick.get("position_base")) or 0.0) for tick in ticks] or [0.0]
        ),
        "max_short_exposure_base": max(
            [max(0.0, -(finite_float(tick.get("position_base")) or 0.0)) for tick in ticks] or [0.0]
        ),
        "time_above_inventory_soft_limit_pct": (
            sum(value >= 0.60 for value in inventory_ratios) / len(ticks) * 100 if ticks else 0.0
        ),
        "hard_limit_blocks": len(hard_blocks),
        "pause_count": sum(
            str(previous.get("mode")) != "pause" and str(current.get("mode")) == "pause"
            for previous, current in zip(ticks, ticks[1:], strict=False)
        ),
        "pause_duration_seconds": sum(
            max(
                0.0,
                (parse_timestamp(current.get("timestamp")) or 0.0)
                - (parse_timestamp(previous.get("timestamp")) or 0.0),
            )
            for previous, current in zip(ticks, ticks[1:], strict=False)
            if str(previous.get("mode")) == "pause"
        ),
        "defensive_duration_seconds": sum(
            max(
                0.0,
                (parse_timestamp(current.get("timestamp")) or 0.0)
                - (parse_timestamp(previous.get("timestamp")) or 0.0),
            )
            for previous, current in zip(ticks, ticks[1:], strict=False)
            if str(previous.get("mode")) == "defensive"
        ),
        "entry_creates": len(creates),
        "entry_cancels": len(cancels),
        "keep_count": len(keeps),
        "refresh_count": sum("stale" in str(event.get("reason", "")) for event in cancels),
        "cancel_create_ratio": len(cancels) / len(creates) if creates else None,
        "fills_per_created_entry": len(entries) / len(creates) if creates else None,
        "average_quote_lifetime_before_cancel_or_fill": _mean(
            [
                finite_float(event.get("lifetime_seconds"))
                for event in cancels
                if finite_float(event.get("lifetime_seconds")) is not None
            ]
            + [
                (finite_float(event.get("timestamp_seconds")) or 0.0)
                - next(
                    (
                        finite_float(created.get("timestamp_seconds")) or 0.0
                        for created in creates
                        if created.get("order_id") == event.get("order_id")
                    ),
                    finite_float(event.get("timestamp_seconds")) or 0.0,
                )
                for event in entries
            ]
        ),
        "quote_distance_from_mid_bps": _mean(
            abs(finite_float(event.get("price")) - finite_float(event.get("mid_price")))
            / finite_float(event.get("mid_price"))
            * 10_000
            for event in creates
            if finite_float(event.get("price")) is not None
            and finite_float(event.get("mid_price")) not in {None, 0}
        ),
        "quote_distance_from_touch_bps": _mean(
            (
                (finite_float(event.get("best_ask")) - finite_float(event.get("price")))
                if str(event.get("side")) == "buy"
                else (finite_float(event.get("price")) - finite_float(event.get("best_bid")))
            )
            / finite_float(event.get("mid_price"))
            * 10_000
            for event in creates
            if finite_float(event.get("mid_price")) not in {None, 0}
            and (
                finite_float(event.get("best_ask")) is not None
                if str(event.get("side")) == "buy"
                else finite_float(event.get("best_bid")) is not None
            )
        ),
        "markout": markouts,
        "performance_by_entry_mode": mode_groups,
        "performance_by_iv_regime": iv_groups,
    }


def plan_stability(
    frames: Sequence[EvaluationFrame], refresh_price_tolerance_bps: float = 5.0
) -> dict[str, Any]:
    """Estimate Stage 5 queue-preserving actions from recorded GridPlans."""

    ordered = sorted(frames, key=lambda frame: frame.timestamp_seconds)
    actions: Counter[str] = Counter()
    active: dict[str, tuple[float, float, str, float]] = {}
    per_level: Counter[str] = Counter()
    for frame in ordered:
        timestamp = frame.timestamp_seconds
        desired: dict[str, tuple[float, float, str]] = {}
        for level in [*frame.plan.get("buy_levels", []), *frame.plan.get("sell_levels", [])]:
            if not isinstance(level, Mapping):
                continue
            level_id = f"{level.get('side')}_{level.get('level_index')}"
            price = finite_float(level.get("theoretical_price"))
            quote = finite_float(level.get("quote_amount"))
            if price is not None and quote is not None:
                desired[level_id] = (price, quote, str(frame.mode.get("mode", "normal")))
        for level_id in list(active):
            if level_id not in desired:
                actions["removed"] += 1
                per_level[f"{level_id}:removed"] += 1
                del active[level_id]
        for level_id, (price, quote, mode) in desired.items():
            if level_id not in active:
                actions["new"] += 1
                per_level[f"{level_id}:new"] += 1
                active[level_id] = (price, quote, mode, timestamp)
                continue
            old_price, old_quote, old_mode, created = active[level_id]
            price_deviation = abs(price - old_price) / old_price * 10_000 if old_price else math.inf
            amount_deviation = abs(quote - old_quote) / old_quote if old_quote else math.inf
            age = max(0.0, timestamp - created)
            if age >= 30.0 and (
                price_deviation > refresh_price_tolerance_bps
                or amount_deviation > 0.05
                or mode != old_mode
            ):
                actions["refresh"] += 1
                per_level[f"{level_id}:refresh"] += 1
                active[level_id] = (price, quote, mode, timestamp)
            else:
                actions["keep"] += 1
                per_level[f"{level_id}:keep"] += 1
    total = sum(actions.values())
    return {
        "actions": dict(sorted(actions.items())),
        "total_actions": total,
        "rates": {
            key: value / total * 100 if total else 0.0 for key, value in sorted(actions.items())
        },
        "per_level": dict(sorted(per_level.items())),
    }


def comparison_rows(
    summaries: Sequence[Mapping[str, Any]], fill_model: str
) -> list[dict[str, Any]]:
    """Return the requested static/RV/IV comparison table for one fill model."""

    selected = [summary for summary in summaries if summary.get("fill_model") == fill_model]
    return [
        {
            "metric": metric,
            **{str(summary.get("strategy")): summary.get(metric) for summary in selected},
        }
        for metric in (
            "total_pnl",
            "net_realized_pnl",
            "maximum_drawdown",
            "maximum_absolute_inventory_base",
            "filled_quote_volume",
            "completed_grid_cycles",
            "cancel_create_ratio",
            "maximum_inventory_ratio",
            "defensive_duration_seconds",
            "hard_limit_blocks",
        )
    ]


__all__ = [
    "categorical_distribution",
    "comparison_rows",
    "geometry_statistics",
    "iv_regime",
    "numeric_summary",
    "plan_stability",
    "summarize_replay",
    "transition_matrix",
]
