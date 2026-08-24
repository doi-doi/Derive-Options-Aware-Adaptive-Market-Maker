"""Writers for Stage 6.5 machine-readable evidence and validation report."""

# Human-readable report literals intentionally exceed the code line limit.
# ruff: noqa: E501

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .audit import Stage65Audit, _json_safe
from .baselines import StrategyVariant


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_json_safe(row), sort_keys=True, allow_nan=False) + "\n")


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_safe(value), sort_keys=True, allow_nan=False)
    return _json_safe(value)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if str(key) not in columns:
                columns.append(str(key))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in columns})


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _svg_text(x: float, y: float, text: str, size: int = 12, anchor: str = "start") -> str:
    escaped = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="#273142">{escaped}</text>'


def _chart_shell(
    title: str, width: int, height: int, body: str, legend: Sequence[tuple[str, str]]
) -> str:
    legend_body = "".join(
        f'<rect x="{width - 210 + index * 100}" y="16" width="10" height="10" fill="{color}" />'
        f"{_svg_text(width - 195 + index * 100, 26, label, 10)}"
        for index, (label, color) in enumerate(legend)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#ffffff" />'
        f"{_svg_text(20, 26, title, 16)}"
        f"{legend_body}{body}</svg>"
    )


def _line_chart(
    path: Path,
    title: str,
    series: Sequence[tuple[str, Sequence[Any], str]],
    *,
    zero_line: bool = False,
) -> None:
    width, height = 1000, 430
    left, right, top, bottom = 65, 25, 48, 45
    plot_width, plot_height = width - left - right, height - top - bottom
    values = [
        _finite(value, math.nan)
        for _, entries, _ in series
        for value in entries
        if math.isfinite(_finite(value, math.nan))
    ]
    lower = min(values) if values else -1.0
    upper = max(values) if values else 1.0
    if lower == upper:
        lower -= 1.0
        upper += 1.0
    body = (
        f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" '
        f'y2="{top + plot_height}" stroke="#777" />'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#777" />'
        f"{_svg_text(left - 8, top + 5, f'{upper:.5g}', 10, 'end')}"
        f"{_svg_text(left - 8, top + plot_height, f'{lower:.5g}', 10, 'end')}"
    )
    if zero_line and lower < 0 < upper:
        y = top + (upper / (upper - lower)) * plot_height
        body += f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#999" stroke-dasharray="4 4" />'
    maximum_length = max((len(entries) for _, entries, _ in series), default=1)
    for _label, entries, color in series:
        points: list[str] = []
        for index, value in enumerate(entries):
            numeric = _finite(value, math.nan)
            if not math.isfinite(numeric):
                continue
            x = left + index / max(1, maximum_length - 1) * plot_width
            y = top + (upper - numeric) / (upper - lower) * plot_height
            points.append(f"{x:.1f},{y:.1f}")
        if points:
            body += f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="1.7" />'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _chart_shell(title, width, height, body, [(name, color) for name, _, color in series]),
        encoding="utf-8",
    )


def _bar_chart(
    path: Path, title: str, values: Mapping[str, float], colors: Mapping[str, str] | None = None
) -> None:
    width, height = 1000, 430
    left, right, top, bottom = 70, 25, 48, 70
    plot_width, plot_height = width - left - right, height - top - bottom
    numeric = {str(key): _finite(value) for key, value in values.items()}
    maximum = max([abs(value) for value in numeric.values()] or [1.0])
    if maximum == 0:
        maximum = 1.0
    body = f'<line x1="{left}" y1="{top + plot_height}" x2="{width - right}" y2="{top + plot_height}" stroke="#777" />'
    items = list(numeric.items())
    bar_slot = plot_width / max(1, len(items))
    for index, (label, value) in enumerate(items):
        bar_width = bar_slot * 0.68
        x = left + index * bar_slot + bar_slot * 0.16
        bar_height = value / maximum * plot_height
        y = top + plot_height - bar_height if bar_height >= 0 else top + plot_height
        color = (colors or {}).get(label, "#2b6cb0")
        body += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{abs(bar_height):.1f}" fill="{color}" />'
        body += _svg_text(x + bar_width / 2, height - 40, label, 10, "middle")
        body += _svg_text(
            x + bar_width / 2,
            y - 5 if bar_height >= 0 else y + abs(bar_height) + 15,
            f"{value:.5g}",
            10,
            "middle",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _chart_shell(title, width, height, body, [("value", "#2b6cb0")]), encoding="utf-8"
    )


