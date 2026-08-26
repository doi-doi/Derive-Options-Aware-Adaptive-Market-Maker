"""Write Stage 11 Phase A measurement artifacts from local JSONL streams.

This command is measurement-only.  It reads the existing Condor streams and
writes evidence/quality artifacts; it never changes a strategy configuration,
starts Hummingbot, places orders, or contacts an exchange.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (SRC_ROOT, PROJECT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from dashboard.config_schema import RuntimePaths  # noqa: E402
from dashboard.state_reader import JsonlTailReader, read_runtime  # noqa: E402
from evaluation.self_tuning_observer import (  # noqa: E402
    ObserverConfig,
    PerformanceObserver,
    VolumeEfficiencyMetrics,
)

DEFAULT_DATA_DIR = PROJECT_ROOT.parent / "condor" / "data"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "stage11"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("CONDOR_DATA_DIR", DEFAULT_DATA_DIR)),
        help="Condor data directory containing the existing JSONL streams.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for Phase A measurement artifacts.",
    )
    parser.add_argument(
        "--asset",
        default="ALL",
        help="Trading pair to measure, or ALL for the portfolio plus observed assets.",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=30,
        help="Bounded observation window length.",
    )
    return parser.parse_args()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=str) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["trading_pair"]
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, sort_keys=True)
                    if isinstance(value, (dict, list, tuple))
                    else value
                    for key, value in row.items()
                }
            )
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _source_record(stream: Any) -> dict[str, Any]:
    return {
        "path": str(stream.path),
        "status": stream.status,
        "records_read": len(stream.records),
        "malformed_lines": stream.malformed_lines,
        "partial_trailing_line": stream.partial_trailing_line,
        "latest_timestamp": stream.latest.get("timestamp") if stream.latest else None,
        "size_bytes": stream.size_bytes,
        "error": stream.error,
    }


def _observe(
    observer: PerformanceObserver,
    runtime: Any,
    *,
    asset: str,
) -> VolumeEfficiencyMetrics:
    streams = runtime.streams
    observation = observer.observe(
        streams.get("execution_journal").records if streams.get("execution_journal") else (),
        state_records=streams.get("state").records if streams.get("state") else (),
        portfolio_records=(
            streams.get("portfolio_risk").records if streams.get("portfolio_risk") else ()
        ),
        relationship_records=(
            streams.get("relationship").records if streams.get("relationship") else ()
        ),
        plan_records=streams.get("plan").records if streams.get("plan") else (),
        asset=asset,
        event_source_status=(
            streams.get("execution_journal").status
            if streams.get("execution_journal")
            else "missing"
        ),
        state_source_status=streams.get("state").status if streams.get("state") else "missing",
        portfolio_source_status=(
            streams.get("portfolio_risk").status
            if streams.get("portfolio_risk")
            else "missing"
        ),
        relationship_source_status=(
            streams.get("relationship").status
            if streams.get("relationship")
            else "missing"
        ),
    )
    if observation.volume_efficiency is None:
        raise RuntimeError("Phase A observer did not return VolumeEfficiencyMetrics")
    return observation.volume_efficiency


def _gate(metrics: VolumeEfficiencyMetrics, field_name: str) -> str:
    return metrics.metric_status.get(field_name, "UNKNOWN")


def main() -> int:
    args = _args()
    if args.window_minutes <= 0:
        raise SystemExit("--window-minutes must be positive")

    data_dir = args.data_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    paths = RuntimePaths(data_dir=data_dir)
    reader = JsonlTailReader(max_records=100_000, max_bytes=32_000_000)
    runtime = read_runtime(paths.stream_paths(), reader)
    observer = PerformanceObserver(ObserverConfig(evaluation_window_minutes=args.window_minutes))

    observed_assets = sorted(runtime.latest_by_asset)
    if args.asset.upper() not in {"ALL", "*"}:
        assets = [args.asset]
    else:
        assets = observed_assets
    portfolio = _observe(observer, runtime, asset="ALL")
    asset_metrics = [_observe(observer, runtime, asset=asset) for asset in assets]

    _write_csv(
        output_dir / "asset_volume_efficiency.csv",
        [item.to_record() for item in asset_metrics],
    )
    _write_csv(output_dir / "portfolio_volume_efficiency.csv", [portfolio.to_record()])
    _write_csv(
        output_dir / "cycle_efficiency.csv",
        [
            {
                "trading_pair": item.trading_pair,
                "evidence_source": item.evidence_source,
                "completed_cycles": item.completed_cycles,
                "cycles_per_hour": item.cycles_per_hour,
                "median_cycle_duration_seconds": item.median_cycle_duration_seconds,
                "mean_cycle_duration_seconds": item.mean_cycle_duration_seconds,
                "capital_time_efficiency": item.capital_time_efficiency,
                "inventory_exposure_time_notional_seconds": (
                    item.inventory_exposure_time_notional_seconds
                ),
                "metric_status": item.metric_status,
                "reasons": item.reasons,
            }
            for item in [portfolio, *asset_metrics]
        ],
    )

    sources = {name: _source_record(stream) for name, stream in runtime.streams.items()}
    source_status = {
        name: stream.status for name, stream in runtime.streams.items()
    }
    validation = {
        "executed_notional": _gate(portfolio, "executed_total_notional"),
        "average_deployed_gross_exposure": _gate(portfolio, "average_gross_exposure"),
        "average_inventory": _gate(portfolio, "average_absolute_inventory"),
        "completed_cycles": _gate(portfolio, "completed_cycles"),
        "cycle_duration": _gate(portfolio, "mean_cycle_duration_seconds"),
        "fill_create": _gate(portfolio, "fill_create_ratio"),
        "cancel_create": _gate(portfolio, "cancel_create_ratio"),
        "quote_lifetime": _gate(portfolio, "median_quote_lifetime"),
        "markout_5s": _gate(portfolio, "markout_5s"),
        "markout_30s": _gate(portfolio, "markout_30s"),
        "markout_60s": _gate(portfolio, "markout_60s"),
        "realized_capture": _gate(portfolio, "realized_grid_capture"),
        "drawdown": _gate(portfolio, "drawdown"),
    }
    measurement = {
        "stage": 11,
        "phase": "PHASE_A_MEASUREMENT_ONLY",
        "mode": "MEASUREMENT_ONLY",
        "data_dir": str(data_dir),
        "window_minutes": args.window_minutes,
        "portfolio": portfolio.to_record(),
        "assets": [item.to_record() for item in asset_metrics],
        "validation": validation,
        "sources": sources,
        "source_status": source_status,
        "deferred": [
            "diagnosis",
            "shadow optimization",
            "walk-forward tuning",
            "quote-distance/TP/size/level/allocation changes",
            "dashboard optimization controls",
        ],
    }
    _write_json(output_dir / "phase_a_measurement.json", measurement)

    print("STAGE 11 — RISK-ADJUSTED VOLUME EFFICIENCY")
    print("PHASE A — MEASUREMENT ONLY")
    for name, status in validation.items():
        print(f"{name}: {status}")
    print(f"portfolio_evidence={portfolio.evidence_source}")
    print(f"execution_journal={source_status.get('execution_journal', 'UNKNOWN')}")
    print(f"asset_rows={len(asset_metrics)}")
    print(f"output_dir={output_dir}")
    print(
        "No optimizer, quote change, TP change, level change, allocation, or "
        "execution mutation ran."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
