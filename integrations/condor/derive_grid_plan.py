"""Read-only Stage 4 adaptive-grid plan routine for Condor.

This routine joins the append-only outputs of Stages 1--3 and persists
theoretical ``GridPlan`` records.  It never opens an exchange connection,
places an order, creates an executor, or changes a live controller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field

_PROJECT_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from derive_options_mm.grid_engine import (  # noqa: E402
    GridParameterConfig,
    GridParameterEngine,
    GridPlan,
    format_grid_plan_summary,
)

logger = logging.getLogger(__name__)

CATEGORY = "Analytics"
CONTINUOUS = True


class Config(GridParameterConfig):
    """Stage 4 configuration plus the three JSONL input boundaries."""

    trading_pair: str = Field(
        default="BTC-USDC",
        description="Only records for this Hummingbot pair are joined",
    )
    snapshot_path: str = Field(
        default="data/derive_market_snapshots.jsonl",
        description="Stage 1 append-only JSONL path relative to Condor",
    )
    state_path: str = Field(
        default="data/derive_market_states.jsonl",
        description="Stage 2 append-only JSONL path relative to Condor",
    )
    mode_path: str = Field(
        default="data/derive_grid_modes.jsonl",
        description="Stage 3 append-only JSONL path relative to Condor",
    )
    output_path: str = Field(
        default="data/derive_grid_plans.jsonl",
        description="Append-only theoretical plan JSONL path relative to Condor",
    )
    input_poll_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        le=60,
        description="How often to check the three input streams",
    )
    replay_existing_inputs: bool = Field(
        default=True,
        description="Warm from the latest bounded record in each input stream",
    )
    bootstrap_max_samples: int = Field(
        default=1000,
        ge=0,
        le=10_000,
        description="Maximum existing records scanned per input for warm-up",
    )
    max_output_file_bytes: int = Field(default=50_000_000, ge=1024, le=1_000_000_000)
    max_rotated_files: int = Field(default=3, ge=0, le=10)


def _json_record(raw_line: bytes, source: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(raw_line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Skipping malformed Stage 4 input JSONL record from %s", source)
        return None
    return value if isinstance(value, dict) else None


class JsonlTailer:
    """Read complete appended JSONL records and tolerate rotation."""

    def __init__(self, path: str | Path, *, bootstrap_max_samples: int = 1000) -> None:
        self.path = Path(path).expanduser()
        self.bootstrap_max_samples = bootstrap_max_samples
        self._offset = 0
        self._inode: int | None = None
        self._pending = b""

    def bootstrap(self) -> list[dict[str, Any]]:
        self._pending = b""
        if not self.path.exists():
            return []
        stat = self.path.stat()
        self._inode = stat.st_ino
        recent: deque[dict[str, Any]] = deque(maxlen=self.bootstrap_max_samples)
        with self.path.open("rb") as handle:
            for raw_line in handle:
                if raw_line.endswith(b"\n") and self.bootstrap_max_samples:
                    record = _json_record(raw_line.rstrip(b"\r\n"), self.path)
                    if record is not None:
                        recent.append(record)
            handle.seek(0, 2)
            self._offset = handle.tell()
        return list(recent)

    def poll(self) -> list[dict[str, Any]]:
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
            record = _json_record(raw_line.rstrip(b"\r"), self.path)
            if record is not None:
                records.append(record)
        return records


def append_grid_plan(
    plan: GridPlan,
    output_path: str | Path,
    *,
    max_file_bytes: int = 50_000_000,
    max_rotated_files: int = 3,
) -> Path:
    """Append one machine-readable plan and rotate bounded backups."""

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
        handle.write(json.dumps(plan.to_record(), sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
    return path


def _pair_matches(record: Mapping[str, Any], trading_pair: str) -> bool:
    pair = record.get("trading_pair")
    return pair is None or pair == trading_pair


def _latest_matching(records: list[dict[str, Any]], trading_pair: str) -> dict[str, Any] | None:
    for record in reversed(records):
        if _pair_matches(record, trading_pair):
            return record
    return None


async def run(config: Config, context: Any) -> str:
    """Join live Stage 1--3 outputs and persist theoretical plans."""

    del context
    tailers = {
        "snapshot": JsonlTailer(
            config.snapshot_path,
            bootstrap_max_samples=config.bootstrap_max_samples,
        ),
        "state": JsonlTailer(
            config.state_path,
            bootstrap_max_samples=config.bootstrap_max_samples,
        ),
        "mode": JsonlTailer(
            config.mode_path,
            bootstrap_max_samples=config.bootstrap_max_samples,
        ),
    }
    latest: dict[str, dict[str, Any]] = {}
    for name, tailer in tailers.items():
        record = _latest_matching(
            tailer.bootstrap(),
            config.trading_pair,
        )
        if record is not None:
            latest[name] = record

    engine = GridParameterEngine(config)
    plan_count = 0
    last_path: Path | None = None

    def emit_if_ready() -> None:
        nonlocal last_path, plan_count
        if set(latest) != set(tailers):
            return
        plan = engine.build(latest["snapshot"], latest["state"], latest["mode"])
        try:
            last_path = append_grid_plan(
                plan,
                config.output_path,
                max_file_bytes=config.max_output_file_bytes,
                max_rotated_files=config.max_rotated_files,
            )
        except Exception as exc:
            logger.warning("Could not persist adaptive grid plan: %s", type(exc).__name__)
        plan_count += 1
        logger.info("%s", format_grid_plan_summary(plan))

    if config.replay_existing_inputs:
        emit_if_ready()

    try:
        while True:
            changed = False
            for name, tailer in tailers.items():
                for record in tailer.poll():
                    if _pair_matches(record, config.trading_pair):
                        latest[name] = record
                        changed = True
            if changed:
                emit_if_ready()
            await asyncio.sleep(config.input_poll_interval_seconds)
    except asyncio.CancelledError:
        return (
            f"Stopped after {plan_count} grid plans"
            + (f"; JSONL: {last_path}" if last_path else "; no plan was persisted")
        )


__all__ = [
    "CATEGORY",
    "CONTINUOUS",
    "Config",
    "JsonlTailer",
    "append_grid_plan",
    "run",
]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(Config(), None))
