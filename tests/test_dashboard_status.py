"""Stage 9 JSONL tail-reader safety tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dashboard.state_reader import (  # noqa: E402
    JsonlTailReader,
    latest_by_asset,
    read_runtime,
    summarize_churn,
)


def _record(pair: str, timestamp: str, value: int) -> dict:
    return {"trading_pair": pair, "timestamp": timestamp, "value": value}


def test_jsonl_reader_handles_partial_malformed_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    with path.open("wb") as handle:
        handle.write((json.dumps(_record("ETH-USDC", "2026-08-25T00:00:00Z", 1)) + "\n").encode())
        handle.write(b"not-json\n")
        handle.write(b'{"trading_pair":"SOL-USDC","timestamp":"2026-08-25T00:00:01Z"')
    reader = JsonlTailReader()
    result = reader.read("state", path)
    assert len(result.records) == 1
    assert result.malformed_lines == 1
    assert result.partial_trailing_line is True
    assert reader.read("missing", tmp_path / "missing.jsonl").status == "missing"


def test_reader_reloads_after_truncation_and_latest_per_asset() -> None:
    records = [
        _record("ETH-USDC", "2026-08-25T00:00:00Z", 1),
        _record("ETH-USDC", "2026-08-25T00:00:02Z", 2),
        _record("SOL-USDC", "2026-08-25T00:00:01Z", 3),
    ]
    latest = latest_by_asset(records)
    assert latest["ETH-USDC"]["value"] == 2
    assert latest["SOL-USDC"]["value"] == 3


def test_runtime_reader_tolerates_missing_streams(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    snapshot.write_text(
        json.dumps(_record("BTC-USDC", "2026-08-25T00:00:00Z", 1)) + "\n",
        encoding="utf-8",
    )
    runtime = read_runtime({"snapshot": snapshot, "state": tmp_path / "state.jsonl"})
    assert runtime.latest_by_asset["BTC-USDC"]["snapshot"]["value"] == 1
    assert runtime.streams["state"].status == "missing"


def test_churn_counts_confirmed_controller_lifecycle_events(tmp_path: Path) -> None:
    path = tmp_path / "execution.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"timestamp": 100.0, "event": "CREATE_REQUEST"},
                {"timestamp": 101.0, "event": "CREATE_SUCCESS"},
                {"timestamp": 110.0, "event": "STOP_REQUEST"},
                {"timestamp": 112.0, "event": "STOP_SUCCESS"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    stream = JsonlTailReader().read("execution_journal", path)
    churn = summarize_churn(stream, now=112.0)
    assert churn.orders_created == 1
    assert churn.orders_cancelled == 1