def _base_summaries(audit: Stage65Audit) -> list[dict[str, Any]]:
    return list(audit.analysis.get("base_replay_summaries", []))


def _write_charts(audit: Stage65Audit, chart_dir: Path) -> None:
    chart_dir.mkdir(parents=True, exist_ok=True)
    rows = audit.analysis.get("volatility_decomposition", {}).get("rows", [])
    _line_chart(
        chart_dir / "01_rv_vs_iv_contribution.svg",
        "Weighted RV and IV contribution",
        [
            ("RV contribution", [row.get("rv_contribution") for row in rows], "#2b6cb0"),
            ("IV contribution", [row.get("iv_contribution") for row in rows], "#c05621"),
        ],
    )
    _line_chart(
        chart_dir / "02_full_vs_rv_volatility_score.svg",
        "Full volatility score versus RV-only counterfactual",
        [
            ("Full IV", [row.get("combined_volatility_score") for row in rows], "#2b6cb0"),
            ("RV-only", [row.get("rv_ratio") for row in rows], "#805ad5"),
        ],
    )
    counterfactual_rows = audit.analysis.get("counterfactual_iv_impact", {}).get("rows", [])
    _line_chart(
        chart_dir / "03_iv_minus_rv_grid_width.svg",
        "IV-aware minus RV-only grid width",
        [("width delta", [row.get("delta_grid_width") for row in counterfactual_rows], "#c05621")],
        zero_line=True,
    )
    def cumulative_pnl(strategy: StrategyVariant) -> list[Any]:
        result = audit.base_results.get((strategy.value, "conservative_cross_through"))
        return [tick.get("net_pnl") for tick in result.ticks] if result else []

    _line_chart(
        chart_dir / "04_cumulative_pnl_by_strategy.svg",
        "Cumulative replay PnL by strategy",
        [
            ("static", cumulative_pnl(StrategyVariant.STATIC), "#718096"),
            ("RV-only", cumulative_pnl(StrategyVariant.RV_ONLY), "#805ad5"),
            ("IV-aware", cumulative_pnl(StrategyVariant.IV_ADAPTIVE), "#2b6cb0"),
        ],
        zero_line=True,
    )
    pnl_rows = audit.analysis.get("pnl_decomposition", [])
    conservative_pnl = [
        row for row in pnl_rows if row.get("fill_model") == "conservative_cross_through"
    ]
    _line_chart(
        chart_dir / "05_realized_vs_unrealized_pnl.svg",
        "Realized after fees versus ending unrealized PnL",
        [
            (
                "realized",
                [row.get("realized_pnl_after_fees") for row in conservative_pnl],
                "#2f855a",
            ),
            (
                "unrealized",
                [row.get("open_position_unrealized_pnl") for row in conservative_pnl],
                "#c05621",
            ),
        ],
        zero_line=True,
    )
    _bar_chart(
        chart_dir / "06_ending_inventory_by_strategy.svg",
        "Ending inventory by strategy",
        {
            str(row.get("strategy")): _finite(row.get("ending_inventory_base"))
            for row in conservative_pnl
        },
    )
    subperiod = [
        row
        for row in audit.analysis.get("subperiod_results", [])
        if _finite(row.get("window_seconds"), 3600.0) == 3600.0
    ]
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in subperiod:
        grouped[str(row.get("strategy"))].append(_finite(row.get("total_pnl")))
    _line_chart(
        chart_dir / "07_subperiod_pnl_comparison.svg",
        "One-hour subperiod total PnL",
        [
            ("static", grouped.get(StrategyVariant.STATIC.value, []), "#718096"),
            ("RV-only", grouped.get(StrategyVariant.RV_ONLY.value, []), "#805ad5"),
            ("IV-aware", grouped.get(StrategyVariant.IV_ADAPTIVE.value, []), "#2b6cb0"),
        ],
        zero_line=True,
    )
    rolling = audit.analysis.get("rolling_comparison", [])
    _line_chart(
        chart_dir / "08_rolling_iv_minus_rv_pnl.svg",
        "IV-aware minus RV-only replay PnL",
        [("IV - RV", [row.get("iv_minus_rv_pnl") for row in rolling], "#c05621")],
        zero_line=True,
    )


