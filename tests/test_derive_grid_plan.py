from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

ROUTINE_PATH = Path(__file__).parents[1] / "integrations" / "condor" / "derive_grid_plan.py"
spec = importlib.util.spec_from_file_location("derive_grid_plan", ROUTINE_PATH)
assert spec and spec.loader
routine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routine)


def _snapshot(timestamp: str = "2026-08-24T00:00:00Z") -> dict:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "data_valid": True,
        "best_bid": 77000.0,
        "best_ask": 77010.0,
        "mid_price": 77005.0,
        "spread_bps": 1.2986,
        "best_bid_size": 2.0,
        "best_ask_size": 1.0,
    }


def _state(timestamp: str = "2026-08-24T00:00:00Z") -> dict:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "volatility_state": "normal",
        "volatility_score": 1.0,
        "direction_state": "neutral",
        "direction_score": 0.0,
        "inventory_state": "neutral",
        "inventory_ratio": 0.0,
        "confidence": 0.925,
        "state_valid": True,
        "reasons": [],
    }


def _mode(timestamp: str = "2026-08-24T00:00:00Z") -> dict:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "mode": "normal",
        "previous_mode": None,
        "transition_occurred": False,
        "volatility_state": "normal",
        "volatility_score": 1.0,
        "direction_state": "neutral",
        "direction_score": 0.0,
        "inventory_state": "neutral",
        "inventory_ratio": 0.0,
        "confidence": 0.925,
        "valid": True,
        "reasons": ["normal"],
        "recommended_profile": "standard",
    }


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_config_defaults_to_stage_boundaries() -> None:
    config = routine.Config()

    assert config.trading_pair == "BTC-USDC"
    assert config.snapshot_path == "data/derive_market_snapshots.jsonl"
    assert config.state_path == "data/derive_market_states.jsonl"
    assert config.mode_path == "data/derive_grid_modes.jsonl"
    assert config.output_path == "data/derive_grid_plans.jsonl"
    assert config.normal_levels_per_side == 5


def test_tailer_waits_for_complete_jsonl_line(tmp_path: Path) -> None:
    path = tmp_path / "input.jsonl"
    path.write_text("", encoding="utf-8")
    tailer = routine.JsonlTailer(path, bootstrap_max_samples=0)
    tailer.bootstrap()
    raw = json.dumps(_snapshot()).encode()
    with path.open("ab") as handle:
        handle.write(raw[:20])
    assert tailer.poll() == []
    with path.open("ab") as handle:
        handle.write(raw[20:] + b"\n")
    assert tailer.poll() == [_snapshot()]


def test_append_grid_plan_serializes_decimal_values(tmp_path: Path) -> None:
    from derive_options_mm.grid_engine import build_grid_plan

    plan = build_grid_plan(_snapshot(), _state(), _mode())
    path = tmp_path / "plans.jsonl"
    routine.append_grid_plan(plan, path)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    assert record["mode"] == "normal"
    assert record["enabled"] is True
    assert isinstance(record["center_price"], float)
    assert record["buy_levels"]
    assert "raw_snapshot" not in record


def test_run_joins_three_inputs_and_cancels_cleanly(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshots.jsonl"
    state_path = tmp_path / "states.jsonl"
    mode_path = tmp_path / "modes.jsonl"
    output_path = tmp_path / "plans.jsonl"
    _write(snapshot_path, [_snapshot()])
    _write(state_path, [_state()])
    _write(mode_path, [_mode()])
    config = routine.Config(
        snapshot_path=str(snapshot_path),
        state_path=str(state_path),
        mode_path=str(mode_path),
        output_path=str(output_path),
        input_poll_interval_seconds=0.01,
        replay_existing_inputs=True,
    )

    async def scenario() -> str:
        task = asyncio.create_task(routine.run(config, None))
        await asyncio.sleep(0.03)
        with snapshot_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_snapshot("2026-08-24T00:00:05Z")) + "\n")
        await asyncio.sleep(0.04)
        task.cancel()
        return await task

    result = asyncio.run(scenario())
    records = [json.loads(line) for line in output_path.read_text().splitlines()]

    assert result.startswith("Stopped after ")
    assert len(records) >= 2
    assert all(record["trading_pair"] == "BTC-USDC" for record in records)
    assert all(record["enabled"] is True for record in records)


def test_runner_has_no_execution_surface() -> None:
    source = ROUTINE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "place_order",
        "cancel_order",
        "set_leverage",
        "set_position_mode",
        "GridExecutor",
        "PositionExecutor",
    ):
        assert forbidden not in source.lower()
