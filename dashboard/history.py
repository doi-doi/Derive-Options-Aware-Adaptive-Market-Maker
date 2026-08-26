"""Presentation-neutral helpers for configuration history and rollback."""

from __future__ import annotations

from typing import Any

from .config_validation import ConfigChange, diff_configs


def history_rows(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "version": row.get("version"),
            "timestamp": row.get("timestamp"),
            "previous_hash": row.get("previous_hash"),
            "new_hash": row.get("new_hash"),
            "changed_fields": row.get("changed_fields", []),
            "operator_note": row.get("operator_note", ""),
        }
        for row in reversed(history)
    ]


def rollback_diff(current: dict[str, Any], target: dict[str, Any]) -> tuple[ConfigChange, ...]:
    return diff_configs(current, target)


__all__ = ["history_rows", "rollback_diff"]
