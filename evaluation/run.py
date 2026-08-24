"""Run the complete deterministic Stage 6 evaluation."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from derive_options_mm.grid_engine import GridParameterConfig

from .data_loader import load_dataset
from .fill_models import FillModelName
from .replay import ReplayConfig, run_replay
from .reports import write_stage6_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-snapshots", required=True, type=Path)
    parser.add_argument("--states", required=True, type=Path)
    parser.add_argument("--modes", required=True, type=Path)
    parser.add_argument("--plans", required=True, type=Path)
    parser.add_argument("--trades", type=Path, default=None)
    parser.add_argument("--trading-pair", default="BTC-USDC")
    parser.add_argument("--output", type=Path, default=Path("reports/stage6"))
    parser.add_argument("--report", type=Path, default=Path("reports/stage6_evaluation.md"))
    parser.add_argument("--order-scale", type=Decimal, default=Decimal("9.30"))
    parser.add_argument("--maker-fee-bps", type=Decimal, default=Decimal("0"))
    parser.add_argument("--maker-adverse-fill-buffer-bps", type=Decimal, default=Decimal("0"))
    parser.add_argument(
        "--fill-model",
        choices=[model.value for model in FillModelName if model is not FillModelName.TRADE_BASED],
        action="append",
        dest="fill_models",
        help="Repeat to select fill models; default runs conservative and touch.",
    )
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
    frames = dataset.plan_frames()
    start = dataset.common_start_seconds
    end = dataset.common_end_seconds
    if start is None or end is None or end < start:
        raise SystemExit("No common Stage 1--4 evaluation window was available")
    replay_config = ReplayConfig(
        order_scale=args.order_scale,
        maker_fee_bps=args.maker_fee_bps,
        maker_adverse_fill_buffer_bps=args.maker_adverse_fill_buffer_bps,
    )
    selected_models = tuple(
        FillModelName(value)
        for value in (
            args.fill_models
            or [
                FillModelName.CONSERVATIVE_CROSS_THROUGH.value,
                FillModelName.TOUCH_OPTIMISTIC.value,
            ]
        )
    )
    replay_results = run_replay(
        dataset.sorted_snapshots(),
        evaluation_start_seconds=start,
        evaluation_end_seconds=end,
        fill_models=selected_models,
        grid_config=GridParameterConfig(),
        replay_config=replay_config,
    )
    analysis = write_stage6_outputs(
        dataset=dataset,
        frames=frames,
        replay_results=replay_results,
        output_dir=args.output,
        report_path=args.report,
        replay_config=replay_config,
        grid_config=GridParameterConfig(),
    )
    print(
        json.dumps(
            {
                "output_dir": str(Path(args.output).expanduser().resolve()),
                "report": str(Path(args.report).expanduser().resolve()),
                "plan_frames": len(frames),
                "replay_runs": len(replay_results),
                "warnings": analysis["manifest"]["warnings"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