def _markdown_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "_No rows._"
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(str(row.get(column, "")).replace("|", "\\|") for column in columns)
            + " |"
        )
    return "\n".join(lines)


def _claims(audit: Stage65Audit) -> str:
    return """# Stage 6.5 validated claims

## PROVEN LIVE

- Derive testnet `LIMIT_MAKER` submission, post-only behavior, and authenticated order IDs are documented in the separate Stage 5 evidence.
- Stage 5 testnet cancel/replace, KEEP, PAUSE, recovery, and one-level safety gates are documented in the separate Stage 5 evidence.

## PROVEN FROM RECORDED BEHAVIOR

- Stage 1--4 JSONL streams contain measurable mode frequencies, transitions, grid widths, allocations, and plan lifecycle actions.
- The Stage 6.5 audit records duplicate timestamps, exact duplicate rows, repeated plan versions, and conflicting timestamp records without deleting the source file.
- IV materially changes the Stage 2 combined volatility score and/or Stage 4 geometry only to the extent shown in the machine-readable counterfactual output; it is not assumed to improve PnL.

## SIMULATED / REPLAY

- Static, RV-only, and IV-aware PnL, drawdown, markout, inventory, TP cycles, fee sensitivity, and subperiod results are offline BBO-model replay outputs.
- Simulated inventory is fed back before the next replay State -> Mode -> GridPlan decision.
- Adjacent-grid TP lifecycle and position-accounting invariants are tested offline.

## NOT PROVEN

- Live profitability, live maker fill quality, queue position, partial-fill behavior, or live TP fills.
- Any mainnet behavior or production capital safety beyond the documented testnet gates.
- Statistical significance or out-of-sample robustness from the short common history.
"""


