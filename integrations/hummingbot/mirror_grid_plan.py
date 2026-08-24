"""Mirror the Stage 4 GridPlan JSONL into a Hummingbot bot data volume.

Stage 4 and the Hummingbot bot intentionally run in separate processes and
directories.  This bridge only copies the already-produced GridPlan; it does
not calculate signals or call an exchange API.  Replacement is atomic so the
Stage 5 JSONL tailer can safely handle file rotation between copies.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MirrorConfig:
    source_path: Path
    target_path: Path
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if self.source_path.resolve() == self.target_path.resolve():
            raise ValueError("source and target must be different files")


def mirror_once(
    config: MirrorConfig, previous_signature: tuple[int, int] | None = None
) -> tuple[int, int] | None:
    """Copy a changed source file and return its (size, mtime_ns) signature."""

    try:
        source_stat = config.source_path.stat()
    except OSError:
        return previous_signature
    signature = (source_stat.st_size, source_stat.st_mtime_ns)
    if signature == previous_signature:
        return previous_signature

    config.target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config.target_path.with_name(f".{config.target_path.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(config.source_path, temporary_path)
        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, config.target_path)
    except OSError:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        return previous_signature
    return signature


async def run(config: MirrorConfig) -> None:
    signature = None
    while True:
        signature = mirror_once(config, signature)
        await asyncio.sleep(config.poll_interval_seconds)


def _parse_args() -> MirrorConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    return MirrorConfig(args.source, args.target, args.interval)


def main() -> None:
    config = _parse_args()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
