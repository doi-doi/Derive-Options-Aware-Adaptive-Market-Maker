"""Write Phase 1 Stage 10 observer outputs from local Condor JSONL streams.

The command is intentionally read-only.  It reads the existing Stage 1--4,
relationship, portfolio, and optional execution-journal streams, then writes
observer evidence and status files.  It never writes a strategy configuration,
calls Hummingbot, or contacts an exchange.

Example::

    .venv/bin/python tools/run_stage10_observer.py \
        --data-dir /Users/wilfred/Documents/Hummingbot/condor/data
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (SRC_ROOT, PROJECT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from dashboard.config_schema import RuntimePaths  # noqa: E402
from dashboard.state_reader import JsonlTailReader, read_runtime  # noqa: E402
from evaluation.data_loader import parse_timestamp  # noqa: E402
from evaluation.self_tuning_observer import (  # noqa: E402
    ObserverConfig,
    PerformanceObserver,
)

DEFAULT_DATA_DIR = PROJECT_ROOT.parent / "condor" / "data"
LOCKED_PARAMETERS = [
    "leverage",
    "execution_enabled",
    "allow_mainnet_trading",
    "connector",
    "market_environment",
    "post_only",
    "supported_assets",
    "collateral_reserve_pct",
    "portfolio_hard_gross_notional",
    "portfolio_hard_beta_exposure",
    "asset_hard_position_limits",
    "hard_drawdown_thresholds",
    "emergency_stop_rules",
    "credentials",
    "stale_data_safety_rules",
]
DEFERRED_PHASES = {
    "phase_2": "diagnosis only after Phase 1 evidence is sufficient",
    "phase_3": "suggest-only proposals after diagnosis is implemented",
    "phase_4_plus": "shadow/champion evaluation and any bounded promotion workflow",
    "automatic_configuration_mutation": "not implemented; requires a later approved phase",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("CONDOR_DATA_DIR", DEFAULT_DATA_DIR)),
        help="Condor data directory containing the existing JSONL streams.",
    )
    parser.add_argument(
        "--asset",
        default="ALL",
        help="Trading pair to observe, or ALL for the combined stream.",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=30,
        help="Bounded observation window length.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "reports" / "stage10",
        help="Directory for machine-readable observer artifacts.",
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        default=PROJECT_ROOT / "data" / "self_tuning_status.json",
        help="Local status JSON path; this is not a strategy configuration file.",
    )
    return parser.parse_args()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_json_safe(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _write_csv(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(row)
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
        writer.writerow(
            {
                key: json.dumps(_json_safe(value), sort_keys=True)
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


def _status_for(observation: Any, source_records: dict[str, Any]) -> str:
    if observation.window.end_timestamp is None:
        return "NO_TIMESTAMPED_DATA"
    if observation.window.confidence == "UNKNOWN":
        return "PARTIAL"
    if any(value != "ok" for value in observation.source_status.values()):
        return "PARTIAL"
    return "SUPPORTED"


def _next_evaluation(end_timestamp: str | None, minutes: int) -> str | None:
    parsed = parse_timestamp(end_timestamp)
    if parsed is None:
        return None
    return (
        (datetime.fromtimestamp(parsed, tz=UTC) + timedelta(minutes=minutes))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def main() -> int:
    args = _args()
    if args.window_minutes <= 0:
        raise SystemExit("--window-minutes must be positive")

    paths = RuntimePaths(data_dir=args.data_dir.expanduser().resolve())
    reader = JsonlTailReader(max_records=100_000, max_bytes=32_000_000)
    runtime = read_runtime(paths.stream_paths(), reader)
    observer = PerformanceObserver(ObserverConfig(evaluation_window_minutes=args.window_minutes))
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
        asset=args.asset,
        event_source_status=(
            streams.get("execution_journal").status
            if streams.get("execution_journal")
            else "missing"
        ),
        state_source_status=(streams.get("state").status if streams.get("state") else "missing"),
        portfolio_source_status=(
            streams.get("portfolio_risk").status if streams.get("portfolio_risk") else "missing"
        ),
        relationship_source_status=(
            streams.get("relationship").status if streams.get("relationship") else "missing"
        ),
    )
    window = observation.window
    window_record = window.to_record()
    _write_csv(args.output_dir / "performance_windows.csv", window_record)

    sources = {name: _source_record(stream) for name, stream in streams.items()}
    supportability = {
        "stage": 10,
        "phase": "PHASE1_OBSERVER_ONLY",
        "mode": "SUGGEST_ONLY",
        "asset": args.asset,
        "observer_config": observer.config.to_record(),
        "sources": sources,
        "source_status": observation.source_status,
        "source_record_counts": observation.source_record_counts,
        "observation": observation.to_record(),
        "deferred": DEFERRED_PHASES,
    }
    _write_json(args.output_dir / "observer_supportability.json", supportability)

    known_metrics = sum(value == "AVAILABLE" for value in window.metric_status.values())
    unknown_metrics = sum(value == "UNKNOWN" for value in window.metric_status.values())
    status = {
        "stage": 10,
        "phase": "PHASE1_OBSERVER_ONLY",
        "mode": "SUGGEST_ONLY",
        "observer_status": _status_for(observation, sources),
        "current_champion_version": "BASELINE",
        "active_experiment": None,
        "last_diagnosis": "PHASE1_OBSERVER_ONLY",
        "last_proposal": None,
        "last_promotion": None,
        "last_rollback": None,
        "next_evaluation": _next_evaluation(window.end_timestamp, args.window_minutes),
        "locked_parameters": LOCKED_PARAMETERS,
        "known_metric_count": known_metrics,
        "unknown_metric_count": unknown_metrics,
        "last_observation": window_record,
        "source_status": observation.source_status,
        "source_record_counts": observation.source_record_counts,
        "deferred": DEFERRED_PHASES,
    }
    _write_json(args.status_path.expanduser().resolve(), status)

    print("STAGE 10 — BOUNDED SELF-TUNING")
    print("phase=PHASE1_OBSERVER_ONLY mode=SUGGEST_ONLY")
    print(f"asset={args.asset} observer_status={status['observer_status']}")
    print(
        f"window={window.start_timestamp or 'UNKNOWN'}..{window.end_timestamp or 'UNKNOWN'} "
        f"known_metrics={known_metrics} unknown_metrics={unknown_metrics}"
    )
    print(
        "execution_journal="
        f"{observation.source_status.get('execution_journal', 'UNKNOWN')} "
        "automatic_configuration_mutation=DISABLED"
    )
    print(f"supportability={args.output_dir / 'observer_supportability.json'}")
    print(f"status={args.status_path.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
