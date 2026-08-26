"""Atomic configuration storage, version history, and rollback support."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .config_schema import DashboardConfig, default_strategy_settings
from .config_validation import (
    ConfigChange,
    config_hash,
    diff_configs,
    redact_secrets,
    validate_bundle,
    yaml_export,
)
from .controller_config import controller_yaml


@dataclass(frozen=True)
class ApplyResult:
    version: int
    timestamp: str
    previous_hash: str
    new_hash: str
    changed_fields: tuple[str, ...]
    runtime_reload: str = "RESTART_REQUIRED"


class ConfigStore:
    """Manage only local YAML/JSONL state; never talks to an exchange."""

    def __init__(
        self,
        profile_path: str | Path,
        *,
        strategy_path: str | Path | None = None,
        controller_path: str | Path | None = None,
        history_dir: str | Path | None = None,
        events_path: str | Path | None = None,
    ) -> None:
        self.profile_path = Path(profile_path).expanduser()
        self.strategy_path = Path(strategy_path).expanduser() if strategy_path else None
        self.controller_path = Path(controller_path).expanduser() if controller_path else None
        root = (
            self.profile_path.parent.parent
            if self.profile_path.parent.name == "configs"
            else self.profile_path.parent
        )
        self.history_dir = Path(history_dir or root / "data" / "config_history").expanduser()
        self.events_path = Path(
            events_path or root / "data" / "config_change_events.jsonl"
        ).expanduser()

    def load(self) -> DashboardConfig:
        profile_raw = self._read_yaml(self.profile_path)
        strategy_raw = self._read_yaml(self.strategy_path) if self.strategy_path else None
        profile = DashboardConfig.model_validate(
            {
                "competition": profile_raw,
                "strategy": strategy_raw or default_strategy_settings().model_dump(mode="python"),
            }
        )
        profile.strategy.to_multi_asset_config()
        return profile

    def source_record(self) -> dict[str, Any]:
        return self.load().to_record()

    def saved_hash(self) -> str:
        return config_hash(self.source_record())

    def version(self) -> int:
        versions = [self._version_from_path(path) for path in self.history_dir.glob("v*.json")]
        return max(versions, default=0)

    def detect_drift(self, loaded_hash: str) -> bool:
        try:
            return self.saved_hash() != loaded_hash
        except Exception:
            return True

    def validate(self, bundle: DashboardConfig) -> tuple[Any, tuple[ConfigChange, ...]]:
        current = self.source_record()
        proposed = bundle.to_record()
        report = validate_bundle(proposed)
        return report, diff_configs(current, proposed)

    def apply(self, bundle: DashboardConfig, *, operator_note: str = "") -> ApplyResult:
        proposed = bundle.to_record()
        validation = validate_bundle(proposed)
        if not validation.valid:
            raise ValueError("configuration validation failed: " + "; ".join(validation.errors))
        current = self.source_record()
        changes = diff_configs(current, proposed)
        previous_hash = config_hash(current)
        new_hash = config_hash(proposed)
        version = self.version() + 1
        timestamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        controller_content = None
        if self.controller_path is not None:
            controller_content = controller_yaml(
                DashboardConfig.model_validate(proposed).competition
            )

        self._atomic_write(self.profile_path, yaml_export(proposed["competition"]))
        if self.strategy_path is not None:
            self._atomic_write(self.strategy_path, yaml_export(proposed["strategy"]))
        if controller_content is not None:
            self._atomic_write(self.controller_path, controller_content)

        snapshot = {
            "timestamp": timestamp,
            "version": version,
            "previous_hash": previous_hash,
            "new_hash": new_hash,
            "changed_fields": [change.path for change in changes],
            "operator_note": operator_note[:500],
            "config": redact_secrets(proposed),
        }
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self.history_dir / f"v{version:04d}.json",
            json.dumps(snapshot, sort_keys=True, indent=2) + "\n",
        )
        event = {
            "timestamp": timestamp,
            "event": "CONFIG_CHANGED",
            "version": version,
            "changed_fields": [change.path for change in changes],
            "old_hash": previous_hash,
            "new_hash": new_hash,
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return ApplyResult(
            version=version,
            timestamp=timestamp,
            previous_hash=previous_hash,
            new_hash=new_hash,
            changed_fields=tuple(change.path for change in changes),
        )

    def load_history(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.history_dir.glob("v*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def load_version(self, version: int) -> DashboardConfig:
        path = self.history_dir / f"v{version:04d}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return DashboardConfig.model_validate(raw["config"])
        except (OSError, KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"cannot load configuration version v{version:04d}") from exc

    def apply_version(self, version: int, *, operator_note: str = "rollback") -> ApplyResult:
        """Restore through the normal validate/diff/apply path, never overwrite history."""

        return self.apply(self.load_version(version), operator_note=operator_note)

    @staticmethod
    def _read_yaml(path: Path | None) -> dict[str, Any]:
        if path is None or not path.exists():
            return {}
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"configuration file must contain a mapping: {path}")
        return raw

    @staticmethod
    def _version_from_path(path: Path) -> int:
        try:
            return int(path.stem[1:])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_suffix(path.suffix + ".bak")
        if path.exists():
            shutil.copy2(path, backup)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()


__all__ = ["ApplyResult", "ConfigStore"]
