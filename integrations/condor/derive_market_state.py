"""Read-only Stage 2 state routine for Condor.

This routine consumes the append-only JSONL emitted by Stage 1.  It does not
poll Hummingbot, open an exchange connection, or call any trading surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import deque
from pathlib import Path
from typing import Any

from pydantic import Field

_PROJECT_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from derive_options_mm.state_engine import (  # noqa: E402
    MarketState,
    StateEngine,
    StateEngineConfig,
    format_state_summary,
)

logger = logging.getLogger(__name__)

CATEGORY = "Market Data"
CONTINUOUS = True


class Config(StateEngineConfig):
    """Stage 2 configuration plus the Stage 1 JSONL boundary."""

    trading_pair: str = Field(
        default="BTC-USDC",
        description="Only snapshots for this Hummingbot pair are consumed",
    )
    input_path: str = Field(
        default="data/derive_market_snapshots.jsonl",
        description="Stage 1 append-only JSONL path relative to Condor",
    )
    output_path: str = Field(
        default="data/derive_market_states.jsonl",
        description="Stage 2 append-only JSONL path relative to Condor",
    )
    input_poll_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        le=60,
        description="How often to check for newly appended Stage 1 snapshots",
    )
    replay_existing_snapshots: bool = Field(
        default=True,
        description="Warm the in-memory history from a bounded tail at startup",
    )
    bootstrap_max_samples: int = Field(
        default=1000,
        ge=0,
        le=10_000,
        description="Maximum existing JSONL records used only for warm-up",
    )
    max_output_file_bytes: int = Field(default=50_000_000, ge=1024, le=1_000_000_000)
    max_rotated_files: int = Field(default=3, ge=0, le=10)


def _json_record(raw_line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw_line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Skipping malformed Stage 1 JSONL record")
        return None
    return value if isinstance(value, dict) else None


class SnapshotFileTailer:
    """Read only new complete JSONL lines while retaining a bounded bootstrap."""

    def __init__(self, path: str | Path, *, bootstrap_max_samples: int = 1000) -> None:
        self.path = Path(path).expanduser()
        self.bootstrap_max_samples = bootstrap_max_samples
        self._offset = 0
        self._inode: int | None = None
        self._pending = b""

    def bootstrap(self) -> list[dict[str, Any]]:
        """Return a bounded tail and position the reader at the active EOF."""

        self._pending = b""
        if not self.path.exists():
            return []
        stat = self.path.stat()
        self._inode = stat.st_ino
        recent: deque[dict[str, Any]] = deque(maxlen=self.bootstrap_max_samples)
        with self.path.open("rb") as handle:
            if self.bootstrap_max_samples:
                for raw_line in handle:
                    if raw_line.endswith(b"\n"):
                        record = _json_record(raw_line.rstrip(b"\r\n"))
                        if record is not None:
                            recent.append(record)
            handle.seek(0, 2)
            self._offset = handle.tell()
        return list(recent)

    def poll(self) -> list[dict[str, Any]]:
        """Return complete records appended since the previous poll."""

        if not self.path.exists():
            return []
        stat = self.path.stat()
        if self._inode is None:
            self._inode = stat.st_ino
        elif stat.st_ino != self._inode or stat.st_size < self._offset:
            self._inode = stat.st_ino
            self._offset = 0
            self._pending = b""

        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            raw = handle.read()
            self._offset = handle.tell()
        if not raw:
            return []

        combined = self._pending + raw
        parts = combined.split(b"\n")
        if combined.endswith(b"\n"):
            complete_lines = parts[:-1]
            self._pending = b""
        else:
            complete_lines = parts[:-1]
            self._pending = parts[-1]

        records: list[dict[str, Any]] = []
        for raw_line in complete_lines:
            if not raw_line.strip():
                continue
            record = _json_record(raw_line.rstrip(b"\r"))
            if record is not None:
                records.append(record)
        return records


def append_state(
    state: MarketState,
    output_path: str | Path,
    *,
    max_file_bytes: int = 50_000_000,
    max_rotated_files: int = 3,
) -> Path:
    """Append one state without duplicating the Stage 1 order book."""

    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= max_file_bytes:
        for index in range(max_rotated_files - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}")
            newer = path.with_name(f"{path.name}.{index + 1}")
            if older.exists():
                older.replace(newer)
        if max_rotated_files > 0:
            path.replace(path.with_name(f"{path.name}.1"))
        else:
            path.unlink()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(state.model_dump(mode="json"), sort_keys=True, allow_nan=False) + "\n"
        )
        handle.flush()
    return path


async def run(config: Config, context: Any) -> str:
    """Warm and continuously consume Stage 1 snapshots until Condor stops us."""

    del context
    tailer = SnapshotFileTailer(
        config.input_path,
        bootstrap_max_samples=config.bootstrap_max_samples,
    )
    engine = StateEngine(config)
    input_path = Path(config.input_path).expanduser()
    output_path = Path(config.output_path).expanduser()
    state_count = 0
    last_path: Path | None = None

    if config.replay_existing_snapshots:
        for record in tailer.bootstrap():
            if record.get("trading_pair") == config.trading_pair:
                engine.update(record)
    else:
        tailer.bootstrap()

    logger.info(
        "Stage 2 state engine consuming %s for %s; output=%s; warm_history=%d",
        input_path,
        config.trading_pair,
        output_path,
        engine.history_size,
    )
    try:
        while True:
            for record in tailer.poll():
                if record.get("trading_pair") != config.trading_pair:
                    continue
                state = engine.update(record)
                try:
                    last_path = append_state(
                        state,
                        output_path,
                        max_file_bytes=config.max_output_file_bytes,
                        max_rotated_files=config.max_rotated_files,
                    )
                except Exception as exc:
                    logger.warning("Could not persist market state: %s", type(exc).__name__)
                state_count += 1
                logger.info("%s", format_state_summary(state))
            await asyncio.sleep(config.input_poll_interval_seconds)
    except asyncio.CancelledError:
        return (
            f"Stopped after {state_count} states"
            + (f"; JSONL: {last_path}" if last_path else "; no state was persisted")
        )


__all__ = ["CATEGORY", "CONTINUOUS", "Config", "SnapshotFileTailer", "append_state", "run"]
