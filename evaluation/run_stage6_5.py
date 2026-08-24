"""Run the Stage 6.5 audit without contacting an exchange."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .audit import run_stage65_audit
from .audit_reports import write_stage65_outputs
from .data_loader import load_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-snapshots", required=True, type=Path)
    parser.add_argument("--states", required=True, type=Path)
    parser.add_argument("--modes", required=True, type=Path)
    parser.add_argument("--plans", required=True, type=Path)
    parser.add_argument(
        "--validation-plans",
        action="append",
        default=[],
        type=Path,
        help="Optional isolated Stage 5 validation plan stream; repeat as needed.",
    )
    parser.add_argument("--trades", type=Path, default=None)
    parser.add_argument("--trading-pair", default="BTC-USDC")
    parser.add_argument("--output", type=Path, default=Path("reports/stage6_5"))
    parser.add_argument("--report", type=Path, default=Path("reports/stage6_5_validation.md"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dataset = load_dataset(
        snapshots_path=args.market_snapshots,
        states_path=args.states,
        modes_path=args.modes,
        plans_path=args.plans,
        trades_path=args.trades,
        trading_pair=args.trading_pair,
    )
    audit = run_stage65_audit(dataset, validation_plan_paths=args.validation_plans)
    analysis = write_stage65_outputs(audit, args.output, args.report)
    print(
        json.dumps(
            {
                "output_dir": str(args.output.expanduser().resolve()),
                "report": str(args.report.expanduser().resolve()),
                "status": analysis.get("audit_verdict", {}).get("status"),
                "raw_plan_records": analysis.get("deduplication", {}).get("raw_record_count"),
                "canonical_plan_records": analysis.get("deduplication", {}).get(
                    "canonical_record_count"
                ),
                "canonical_frames": analysis.get("canonical_frame_count"),
                "warnings": analysis.get("dataset", {}).get("warnings", []),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