def _report(audit: Stage65Audit, output_dir: Path) -> str:
    analysis = audit.analysis
    dedup = analysis.get("deduplication", {})
    coverage = analysis.get("iv_coverage", {})
    decomposition = analysis.get("volatility_decomposition", {}).get("summary", {})
    counterfactual = analysis.get("counterfactual_iv_impact", {}).get("summary", {})
    validation_artifacts = analysis.get("dataset_contamination", {}).get(
        "external_validation_artifacts", []
    )
    base = [
        row
        for row in analysis.get("base_replay_summaries", [])
        if row.get("fill_model") == "conservative_cross_through"
    ]
    staleness = analysis.get("iv_staleness_sensitivity", [])
    subperiod = analysis.get("subperiod_results", [])
    scale_rows = analysis.get("scale_sensitivity", [])
    fee_rows = analysis.get("fee_sensitivity", [])
    return f"""# Stage 6.5 Validation: Stage 6 Audit and Robustness

## 1. Audit verdict

**{analysis.get("audit_verdict", {}).get("status", "unknown")}**. The audit keeps the Stage 6 strategy and Stage 5 live controller unchanged. It finds a timestamp-conflicted plan stream that requires an explicit canonical view, confirms no future as-of input selection, and retains the limits of BBO-only replay. No parameter optimization, mainnet action, or live execution change was performed.

Common window: `{analysis.get("common_window", {}).get("start")}` to `{analysis.get("common_window", {}).get("end")}`; canonical complete frames: `{analysis.get("canonical_frame_count")}`.

## 2. Data deduplication

{_markdown_table([dedup], ["raw_record_count", "canonical_record_count", "duplicate_timestamp_count", "exact_duplicate_record_count", "duplicate_plan_version_count", "conflicting_timestamp_count", "controlled_record_count"])}

The duplicate timestamp count is **extra rows beyond the first row in each timestamp group**, not a count of unique timestamps. Exact duplicates are structurally identical JSON objects after sorted-key canonicalization. Repeated `plan_version` values are a separate diagnostic: Stage 4 intentionally keeps a plan version across insignificant updates, so repeated versions are not independently treated as data corruption. Conflicting timestamp groups are retained in `deduplication_report.json`; the canonical rule selects the last production source row after excluding explicit validation-only markers.

## 3. Dataset contamination

{analysis.get("dataset_contamination", {}).get("verdict")}

The canonical Condor plan rows contained `{dedup.get("controlled_record_count", 0)}` explicit controlled markers. Isolated validation artifacts, where supplied, are listed separately and are not merged into the production stream. Original JSONL files were not deleted or rewritten.

External validation artifacts:

{_markdown_table(validation_artifacts, ["path", "exists", "records", "controlled_records", "validation_stages"])}

## 4. IV freshness

Snapshot ATM-IV coverage was `{coverage.get("snapshot_coverage", {}).get("coverage_pct"):.3f}%`; state ATM-IV coverage was `{coverage.get("state_coverage", {}).get("coverage_pct"):.3f}%`; common-window state coverage was `{coverage.get("common_window_coverage", {}).get("coverage_pct"):.3f}%`. As-of carried IV age summary: `{coverage.get("asof_carried_iv_coverage", {}).get("age_seconds")}`. The state stream can carry a prior state observation into a later plan frame, so raw snapshot coverage and frame-level state coverage are different; the report does not call carried observations fresh without an age rule.

Freshness sensitivity:

{_markdown_table(staleness, ["threshold_seconds", "fresh_iv_frames", "stale_iv_frames", "missing_iv_frames", "rv_fallback_frames", "entry_fills", "total_pnl", "maximum_drawdown"])}

## 5. Volatility decomposition

The Stage 2 score is audited as the weight-renormalized combination of `realized_volatility_ratio` and `iv_ratio`. The Stage 4 width formula is checked against each recorded plan's own volatility and mode multipliers, then separately compared with the as-of state/mode inputs selected at that timestamp. Maximum score formula error: `{decomposition.get("max_combined_score_error")}`. Maximum recorded-plan width formula error: `{decomposition.get("max_grid_width_error")}`. Score formula pass: `{decomposition.get("formula_score_pass")}`; recorded-plan width formula pass: `{decomposition.get("formula_width_pass")}`. The maximum as-of input width mismatch is `{decomposition.get("max_asof_grid_width_error")}` across `{decomposition.get("asof_input_width_mismatch_frames")}` frames; these mismatches are retained as timestamp-join/conflict evidence rather than hidden.

Mean absolute RV contribution: `{decomposition.get("mean_absolute_rv_contribution")}`. Mean absolute IV contribution: `{decomposition.get("mean_absolute_iv_contribution")}`. Contribution-series variance shares: RV `{decomposition.get("rv_variance_share")}`, IV `{decomposition.get("iv_variance_share")}`.

## 6. Options counterfactual impact

The counterfactual holds the recorded frame inputs constant and compares a stateless full-IV candidate with the same candidate after removing IV. It is not a retuned strategy and does not re-fit thresholds.

{_markdown_table([{"metric": key, **value} for key, value in counterfactual.items() if isinstance(value, dict) and key in {"score", "grid_width", "capital", "level_count"}], ["metric", "count", "mean", "median", "p90", "maximum"])}

Frames with candidate mode changes: `{counterfactual.get("frames_iv_changes_candidate_mode")}`. Frames with grid-width changes greater than 5%: `{counterfactual.get("frames_iv_changes_grid_width_gt_5_pct")}`. Frames with level-count changes: `{counterfactual.get("frames_iv_changes_level_count")}`.

## 7. IV regime label audit

`relative_iv_bucket` uses low `< 0.90`, normal `0.90–1.10`, and high `> 1.10`. `rv_iv_joint_bucket` uses the separate boundary `1.0` for each RV/IV component. The two labels are intentionally distinct and are not interchangeable.

Boundary consistency pass: `{analysis.get("iv_regime_audit", {}).get("pass")}`. Observed frame buckets: `{analysis.get("iv_regime_audit", {}).get("frame_bucket_counts")}`.

## 8. Look-ahead audit

The as-of audit found `{len(analysis.get("lookahead_audit", {}).get("violations", []))}` future-input violations. The replay fill path requires a future snapshot timestamp strictly greater than order creation; same-timestamp evidence is rejected. Result: **{analysis.get("lookahead_audit", {}).get("pass")}**.

## 9. Fill-model audit

{_markdown_table(analysis.get("fill_model_audit", []), ["strategy", "conservative_fill_count", "touch_fill_count", "fills_satisfying_both_models", "touch_only_fills", "conservative_only_fills", "touch_is_distinct"])}

The models are distinct: conservative BUY/SELL use strict cross-through inequalities, while touch uses inclusive inequalities. A touch-only fill is therefore valid sensitivity evidence, not evidence that the conservative condition was accidentally reused.

## 10. Baseline fairness

{analysis.get("baseline_fairness", {}).get("fairness_verdict")}

The static baseline recenters each replay tick around the current reference, but it goes through the same Stage 5-equivalent KEEP/refresh/cancel, order lifetime, scale, minimum, exposure, fee, fill, and TP path. Its intended difference is fixed Stage 4 base width, five levels per side, and 50/50 allocation without adaptive state logic.

## 11. PnL decomposition

{_markdown_table(base, ["strategy", "entry_fills", "completed_grid_cycles", "gross_realized_pnl", "fees", "net_realized_pnl", "unrealized_pnl_end", "total_pnl", "maximum_drawdown"])}

Formula: `total_pnl = realized_grid_capture_gross - fees + open_position_unrealized_pnl`. Positive realized capture alongside negative total PnL means the remaining open inventory was marked below its entry cost by the replay endpoint; it is not a contradiction. The default maker fee is 0 bps because no reliable local Derive fee schedule was supplied; fee sensitivity is hypothetical.

## 12. Inventory accounting

{_markdown_table([row for row in analysis.get("position_accounting", []) if row.get("fill_model") == "conservative_cross_through"], ["strategy", "ending_inventory_base", "average_entry_cost", "ending_mark_price", "weighted_ledger_unrealized_pnl", "recorded_unrealized_pnl", "unrealized_model_difference", "total_pnl_model_difference", "position_accounting_total_pass", "liquidation_at_end_hypothetical_total_pnl"])}

Liquidation-at-end values are a separate weighted-net hypothetical mark at the final touch and are not the default result. Long PnL has the correct positive sign when mark exceeds cost; short PnL has the reverse sign. The replay keeps filled positions as per-lot objects, while the independent audit ledger uses weighted-net crossing; their unrealized components can differ when opposite lots coexist. The total pre-fee PnL reconciliation is the invariant, and it passes in the machine output. Weighted-average crossing, additions, reductions, and zero-crossing are covered by focused `PositionLedger` tests; partial fills remain outside the Stage 6 replay model. Same-timestamp entry and adjacent-TP fills are aggregated when checking that inventory is visible before the next State -> Mode -> GridPlan call.

## 13. Replay lifecycle parity

The replay timeline audit pass is `{analysis.get("replay_timeline_audit", {}).get("pass")}` across `{len(analysis.get("replay_timeline_audit", {}).get("ordering_checks", []))}` strategy/fill-model runs. Sampled timelines are in `replay_timelines.json`. Stage 5 adjacent-grid TP parity pass is `{analysis.get("tp_parity_audit", {}).get("pass")}` with `{analysis.get("tp_parity_audit", {}).get("sample_count")}` samples. Inventory feedback pass is `{analysis.get("inventory_feedback_audit", {}).get("pass")}`. The TP comparison uses the same previous-level/center rule with the configured one-step multiplier.

## 14. Subperiod robustness

The common window is split into 30-minute and one-hour chronological windows. Results are descriptive only:

{_markdown_table(subperiod[:18], ["window_label", "window_index", "strategy", "entry_fills", "completed_cycles", "total_pnl", "unrealized_pnl_end", "maximum_drawdown", "maximum_absolute_inventory_base"])}

The full subperiod table is in `subperiod_results.csv`; it should be read before making any aggregate claim.

## 15. Scale and capital sensitivity

The scale comparison keeps the Stage 4 theoretical allocations unchanged and reports native scale `1.0` against the testnet-minimum-normalized scale `9.30`. Deployed notional is measured from simulated open position plus pending entry notional; capital percentages use the replay's fixed initial-capital assumption.

{_markdown_table(scale_rows, ["order_scale", "strategy", "entry_creates", "minimum_order_blocks", "entry_fills", "maximum_deployed_notional", "maximum_deployed_capital_pct", "total_pnl", "maximum_drawdown"])}

## 16. Fee sensitivity

Fee values `{sorted({row.get("maker_fee_bps") for row in fee_rows})}` are hypothetical maker rebates/fees, not a Derive schedule. Full results are in `fee_sensitivity.csv`.

## 17. Final static vs RV vs IV comparison

{_markdown_table(base, ["strategy", "entry_fills", "completed_grid_cycles", "net_realized_pnl", "unrealized_pnl_end", "total_pnl", "maximum_drawdown", "maximum_absolute_inventory_base", "cancel_create_ratio"])}

## 18. What IV actually contributed

IV is measured as a geometry/state input, not a required winner. The central RV-only versus IV-aware table is in `iv_ablation.csv`; the per-frame score, width, capital, level, and candidate-mode counterfactual is in `counterfactual_impact.csv`. The honest conclusion is sample-specific: IV can materially change geometry while static may still have the better total PnL and drawdown on this short sample.

## 19. Validated hackathon claims

See `validated_claims.md`. The claims are split into proven live, proven recorded behavior, simulated replay, and not proven. No simulated PnL number is presented as live Derive performance.

## 20. Remaining limitations

{_markdown_table([{"limitation": item} for item in analysis.get("limitations", [])], ["limitation"])}

## 21. Reproduction

```bash
cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
PYTHONPATH=src:. .venv/bin/python -m evaluation.run_stage6_5 \\
  --market-snapshots /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_snapshots.jsonl \\
  --states /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_states.jsonl \\
  --modes /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_modes.jsonl \\
  --plans /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_plans.jsonl \\
  --validation-plans /Users/wilfred/Documents/Hummingbot/condor/data/stage5e-feedback-20260824/derive_grid_plans.jsonl \\
  --output reports/stage6_5
```
"""


