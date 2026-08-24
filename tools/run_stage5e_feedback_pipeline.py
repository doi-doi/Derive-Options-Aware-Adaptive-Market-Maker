"""Run isolated testnet Stage 1--4 feedback streams for Stage 5E validation.

This is a validation harness only. It imports the existing Stage 1--4
routines, changes only their output paths and connector configuration, and
never writes the production Condor data files.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
from pathlib import Path
from types import ModuleType


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[1]
    condor_dir = root / "integrations" / "condor"
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    snapshot_module = _load_module(
        "stage5e_derive_market_snapshot", condor_dir / "derive_market_snapshot.py"
    )
    state_module = _load_module(
        "stage5e_derive_market_state", condor_dir / "derive_market_state.py"
    )
    mode_module = _load_module("stage5e_derive_grid_mode", condor_dir / "derive_grid_mode.py")
    plan_module = _load_module("stage5e_derive_grid_plan", condor_dir / "derive_grid_plan.py")

    snapshots = data_dir / "derive_market_snapshots.jsonl"
    states = data_dir / "derive_market_states.jsonl"
    modes = data_dir / "derive_grid_modes.jsonl"
    plans = data_dir / "derive_grid_plans.jsonl"

    snapshot_config = snapshot_module.Config(
        connector_name="derive_perpetual_testnet",
        trading_pair="BTC-USDC",
        account_name="master_account",
        output_path=str(snapshots),
        snapshot_interval_seconds=args.snapshot_interval,
        options_enabled=True,
    )
    state_config = state_module.Config(
        trading_pair="BTC-USDC",
        input_path=str(snapshots),
        output_path=str(states),
        input_poll_interval_seconds=args.poll_interval,
    )
    mode_config = mode_module.Config(
        trading_pair="BTC-USDC",
        input_path=str(states),
        output_path=str(modes),
        input_poll_interval_seconds=args.poll_interval,
    )
    plan_config = plan_module.Config(
        trading_pair="BTC-USDC",
        snapshot_path=str(snapshots),
        state_path=str(states),
        mode_path=str(modes),
        output_path=str(plans),
        input_poll_interval_seconds=args.poll_interval,
    )

    logging.info("Stage 5E isolated feedback pipeline writing under %s", data_dir)
    await asyncio.gather(
        snapshot_module.run(snapshot_config, None),
        state_module.run(state_config, None),
        mode_module.run(mode_config, None),
        plan_module.run(plan_config, None),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--snapshot-interval", type=float, default=5.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
