"""Machine-readable outputs, neutral SVG charts, and the Stage 6 report."""

# The report body intentionally contains long human-readable evidence strings.
# ruff: noqa: E501

from __future__ import annotations

import csv
import html
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from derive_options_mm.grid_engine import GridParameterConfig, build_grid_plan
from derive_options_mm.mode_selector import (
    GridModeDecision,
    ModeSelectorConfig,
    determine_candidate_mode,
)
from derive_options_mm.state_engine import InventoryState, MarketState

from .baselines import StrategyVariant, strategy_description
from .data_loader import EvaluationDataset, EvaluationFrame, finite_float
from .metrics import (
    categorical_distribution,
    comparison_rows,
    geometry_statistics,
    numeric_summary,
    plan_stability,
    summarize_replay,
    transition_matrix,
)
from .replay import ReplayConfig, ReplayResult


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_json_safe(row), sort_keys=True) + "\n")


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    fields = sorted({str(key) for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
    return _json_safe(value)


_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]


def _series_values(values: Sequence[Any]) -> list[float | None]:
    return [finite_float(value) for value in values]


def _downsample(values: Sequence[float | None], limit: int = 600) -> list[float | None]:
    if len(values) <= limit:
        return list(values)
    step = max(1, len(values) // limit)
    return [values[index] for index in range(0, len(values), step)]


def _svg_text(x: float, y: float, text: str, size: int = 12, anchor: str = "start") -> str:
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="#333">{html.escape(text)}</text>'


def _chart_shell(
    title: str, width: int, height: int, body: str, legend: Sequence[tuple[str, str]]
) -> str:
    legend_body = "".join(
        f'<rect x="{width - 220 + index * 105}" y="25" width="10" height="10" fill="{color}" />'
        f"{_svg_text(width - 205 + index * 105, 35, name, 10)}"
        for index, (name, color) in enumerate(legend)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="100%" height="100%" fill="white" />'
        f"{_svg_text(20, 22, title, 15)}"
        f"{legend_body}{body}</svg>\n"
    )


def _line_svg(
    path: Path,
    title: str,
    series: Sequence[tuple[str, Sequence[Any], str]],
    *,
    backgrounds: Sequence[str] | None = None,
) -> None:
    width, height = 1100, 380
    left, right, top, bottom = 60, 20, 45, 45
    plot_width, plot_height = width - left - right, height - top - bottom
    numeric = [
        value for _, values, _ in series for value in _series_values(values) if value is not None
    ]
    if not numeric:
        numeric = [0.0, 1.0]
    lower, upper = min(numeric), max(numeric)
    if lower == upper:
        lower -= 1
        upper += 1
    body = (
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" '
        f'y2="{top + plot_height}" stroke="#777" />'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#777" />'
        f"{_svg_text(left - 8, top + 5, f'{upper:.4g}', 10, 'end')}"
        f"{_svg_text(left - 8, top + plot_height, f'{lower:.4g}', 10, 'end')}"
    )
    if backgrounds:
        mode_colors = {
            "normal": "#e8f1fb",
            "defensive": "#fff2cc",
            "long_bias": "#e6f4ea",
            "short_bias": "#fce8e6",
            "pause": "#eeeeee",
        }
        sample = (
            backgrounds
            if len(backgrounds) <= 600
            else [
                backgrounds[index]
                for index in range(0, len(backgrounds), max(1, len(backgrounds) // 600))
            ]
        )
        for index, mode in enumerate(sample):
            x = left + index / max(1, len(sample) - 1) * plot_width
            next_x = (
                left + (index + 1) / max(1, len(sample) - 1) * plot_width
                if index + 1 < len(sample)
                else width - right
            )
            body += f'<rect x="{x:.1f}" y="{top}" width="{max(1, next_x - x):.1f}" height="{plot_height}" fill="{mode_colors.get(str(mode), "#ffffff")}" opacity="0.55" />'
    for _, (_, values, color) in enumerate(series):
        sampled = _downsample(_series_values(values))
        points: list[str] = []
        for index, value in enumerate(sampled):
            if value is None:
                continue
            x = left + index / max(1, len(sampled) - 1) * plot_width
            y = top + (upper - value) / (upper - lower) * plot_height
            points.append(f"{x:.1f},{y:.1f}")
        if points:
            body += f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="1.5" />'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _chart_shell(title, width, height, body, [(name, color) for name, _, color in series]),
        encoding="utf-8",
    )


def _bar_svg(path: Path, title: str, values: Mapping[str, float], color: str = "#1f77b4") -> None:
    width, height = 900, 380
    left, right, top, bottom = 70, 20, 45, 65
    plot_width, plot_height = width - left - right, height - top - bottom
    maximum = max([abs(value) for value in values.values()] or [1.0])
    body = (
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" '
        f'y2="{top + plot_height}" stroke="#777" />'
        f"{_svg_text(left - 8, top + 5, f'{maximum:.4g}', 10, 'end')}"
    )
    items = list(values.items())
    bar_width = plot_width / max(1, len(items)) * 0.7
    for index, (label, value) in enumerate(items):
        x = left + (index + 0.15) * plot_width / max(1, len(items))
        bar_height = value / maximum * plot_height if maximum else 0
        y = top + plot_height - bar_height
        body += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{max(0, bar_height):.1f}" fill="{color}" />'
        body += _svg_text(x + bar_width / 2, height - 35, label, 10, "middle")
        body += _svg_text(x + bar_width / 2, y - 5, f"{value:.2f}", 10, "middle")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _chart_shell(title, width, height, body, [("percentage", color)]), encoding="utf-8"
    )


def _scatter_svg(path: Path, title: str, points: Sequence[tuple[float, float]]) -> None:
    width, height = 900, 380
    left, right, top, bottom = 65, 25, 45, 45
    plot_width, plot_height = width - left - right, height - top - bottom
    if not points:
        points = [(0.0, 0.0)]
    xs, ys = zip(*points, strict=False)
    x_low, x_high = min(xs), max(xs)
    y_low, y_high = min(ys), max(ys)
    if x_low == x_high:
        x_low -= 1
        x_high += 1
    if y_low == y_high:
        y_low -= 1
        y_high += 1
    body = (
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" '
        f'y2="{top + plot_height}" stroke="#777" />'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#777" />'
        f"{_svg_text(left, height - 10, f'x: {x_low:.4g}..{x_high:.4g}', 10)}"
        f"{_svg_text(width - right, height - 10, 'IV ratio', 10, 'end')}"
    )
    for x_value, y_value in points:
        x = left + (x_value - x_low) / (x_high - x_low) * plot_width
        y = top + (y_high - y_value) / (y_high - y_low) * plot_height
        body += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#1f77b4" opacity="0.55" />'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _chart_shell(title, width, height, body, [("observations", "#1f77b4")]), encoding="utf-8"
    )


def _correlation(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    x_values, y_values = zip(*pairs, strict=False)
    x_mean, y_mean = statistics.mean(x_values), statistics.mean(y_values)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    denominator = math.sqrt(
        sum((x - x_mean) ** 2 for x in x_values) * sum((y - y_mean) ** 2 for y in y_values)
    )
    return numerator / denominator if denominator else None


def _iv_lead_lag(frames: Sequence[EvaluationFrame]) -> list[dict[str, Any]]:
    """Exploratory correlation of current IV change with future price movement."""

    ordered = sorted(frames, key=lambda frame: frame.timestamp_seconds)
    timestamps = [frame.timestamp_seconds for frame in ordered]
    mids = [finite_float(frame.snapshot.get("mid_price")) for frame in ordered]
    rows: list[dict[str, Any]] = []
    for horizon in (30, 60, 300):
        pairs: list[tuple[float, float]] = []
        for index, frame in enumerate(ordered):
            iv_change = finite_float(frame.state.get("iv_change"))
            current_mid = mids[index]
            if iv_change is None or current_mid is None or current_mid <= 0:
                continue
            target = frame.timestamp_seconds + horizon
            future_index = next(
                (
                    candidate
                    for candidate, timestamp in enumerate(timestamps)
                    if timestamp >= target
                ),
                None,
            )
            if future_index is None or mids[future_index] is None or mids[future_index] <= 0:
                continue
            future_move = abs(math.log(mids[future_index] / current_mid))
            pairs.append((iv_change, future_move))
        rows.append(
            {
                "horizon_seconds": horizon,
                "observations": len(pairs),
                "iv_change_vs_future_absolute_log_return_correlation": _correlation(pairs),
                "exploratory_only": True,
            }
        )
    return rows


def _rv_iv_buckets(frames: Sequence[EvaluationFrame]) -> list[dict[str, Any]]:
    buckets: dict[str, list[EvaluationFrame]] = defaultdict(list)
    for frame in frames:
        rv = finite_float(frame.state.get("realized_volatility_ratio"))
        iv = finite_float(frame.state.get("iv_ratio"))
        if rv is None or iv is None:
            continue
        rv_bucket = "high" if rv >= 1.0 else "low"
        iv_bucket = "high" if iv >= 1.0 else "low"
        buckets[f"rv_{rv_bucket}_iv_{iv_bucket}"].append(frame)
    rows = []
    for bucket, group in sorted(buckets.items()):
        scores = [finite_float(frame.state.get("volatility_score")) for frame in group]
        widths = [finite_float(frame.plan.get("total_grid_width_pct")) for frame in group]
        rows.append(
            {
                "bucket": bucket,
                "records": len(group),
                "average_volatility_score": statistics.mean([x for x in scores if x is not None])
                if any(x is not None for x in scores)
                else None,
                "average_grid_width_pct": statistics.mean([x for x in widths if x is not None])
                if any(x is not None for x in widths)
                else None,
                "volatility_state_distribution": dict(
                    Counter(str(frame.state.get("volatility_state")) for frame in group)
                ),
                "mode_distribution": dict(Counter(str(frame.mode.get("mode")) for frame in group)),
            }
        )
    return rows


def _inventory_stress(frame: EvaluationFrame, config: GridParameterConfig) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ratios = (-1.0, -0.75, -0.50, -0.25, 0.0, 0.25, 0.50, 0.75, 1.0)
    for ratio in ratios:
        state_data = dict(frame.state)
        state_data["inventory_ratio"] = ratio
        state_data["inventory_state"] = (
            InventoryState.LONG.value
            if ratio > 0.10
            else InventoryState.SHORT.value
            if ratio < -0.10
            else InventoryState.NEUTRAL.value
        )
        try:
            state = MarketState.model_validate(state_data)
            candidate = determine_candidate_mode(state, ModeSelectorConfig())
            mode_data = dict(frame.mode)
            mode_data["mode"] = candidate.mode.value
            mode_data["recommended_profile"] = {
                "normal": "standard",
                "defensive": "risk_reduced",
                "long_bias": "long_bias",
                "short_bias": "short_bias",
                "pause": "disabled",
            }[candidate.mode.value]
            mode_data["inventory_ratio"] = ratio
            mode_data["inventory_state"] = state.inventory_state.value
            mode_data["reasons"] = list(candidate.reasons)
            decision = GridModeDecision.model_validate(mode_data)
            plan = build_grid_plan(frame.snapshot, state, decision, config)
            rows.append(
                {
                    "inventory_ratio": ratio,
                    "mode": decision.mode.value,
                    "enabled": plan.enabled,
                    "center_shift_bps": float(plan.center_shift_bps),
                    "buy_allocation_pct": float(plan.buy_allocation_pct),
                    "sell_allocation_pct": float(plan.sell_allocation_pct),
                    "reason": "; ".join(plan.reasons[:2]),
                }
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            rows.append(
                {
                    "inventory_ratio": ratio,
                    "mode": "pause",
                    "enabled": False,
                    "center_shift_bps": None,
                    "buy_allocation_pct": None,
                    "sell_allocation_pct": None,
                    "reason": f"stress case failed closed: {type(exc).__name__}",
                }
            )
    return rows


def build_analysis(
    dataset: EvaluationDataset,
    frames: Sequence[EvaluationFrame],
    replay_results: Sequence[ReplayResult],
    *,
    replay_config: ReplayConfig,
    grid_config: GridParameterConfig,
) -> dict[str, Any]:
    """Build the complete analysis object used by all output writers."""

    snapshot_records = dataset.snapshots.sorted_records()
    state_records = dataset.states.sorted_records()
    mode_records = dataset.modes.sorted_records()
    plan_records = dataset.plans.sorted_records()
    mode_groups, iv_groups = (
        [],
        [],
    )
    from .metrics import _mode_and_iv_stats  # local import keeps public API small

    mode_groups, iv_groups = _mode_and_iv_stats(frames)
    summaries = [summarize_replay(result) for result in replay_results]
    full_state_pairs = [
        (
            finite_float(frame.state.get("iv_ratio")),
            finite_float(frame.state.get("volatility_score")),
        )
        for frame in frames
    ]
    iv_vol_pairs = [(x, y) for x, y in full_state_pairs if x is not None and y is not None]
    iv_width_pairs = [
        (
            finite_float(frame.state.get("iv_ratio")),
            finite_float(frame.plan.get("total_grid_width_pct")),
        )
        for frame in frames
    ]
    iv_width_pairs = [(x, y) for x, y in iv_width_pairs if x is not None and y is not None]
    direction_center_pairs = [
        (
            finite_float(frame.state.get("direction_score")),
            finite_float(frame.plan.get("center_shift_bps")),
        )
        for frame in frames
    ]
    direction_center_pairs = [
        (x, y) for x, y in direction_center_pairs if x is not None and y is not None
    ]
    direction_buy_pairs = [
        (
            finite_float(frame.state.get("direction_score")),
            finite_float(frame.plan.get("buy_allocation_pct")),
        )
        for frame in frames
    ]
    direction_buy_pairs = [
        (x, y) for x, y in direction_buy_pairs if x is not None and y is not None
    ]
    direction_sell_pairs = [
        (
            finite_float(frame.state.get("direction_score")),
            finite_float(frame.plan.get("sell_allocation_pct")),
        )
        for frame in frames
    ]
    direction_sell_pairs = [
        (x, y) for x, y in direction_sell_pairs if x is not None and y is not None
    ]
    inventory_center_pairs = [
        (
            finite_float(frame.state.get("inventory_ratio")),
            finite_float(frame.plan.get("center_shift_bps")),
        )
        for frame in frames
    ]
    inventory_center_pairs = [
        (x, y) for x, y in inventory_center_pairs if x is not None and y is not None
    ]
    first_frame = frames[0] if frames else None
    stress = _inventory_stress(first_frame, grid_config) if first_frame else []
    behavior = {
        "snapshot_count": len(snapshot_records),
        "state_count": len(state_records),
        "mode_count": len(mode_records),
        "plan_count": len(plan_records),
        "state_distributions": {
            field: categorical_distribution(state_records, field)
            for field in ("volatility_state", "direction_state", "inventory_state")
        },
        "mode_distribution": categorical_distribution(mode_records, "mode"),
        "mode_transitions": transition_matrix(mode_records),
        "mode_to_mode_transition_count": sum(
            sum(row.values()) for row in transition_matrix(mode_records).values()
        ),
        "plan_frames": len(frames),
        "mode_iv_groups": {"by_mode": mode_groups, "by_iv_regime": iv_groups},
        "iv_distribution": {
            "atm_iv": numeric_summary(state_records, "atm_iv"),
            "iv_ratio": numeric_summary(state_records, "iv_ratio"),
            "iv_change": numeric_summary(state_records, "iv_change"),
            "iv_ratio_vs_volatility_score_correlation": _correlation(iv_vol_pairs),
            "iv_ratio_vs_grid_width_correlation": _correlation(iv_width_pairs),
        },
        "iv_lead_lag": _iv_lead_lag(frames),
        "rv_vs_iv_buckets": _rv_iv_buckets(frames),
        "direction": {
            "score": numeric_summary(state_records, "direction_score"),
            "state_distribution": categorical_distribution(state_records, "direction_state"),
            "direction_score_vs_center_shift_correlation": _correlation(direction_center_pairs),
            "direction_score_vs_buy_allocation_correlation": _correlation(direction_buy_pairs),
            "direction_score_vs_sell_allocation_correlation": _correlation(direction_sell_pairs),
            "bullish_and_long_bias_records": sum(
                frame.state.get("direction_state") == "bullish"
                and frame.mode.get("mode") == "long_bias"
                for frame in frames
            ),
            "bearish_and_short_bias_records": sum(
                frame.state.get("direction_state") == "bearish"
                and frame.mode.get("mode") == "short_bias"
                for frame in frames
            ),
            "directional_bias_not_selected_records": sum(
                frame.state.get("direction_state") in {"bullish", "bearish"}
                and frame.mode.get("mode") not in {"long_bias", "short_bias"}
                for frame in frames
            ),
        },
        "inventory": {
            "ratio": numeric_summary(state_records, "inventory_ratio"),
            "inventory_state_distribution": categorical_distribution(
                state_records, "inventory_state"
            ),
            "inventory_ratio_vs_center_shift_correlation": _correlation(inventory_center_pairs),
            "mostly_zero_inventory": all(
                abs(finite_float(record.get("inventory_ratio")) or 0.0) < 1e-12
                for record in state_records
                if finite_float(record.get("inventory_ratio")) is not None
            ),
            "synthetic_inventory_stress": stress,
        },
        "geometry": geometry_statistics(frames),
        "plan_stability": plan_stability(frames, float(replay_config.refresh_price_tolerance_bps)),
    }
    return {
        "manifest": dataset.manifest(),
        "replay_config": replay_config.to_record(),
        "strategy_variants": {
            variant.value: strategy_description(variant) for variant in StrategyVariant
        },
        "behavior": behavior,
        "replay_summaries": summaries,
        "conservative_comparison": comparison_rows(summaries, "conservative_cross_through"),
        "touch_comparison": comparison_rows(summaries, "touch_optimistic"),
        "live_vs_simulated": {
            "live_derive_testnet_evidence": [
                "Stage 5 authenticated LIMIT_MAKER entry submission with real exchange IDs",
                "post-only status, KEEP, cancel/replace, DEFENSIVE, PAUSE/recovery, and cleanup",
                "no live BTC-PERP maker fill was available in the bounded window",
            ],
            "simulated_replay_evidence": [
                "BBO-based maker fills, adjacent-grid TP, PnL, inventory feedback, and comparisons",
                "all replay results are simulated and are not live Derive PnL",
            ],
        },
    }


def _write_charts(
    output_dir: Path,
    frames: Sequence[EvaluationFrame],
    replay_results: Sequence[ReplayResult],
    behavior: Mapping[str, Any],
) -> list[str]:
    charts_dir = output_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    chart_paths: list[str] = []
    frame_records = [frame.to_record() for frame in frames]
    modes = [record.get("mode") for record in frame_records]

    def add_line(
        name: str,
        title: str,
        series: Sequence[tuple[str, Sequence[Any], str]],
        backgrounds: Sequence[str] | None = None,
    ) -> None:
        path = charts_dir / name
        _line_svg(path, title, series, backgrounds=backgrounds)
        chart_paths.append(str(path))

    add_line(
        "01_mid_price_by_mode.svg",
        "BTC mid price over time with GridMode background",
        [("mid price", [row.get("mid_price") for row in frame_records], _COLORS[0])],
        modes,
    )
    add_line(
        "02_atm_iv_and_ratio.svg",
        "ATM IV and IV ratio over time",
        [
            ("ATM IV", [row.get("atm_iv") for row in frame_records], _COLORS[1]),
            ("IV ratio", [row.get("iv_ratio") for row in frame_records], _COLORS[0]),
        ],
    )
    add_line(
        "03_volatility_and_width.svg",
        "Volatility score and adaptive grid width",
        [
            (
                "volatility score",
                [row.get("volatility_score") for row in frame_records],
                _COLORS[0],
            ),
            ("grid width pct", [row.get("grid_width_pct") for row in frame_records], _COLORS[1]),
        ],
    )
    add_line(
        "04_direction_and_center_shift.svg",
        "Direction score and center shift",
        [
            ("direction score", [row.get("direction_score") for row in frame_records], _COLORS[0]),
            (
                "center shift bps",
                [row.get("center_shift_bps") for row in frame_records],
                _COLORS[1],
            ),
        ],
    )
    add_line(
        "05_allocations.svg",
        "Buy and sell allocation",
        [
            (
                "buy allocation",
                [row.get("buy_allocation_pct") for row in frame_records],
                _COLORS[0],
            ),
            (
                "sell allocation",
                [row.get("sell_allocation_pct") for row in frame_records],
                _COLORS[1],
            ),
        ],
    )
    mode_percentages = {
        row["value"]: row["time_percentage"] for row in behavior.get("mode_distribution", [])
    }
    path = charts_dir / "06_mode_frequency.svg"
    _bar_svg(path, "Grid mode time frequency", mode_percentages)
    chart_paths.append(str(path))

    conservative = [
        result for result in replay_results if result.fill_model == "conservative_cross_through"
    ]
    by_strategy = {result.strategy: result for result in conservative}
    add_line(
        "07_cumulative_simulated_pnl.svg",
        "Cumulative simulated PnL — conservative cross-through model",
        [
            (
                strategy,
                [tick.get("net_pnl") for tick in by_strategy[strategy].ticks],
                _COLORS[index],
            )
            for index, strategy in enumerate(sorted(by_strategy))
        ],
    )
    add_line(
        "08_inventory_by_strategy.svg",
        "Simulated net inventory by strategy",
        [
            (
                strategy,
                [tick.get("position_base") for tick in by_strategy[strategy].ticks],
                _COLORS[index],
            )
            for index, strategy in enumerate(sorted(by_strategy))
        ],
    )
    add_line(
        "09_drawdown_by_strategy.svg",
        "Simulated drawdown by strategy",
        [
            (
                strategy,
                [tick.get("drawdown") for tick in by_strategy[strategy].ticks],
                _COLORS[index],
            )
            for index, strategy in enumerate(sorted(by_strategy))
        ],
    )
    points = [
        (finite_float(row.get("iv_ratio")), finite_float(row.get("grid_width_pct")))
        for row in frame_records
    ]
    _scatter_svg(
        charts_dir / "10_iv_ratio_vs_grid_width.svg",
        "IV ratio versus adaptive grid width",
        [(x, y) for x, y in points if x is not None and y is not None],
    )
    chart_paths.append(str(charts_dir / "10_iv_ratio_vs_grid_width.svg"))
    return chart_paths


def _markdown_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "_No rows available._"
    header = "| " + " | ".join(columns) + " |\n|" + "|".join("---" for _ in columns) + "|\n"
    body = "".join(
        "| " + " | ".join(str(row.get(column, "")) for column in columns) + " |\n" for row in rows
    )
    return header + body


def _report_markdown(
    analysis: Mapping[str, Any],
    dataset: EvaluationDataset,
    frames: Sequence[EvaluationFrame],
    summaries: Sequence[Mapping[str, Any]],
    chart_paths: Sequence[str],
    output_dir: Path,
) -> str:
    behavior = analysis["behavior"]
    manifest = analysis["manifest"]
    quality_rows = [
        {
            "stream": name,
            "records": quality["records"],
            "start": quality["start_timestamp"],
            "end": quality["end_timestamp"],
            "duration_h": round((quality["duration_hours"] or 0), 3),
            "median_interval_s": round(quality["median_sampling_interval_seconds"], 3)
            if quality["median_sampling_interval_seconds"] is not None
            else None,
            "missing_iv_pct": quality["missing_rates"].get("atm_iv"),
            "data_invalid_pct": round(
                100 - quality["availability_rates"].get("data_valid", 100), 6
            )
            if "data_valid" in quality["availability_rates"]
            else None,
            "account_available_pct": quality["availability_rates"].get(
                "account_data_available"
            ),
            "trade_available_pct": quality["availability_rates"].get(
                "trade_data_available"
            ),
            "duplicates": quality["duplicate_timestamp_count"],
            "out_of_order": quality["out_of_order_count"],
        }
        for name, quality in manifest["streams"].items()
    ]
    mode_rows = behavior["mode_distribution"]
    comparison_rows_data = [
        {
            "strategy": summary["strategy"],
            "fill_model": summary["fill_model"],
            "entries": summary["entry_fills"],
            "cycles": summary["completed_grid_cycles"],
            "net_realized": round(summary["net_realized_pnl"], 6),
            "total_pnl": round(summary["total_pnl"], 6),
            "max_dd": round(summary["maximum_drawdown"], 6),
            "max_inventory": round(summary["maximum_absolute_inventory_base"], 8),
            "cancel_create": round(summary["cancel_create_ratio"], 4)
            if summary["cancel_create_ratio"] is not None
            else None,
        }
        for summary in summaries
    ]
    report_lines = [
        "# Stage 6 Evaluation, Replay, Baselines, and Hackathon Evidence",
        "",
        "## 1. Executive summary",
        "",
        f"The deterministic evaluation joined {len(frames):,} plan frames over "
        f"{(manifest['common_duration_seconds'] or 0) / 3600:.2f} common hours. "
        "It measures the existing Stage 1–4 behavior and runs separate offline "
        "replays for a static geometric grid, RV-only adaptive grid, and full "
        "Derive options-aware adaptive grid.",
        "",
        "No parameters were optimized. Simulated fills, PnL, TP exits, and inventory "
        "feedback are replay evidence only; they are not live Derive results.",
        "",
        "## 2. Dataset",
        "",
        _markdown_table(
            quality_rows,
            [
                "stream",
                "records",
                "start",
                "end",
                "duration_h",
                "median_interval_s",
                "missing_iv_pct",
                "data_invalid_pct",
                "account_available_pct",
                "trade_available_pct",
                "duplicates",
                "out_of_order",
            ],
        ),
        "",
        f"Common evaluation window: `{manifest['common_start_timestamp']}` to `{manifest['common_end_timestamp']}`. "
        f"Stage 1–4 as-of frames: `{len(frames):,}`. Source warnings: `{'; '.join(manifest['warnings']) or 'none'}`.",
        "",
        "## 3. Methodology",
        "",
        "Recorded behavior uses latest-at-or-before timestamp joins. No state, mode, plan, "
        "or option value is forward-filled from the future. Performance replay warms the "
        "existing deterministic State → Mode → GridPlan chain, starts with zero simulated "
        "inventory, and feeds simulated fills back into Stage 2 inventory before the next plan.",
        "",
        f"Shared replay assumptions: `{json.dumps(analysis['replay_config'], sort_keys=True)}`.",
        "The 9.30x quote scale is a documented offline capacity normalization based on the "
        "observed 0.01 BTC testnet minimum; Stage 4 quote allocations are not modified.",
        "",
        "## 4. Important limitations",
        "",
        "- The collected snapshots do not include a raw public trade-by-trade execution stream; conservative and touch BBO models are therefore separated.",
        "- Conservative BUY requires a future best ask strictly below the resting bid; conservative SELL requires a future best bid strictly above the resting offer.",
        "- Touch replay is optimistic and may overstate queue fills. Neither BBO model proves a maker fill.",
        "- Partial fills, queue priority, and adverse selection are not directly observed. Maker fee defaults to configurable 0 bps because no reliable local Derive fee schedule was supplied; gross and fee-adjusted values are both emitted.",
        "- The canonical Condor streams are append-only and may change while routines are running; the manifest records hashes and read-time mutation warnings.",
        "",
        "## 5. Strategy behavior",
        "",
        _markdown_table(
            mode_rows,
            ["value", "records", "record_percentage", "time_percentage", "duration_seconds"],
        ),
        "",
        f"Mode transitions: `{behavior['mode_to_mode_transition_count']}`. Transition matrix: `{json.dumps(behavior['mode_transitions'], sort_keys=True)}`.",
        "",
        "## 6. ATM IV effect",
        "",
        f"ATM IV coverage in the snapshot stream is `{100 - (manifest['streams']['snapshots']['missing_rates'].get('atm_iv', 100)):.2f}%`; IV ratio versus volatility-score correlation is `{behavior['iv_distribution']['iv_ratio_vs_volatility_score_correlation']}` and IV ratio versus grid-width correlation is `{behavior['iv_distribution']['iv_ratio_vs_grid_width_correlation']}`. These are associations, not causality.",
        "",
        _markdown_table(
            behavior["mode_iv_groups"]["by_iv_regime"],
            [
                "iv_regime",
                "records",
                "average_grid_width_pct",
                "average_level_count_total",
                "average_effective_quote_amount",
                "mode_distribution",
            ],
        ),
        "",
        "Exploratory IV lead/lag correlations (not significance-tested):",
        "",
        _markdown_table(
            behavior["iv_lead_lag"],
            [
                "horizon_seconds",
                "observations",
                "iv_change_vs_future_absolute_log_return_correlation",
                "exploratory_only",
            ],
        ),
        "",
        "## 7. Mode analysis",
        "",
        _markdown_table(
            behavior["rv_vs_iv_buckets"],
            [
                "bucket",
                "records",
                "average_volatility_score",
                "average_grid_width_pct",
                "volatility_state_distribution",
                "mode_distribution",
            ],
        ),
        "",
        f"Direction-score/center-shift correlation: `{behavior['direction']['direction_score_vs_center_shift_correlation']}`; "
        f"direction/buy-allocation: `{behavior['direction']['direction_score_vs_buy_allocation_correlation']}`; "
        f"direction/sell-allocation: `{behavior['direction']['direction_score_vs_sell_allocation_correlation']}`. "
        f"Inventory-ratio/center-shift correlation: `{behavior['inventory']['inventory_ratio_vs_center_shift_correlation']}`. "
        f"Directional states without a selected bias mode: `{behavior['direction']['directional_bias_not_selected_records']}`.",
        "",
        "## 8. Grid geometry",
        "",
        f"Geometry rows and summary are machine-readable in `evaluation_summary.json`; the mean full width is `{behavior['geometry']['summary'].get('mean')}`.",
        "",
        "## 9. Replay methodology",
        "",
        "Each resting entry must exist before later BBO evidence can fill it. Filled entries "
        "remain occupied while their native adjacent-grid LIMIT_MAKER TP is managed. PAUSE "
        "cancels unfilled entries without forcing liquidation. Significant refreshes cancel "
        "first and defer replacement to a later replay tick.",
        "",
        "## 10. Fill-model assumptions",
        "",
        "Results are always shown separately as `conservative_cross_through` and `touch_optimistic`.",
        "",
        "## 11–13. Static, RV-only, and full IV-adaptive strategies",
        "",
        "The static baseline uses fixed Stage 4 base width, five geometric levels, and 50/50 allocation. "
        "The RV-only ablation uses the existing State → Mode → GridPlan architecture with only the ATM-IV "
        "weight removed. The full variant uses the existing options-aware configuration.",
        "",
        "## 14. Performance comparison",
        "",
        _markdown_table(
            comparison_rows_data,
            [
                "strategy",
                "fill_model",
                "entries",
                "cycles",
                "net_realized",
                "total_pnl",
                "max_dd",
                "max_inventory",
                "cancel_create",
            ],
        ),
        "",
        "Mode-specific replay summaries are stored under `performance_by_entry_mode` in `evaluation_summary.json`; small samples should not be generalized.",
        "",
        "## 15. Inventory and risk comparison",
        "",
        "Synthetic inventory stress cases are parameter-response evidence, not performance. They use ratios from -1.00 to +1.00 without changing production history:",
        "",
        _markdown_table(
            behavior["inventory"]["synthetic_inventory_stress"],
            [
                "inventory_ratio",
                "mode",
                "enabled",
                "center_shift_bps",
                "buy_allocation_pct",
                "sell_allocation_pct",
            ],
        ),
        "",
        "## 16. Execution stability",
        "",
        f"Recorded plan queue estimate: `{json.dumps(behavior['plan_stability'], sort_keys=True)}`. "
        "This estimates KEEP/REFRESH/REMOVED/NEW using Stage 5 price, amount, mode, and 30-second lifetime thresholds.",
        "",
        "## 17. Options ablation result",
        "",
        "Compare `iv_adaptive_grid` minus `rv_only_adaptive_grid` in the machine-readable summaries for PnL, drawdown, inventory, markout, width, and fills. The result is a sample-specific ablation, not proof that IV causes performance.",
        "",
        "## 18. Conclusions",
        "",
        "This dataset supports an honest description of adaptive geometry, state/mode frequency, IV-conditioned width, quote stability, and replay sensitivity. It does not support a live profitability claim or a queue-aware execution claim.",
        "",
        "## 19. What is LIVE evidence vs SIMULATED evidence",
        "",
        "### LIVE DERIVE TESTNET EVIDENCE",
        "",
        "Stage 5 separately proved authenticated testnet LIMIT_MAKER submission with real Derive/Hummingbot IDs, post-only status, passive placement, KEEP, cancel/replace, DEFENSIVE, PAUSE/recovery, and cleanup. The bounded window produced no public BTC-PERP trade and no authorized counterparty was available, so no live fill, TP, realized PnL, or live inventory feedback is claimed.",
        "",
        "### SIMULATED / REPLAY EVIDENCE",
        "",
        "This Stage 6 package produces BBO-model fills, adjacent-grid TP exits, simulated PnL, inventory feedback, markout, risk, churn, and static/RV/IV comparisons. Every such number is simulated/replay evidence and must not be described as live Derive PnL.",
        "",
        "## 20. Reproduction commands",
        "",
        "```bash",
        "cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot",
        "PYTHONPATH=src:. .venv/bin/python -m evaluation.run \\",
        "  --market-snapshots /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_snapshots.jsonl \\",
        "  --states /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_states.jsonl \\",
        "  --modes /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_modes.jsonl \\",
        "  --plans /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_plans.jsonl \\",
        "  --output reports/stage6",
        "```",
        "",
        "Charts:",
        "",
        *[f"- `{path}`" for path in chart_paths],
        "",
        "The prior Stage 5 live report remains the source for live execution evidence; this report deliberately keeps that boundary separate.",
        "",
    ]
    return "\n".join(report_lines)


def write_stage6_outputs(
    *,
    dataset: EvaluationDataset,
    frames: Sequence[EvaluationFrame],
    replay_results: Sequence[ReplayResult],
    output_dir: str | Path,
    report_path: str | Path,
    replay_config: ReplayConfig,
    grid_config: GridParameterConfig,
) -> dict[str, Any]:
    """Write all requested outputs and return the analysis object."""

    output = Path(output_dir).expanduser().resolve()
    report = Path(report_path).expanduser().resolve()
    analysis = build_analysis(
        dataset,
        frames,
        replay_results,
        replay_config=replay_config,
        grid_config=grid_config,
    )
    chart_paths = _write_charts(output, frames, replay_results, analysis["behavior"])
    analysis["charts"] = chart_paths
    summaries = analysis["replay_summaries"]
    write_json(output / "evaluation_summary.json", analysis)
    write_json(output / "hackathon_metrics.json", _hackathon_metrics(analysis, dataset, frames))
    write_csv(output / "strategy_comparison.csv", summaries)
    mode_rows = []
    for stream_name in ("state_distributions", "mode_distribution"):
        values = analysis["behavior"].get(stream_name, {})
        if isinstance(values, dict):
            for field_name, rows in values.items():
                for row in rows:
                    mode_rows.append({"source": field_name, **row})
        else:
            for row in values:
                mode_rows.append({"source": stream_name, **row})
    write_csv(output / "mode_statistics.csv", mode_rows)
    iv_rows = [
        {"source": "behavior", **row}
        for row in analysis["behavior"]["mode_iv_groups"]["by_iv_regime"]
    ]
    for summary in summaries:
        for row in summary.get("performance_by_iv_regime", []):
            iv_rows.append(
                {"source": f"replay:{summary['strategy']}:{summary['fill_model']}", **row}
            )
    write_csv(output / "iv_regime_statistics.csv", iv_rows)
    event_rows = []
    for result in replay_results:
        for event in result.events:
            event_rows.append(event)
        for tick in result.ticks:
            event_rows.append({"event": "TICK", **tick})
    write_jsonl(output / "replay_events.jsonl", event_rows)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        _report_markdown(analysis, dataset, frames, summaries, chart_paths, output),
        encoding="utf-8",
    )
    return analysis


def _hackathon_metrics(
    analysis: Mapping[str, Any], dataset: EvaluationDataset, frames: Sequence[EvaluationFrame]
) -> dict[str, Any]:
    summaries = analysis["replay_summaries"]
    conservative = {
        summary["strategy"]: summary
        for summary in summaries
        if summary["fill_model"] == "conservative_cross_through"
    }
    behavior = analysis["behavior"]
    mode_percentages = {
        row["value"]: row["time_percentage"] for row in behavior["mode_distribution"]
    }
    iv_groups = {row["iv_regime"]: row for row in behavior["mode_iv_groups"]["by_iv_regime"]}
    snapshots_quality = dataset.snapshots.quality
    return {
        "dataset_duration_hours": (dataset.common_duration_seconds or 0.0) / 3600,
        "market_snapshot_count": snapshots_quality.records,
        "asof_plan_frame_count": len(frames),
        "atm_iv_coverage_pct": 100 - snapshots_quality.missing_rates.get("atm_iv", 100.0),
        "normal_mode_pct": mode_percentages.get("normal", 0.0),
        "defensive_mode_pct": mode_percentages.get("defensive", 0.0),
        "long_bias_mode_pct": mode_percentages.get("long_bias", 0.0),
        "short_bias_mode_pct": mode_percentages.get("short_bias", 0.0),
        "pause_mode_pct": mode_percentages.get("pause", 0.0),
        "average_grid_width_pct": behavior["geometry"]["summary"].get("mean"),
        "high_iv_average_grid_width_pct": iv_groups.get("high", {}).get("average_grid_width_pct"),
        "normal_iv_average_grid_width_pct": iv_groups.get("normal", {}).get(
            "average_grid_width_pct"
        ),
        "low_iv_average_grid_width_pct": iv_groups.get("low", {}).get("average_grid_width_pct"),
        "plan_keep_rate_pct": behavior["plan_stability"]["rates"].get("keep", 0.0),
        "plan_refresh_rate_pct": behavior["plan_stability"]["rates"].get("refresh", 0.0),
        "plan_new_rate_pct": behavior["plan_stability"]["rates"].get("new", 0.0),
        "fill_model": "conservative_cross_through",
        "static_simulated_pnl": conservative.get(StrategyVariant.STATIC.value, {}).get("total_pnl"),
        "rv_adaptive_simulated_pnl": conservative.get(StrategyVariant.RV_ONLY.value, {}).get(
            "total_pnl"
        ),
        "iv_adaptive_simulated_pnl": conservative.get(StrategyVariant.IV_ADAPTIVE.value, {}).get(
            "total_pnl"
        ),
        "static_max_inventory": conservative.get(StrategyVariant.STATIC.value, {}).get(
            "maximum_absolute_inventory_base"
        ),
        "rv_adaptive_max_inventory": conservative.get(StrategyVariant.RV_ONLY.value, {}).get(
            "maximum_absolute_inventory_base"
        ),
        "iv_adaptive_max_inventory": conservative.get(StrategyVariant.IV_ADAPTIVE.value, {}).get(
            "maximum_absolute_inventory_base"
        ),
        "simulated_metrics_are_live": False,
    }


__all__ = [
    "build_analysis",
    "write_stage6_outputs",
]
