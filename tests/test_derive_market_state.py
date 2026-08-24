from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

ROUTINE_PATH = (
    Path(__file__).parents[1] / "integrations" / "condor" / "derive_market_state.py"
)


def _load_routine():
    spec = importlib.util.spec_from_file_location("derive_market_state", ROUTINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


routine = _load_routine()


def _snapshot(timestamp: float, price: float = 100.0) -> dict:
    return {
        "timestamp": timestamp,
        "trading_pair": "BTC-USDC",
        "mid_price": price,
        "depth_imbalance": 0.4,
        "top_level_imbalance": 0.2,
        "order_flow_imbalance": 0.3,
        "trade_data_available": True,
        "atm_iv": None,
        "iv_data_available": False,
        "current_position": 0.0,
        "position_notional": 0.0,
        "account_data_available": True,
        "data_valid": True,
        "validation_errors": [],
    }


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_config_defaults_to_stage1_boundary() -> None:
    config = routine.Config()
    assert config.trading_pair == "BTC-USDC"
    assert config.input_path == "data/derive_market_snapshots.jsonl"
    assert config.output_path == "data/derive_market_states.jsonl"
    assert config.replay_existing_snapshots is True
    assert config.input_poll_interval_seconds == 1.0


def test_tailer_bootstrap_is_bounded_and_poll_reads_new_records(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.jsonl"
    records = [_snapshot(float(index), price=100.0 + index) for index in range(3)]
    _write_records(path, records)
    tailer = routine.SnapshotFileTailer(path, bootstrap_max_samples=2)

    assert tailer.bootstrap() == records[-2:]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_snapshot(3.0, price=103.0)) + "\n")
    assert tailer.poll() == [_snapshot(3.0, price=103.0)]


def test_tailer_waits_for_complete_jsonl_line(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.jsonl"
    path.write_text("", encoding="utf-8")
    tailer = routine.SnapshotFileTailer(path, bootstrap_max_samples=0)
    assert tailer.bootstrap() == []
    raw = json.dumps(_snapshot(1.0)).encode()
    with path.open("ab") as handle:
        handle.write(raw[:20])
    assert tailer.poll() == []
    with path.open("ab") as handle:
        handle.write(raw[20:] + b"\n")
    assert tailer.poll() == [_snapshot(1.0)]


def test_append_state_does_not_duplicate_order_book(tmp_path: Path) -> None:
    from derive_options_mm.state_engine import StateEngine, StateEngineConfig

    engine = StateEngine(
        StateEngineConfig(
            minimum_history_samples=2,
            realized_vol_window_seconds=5,
            realized_vol_baseline_seconds=10,
            direction_return_window_seconds=5,
        )
    )
    state = engine.update(_snapshot(0.0))
    path = tmp_path / "states.jsonl"
    routine.append_state(state, path)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["trading_pair"] == "BTC-USDC"
    assert "bids" not in record
    assert "asks" not in record
    assert "reasons" in record


def test_run_consumes_new_records_and_stops_cleanly(tmp_path: Path) -> None:
    input_path = tmp_path / "snapshots.jsonl"
    output_path = tmp_path / "states.jsonl"
    _write_records(input_path, [_snapshot(0.0), _snapshot(5.0, price=100.1)])
    config = routine.Config(
        input_path=str(input_path),
        output_path=str(output_path),
        input_poll_interval_seconds=0.01,
        minimum_history_samples=2,
        realized_vol_window_seconds=5,
        realized_vol_baseline_seconds=10,
        direction_return_window_seconds=5,
        replay_existing_snapshots=True,
    )

    async def scenario() -> str:
        task = asyncio.create_task(routine.run(config, None))
        await asyncio.sleep(0.03)
        with input_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_snapshot(10.0, price=100.2)) + "\n")
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    result = asyncio.run(scenario())
    assert result.startswith("Stopped after ")
    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert records
    assert all(record["trading_pair"] == "BTC-USDC" for record in records)


def test_state_routine_has_no_mutating_or_market_data_surface() -> None:
    source = ROUTINE_PATH.read_text()
    for forbidden in (
        "get_client(",
        "market_data",
        ".place_order(",
        ".cancel_order(",
        ".set_leverage(",
        ".set_position_mode(",
        ".create_executor(",
        ".stop_executor(",
    ):
        assert forbidden not in source
