"""Evidence-based Stage 14 economics for a frozen mainnet shadow session.

Stage 14 observes the already-validated Stage 13 strategy.  It does not own a
private Derive client and it does not change prices, sizes, risk limits, pause
logic, or asset eligibility.  The conservative trade-through ledger is the
headline result; the touch-optimistic ledger is an isolated sensitivity
ledger.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .shadow import ShadowConfig, ShadowFillModel
from .shadow_baseline import (
    CONSERVATIVE_MODEL,
    TOUCH_MODEL,
    ShadowBaselineSession,
    _lifetime_stats,
    _markout_stats,
    _mean,
    _percentile,
    _safe,
)

STAGE14_MARKOUT_HORIZONS_SECONDS = (5, 30, 60, 300)
STAGE14_MINIMUM_DURATION_SECONDS = 2 * 60 * 60
STAGE14_MAXIMUM_DURATION_SECONDS = 6 * 60 * 60
STAGE14_CLASSIFICATIONS = (
    "HEALTHY_SHADOW_ECONOMICS",
    "LOW_CONSERVATIVE_FILL_RATE",
    "ADVERSE_SELECTION",
    "CAPITAL_LOCKUP",
    "LOW_VOLUME_EFFICIENCY",
    "HIGH_FILL_MODEL_UNCERTAINTY",
    "DATA_QUALITY_INSUFFICIENT",
    "MIXED",
    "INSUFFICIENT_SAMPLE",
)
STAGE14_EVIDENCE_STATUSES = (
    "INSUFFICIENT",
    "DEVELOPING",
    "SUFFICIENT_FOR_DIAGNOSIS",
)
STAGE14_LIFETIME_BUCKETS = (
    "<5 sec",
    "5-30 sec",
    "30-60 sec",
    "1-2 min",
    "2-5 min",
    "5-15 min",
    "15-30 min",
    "30m+",
)
STAGE14_REQUIRED_FILES = (
    "manifest.json",
    "summary.md",
    "summary.json",
    "hourly_metrics.csv",
    "orders.csv",
    "fills.csv",
    "fill_eligibility.csv",
    "markouts.csv",
    "cycles.csv",
    "inventory.csv",
    "portfolio_exposure.csv",
    "risk_events.csv",
    "pause_events.csv",
    "trade_quality.csv",
    "fill_model_comparison.csv",
    "self_tuning_suggestions.csv",
    "asset_summary.csv",
)
STAGE14_HOURLY_FIELDS = (
    "timestamp",
    "hour",
    "checkpoint_type",
    "model",
    "elapsed_seconds",
    "evidence_status",
    "orders_created",
    "resting_orders",
    "orders_kept",
    "operational_cancels",
    "shutdown_cancels",
    "orders_replaced",
    "orders_filled",
    "fills",
    "fill_create_ratio",
    "cancel_create_ratio",
    "keep_rate_pct",
    "median_quote_lifetime_seconds",
    "markout_5s_bps",
    "markout_5s_n",
    "markout_30s_bps",
    "markout_30s_n",
    "markout_60s_bps",
    "markout_60s_n",
    "markout_300s_bps",
    "markout_300s_n",
    "completed_cycles",
    "median_cycle_duration_seconds",
    "p75_cycle_duration_seconds",
    "p90_cycle_duration_seconds",
    "executed_volume",
    "volume_per_average_filled_gross",
    "volume_per_average_worst_case_gross",
    "average_filled_gross",
    "max_filled_gross",
    "average_pending_reserved_gross",
    "max_pending_reserved_gross",
    "average_worst_case_gross",
    "max_worst_case_gross",
    "average_inventory",
    "max_inventory",
    "average_btc_beta",
    "max_btc_beta",
    "gross_pnl",
    "max_drawdown",
    "risk_blocks",
    "hard_limit_attempts",
    "data_quality_coverage_pct",
    "fill_model_sensitivity",
)


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _resolve_path(value: str | Path, root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _git_commit(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _file_hash(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_safe(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            normalized = {
                key: json.dumps(_safe(value), sort_keys=True)
                if isinstance(value, (dict, list, tuple))
                else _safe(value)
                for key, value in row.items()
            }
            writer.writerow(normalized)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error):
        return []


def _lifetime_bucket(seconds: Any) -> str | None:
    value = _number(seconds)
    if value is None or value < 0:
        return None
    if value < 5:
        return "<5 sec"
    if value < 30:
        return "5-30 sec"
    if value < 60:
        return "30-60 sec"
    if value < 120:
        return "1-2 min"
    if value < 300:
        return "2-5 min"
    if value < 900:
        return "5-15 min"
    if value < 1800:
        return "15-30 min"
    return "30m+"


def _directional_markout(fill: Mapping[str, Any], future_mid: float) -> float | None:
    price = _number(fill.get("price"))
    mid = _number(future_mid)
    if price is None or price <= 0 or mid is None or mid <= 0:
        return None
    result = (mid - price) / price * 10_000.0
    if str(fill.get("side", "")).lower() == "sell":
        result = -result
    return result


def _time_weighted_average(
    points: Sequence[Mapping[str, Any]], field: str, start: float, end: float
) -> float | None:
    if end <= start or not points:
        return None
    ordered = sorted(
        (row for row in points if _number(row.get("timestamp_epoch")) is not None),
        key=lambda row: _number(row.get("timestamp_epoch"), 0.0) or 0.0,
    )
    if not ordered:
        return None
    total = 0.0
    weighted = 0.0
    for index, row in enumerate(ordered):
        point_start = max(start, _number(row.get("timestamp_epoch"), start) or start)
        next_timestamp = (
            _number(ordered[index + 1].get("timestamp_epoch")) if index + 1 < len(ordered) else end
        )
        point_end = min(end, next_timestamp or end)
        if point_end <= point_start:
            continue
        value = _number(row.get(field))
        if value is None:
            continue
        duration = point_end - point_start
        total += duration
        weighted += value * duration
    return weighted / total if total > 0 else None


@dataclass(frozen=True)
class Stage14Config:
    """Run policy; strategy parameters remain in the frozen Stage 13 profile."""

    minimum_duration_seconds: float = STAGE14_MINIMUM_DURATION_SECONDS
    maximum_duration_seconds: float = STAGE14_MAXIMUM_DURATION_SECONDS
    checkpoint_interval_seconds: float = 3600.0
    diagnostic_fill_target: int = 20
    minimum_markout_samples: int = 5
    minimum_cycle_samples: int = 3

    def validate_duration(self, duration_seconds: float, *, cycles: int | None = None) -> None:
        if duration_seconds <= 0:
            raise ValueError("Stage 14 duration must be positive")
        if duration_seconds > self.maximum_duration_seconds:
            raise ValueError("Stage 14 duration cannot exceed 6 hours")
        if cycles is None and duration_seconds < self.minimum_duration_seconds:
            raise ValueError(
                "Stage 14 requires at least 2 hours unless a cycle-limited test is used"
            )
        if cycles is not None and cycles < 1:
            raise ValueError("Stage 14 cycle limit must be positive")


@dataclass(frozen=True)
class Stage14Evidence:
    status: str
    elapsed_seconds: float
    conservative_fills: int
    diagnostic_fill_target: int
    markout_30s_n: int
    markout_60s_n: int
    completed_cycles: int
    state_observations: int
    inventory_observed: bool
    stability_ok: bool
    data_quality_ok: bool
    reasons: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "conservative_fills": self.conservative_fills,
            "diagnostic_fill_target": self.diagnostic_fill_target,
            "markout_30s_n": self.markout_30s_n,
            "markout_60s_n": self.markout_60s_n,
            "completed_cycles": self.completed_cycles,
            "state_observations": self.state_observations,
            "inventory_observed": self.inventory_observed,
            "stability_ok": self.stability_ok,
            "data_quality_ok": self.data_quality_ok,
            "reasons": list(self.reasons),
        }


def assess_economic_evidence(
    metrics: Mapping[str, Any],
    markout_rows: Sequence[Mapping[str, Any]],
    elapsed_seconds: float,
    *,
    policy: Stage14Config | None = None,
    stability_ok: bool = True,
    data_quality_ok: bool = True,
) -> Stage14Evidence:
    """Classify evidence without treating a fill target as scientific truth."""

    selected = policy or Stage14Config()
    conservative_rows = [
        row for row in markout_rows if str(row.get("model", "")).upper() == CONSERVATIVE_MODEL
    ]
    markout_counts = {
        horizon: sum(
            row.get("status") == "COMPLETE" and _number(row.get("markout_bps")) is not None
            for row in conservative_rows
            if int(row.get("horizon_seconds", 0) or 0) == horizon
        )
        for horizon in STAGE14_MARKOUT_HORIZONS_SECONDS
    }
    fills = int(_number(metrics.get("fills"), 0.0) or 0)
    cycles = int(_number(metrics.get("completed_cycles"), 0.0) or 0)
    states = int(_number(metrics.get("state_observation_count"), 0.0) or 0)
    inventory_observed = bool(
        metrics.get("inventory_by_asset") or metrics.get("average_absolute_inventory") is not None
    )
    reasons: list[str] = []
    if elapsed_seconds < selected.minimum_duration_seconds:
        reasons.append("MINIMUM_2_HOUR_WINDOW_NOT_REACHED")
    if fills < selected.diagnostic_fill_target:
        reasons.append("CONSERVATIVE_FILL_TARGET_NOT_MET")
    if markout_counts[30] < selected.minimum_markout_samples:
        reasons.append("30S_MARKOUT_SAMPLE_NOT_MEANINGFUL")
    if markout_counts[60] < selected.minimum_markout_samples:
        reasons.append("60S_MARKOUT_SAMPLE_NOT_MEANINGFUL")
    if cycles < selected.minimum_cycle_samples:
        reasons.append("COMPLETED_CYCLE_SAMPLE_NOT_MEANINGFUL")
    if states <= 0:
        reasons.append("STATE_OBSERVATION_NOT_AVAILABLE")
    if not inventory_observed:
        reasons.append("INVENTORY_OBSERVATION_NOT_AVAILABLE")
    if not stability_ok:
        reasons.append("STABILITY_REGRESSION")
    if not data_quality_ok:
        reasons.append("DATA_QUALITY_NOT_HEALTHY")
    sufficient = not reasons
    observed_anything = bool(fills or cycles or states or markout_counts[30] or markout_counts[60])
    status = (
        "SUFFICIENT_FOR_DIAGNOSIS"
        if sufficient
        else "DEVELOPING"
        if observed_anything
        else "INSUFFICIENT"
    )
    return Stage14Evidence(
        status=status,
        elapsed_seconds=max(0.0, elapsed_seconds),
        conservative_fills=fills,
        diagnostic_fill_target=selected.diagnostic_fill_target,
        markout_30s_n=markout_counts[30],
        markout_60s_n=markout_counts[60],
        completed_cycles=cycles,
        state_observations=states,
        inventory_observed=inventory_observed,
        stability_ok=stability_ok,
        data_quality_ok=data_quality_ok,
        reasons=tuple(reasons),
    )


def validate_stage13_reference(config: ShadowConfig, project_root: str | Path) -> dict[str, Any]:
    """Fail closed when the current profile differs from the validated Stage 13 record."""

    root = Path(project_root).expanduser().resolve()
    diff_path = root / "reports" / "stage13" / "config_diff.md"
    if not diff_path.is_file():
        return {"status": "SKIPPED", "reason": "Stage 13 config_diff.md is not present"}
    try:
        raw_diff = diff_path.read_text(encoding="utf-8")
        code_block = re.search(r"```json\s*(\{.*?\})\s*```", raw_diff, re.DOTALL)
        record = json.loads(code_block.group(1) if code_block else raw_diff)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Stage 14 requires a readable Stage 13 config_diff.md") from exc
    expected_behavior = record.get("stage13_strategy_behavior_hash")
    expected_config = record.get("stage13_config_hash")
    if expected_behavior and config.strategy_config_hash != expected_behavior:
        raise ValueError("Stage 14 Stage 13 behavior hash does not match the validated profile")
    if expected_config and config.config_hash != expected_config:
        raise ValueError("Stage 14 config hash does not match the validated Stage 13 profile")
    if record.get("strategy_parameters_changed") is True:
        raise ValueError("Stage 14 cannot run because Stage 13 strategy parameters changed")
    expected_status = (record.get("stage13_profile") or {}).get("asset_execution_status") or {}
    actual_status = dict(config.stage13.asset_execution_status)
    if expected_status and actual_status != expected_status:
        raise ValueError("Stage 14 asset execution status differs from validated Stage 13")
    return {
        "status": "PASS",
        "config_hash": config.config_hash,
        "stage13_behavior_hash": config.strategy_config_hash,
        "stage13_config_diff": str(diff_path),
    }


class Stage14EconomicValidator:
    """Observe a :class:`ShadowBaselineSession` and write Stage 14 evidence."""

    def __init__(
        self,
        session: ShadowBaselineSession,
        *,
        profile_path: str | Path,
        project_root: str | Path | None = None,
        policy: Stage14Config | None = None,
        stage13_reference: Mapping[str, Any] | None = None,
    ) -> None:
        self.session = session
        self.config = session.config
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else session.project_root
        )
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.policy = policy or Stage14Config()
        self.stage13_reference = dict(stage13_reference or {})
        self.root = self.project_root / "reports" / "stage14" / session.session_id
        self.manifest_path = self.root / "manifest.json"
        self.started_epoch: float | None = None
        self._next_checkpoint_epoch: float | None = None
        self._hourly_rows: list[dict[str, Any]] = []
        self._last_snapshot: dict[str, Any] | None = None

    @property
    def data_dir(self) -> Path:
        return _resolve_path(self.config.sqlite_path, self.project_root).parent

    def _asset_status(self) -> dict[str, str]:
        configured = dict(self.config.stage13.asset_execution_status)
        return {pair: configured.get(pair, "UNKNOWN") for pair in self.config.markets}

    def _manifest(self, status: str, reason: str | None = None) -> dict[str, Any]:
        stage13 = self.config.stage13.model_dump(mode="python")
        assets = self._asset_status()
        values: dict[str, Any] = {
            "manifest_type": "STAGE14_ECONOMIC_VALIDATION",
            "stage": "STAGE14",
            "status": status,
            "session_id": self.session.session_id,
            "resolved_profile_path": str(self.profile_path),
            "source_profile_hash": _file_hash(self.profile_path),
            "full_config_hash": self.config.config_hash,
            "config_hash": self.config.config_hash,
            "stage13_behavior_hash": self.config.strategy_config_hash,
            "stage13_strategy_behavior_hash": self.config.strategy_config_hash,
            "stage13_reference": self.stage13_reference,
            "git_commit": _git_commit(self.project_root),
            "starting_paper_equity": self.config.starting_equity_usdc,
            "markets": list(self.config.markets),
            "execution_enabled_assets": [
                pair for pair, value in assets.items() if value == "EXECUTION_ENABLED"
            ],
            "signal_only_assets": [
                pair for pair, value in assets.items() if value != "EXECUTION_ENABLED"
            ],
            "asset_execution_status": assets,
            "order_sizing": {
                "order_scale": self.config.order_scale,
                "min_order_size": self.config.min_order_size,
                "amount_increment": self.config.amount_increment,
                "price_increment": self.config.price_increment,
                "min_notional_size": self.config.min_notional_size,
                "execution_max_levels_per_side": self.config.execution_max_levels_per_side,
            },
            "quote_settings": {
                "minimum_order_lifetime_seconds": self.config.minimum_order_lifetime_seconds,
                "minimum_replace_interval_seconds": self.config.minimum_replace_interval_seconds,
                "maximum_order_lifetime_seconds": self.config.maximum_order_lifetime_seconds,
                "refresh_price_tolerance_bps": self.config.refresh_price_tolerance_bps,
                "refresh_amount_tolerance_pct": self.config.refresh_amount_tolerance_pct,
            },
            "risk_settings": {
                "max_total_position_notional": self.config.max_total_position_notional,
                "max_side_position_notional": self.config.max_side_position_notional,
                "max_active_grid_levels": self.config.max_active_grid_levels,
                "max_active_executors": self.config.max_active_executors,
                "collateral_safety_buffer_pct": self.config.collateral_safety_buffer_pct,
                "inventory_soft_threshold_ratio": self.config.inventory_soft_threshold_ratio,
                "inventory_defensive_threshold_ratio": (
                    self.config.inventory_defensive_threshold_ratio
                ),
                "inventory_hard_threshold_ratio": self.config.inventory_hard_threshold_ratio,
                "leverage": self.config.leverage,
            },
            "pause_settings": stage13,
            "fill_models": {
                "primary": ShadowFillModel.CONSERVATIVE_TRADE_THROUGH.value,
                "sensitivity": ShadowFillModel.TOUCH_OPTIMISTIC.value,
            },
            "self_tuning_mode": self.config.self_tuning_mode.upper(),
            "self_tuning_applications": 0,
            "market_environment": self.config.market_environment,
            "execution_backend": self.config.execution_backend,
            "execution_mode": self.config.execution_mode,
            "execution_enabled": self.config.execution_enabled,
            "allow_mainnet_trading": self.config.allow_mainnet_trading,
            "post_only": self.config.post_only,
            "private_derive_trading_client": "NOT_ENABLED",
            "real_exchange_mutation_calls": 0,
            "data_dir": str(self.data_dir),
            "sqlite_path": str(_resolve_path(self.config.sqlite_path, self.project_root)),
            "event_path": str(_resolve_path(self.config.event_path, self.project_root)),
            "base_report_root": str(_resolve_path(self.config.report_root, self.project_root)),
            "stage14_report_root": str(self.root),
            "policy": {
                "minimum_duration_seconds": self.policy.minimum_duration_seconds,
                "maximum_duration_seconds": self.policy.maximum_duration_seconds,
                "checkpoint_interval_seconds": self.policy.checkpoint_interval_seconds,
                "diagnostic_fill_target": self.policy.diagnostic_fill_target,
                "minimum_markout_samples": self.policy.minimum_markout_samples,
                "minimum_cycle_samples": self.policy.minimum_cycle_samples,
            },
        }
        if reason is not None:
            values["reason"] = reason
        return values

    def prepare(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest("READY")
        _write_json(self.manifest_path, manifest)
        return manifest

    def start(self, timestamp: float | None = None) -> None:
        now = timestamp if timestamp is not None else self.session._start_epoch or time.time()
        self.started_epoch = now
        self._next_checkpoint_epoch = now + self.policy.checkpoint_interval_seconds
        _write_json(self.manifest_path, self._manifest("RUNNING"))
        self._write_live_snapshot(now)

    def _elapsed(self, now: float) -> float:
        start = self.started_epoch or self.session._start_epoch or now
        return max(0.0, now - start)

    def _markout_rows(self) -> list[dict[str, Any]]:
        frames_by_pair: dict[str, list[Any]] = defaultdict(list)
        for frame in self.session._frame_history:
            frames_by_pair[frame.trading_pair].append(frame)
        for rows in frames_by_pair.values():
            rows.sort(key=lambda frame: frame.timestamp)
        stop_epoch = self.session._stop_epoch or time.time()
        result: list[dict[str, Any]] = []
        for model, model_session in self.session.sessions.items():
            for fill in model_session.engine.fills:
                fill_row = fill.to_record()
                fill_timestamp = _number(fill.timestamp_epoch)
                if fill_timestamp is None:
                    continue
                candidates = frames_by_pair.get(fill.trading_pair, [])
                for horizon in STAGE14_MARKOUT_HORIZONS_SECONDS:
                    target = fill_timestamp + horizon
                    future = next(
                        (
                            frame
                            for frame in candidates
                            if frame.timestamp > fill_timestamp
                            and frame.timestamp >= target
                            and frame.mid_price > 0
                        ),
                        None,
                    )
                    if future is not None:
                        status = "COMPLETE"
                        markout = _directional_markout(fill_row, future.mid_price)
                        future_timestamp = future.timestamp
                    elif target > stop_epoch:
                        status = "MISSING_SESSION_END"
                        markout = None
                        future_timestamp = None
                    else:
                        status = "MISSING_DATA"
                        markout = None
                        future_timestamp = None
                    result.append(
                        {
                            "fill_id": fill.fill_id,
                            "model": model,
                            "fill_model": fill.fill_model,
                            "trading_pair": fill.trading_pair,
                            "side": fill.side,
                            "entry_exit": fill.entry_exit,
                            "fill_timestamp": fill.timestamp,
                            "fill_timestamp_epoch": fill_timestamp,
                            "horizon_seconds": horizon,
                            "target_timestamp_epoch": target,
                            "future_timestamp_epoch": future_timestamp,
                            "price": fill.price,
                            "amount": fill.amount,
                            "notional": fill.notional,
                            "markout_bps": markout,
                            "status": status,
                            "eligible": status == "COMPLETE" and markout is not None,
                            "mode": fill.mode,
                            "global_iv_regime": fill.global_iv_regime,
                            "quote_distance_bps": fill.quote_distance_bps,
                            "evidence": fill.evidence,
                        }
                    )
        return result

    @staticmethod
    def _markout_summary(
        rows: Sequence[Mapping[str, Any]], model: str
    ) -> dict[str, dict[str, Any]]:
        selected = [row for row in rows if str(row.get("model", "")).upper() == model]
        summary: dict[str, dict[str, Any]] = {}
        for horizon in STAGE14_MARKOUT_HORIZONS_SECONDS:
            horizon_rows = [
                row for row in selected if int(row.get("horizon_seconds", 0) or 0) == horizon
            ]
            values = [
                value
                for row in horizon_rows
                if row.get("status") == "COMPLETE"
                and (value := _number(row.get("markout_bps"))) is not None
            ]
            summary[f"{horizon}s"] = {
                **_markout_stats(values),
                "sample_count": len(values),
                "eligible_count": sum(row.get("eligible") is True for row in horizon_rows),
                "missing_count": sum(row.get("status") != "COMPLETE" for row in horizon_rows),
            }
        return summary

    @staticmethod
    def _markout_breakdown(
        rows: Sequence[Mapping[str, Any]], model: str
    ) -> dict[str, dict[str, dict[str, Any]]]:
        selected = [row for row in rows if str(row.get("model", "")).upper() == model]
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for dimension in ("trading_pair", "side", "mode", "global_iv_regime"):
            grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
            for row in selected:
                if row.get("status") != "COMPLETE":
                    continue
                value = _number(row.get("markout_bps"))
                if value is None:
                    continue
                label = str(row.get(dimension) or "UNKNOWN").upper()
                horizon = f"{int(row.get('horizon_seconds', 0) or 0)}s"
                grouped[label][horizon].append(value)
            result[dimension] = {
                label: {horizon: _markout_stats(values) for horizon, values in horizons.items()}
                for label, horizons in grouped.items()
            }
        return result

    def _model_item(self, model: str, end: float) -> Any:
        return self.session._model_metrics(model, end)

    def _risk_snapshot(self, model: str, metrics: Mapping[str, Any], end: float) -> dict[str, Any]:
        points = self.session.exposure[model].points
        pending_values = [_number(row.get("resting_quote_exposure"), 0.0) or 0.0 for row in points]
        worst_values = [
            (_number(row.get("gross_exposure"), 0.0) or 0.0)
            + (_number(row.get("resting_quote_exposure"), 0.0) or 0.0)
            for row in points
        ]
        return {
            "average_filled_gross": metrics.get("average_gross_exposure"),
            "max_filled_gross": metrics.get("max_gross_exposure"),
            "average_pending_reserved_gross": metrics.get("average_resting_quote_exposure"),
            "max_pending_reserved_gross": max(pending_values, default=None),
            "average_worst_case_gross": _time_weighted_average(
                [
                    {**row, "worst_case_gross": worst}
                    for row, worst in zip(points, worst_values, strict=False)
                ],
                "worst_case_gross",
                self.session._start_epoch,
                end,
            ),
            "max_worst_case_gross": max(worst_values, default=None),
            "average_inventory": metrics.get("average_absolute_inventory"),
            "max_inventory": metrics.get("max_inventory"),
            "average_btc_beta": metrics.get("average_btc_beta_exposure"),
            "max_btc_beta": metrics.get("max_btc_beta_exposure"),
        }

    def _cycle_stats(self, model: str, end: float) -> dict[str, Any]:
        item = self._model_item(model, end)
        completed = [row for row in item.cycles if row.get("status") == "COMPLETE"]
        durations = [
            value
            for row in completed
            if (value := _number(row.get("cycle_duration_seconds"))) is not None
        ]
        stats = _lifetime_stats(durations)
        stats["completed_cycles"] = len(completed)
        stats["gross_capture_per_cycle"] = _mean(
            [_number(row.get("realized_capture"), 0.0) or 0.0 for row in completed]
        )
        return stats

    def _quote_lifetime(self, model: str, end: float) -> dict[str, Any]:
        item = self._model_item(model, end)
        values = [
            _number(row.get("resting_lifetime_seconds"))
            for row in item.orders
            if _number(row.get("resting_lifetime_seconds")) is not None
            and row.get("lifecycle_state") != "NEVER_RESTED_REJECTED"
        ]
        return {
            **_lifetime_stats(values),
            "max": max(values, default=None),
            "buckets": {
                bucket: sum(_lifetime_bucket(value) == bucket for value in values)
                for bucket in STAGE14_LIFETIME_BUCKETS
            },
        }

    def _keep_summary(self, model: str) -> dict[str, Any]:
        events = self.session.sessions[model].engine.events
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for event in events:
            if event.get("level_id") and event.get("trading_pair"):
                grouped[(str(event["trading_pair"]), str(event["level_id"]))].append(event)
        streaks: list[int] = []
        durations: list[float] = []
        for rows in grouped.values():
            current = 0
            first: float | None = None
            last: float | None = None
            for event in sorted(
                rows, key=lambda row: _number(row.get("timestamp_epoch"), 0.0) or 0.0
            ):
                timestamp = _number(event.get("timestamp_epoch"))
                if event.get("event") == "ORDER_KEEP":
                    current += 1
                    first = timestamp if first is None else first
                    last = timestamp
                    continue
                if current:
                    streaks.append(current)
                    if first is not None and last is not None:
                        durations.append(max(0.0, last - first))
                current = 0
                first = None
                last = None
            if current:
                streaks.append(current)
                if first is not None and last is not None:
                    durations.append(max(0.0, last - first))
        return {
            "keep_count": sum(event.get("event") == "ORDER_KEEP" for event in events),
            "keep_streak_count": len(streaks),
            "max_consecutive_keep_streak": max(streaks, default=0),
            "median_consecutive_keep_streak": _percentile(streaks, 0.5),
            "median_keep_duration_seconds": _percentile(durations, 0.5),
        }

    def _stage13(self, base_summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if base_summary and isinstance(base_summary.get("stage13"), Mapping):
            return dict(base_summary["stage13"])
        if isinstance(getattr(self.session, "_stage13_summary", None), Mapping):
            return dict(self.session._stage13_summary or {})
        return {}

    def _stability(self, metrics: Mapping[str, Any], stage13: Mapping[str, Any]) -> dict[str, Any]:
        same_frame = _number((stage13.get("same_frame_cancel") or {}).get("count"), 0.0)
        self_invalidation = _number(
            (stage13.get("risk_reservation") or {}).get("self_invalidation_events"), 0.0
        )
        health = metrics.get("health_checks") or {}
        regression = bool(
            same_frame or self_invalidation or health.get("ORDER STABILITY") == "FAIL"
        )
        return {
            "status": "FAIL" if regression else "PASS",
            "same_frame_cancels": int(same_frame or 0),
            "pending_risk_self_invalidation": int(self_invalidation or 0),
            "stage13_available": bool(stage13),
            "stage13_same_frame_status": "UNKNOWN"
            if not stage13
            else "PASS"
            if not same_frame
            else "FAIL",
            "stage13_pending_risk_status": (
                "UNKNOWN" if not stage13 else "PASS" if not self_invalidation else "FAIL"
            ),
        }

    def _data_quality(self, metrics: Mapping[str, Any]) -> dict[str, Any]:
        health = metrics.get("health_checks") or {}
        coverage = metrics.get("trade_coverage") or {}
        overall = coverage.get("overall") or {}
        return {
            "status": "PASS"
            if health.get("MAINNET PUBLIC DATA") == "PASS" and health.get("DATA QUALITY") == "PASS"
            else "FAIL",
            "bbo": metrics.get("data_quality", {}).get("ticker"),
            "btc_iv": metrics.get("data_quality", {}).get("options"),
            "trade_stream": metrics.get("data_quality", {}).get("trade_stream"),
            "overall_trade_coverage_pct": overall.get("coverage_pct"),
            "order_level_trade_evidence": metrics.get("fill_eligibility"),
            "trade_coverage": coverage,
            "health_checks": health,
        }

    def _risk(self, metrics: Mapping[str, Any], stage13: Mapping[str, Any]) -> dict[str, Any]:
        counts = metrics.get("risk_block_counts") or {}
        hard_categories = {
            "ASSET_INVENTORY_RISK",
            "PORTFOLIO_GROSS_RISK",
            "PORTFOLIO_BETA_RISK",
            "DRAWDOWN_RISK",
            "COLLATERAL_RESERVE",
        }
        hard_attempts = sum(
            int(_number(counts.get(category), 0.0) or 0) for category in hard_categories
        )
        return {
            "raw_checks": metrics.get("risk_checks_total"),
            "risk_blocks": metrics.get("risk_blocks"),
            "risk_blocks_raw": metrics.get("risk_blocks_raw"),
            "risk_block_counts": counts,
            "unique_risk_episodes": metrics.get("unique_risk_episodes"),
            "blocked_duration_seconds": metrics.get("duration_blocked_seconds"),
            "hard_limit_attempts": hard_attempts,
            "hard_risk_breaches": 0 if hard_attempts == 0 else hard_attempts,
            "risk_reducing_side": {
                "status": "NOT_SEPARATELY_INSTRUMENTED",
                "verified": False,
            },
            "episodes": metrics.get("risk_episode_summary") or [],
            "stage13_reservation": stage13.get("risk_reservation") or {},
        }

    def _capital(
        self, metrics: Mapping[str, Any], cycle_stats: Mapping[str, Any]
    ) -> dict[str, Any]:
        base = dict(metrics.get("capital_recycling") or {})
        durations = [
            _number(row.get("cycle_duration_seconds"))
            for row in self.session.sessions[CONSERVATIVE_MODEL].cycles
            if row.get("status") == "COMPLETE"
            and _number(row.get("cycle_duration_seconds")) is not None
        ]
        entry_fills = int(_number(metrics.get("entry_fills"), 0.0) or 0)
        base["percentage_inventory_closed_within_2h"] = (
            sum(value <= 7200 for value in durations) / entry_fills * 100.0 if entry_fills else None
        )
        positions = self.session.sessions[CONSERVATIVE_MODEL].engine.ledger.positions
        open_positions = [
            {
                "trading_pair": pair,
                "amount": position.amount,
                "average_entry_price": position.average_entry_price,
            }
            for pair, position in positions.items()
            if position.amount != 0
        ]
        base.update(
            {
                "open_position_count": len(open_positions),
                "open_positions": open_positions,
                "completed_cycles": cycle_stats.get("completed_cycles", 0),
                "median_cycle_duration_seconds": cycle_stats.get("median"),
                "p75_cycle_duration_seconds": cycle_stats.get("p75"),
                "p90_cycle_duration_seconds": cycle_stats.get("p90"),
                "cycle_gross_capture_per_cycle": cycle_stats.get("gross_capture_per_cycle"),
            }
        )
        return base

    @staticmethod
    def _ratio(numerator: Any, denominator: Any) -> float | None:
        top = _number(numerator)
        bottom = _number(denominator)
        return (
            top / bottom if top is not None and bottom is not None and abs(bottom) > 1e-12 else None
        )

    def _volume_risk(
        self, metrics: Mapping[str, Any], risk_snapshot: Mapping[str, Any], elapsed: float
    ) -> dict[str, Any]:
        volume = _number(metrics.get("total_executed_notional"), 0.0) or 0.0
        average_filled = risk_snapshot.get("average_filled_gross")
        average_worst = risk_snapshot.get("average_worst_case_gross")
        average_inventory = risk_snapshot.get("average_inventory")
        average_margin = metrics.get("average_margin_used")
        ratios = {
            "volume_per_starting_equity": self._ratio(volume, self.config.starting_equity_usdc),
            "volume_per_average_filled_gross": self._ratio(volume, average_filled),
            "volume_per_average_worst_case_gross": self._ratio(volume, average_worst),
            "volume_per_average_inventory": self._ratio(volume, average_inventory),
            "volume_per_estimated_margin_used": self._ratio(volume, average_margin),
            "volume_per_hour": self._ratio(volume, elapsed / 3600.0),
        }
        return {
            "executed_volume": volume,
            "buy_volume": metrics.get("buy_executed_notional"),
            "sell_volume": metrics.get("sell_executed_notional"),
            "volume_by_asset": metrics.get("volume_by_asset") or {},
            "average_filled_gross": average_filled,
            "max_filled_gross": risk_snapshot.get("max_filled_gross"),
            "average_pending_reserved_gross": risk_snapshot.get("average_pending_reserved_gross"),
            "max_pending_reserved_gross": risk_snapshot.get("max_pending_reserved_gross"),
            "average_worst_case_gross": average_worst,
            "max_worst_case_gross": risk_snapshot.get("max_worst_case_gross"),
            "average_inventory": average_inventory,
            "max_inventory": risk_snapshot.get("max_inventory"),
            "average_btc_beta": risk_snapshot.get("average_btc_beta"),
            "max_btc_beta": risk_snapshot.get("max_btc_beta"),
            "ratios": ratios,
            "undefined_ratio_fields": [key for key, value in ratios.items() if value is None],
        }

    def _order_execution(
        self,
        metrics: Mapping[str, Any],
        stage13: Mapping[str, Any],
        quote_lifetime: Mapping[str, Any],
        keep: Mapping[str, Any],
    ) -> dict[str, Any]:
        funnel = stage13.get("order_funnel") or {}
        reconciliation = stage13.get("create_decision_reconciliation") or {}
        lifecycle = metrics.get("lifecycle_states") or {}
        resting_fallback = sum(
            int(_number(lifecycle.get(state), 0.0) or 0)
            for state in ("RESTING", "CANCELLED_AFTER_RESTING", "FILLED_AFTER_RESTING", "COMPLETE")
        )
        raw_candidates = funnel.get("candidate_grid_levels")
        if raw_candidates is None:
            raw_candidates = funnel.get(
                "create_decisions", reconciliation.get("raw_create_decisions")
            )
        instantiated = funnel.get(
            "shadow_order_objects_instantiated",
            reconciliation.get("instantiated", metrics.get("orders_created")),
        )
        entered_resting = funnel.get("entered_resting", resting_fallback)
        return {
            "raw_candidate_evaluations": raw_candidates,
            "risk_eligible": funnel.get("risk_eligible"),
            "already_active": funnel.get("validated"),
            "keep": metrics.get("orders_kept", keep.get("keep_count")),
            "keep_rate_pct": metrics.get("keep_pct"),
            "keep_streaks": keep,
            "signal_only": funnel.get("signal_only"),
            "pause_suppressed": funnel.get("pause_suppressed"),
            "risk_blocked": funnel.get("risk_blocked"),
            "minimum_size_blocked": funnel.get("minimum_size_blocked"),
            "actual_instantiated_orders": instantiated,
            "entered_resting": entered_resting,
            "resting_orders": metrics.get("active_orders"),
            "operational_cancels": metrics.get("operational_cancels"),
            "shutdown_cancels": metrics.get("shutdown_cancels"),
            "replacements": metrics.get("orders_replaced"),
            "fills": metrics.get("fills"),
            "entry_fills": metrics.get("entry_fills"),
            "exit_fills": metrics.get("exit_fills"),
            "tp_orders_created": metrics.get("tp_orders_created"),
            "tp_orders_filled": metrics.get("tp_orders_filled"),
            "completed_cycles": metrics.get("completed_cycles"),
            "fill_create_ratio": metrics.get("fill_create_ratio"),
            "entry_fill_create_ratio": metrics.get("entry_fill_create_ratio"),
            "cancel_create_ratio": metrics.get("cancel_create_ratio"),
            "quote_lifetime": quote_lifetime,
            "same_frame_cancels": (stage13.get("same_frame_cancel") or {}).get("count"),
            "pending_risk_self_invalidation": (stage13.get("risk_reservation") or {}).get(
                "self_invalidation_events"
            ),
        }

    def _snapshot(
        self, now: float, *, base_summary: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        end = self.session._stop_epoch or now
        metrics = self.session.metrics(now=end)
        stage13 = self._stage13(base_summary)
        markout_rows = self._markout_rows()
        markout_summary = {
            model: self._markout_summary(markout_rows, model)
            for model in (CONSERVATIVE_MODEL, TOUCH_MODEL)
        }
        markout_breakdown = {
            model: self._markout_breakdown(markout_rows, model)
            for model in (CONSERVATIVE_MODEL, TOUCH_MODEL)
        }
        conservative_item = self._model_item(CONSERVATIVE_MODEL, end)
        touch_item = self._model_item(TOUCH_MODEL, end)
        conservative = conservative_item.metrics
        touch = touch_item.metrics
        stability = self._stability(metrics, stage13)
        data_quality = self._data_quality(metrics)
        evidence = assess_economic_evidence(
            metrics,
            markout_rows,
            self._elapsed(now),
            policy=self.policy,
            stability_ok=stability["status"] == "PASS",
            data_quality_ok=data_quality["status"] == "PASS",
        )
        quote_lifetime = self._quote_lifetime(CONSERVATIVE_MODEL, end)
        keep = self._keep_summary(CONSERVATIVE_MODEL)
        cycle_stats = self._cycle_stats(CONSERVATIVE_MODEL, end)
        risk_snapshot = self._risk_snapshot(CONSERVATIVE_MODEL, conservative, end)
        capital = self._capital(metrics, cycle_stats)
        risk = self._risk(metrics, stage13)
        volume_risk = self._volume_risk(metrics, risk_snapshot, self._elapsed(now))
        order_execution = self._order_execution(metrics, stage13, quote_lifetime, keep)
        return {
            "timestamp": _iso(now),
            "elapsed_seconds": self._elapsed(now),
            "metrics": metrics,
            "markout_rows": markout_rows,
            "markout": markout_summary,
            "markout_breakdown": markout_breakdown,
            "conservative": conservative,
            "touch": touch,
            "stability": stability,
            "data_quality": data_quality,
            "evidence": evidence,
            "quote_lifetime": quote_lifetime,
            "keep": keep,
            "cycle_stats": cycle_stats,
            "capital": capital,
            "risk_snapshot": risk_snapshot,
            "volume_risk": volume_risk,
            "risk": risk,
            "order_execution": order_execution,
            "stage13": stage13,
            "touch_sensitivity": metrics.get("fill_model_sensitivity", "UNKNOWN"),
        }

    def _compact_metrics(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        conservative = snapshot["conservative"]
        touch = snapshot["touch"]
        return {
            "elapsed_seconds": snapshot["elapsed_seconds"],
            "conservative_fills": conservative.get("fills"),
            "touch_fills": touch.get("fills"),
            "conservative_volume": conservative.get("total_executed_notional"),
            "touch_volume": touch.get("total_executed_notional"),
            "completed_cycles": conservative.get("completed_cycles"),
            "executed_volume": conservative.get("total_executed_notional"),
            "volume_per_average_deployed_risk": (
                conservative.get("volume_per_average_deployed_risk")
            ),
            "average_inventory": conservative.get("average_absolute_inventory"),
            "max_inventory": conservative.get("max_inventory"),
            "gross_pnl": conservative.get("gross_pnl"),
            "max_drawdown": conservative.get("max_drawdown_quote"),
            "orders_created": conservative.get("orders_created"),
            "resting_orders": conservative.get("active_orders"),
            "keep": conservative.get("orders_kept"),
            "operational_cancels": conservative.get("operational_cancels"),
            "fill_create_ratio": conservative.get("fill_create_ratio"),
            "cancel_create_ratio": conservative.get("cancel_create_ratio"),
            "markout": snapshot["markout"][CONSERVATIVE_MODEL],
            "evidence_status": snapshot["evidence"].status,
            "data_quality_status": snapshot["data_quality"]["status"],
            "fill_model_sensitivity": snapshot["touch_sensitivity"],
        }

    def _build_summary(
        self,
        now: float,
        *,
        final: bool,
        reason: str | None = None,
        base_summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self._snapshot(now, base_summary=base_summary)
        metrics = snapshot["metrics"]
        evidence: Stage14Evidence = snapshot["evidence"]
        conservative = snapshot["conservative"]
        touch = snapshot["touch"]
        stage13 = snapshot["stage13"]
        safety = {
            "market_environment": self.config.market_environment,
            "mainnet_public_data": self.config.market_environment.lower() == "mainnet",
            "execution_mode": self.config.execution_mode,
            "execution_backend": self.config.execution_backend,
            "shadow_only": self.config.execution_mode.upper() == "SHADOW"
            and self.config.execution_backend.upper() == "SHADOW",
            "execution_enabled": self.config.execution_enabled,
            "allow_mainnet_trading": self.config.allow_mainnet_trading,
            "private_derive_trading_client": "NOT_ENABLED",
            "real_exchange_mutation_calls": 0,
            "config_frozen": not self.session.config_contaminated,
            "self_tuning_applications": metrics.get("self_tuning_applications", 0),
            "status": "PASS"
            if self.config.market_environment.lower() == "mainnet"
            and self.config.execution_mode.upper() == "SHADOW"
            and self.config.execution_backend.upper() == "SHADOW"
            and not self.config.execution_enabled
            and not self.config.allow_mainnet_trading
            and not self.session.config_contaminated
            and metrics.get("self_tuning_applications", 0) == 0
            else "FAIL",
        }
        data_quality_fail = snapshot["data_quality"]["status"] != "PASS"
        stage13_regression = snapshot["stability"]["status"] != "PASS"
        adverse = self._adverse_selection(snapshot["markout"][CONSERVATIVE_MODEL])
        capital_lockup = bool(
            conservative.get("fills", 0)
            and conservative.get("completed_cycles", 0) == 0
            and (
                (_number(snapshot["capital"].get("max_open_position_age_seconds"), 0.0) or 0.0)
                >= 1800
                or (
                    _number(snapshot["capital"].get("percentage_session_with_open_inventory"), 0.0)
                    or 0.0
                )
                >= 50
            )
        )
        if not final:
            classification = "IN_PROGRESS"
            weakness = "EVIDENCE_COLLECTION"
            readiness = {
                "ready_for_bounded_economic_optimization": "NO",
                "ready_for_tiny_live_money_canary_review": "NO",
                "reasons": ["Stage 14 is still collecting frozen shadow evidence"],
            }
        else:
            classification, weakness = self._classification(
                evidence=evidence,
                conservative=conservative,
                touch=touch,
                snapshot=snapshot,
                safety=safety,
                data_quality_fail=data_quality_fail,
                stage13_regression=stage13_regression,
                adverse=adverse,
                capital_lockup=capital_lockup,
                reason=reason,
            )
            readiness = self._readiness(
                classification=classification,
                evidence=evidence,
                snapshot=snapshot,
                safety=safety,
                adverse=adverse,
            )
        summary = {
            "stage": "STAGE14",
            "title": "EVIDENCE-BASED ECONOMIC SHADOW VALIDATION",
            "status": "COMPLETE" if final else "RUNNING",
            "session_id": self.session.session_id,
            "start_timestamp": self.session.start_timestamp,
            "end_timestamp": self.session.stop_timestamp or _iso(now),
            "duration_seconds": snapshot["elapsed_seconds"],
            "duration_hours": snapshot["elapsed_seconds"] / 3600.0,
            "early_evidence_completion": final and reason == "EARLY_EVIDENCE_SUFFICIENT",
            "stop_reason": reason or self.session.stop_reason,
            "why_stopped": reason or "EVIDENCE_COLLECTION_IN_PROGRESS",
            "resolved_profile_path": str(self.profile_path),
            "config_hash": self.config.config_hash,
            "full_config_hash": self.config.config_hash,
            "stage13_behavior_hash": self.config.strategy_config_hash,
            "stage13_strategy_behavior_hash": self.config.strategy_config_hash,
            "git_commit": _git_commit(self.project_root),
            "config_frozen": not self.session.config_contaminated,
            "safety": safety,
            "asset_execution_status": self._asset_status(),
            "order_execution": snapshot["order_execution"],
            "fill_quality": {
                "primary_model": ShadowFillModel.CONSERVATIVE_TRADE_THROUGH.value,
                "sensitivity_model": ShadowFillModel.TOUCH_OPTIMISTIC.value,
                "conservative_fills": conservative.get("fills"),
                "conservative_volume": conservative.get("total_executed_notional"),
                "touch_fills": touch.get("fills"),
                "touch_volume": touch.get("total_executed_notional"),
                "sensitivity": snapshot["touch_sensitivity"],
                "adverse_selection": adverse,
                "markouts": snapshot["markout"][CONSERVATIVE_MODEL],
                "markouts_by_asset_side_mode_regime": snapshot["markout_breakdown"][
                    CONSERVATIVE_MODEL
                ],
            },
            "capital_recycling": snapshot["capital"],
            "inventory": {
                "by_asset": metrics.get("inventory_by_asset") or {},
                "average_absolute_inventory": conservative.get("average_absolute_inventory"),
                "max_inventory": conservative.get("max_inventory"),
                "soft_threshold_ratio": self.config.inventory_soft_threshold_ratio,
                "defensive_threshold_ratio": self.config.inventory_defensive_threshold_ratio,
                "hard_threshold_ratio": self.config.inventory_hard_threshold_ratio,
            },
            "volume_risk": snapshot["volume_risk"],
            "risk": snapshot["risk"],
            "economics": {
                "starting_paper_equity": conservative.get("starting_equity"),
                "ending_gross_paper_equity": conservative.get("ending_equity"),
                "gross_realized_capture": conservative.get("realized_grid_capture"),
                "gross_realized_pnl": conservative.get("realized_pnl"),
                "unrealized_inventory_pnl": conservative.get("unrealized_inventory_pnl"),
                "gross_total_pnl": conservative.get("gross_pnl"),
                "fee_model": conservative.get("fees_status", "UNKNOWN"),
                "fees": conservative.get("fees"),
                "verified_net_pnl": conservative.get("verified_net_pnl"),
                "verified_net_pnl_status": conservative.get("verified_net_pnl_status", "UNKNOWN"),
                "fee_sensitivity": conservative.get("fee_sensitivity"),
                "max_drawdown": conservative.get("max_drawdown_quote"),
                "max_drawdown_pct": conservative.get("max_drawdown_pct"),
                "worst_paper_equity": conservative.get("worst_paper_equity"),
                "time_underwater_seconds": conservative.get("time_underwater_seconds"),
                "pnl_reconciliation": conservative.get("pnl_reconciliation"),
                "pnl_reconciliation_status": conservative.get("pnl_reconciliation_status"),
            },
            "data_quality": snapshot["data_quality"],
            "stability": snapshot["stability"],
            "pause_hysteresis": stage13.get("pause_hysteresis") or {},
            "self_tuning": {
                "mode": self.config.self_tuning_mode.upper(),
                "applications": metrics.get("self_tuning_applications", 0),
                "suggestions": metrics.get("self_tuning_suggestions") or [],
            },
            "evidence": evidence.to_record(),
            "classification": classification,
            "primary_weakness": weakness,
            "readiness": readiness,
            "touch_optimistic": {
                "fills": touch.get("fills"),
                "volume": touch.get("total_executed_notional"),
                "cycles": touch.get("completed_cycles"),
                "pnl": touch.get("gross_pnl"),
                "drawdown": touch.get("max_drawdown_quote"),
                "average_inventory": touch.get("average_absolute_inventory"),
                "max_inventory": touch.get("max_inventory"),
                "markouts": snapshot["markout"][TOUCH_MODEL],
            },
            "metrics": self._compact_metrics(snapshot),
            "base_report_path": str(self.session.report_path) if self.session.report_path else None,
            "stage14_report_root": str(self.root),
            "required_files": list(STAGE14_REQUIRED_FILES),
        }
        return summary

    @staticmethod
    def _adverse_selection(markouts: Mapping[str, Mapping[str, Any]]) -> str:
        thirty = markouts.get("30s") or {}
        sixty = markouts.get("60s") or {}
        if (
            min(
                int(_number(thirty.get("sample_count"), 0.0) or 0),
                int(_number(sixty.get("sample_count"), 0.0) or 0),
            )
            < 5
        ):
            return "INSUFFICIENT_SAMPLE"
        thirty_mean = _number(thirty.get("mean_bps"))
        sixty_mean = _number(sixty.get("mean_bps"))
        if thirty_mean is None or sixty_mean is None:
            return "INSUFFICIENT_SAMPLE"
        if thirty_mean < 0 and sixty_mean < 0:
            return "TOXIC"
        if thirty_mean > 0 and sixty_mean > 0:
            return "HEALTHY"
        return "NEUTRAL"

    def _classification(
        self,
        *,
        evidence: Stage14Evidence,
        conservative: Mapping[str, Any],
        touch: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        safety: Mapping[str, Any],
        data_quality_fail: bool,
        stage13_regression: bool,
        adverse: str,
        capital_lockup: bool,
        reason: str | None,
    ) -> tuple[str, str]:
        if safety.get("status") != "PASS" or data_quality_fail:
            return "DATA_QUALITY_INSUFFICIENT", "DATA QUALITY / SHADOW SAFETY"
        if stage13_regression:
            return "MIXED", "STABILITY REGRESSION"
        if adverse == "TOXIC":
            return "ADVERSE_SELECTION", "ADVERSE SELECTION"
        if capital_lockup:
            return "CAPITAL_LOCKUP", "TP / CAPITAL RECYCLING"
        if (
            reason == "MAXIMUM_6_HOUR_WINDOW"
            and evidence.conservative_fills < self.policy.diagnostic_fill_target
        ):
            return "LOW_CONSERVATIVE_FILL_RATE", "QUOTE PLACEMENT / FILL PROBABILITY"
        if evidence.status != "SUFFICIENT_FOR_DIAGNOSIS":
            return "INSUFFICIENT_SAMPLE", "ECONOMIC EVIDENCE SAMPLE"
        if snapshot["touch_sensitivity"] == "HIGH":
            return "HIGH_FILL_MODEL_UNCERTAINTY", "FILL MODEL UNCERTAINTY"
        volume_risk = snapshot["volume_risk"]
        if volume_risk.get("executed_volume", 0) and volume_risk.get("undefined_ratio_fields") == [
            "volume_per_average_filled_gross",
            "volume_per_average_worst_case_gross",
            "volume_per_average_inventory",
            "volume_per_estimated_margin_used",
        ]:
            return "LOW_VOLUME_EFFICIENCY", "CAPITAL ALLOCATION"
        return "HEALTHY_SHADOW_ECONOMICS", "NONE — READY FOR LIVE CANARY REVIEW"

    def _readiness(
        self,
        *,
        classification: str,
        evidence: Stage14Evidence,
        snapshot: Mapping[str, Any],
        safety: Mapping[str, Any],
        adverse: str,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        stable = snapshot["stability"]["status"] == "PASS"
        controlled_inventory = (
            _number(snapshot["volume_risk"].get("max_inventory"), 0.0) or 0.0
        ) <= self.config.starting_equity_usdc * self.config.inventory_hard_threshold_ratio
        cycles = evidence.completed_cycles >= self.policy.minimum_cycle_samples
        fee_verified = snapshot["conservative"].get("verified_net_pnl_status") == "VERIFIED"
        base_conditions = (
            classification == "HEALTHY_SHADOW_ECONOMICS"
            and evidence.status == "SUFFICIENT_FOR_DIAGNOSIS"
            and stable
            and safety.get("status") == "PASS"
            and adverse in {"HEALTHY", "NEUTRAL"}
            and cycles
            and controlled_inventory
            and snapshot["touch_sensitivity"] != "HIGH"
        )
        if not evidence.status == "SUFFICIENT_FOR_DIAGNOSIS":
            reasons.append("economic evidence is not yet sufficient")
        if not fee_verified:
            reasons.append("fee model is UNKNOWN; verified net PnL is unavailable")
        if adverse not in {"HEALTHY", "NEUTRAL"}:
            reasons.append(f"adverse selection status is {adverse}")
        if not cycles:
            reasons.append("completed cycle sample is not sufficient")
        if not controlled_inventory:
            reasons.append("inventory is not controlled below the hard threshold")
        if snapshot["touch_sensitivity"] == "HIGH":
            reasons.append("fill-model sensitivity is HIGH")
        if not stable:
            reasons.append("Stage 13 stability regression is present")
        return {
            "ready_for_bounded_economic_optimization": "YES" if base_conditions else "NO",
            "ready_for_tiny_live_money_canary_review": (
                "YES" if base_conditions and fee_verified else "NO"
            ),
            "reasons": reasons,
            "positive_pnl_is_not_sufficient": True,
        }

    def _hourly_row(
        self, snapshot: Mapping[str, Any], model: str, checkpoint_type: str
    ) -> dict[str, Any]:
        metrics = snapshot["conservative"] if model == CONSERVATIVE_MODEL else snapshot["touch"]
        markouts = snapshot["markout"][model]
        risk = (
            snapshot["risk_snapshot"]
            if model == CONSERVATIVE_MODEL
            else self._risk_snapshot(model, metrics, self.session._stop_epoch or time.time())
        )
        cycle_stats = (
            snapshot["cycle_stats"]
            if model == CONSERVATIVE_MODEL
            else self._cycle_stats(model, self.session._stop_epoch or time.time())
        )
        volume = _number(metrics.get("total_executed_notional"), 0.0) or 0.0
        evidence: Stage14Evidence = snapshot["evidence"]
        row = {
            "timestamp": snapshot["timestamp"],
            "hour": int(snapshot["elapsed_seconds"] // 3600) + 1,
            "checkpoint_type": checkpoint_type,
            "model": model,
            "elapsed_seconds": snapshot["elapsed_seconds"],
            "evidence_status": evidence.status,
            "orders_created": metrics.get("orders_created"),
            "resting_orders": metrics.get("active_orders"),
            "orders_kept": metrics.get("orders_kept"),
            "operational_cancels": metrics.get("operational_cancels"),
            "shutdown_cancels": metrics.get("shutdown_cancels"),
            "orders_replaced": metrics.get("orders_replaced"),
            "orders_filled": metrics.get("orders_filled"),
            "fills": metrics.get("fills"),
            "fill_create_ratio": metrics.get("fill_create_ratio"),
            "cancel_create_ratio": metrics.get("cancel_create_ratio"),
            "keep_rate_pct": metrics.get("keep_pct"),
            "median_quote_lifetime_seconds": snapshot["quote_lifetime"].get("median"),
            "completed_cycles": metrics.get("completed_cycles"),
            "median_cycle_duration_seconds": cycle_stats.get("median"),
            "p75_cycle_duration_seconds": cycle_stats.get("p75"),
            "p90_cycle_duration_seconds": cycle_stats.get("p90"),
            "executed_volume": volume,
            "volume_per_average_filled_gross": self._ratio(
                volume, risk.get("average_filled_gross")
            ),
            "volume_per_average_worst_case_gross": self._ratio(
                volume, risk.get("average_worst_case_gross")
            ),
            "average_filled_gross": risk.get("average_filled_gross"),
            "max_filled_gross": risk.get("max_filled_gross"),
            "average_pending_reserved_gross": risk.get("average_pending_reserved_gross"),
            "max_pending_reserved_gross": risk.get("max_pending_reserved_gross"),
            "average_worst_case_gross": risk.get("average_worst_case_gross"),
            "max_worst_case_gross": risk.get("max_worst_case_gross"),
            "average_inventory": risk.get("average_inventory"),
            "max_inventory": risk.get("max_inventory"),
            "average_btc_beta": risk.get("average_btc_beta"),
            "max_btc_beta": risk.get("max_btc_beta"),
            "gross_pnl": metrics.get("gross_pnl"),
            "max_drawdown": metrics.get("max_drawdown_quote"),
            "risk_blocks": metrics.get("risk_blocks"),
            "hard_limit_attempts": snapshot["risk"].get("hard_limit_attempts"),
            "data_quality_coverage_pct": snapshot["data_quality"].get("overall_trade_coverage_pct"),
            "fill_model_sensitivity": snapshot["touch_sensitivity"],
        }
        for horizon in STAGE14_MARKOUT_HORIZONS_SECONDS:
            row[f"markout_{horizon}s_bps"] = markouts[f"{horizon}s"].get("mean_bps")
            row[f"markout_{horizon}s_n"] = markouts[f"{horizon}s"].get("sample_count")
        return row

    def record_checkpoint(
        self,
        timestamp: float | None = None,
        *,
        force: bool = False,
        checkpoint_type: str = "HOURLY",
    ) -> Stage14Evidence | None:
        now = timestamp if timestamp is not None else time.time()
        if self.started_epoch is None:
            return None
        checkpoint_due = (
            force
            or self._next_checkpoint_epoch is None
            or now >= self._next_checkpoint_epoch
        )
        snapshot = self._snapshot(now)
        if checkpoint_due:
            for model in (CONSERVATIVE_MODEL, TOUCH_MODEL):
                self._hourly_rows.append(self._hourly_row(snapshot, model, checkpoint_type))
            _write_csv(self.root / "hourly_metrics.csv", self._hourly_rows, STAGE14_HOURLY_FIELDS)
            if self._next_checkpoint_epoch is not None:
                while self._next_checkpoint_epoch <= now:
                    self._next_checkpoint_epoch += self.policy.checkpoint_interval_seconds
        self._last_snapshot = snapshot
        self._write_live_snapshot(now, snapshot=snapshot)
        return snapshot["evidence"]

    def should_early_stop(self, timestamp: float | None = None) -> bool:
        now = timestamp if timestamp is not None else time.time()
        if self.started_epoch is None or self._elapsed(now) < self.policy.minimum_duration_seconds:
            return False
        snapshot = self._snapshot(now)
        return snapshot["evidence"].status == "SUFFICIENT_FOR_DIAGNOSIS"

    def _write_live_snapshot(
        self, now: float, *, snapshot: Mapping[str, Any] | None = None
    ) -> None:
        current = snapshot or self._snapshot(now)
        live = self._build_summary(now, final=False, base_summary=None)
        live["metrics"] = self._compact_metrics(current)
        live["evidence"] = current["evidence"].to_record()
        live["fill_quality"] = {
            "primary_model": ShadowFillModel.CONSERVATIVE_TRADE_THROUGH.value,
            "sensitivity_model": ShadowFillModel.TOUCH_OPTIMISTIC.value,
            "conservative_fills": current["conservative"].get("fills"),
            "touch_fills": current["touch"].get("fills"),
            "sensitivity": current["touch_sensitivity"],
            "markouts": current["markout"][CONSERVATIVE_MODEL],
        }
        _write_json(self.project_root / "reports" / "stage14" / "latest_summary.json", live)

    def _copy_base_files(self, base_report: Path) -> None:
        direct = (
            "orders.csv",
            "fills.csv",
            "fill_eligibility.csv",
            "cycles.csv",
            "inventory.csv",
            "portfolio_exposure.csv",
            "risk_events.csv",
            "fill_model_comparison.csv",
            "self_tuning_suggestions.csv",
        )
        for name in direct:
            source = base_report.parent / name
            destination = self.root / name
            if source.is_file():
                shutil.copy2(source, destination)
            else:
                _write_csv(destination, [], ["timestamp"])
        pause_sources = (
            base_report.parent / "stage12f" / "pause_episodes.csv",
            base_report.parent / "stage12e" / "pause_episodes.csv",
            self.project_root / "reports" / "stage13" / "pause_hysteresis.csv",
        )
        for source in pause_sources:
            if source.is_file():
                shutil.copy2(source, self.root / "pause_events.csv")
                break
        else:
            _write_csv(self.root / "pause_events.csv", [], ["timestamp", "event"])

    def _write_final_artifacts(
        self, summary: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> None:
        base_report = self.session.report_path
        if base_report is not None:
            self._copy_base_files(Path(base_report))
        markout_fields = (
            "fill_id",
            "model",
            "fill_model",
            "trading_pair",
            "side",
            "entry_exit",
            "fill_timestamp",
            "fill_timestamp_epoch",
            "horizon_seconds",
            "target_timestamp_epoch",
            "future_timestamp_epoch",
            "price",
            "amount",
            "notional",
            "markout_bps",
            "status",
            "eligible",
            "mode",
            "global_iv_regime",
            "quote_distance_bps",
            "evidence",
        )
        _write_csv(self.root / "markouts.csv", snapshot["markout_rows"], markout_fields)
        _write_csv(self.root / "hourly_metrics.csv", self._hourly_rows, STAGE14_HOURLY_FIELDS)
        comparison = snapshot["metrics"].get("fill_model_comparison") or []
        _write_csv(
            self.root / "fill_model_comparison.csv",
            comparison if isinstance(comparison, list) else [],
            ["metric", "conservative", "touch_optimistic", "difference", "relative_difference_pct"],
        )
        suggestions = snapshot["metrics"].get("self_tuning_suggestions") or []
        _write_csv(
            self.root / "self_tuning_suggestions.csv",
            suggestions if isinstance(suggestions, list) else [],
            [
                "timestamp",
                "asset",
                "diagnosis",
                "recommendation",
                "current_value",
                "proposed_value",
                "confidence",
                "supporting_metrics",
                "mode",
                "applied",
            ],
        )
        quality_rows = [
            {"source": source, **values}
            for source, values in (snapshot["metrics"].get("data_quality") or {}).items()
            if isinstance(values, Mapping)
        ]
        if quality_rows:
            _write_csv(self.root / "data_quality.csv", quality_rows, list(quality_rows[0].keys()))
        self._write_trade_quality(snapshot)
        self._write_asset_summary(snapshot)

    def _write_trade_quality(self, snapshot: Mapping[str, Any]) -> None:
        rows = []
        for model, label in (
            (CONSERVATIVE_MODEL, "CONSERVATIVE_TRADE_THROUGH"),
            (TOUCH_MODEL, "TOUCH_OPTIMISTIC"),
        ):
            metrics = snapshot["conservative"] if model == CONSERVATIVE_MODEL else snapshot["touch"]
            rows.append(
                {
                    "model": label,
                    "fills": metrics.get("fills"),
                    "entry_fills": metrics.get("entry_fills"),
                    "exit_fills": metrics.get("exit_fills"),
                    "executed_volume": metrics.get("total_executed_notional"),
                    "fill_create_ratio": metrics.get("fill_create_ratio"),
                    "adverse_selection": (
                        self._adverse_selection(snapshot["markout"][CONSERVATIVE_MODEL])
                        if model == CONSERVATIVE_MODEL
                        else "SENSITIVITY_ONLY"
                    ),
                    "5s_markout_bps": snapshot["markout"][model]["5s"].get("mean_bps"),
                    "5s_n": snapshot["markout"][model]["5s"].get("sample_count"),
                    "30s_markout_bps": snapshot["markout"][model]["30s"].get("mean_bps"),
                    "30s_n": snapshot["markout"][model]["30s"].get("sample_count"),
                    "60s_markout_bps": snapshot["markout"][model]["60s"].get("mean_bps"),
                    "60s_n": snapshot["markout"][model]["60s"].get("sample_count"),
                    "300s_markout_bps": snapshot["markout"][model]["300s"].get("mean_bps"),
                    "300s_n": snapshot["markout"][model]["300s"].get("sample_count"),
                    "sensitivity": snapshot["touch_sensitivity"],
                }
            )
        _write_csv(self.root / "trade_quality.csv", rows, list(rows[0].keys()))

    def _write_asset_summary(self, snapshot: Mapping[str, Any]) -> None:
        metrics = snapshot["metrics"]
        assets = metrics.get("per_asset_metrics") or {}
        inventory = metrics.get("inventory_by_asset") or {}
        rows = []
        for pair in self.config.markets:
            asset = assets.get(pair) or {}
            inv = inventory.get(pair) or {}
            pair_rows = [
                row
                for row in snapshot["markout_rows"]
                if row.get("model") == CONSERVATIVE_MODEL and row.get("trading_pair") == pair
            ]
            asset_row = {
                "trading_pair": pair,
                "execution_status": self._asset_status().get(pair),
                "fills": asset.get("fills"),
                "volume": asset.get("volume"),
                "pnl": asset.get("pnl"),
                "orders_created": asset.get("orders_created"),
                "cancels": asset.get("cancels"),
                "orders_kept": asset.get("orders_kept"),
                "cycles": asset.get("cycles"),
                "average_inventory": inv.get("average_inventory", asset.get("average_inventory")),
                "max_inventory": inv.get("max_inventory", asset.get("max_inventory")),
                "average_inventory_ratio": inv.get("average_inventory_ratio"),
                "time_above_soft_threshold_seconds": inv.get("time_above_soft_threshold_seconds"),
                "time_above_defensive_threshold_seconds": inv.get(
                    "time_above_defensive_threshold_seconds"
                ),
                "hard_limit_attempts": asset.get("risk_blocks"),
            }
            for horizon in STAGE14_MARKOUT_HORIZONS_SECONDS:
                horizon_values = [
                    row.get("markout_bps")
                    for row in pair_rows
                    if int(row.get("horizon_seconds", 0) or 0) == horizon
                    and row.get("status") == "COMPLETE"
                ]
                stats = _markout_stats(horizon_values)
                asset_row[f"markout_{horizon}s_bps"] = stats.get("mean_bps")
                asset_row[f"markout_{horizon}s_n"] = stats.get("sample_count")
            rows.append(asset_row)
        _write_csv(
            self.root / "asset_summary.csv",
            rows,
            list(rows[0].keys()) if rows else ["trading_pair"],
        )

    def finalize(
        self, base_report: str | Path | None = None, *, reason: str = "MAXIMUM_6_HOUR_WINDOW"
    ) -> dict[str, Any]:
        report_path = Path(base_report) if base_report is not None else self.session.report_path
        base_summary = _read_json(report_path.parent / "summary.json") if report_path else {}
        now = self.session._stop_epoch or time.time()
        if self.started_epoch is None:
            self.start(self.session._start_epoch or now)
        self.record_checkpoint(now, force=True, checkpoint_type="FINAL")
        summary = self._build_summary(now, final=True, reason=reason, base_summary=base_summary)
        final_snapshot = self._snapshot(now, base_summary=base_summary)
        self._write_final_artifacts(summary, final_snapshot)
        _write_json(self.root / "summary.json", summary)
        (self.root / "summary.md").write_text(self.format_final_output(summary), encoding="utf-8")
        _write_json(self.manifest_path, {**self._manifest("COMPLETE", reason), "summary": summary})
        _write_json(self.project_root / "reports" / "stage14" / "latest_summary.json", summary)
        _write_json(self.project_root / "reports" / "stage14" / "validation_summary.json", summary)
        return summary

    @staticmethod
    def _display(value: Any) -> str:
        if value is None:
            return "UNKNOWN"
        if isinstance(value, bool):
            return "YES" if value else "NO"
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(value)

    @classmethod
    def _markout_text(cls, value: Mapping[str, Any]) -> str:
        return (
            f"{cls._display(value.get('mean_bps'))} bps n={cls._display(value.get('sample_count'))}"
        )

    def format_final_output(self, summary: Mapping[str, Any]) -> str:
        fill = summary.get("fill_quality") or {}
        marks = fill.get("markouts") or {}
        order = summary.get("order_execution") or {}
        capital = summary.get("capital_recycling") or {}
        inventory = summary.get("inventory") or {}
        volume = summary.get("volume_risk") or {}
        risk = summary.get("risk") or {}
        economics = summary.get("economics") or {}
        evidence = summary.get("evidence") or {}
        readiness = summary.get("readiness") or {}
        safety = summary.get("safety") or {}
        ratios = volume.get("ratios") or {}
        cycles_per_hour = (capital.get("completed_cycles") or 0) / (
            summary.get("duration_hours") or 1
        )
        soft_time = sum(
            _number(row.get("time_above_soft_threshold_seconds"), 0.0) or 0.0
            for row in inventory.get("by_asset", {}).values()
            if isinstance(row, Mapping)
        )
        lines = [
            "STAGE 14 — EVIDENCE-BASED ECONOMIC SHADOW VALIDATION",
            f"SESSION ID: {summary.get('session_id', 'UNKNOWN')}",
            f"Duration: {self._display(summary.get('duration_seconds'))} seconds",
            "Early evidence completion: "
            f"{'YES' if summary.get('early_evidence_completion') else 'NO'}",
            f"Why stopped: {summary.get('why_stopped', 'UNKNOWN')}",
            f"Stage13 behavior hash: {summary.get('stage13_behavior_hash', 'UNKNOWN')}",
            f"Config frozen: {'PASS' if summary.get('config_frozen') else 'FAIL'}",
            "",
            "SAFETY",
            f"Mainnet data: {'PASS' if safety.get('mainnet_public_data') else 'FAIL'}",
            f"Shadow only: {'PASS' if safety.get('shadow_only') else 'FAIL'}",
            "Real exchange mutations: 0",
            f"Self-tuning applications: {safety.get('self_tuning_applications', 0)}",
            "Private Derive trading client: NOT ENABLED",
            "",
            "ORDER EXECUTION",
            f"Raw candidate evaluations: {self._display(order.get('raw_candidate_evaluations'))}",
            f"Actual instantiated orders: {self._display(order.get('actual_instantiated_orders'))}",
            f"Resting: {self._display(order.get('entered_resting'))}",
            f"KEEP: {self._display(order.get('keep'))}",
            f"Operational cancels: {self._display(order.get('operational_cancels'))}",
            f"Same-frame cancels: {self._display(order.get('same_frame_cancels'))}",
            f"Conservative fills: {self._display(fill.get('conservative_fills'))}",
            f"Touch fills: {self._display(fill.get('touch_fills'))}",
            f"Fill/Create: {self._display(order.get('fill_create_ratio'))}",
            f"Cancel/Create: {self._display(order.get('cancel_create_ratio'))}",
            "Median quote lifetime: "
            f"{self._display((order.get('quote_lifetime') or {}).get('median'))}",
            "",
            "FILL QUALITY",
            f"5s markout: {self._markout_text(marks.get('5s') or {})}",
            f"30s markout: {self._markout_text(marks.get('30s') or {})}",
            f"60s markout: {self._markout_text(marks.get('60s') or {})}",
            f"5m markout: {self._markout_text(marks.get('300s') or {})}",
            f"Adverse selection: {fill.get('adverse_selection', 'UNKNOWN')}",
            "",
            "CAPITAL RECYCLING",
            f"Completed cycles: {self._display(capital.get('completed_cycles'))}",
            f"Cycles/hour: {self._display(cycles_per_hour)}",
            f"Median cycle duration: {self._display(capital.get('median_cycle_duration_seconds'))}",
            f"P90 cycle duration: {self._display(capital.get('p90_cycle_duration_seconds'))}",
            "Average open inventory age: "
            f"{self._display(capital.get('average_open_position_age_seconds'))}",
            f"Oldest inventory: {self._display(capital.get('max_open_position_age_seconds'))}",
            "",
            "INVENTORY",
        ]
        for pair in ("SOL-USDC", "HYPE-USDC"):
            asset = inventory.get("by_asset", {}).get(pair) or {}
            lines.extend(
                [
                    f"Average {pair.split('-')[0]} inventory: "
                    f"{self._display(asset.get('average_inventory'))}",
                    f"Max {pair.split('-')[0]} inventory: "
                    f"{self._display(asset.get('max_inventory'))}",
                ]
            )
        lines.extend(
            [
                f"Time above soft risk: {self._display(soft_time)}",
                "",
                "VOLUME",
                f"Executed volume: {self._display(volume.get('executed_volume'))}",
                "Volume / starting equity: "
                f"{self._display(ratios.get('volume_per_starting_equity'))}",
                f"Average filled gross: {self._display(volume.get('average_filled_gross'))}",
                "Average worst-case gross: "
                f"{self._display(volume.get('average_worst_case_gross'))}",
                "Volume / avg filled gross: "
                f"{self._display(ratios.get('volume_per_average_filled_gross'))}",
                "Volume / avg worst-case gross: "
                f"{self._display(ratios.get('volume_per_average_worst_case_gross'))}",
                "",
                "RISK",
                f"Average BTC-beta: {self._display(volume.get('average_btc_beta'))}",
                f"Max BTC-beta: {self._display(volume.get('max_btc_beta'))}",
                f"Risk episodes: {self._display(risk.get('unique_risk_episodes'))}",
                f"Hard-risk breaches: {self._display(risk.get('hard_risk_breaches'))}",
                "Risk-reducing side: NOT_SEPARATELY_INSTRUMENTED",
                "",
                "ECONOMICS",
                f"Starting paper equity: {self._display(economics.get('starting_paper_equity'))}",
                "Ending gross paper equity: "
                f"{self._display(economics.get('ending_gross_paper_equity'))}",
                f"Gross realized capture: {self._display(economics.get('gross_realized_capture'))}",
                f"Gross realized PnL: {self._display(economics.get('gross_realized_pnl'))}",
                "Unrealized inventory PnL: "
                f"{self._display(economics.get('unrealized_inventory_pnl'))}",
                f"Gross total PnL: {self._display(economics.get('gross_total_pnl'))}",
                f"Fee model: {self._display(economics.get('fee_model'))}",
                f"Verified net PnL: {self._display(economics.get('verified_net_pnl'))}",
                f"Max drawdown: {self._display(economics.get('max_drawdown'))}",
                f"PnL reconciliation: {self._display(economics.get('pnl_reconciliation_status'))}",
                "",
                "FILL MODEL",
                f"Conservative fills: {self._display(fill.get('conservative_fills'))}",
                f"Conservative volume: {self._display(fill.get('conservative_volume'))}",
                f"Touch fills: {self._display(fill.get('touch_fills'))}",
                f"Touch volume: {self._display(fill.get('touch_volume'))}",
                f"Sensitivity: {self._display(fill.get('sensitivity'))}",
                "",
                "EVIDENCE",
                f"Conservative fill n: {self._display(evidence.get('conservative_fills'))}",
                f"30s markout n: {self._display(evidence.get('markout_30s_n'))}",
                f"60s markout n: {self._display(evidence.get('markout_60s_n'))}",
                f"Completed cycle n: {self._display(evidence.get('completed_cycles'))}",
                f"Economic evidence: {self._display(evidence.get('status'))}",
                "",
                f"FINAL CLASSIFICATION: {summary.get('classification', 'UNKNOWN')}",
                f"PRIMARY WEAKNESS: {summary.get('primary_weakness', 'UNKNOWN')}",
                "READY FOR BOUNDED ECONOMIC OPTIMIZATION: "
                f"{readiness.get('ready_for_bounded_economic_optimization', 'NO')}",
                "READY FOR TINY LIVE-MONEY CANARY REVIEW: "
                f"{readiness.get('ready_for_tiny_live_money_canary_review', 'NO')}",
                "",
                "NEXT STEP: Stage 14 stops here; no live execution or automatic "
                "optimization is enabled.",
            ]
        )
        return "\n".join(lines) + "\n"


__all__ = [
    "STAGE14_CLASSIFICATIONS",
    "STAGE14_EVIDENCE_STATUSES",
    "STAGE14_MARKOUT_HORIZONS_SECONDS",
    "Stage14Config",
    "Stage14EconomicValidator",
    "Stage14Evidence",
    "assess_economic_evidence",
    "validate_stage13_reference",
    "_directional_markout",
]
