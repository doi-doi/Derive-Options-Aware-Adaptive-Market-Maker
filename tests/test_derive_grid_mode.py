from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

ROUTINE_PATH = Path(__file__).parents[1] / "integrations" / "condor" / "derive_grid_mode.py"
spec = importlib.util.spec_from_file_location("derive_grid_mode", ROUTINE_PATH)
assert spec and spec.loader
routine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routine)


def _state(timestamp: float, *, valid: bool = True) -> dict:
    return {
        "timestamp": str(timestamp),
        "trading_pair": "BTC-USDC",
        "volatility_state": "normal",
        "volatility_score": 1.0,
        "direction_state": "neutral",
        "direction_score": 0.0,
        "inventory_state": "neutral",
        "inventory_ratio": 0.0,
        "confidence": 0.90,
        "state_valid": valid,
        "reasons": [],
    }


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_config_defaults_to_stage2_boundary() -> None:
    config = routine.Config()
    assert config.trading_pair == "BTC-USDC"
    assert config.input_path == "data/derive_market_states.jsonl"
    assert config.output_path == "data/derive_grid_modes.jsonl"
    assert config.replay_existing_states is True
    assert config.mode_confirmation_samples == 2


def test_tailer_bootstrap_is_bounded_and_poll_reads_new_records(tmp_path: Path) -> None:
    path = tmp_path / "states.jsonl"
    records = [_state(float(index)) for index in range(3)]
    _write_records(path, records)
    tailer = routine.StateFileTailer(path, bootstrap_max_samples=2)

    assert tailer.bootstrap() == records[-2:]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_state(3.0)) + "\n")
    assert tailer.poll() == [_state(3.0)]


def test_tailer_waits_for_complete_jsonl_line(tmp_path: Path) -> None:
    path = tmp_path / "states.jsonl"
    path.write_text("", encoding="utf-8")
    tailer = routine.StateFileTailer(path, bootstrap_max_samples=0)
    assert tailer.bootstrap() == []
    raw = json.dumps(_state(1.0)).encode()
    with path.open("ab") as handle:
        handle.write(raw[:20])
    assert tailer.poll() == []
    with path.open("ab") as handle:
        handle.write(raw[20:] + b"\n")
    assert tailer.poll() == [_state(1.0)]


def test_append_mode_decision_does_not_duplicate_stage2_fields(tmp_path: Path) -> None:
    selector = routine.ModeSelector(
        routine.Config(
            pause_recovery_samples=1,
            mode_confirmation_samples=1,
            minimum_mode_duration_seconds=0,
        )
    )
    selector.update(_state(0.0))
    decision = selector.update(_state(1.0))
    path = tmp_path / "modes.jsonl"
    routine.append_mode_decision(decision, path)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["mode"] == "normal"
    assert record["recommended_profile"] == "standard"
    assert "mid_price" not in record
    assert "atm_iv" not in record
    assert "reasons" in record


def test_run_consumes_new_states_and_stops_cleanly(tmp_path: Path) -> None:
    input_path = tmp_path / "states.jsonl"
    output_path = tmp_path / "modes.jsonl"
    _write_records(input_path, [_state(0.0), _state(5.0)])
    config = routine.Config(
        input_path=str(input_path),
        output_path=str(output_path),
        input_poll_interval_seconds=0.01,
        pause_recovery_samples=1,
        mode_confirmation_samples=1,
        minimum_mode_duration_seconds=0,
        replay_existing_states=True,
    )

    async def scenario() -> str:
        task = asyncio.create_task(routine.run(config, None))
        await asyncio.sleep(0.03)
        with input_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_state(10.0)) + "\n")
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    result = asyncio.run(scenario())
    assert result.startswith("Stopped after ")
    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert records
    assert all(record["trading_pair"] == "BTC-USDC" for record in records)
    assert all("mid_price" not in record for record in records)


def test_mode_routine_has_no_mutating_or_market_data_surface() -> None:
    source = ROUTINE_PATH.read_text()
    for forbidden in (
        "get_client(",
        "market_data",
        ".place_order(",
        ".cancel_order(",
        ".set_leverage(",
        ".set_position_mode(",
        "PositionExecutor",
        "GridExecutor",
        "grid_width",
        "grid_levels",
        "order_size",
    ):
        assert forbidden not in source
