"""Validation, diff, hashing, and secret-redaction helpers for Stage 9."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import yaml

from .config_schema import DashboardConfig

_SECRET_KEY = re.compile(
    r"(private[_-]?key|api[_-]?(key|secret)|password|token|auth|wallet[_-]?secret)",
    re.I,
)


@dataclass(frozen=True)
class ConfigChange:
    path: str
    old: Any
    new: Any
    classification: str
    risk_increasing: bool = False


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    risk_increasing: bool = False


def redact_secrets(value: Any, *, key_hint: str = "") -> Any:
    """Remove secret-like values before history, exports, or UI display."""

    if _SECRET_KEY.search(key_hint):
        return "********"
    if isinstance(value, dict):
        return {str(key): redact_secrets(item, key_hint=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, key_hint=key_hint) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item, key_hint=key_hint) for item in value]
    return value


def canonical_record(value: Any) -> Any:
    """Return a JSON-safe, secret-free value with stable key ordering."""

    return redact_secrets(value)


def config_hash(value: Any) -> str:
    payload = json.dumps(
        canonical_record(value), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def yaml_export(value: Any) -> str:
    return yaml.safe_dump(canonical_record(value), sort_keys=False, allow_unicode=True)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten(item, path))
        return flattened
    if isinstance(value, (list, tuple)):
        return {prefix: list(value)}
    return {prefix: value}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _is_risk_increasing(path: str, old: Any, new: Any) -> bool:
    old_number = _number(old)
    new_number = _number(new)
    if old_number is None or new_number is None:
        return False
    lowered = path.lower()
    if "reserve" in lowered:
        return new_number < old_number
    if any(token in lowered for token in ("hard", "max_", "leverage", "target_order")):
        return new_number > old_number
    if "drawdown" in lowered:
        return new_number > old_number
    return False


def classify_change(path: str) -> str:
    """Reflect the current architecture: no safe runtime config reload exists."""

    if path.startswith("runtime."):
        return "READ_ONLY"
    return "RESTART_REQUIRED"


def diff_configs(old: dict[str, Any], new: dict[str, Any]) -> tuple[ConfigChange, ...]:
    old_flat = _flatten(canonical_record(old))
    new_flat = _flatten(canonical_record(new))
    changes: list[ConfigChange] = []
    for path in sorted(set(old_flat) | set(new_flat)):
        previous = old_flat.get(path)
        proposed = new_flat.get(path)
        if previous == proposed:
            continue
        changes.append(
            ConfigChange(
                path=path,
                old=previous,
                new=proposed,
                classification=classify_change(path),
                risk_increasing=_is_risk_increasing(path, previous, proposed),
            )
        )
    return tuple(changes)


def validate_bundle(record: dict[str, Any]) -> ValidationReport:
    try:
        bundle = DashboardConfig.model_validate(record)
        bundle.strategy.to_multi_asset_config()
    except Exception as exc:  # Pydantic aggregates nested validation details.
        return ValidationReport(valid=False, errors=(str(exc),))
    return ValidationReport(valid=True)


def validate_and_diff(
    old: dict[str, Any], new: dict[str, Any]
) -> tuple[ValidationReport, tuple[ConfigChange, ...]]:
    report = validate_bundle(new)
    changes = diff_configs(old, new)
    if any(change.risk_increasing for change in changes):
        report = ValidationReport(
            valid=report.valid,
            errors=report.errors,
            warnings=(
                "This staged change increases configured risk; explicit acknowledgement is "
                "required.",
            ),
            risk_increasing=True,
        )
    return report, changes


__all__ = [
    "ConfigChange",
    "ValidationReport",
    "canonical_record",
    "classify_change",
    "config_hash",
    "diff_configs",
    "redact_secrets",
    "validate_and_diff",
    "validate_bundle",
    "yaml_export",
]
