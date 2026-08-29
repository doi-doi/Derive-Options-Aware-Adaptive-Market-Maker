"""Read persisted mainnet-shadow state without opening an exchange client."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _decode(value: str | bytes | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rows(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    cursor = connection.execute(query, params)
    return [_decode(row[0]) for row in cursor.fetchall()]


def _optional_rows(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """Read additive Stage 12 tables without breaking older shadow databases."""

    try:
        return _rows(connection, query, params)
    except sqlite3.Error:
        return []


@dataclass(frozen=True)
class ShadowDashboardState:
    """Latest persisted shadow session and lifecycle tables."""

    available: bool = False
    sqlite_path: Path | None = None
    event_path: Path | None = None
    session: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    orders: tuple[dict[str, Any], ...] = ()
    fills: tuple[dict[str, Any], ...] = ()
    cancels: tuple[dict[str, Any], ...] = ()
    risk_events: tuple[dict[str, Any], ...] = ()
    equity: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    lifecycle_events: tuple[dict[str, Any], ...] = ()
    baseline_records: tuple[dict[str, Any], ...] = ()
    checkpoints: tuple[dict[str, Any], ...] = ()
    stage13: dict[str, Any] = field(default_factory=dict)
    stage14: dict[str, Any] = field(default_factory=dict)


def read_shadow_state(data_dir: str | Path) -> ShadowDashboardState:
    """Read the current shadow state, returning an unavailable snapshot on errors."""

    root = Path(data_dir).expanduser()
    sqlite_path = root / "shadow_execution.sqlite3"
    event_path = root / "shadow_execution_events.jsonl"
    stage13_path = root.parent / "reports" / "stage13" / "shadow_validation_summary.json"
    stage14_path = root.parent / "reports" / "stage14" / "latest_summary.json"
    try:
        stage13 = _decode(stage13_path.read_text(encoding="utf-8"))
    except OSError:
        stage13 = {}
    try:
        stage14 = _decode(stage14_path.read_text(encoding="utf-8"))
    except OSError:
        stage14 = {}
    if not sqlite_path.is_file():
        return ShadowDashboardState(
            sqlite_path=sqlite_path,
            event_path=event_path,
            stage13=stage13,
            stage14=stage14,
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        session_rows = _rows(
            connection,
            "SELECT payload FROM shadow_sessions ORDER BY rowid DESC LIMIT 1",
        )
        metric_rows = _rows(
            connection,
            "SELECT payload FROM shadow_metrics ORDER BY rowid DESC LIMIT 100",
        )
        orders = _rows(
            connection,
            "SELECT payload FROM shadow_orders ORDER BY timestamp DESC",
        )
        fills = _rows(connection, "SELECT payload FROM shadow_fills ORDER BY timestamp DESC")
        cancels = _rows(
            connection,
            "SELECT payload FROM shadow_events WHERE event='ORDER_CANCEL' ORDER BY id DESC",
        )
        risk_events = _rows(
            connection,
            "SELECT payload FROM shadow_risk_events ORDER BY id DESC",
        )
        equity = _rows(connection, "SELECT payload FROM shadow_equity ORDER BY id DESC")
        events = _rows(connection, "SELECT payload FROM shadow_events ORDER BY id DESC LIMIT 200")
        lifecycle_events = _optional_rows(
            connection,
            "SELECT payload FROM shadow_order_lifecycle ORDER BY id DESC LIMIT 500",
        )
        baseline_records = _optional_rows(
            connection,
            "SELECT payload FROM shadow_baseline_records ORDER BY id DESC LIMIT 500",
        )
        checkpoints = _optional_rows(
            connection,
            "SELECT payload FROM shadow_checkpoints ORDER BY id DESC LIMIT 100",
        )
    except (OSError, sqlite3.Error):
        return ShadowDashboardState(
            sqlite_path=sqlite_path,
            event_path=event_path,
            stage13=stage13,
            stage14=stage14,
        )
    finally:
        if connection is not None:
            connection.close()
    session = session_rows[0] if session_rows else {}
    raw_metrics = next(
        (
            row
            for row in metric_rows
            if isinstance(row.get("metrics"), dict)
            or str(row.get("fill_model", "")).lower()
            in {"conservative_trade_through", "conservative"}
        ),
        metric_rows[0] if metric_rows else {},
    )
    metrics = raw_metrics.get("metrics", raw_metrics)
    if not isinstance(metrics, dict):
        metrics = {}
    return ShadowDashboardState(
        available=bool(
            session_rows or metric_rows or orders or events or lifecycle_events or baseline_records
        ),
        sqlite_path=sqlite_path,
        event_path=event_path,
        session=session,
        metrics=metrics,
        orders=tuple(orders),
        fills=tuple(fills),
        cancels=tuple(cancels),
        risk_events=tuple(risk_events),
        equity=tuple(reversed(equity)),
        events=tuple(reversed(events)),
        lifecycle_events=tuple(reversed(lifecycle_events)),
        baseline_records=tuple(reversed(baseline_records)),
        checkpoints=tuple(reversed(checkpoints)),
        stage13=stage13,
        stage14=stage14,
    )


__all__ = ["ShadowDashboardState", "read_shadow_state"]