def write_stage65_outputs(
    audit: Stage65Audit, output_dir: str | Path, report_path: str | Path
) -> dict[str, Any]:
    """Write all Stage 6.5 outputs and return the JSON-safe analysis."""

    output = Path(output_dir).expanduser().resolve()
    report = Path(report_path).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    analysis = audit.analysis
    _write_json(output / "audit_summary.json", analysis)
    _write_json(output / "deduplication_report.json", analysis.get("deduplication", {}))
    _write_json(output / "iv_coverage_report.json", analysis.get("iv_coverage", {}))
    _write_csv(output / "iv_ablation.csv", analysis.get("iv_ablation", []))
    _write_csv(output / "pnl_decomposition.csv", analysis.get("pnl_decomposition", []))
    _write_csv(output / "subperiod_results.csv", analysis.get("subperiod_results", []))
    _write_csv(output / "fill_model_comparison.csv", analysis.get("fill_model_audit", []))
    _write_csv(output / "staleness_sensitivity.csv", analysis.get("iv_staleness_sensitivity", []))
    _write_csv(
        output / "counterfactual_impact.csv",
        analysis.get("counterfactual_iv_impact", {}).get("rows", []),
    )
    _write_csv(output / "scale_sensitivity.csv", analysis.get("scale_sensitivity", []))
    _write_csv(output / "fee_sensitivity.csv", analysis.get("fee_sensitivity", []))
    _write_csv(output / "rolling_comparison.csv", analysis.get("rolling_comparison", []))
    _write_csv(
        output / "volatility_decomposition.csv",
        analysis.get("volatility_decomposition", {}).get("rows", []),
    )
    _write_json(output / "replay_timelines.json", analysis.get("replay_timeline_audit", {}))
    _write_jsonl(
        output / "canonical_production_plans.jsonl",
        audit.canonical_plans,
    )
    claims = _claims(audit)
    (output / "validated_claims.md").write_text(claims, encoding="utf-8")
    _write_charts(audit, output / "charts")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(_report(audit, output), encoding="utf-8")
    return analysis


__all__ = ["write_stage65_outputs"]
