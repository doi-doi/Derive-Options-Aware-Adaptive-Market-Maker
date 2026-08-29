"""Stage 13 bounded market-making stability controls and evidence.

Stage 13 is deliberately narrower than strategy optimization.  It makes
soft-regime pause behavior, pre-create eligibility, and pending-risk deltas
observable without changing Stage 4 allocations or enabling exchange
execution.  The report treats the conservative shadow model as the headline
evidence and leaves economic, quote, fill, and volume optimization disabled.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .stage12g import (
    _lifecycle_epoch,
    _same_cycle_create_cancel,
    build_order_funnel,
    build_risk_reservation_audit,
    build_zero_lifetime_root_causes,
)

EXECUTION_STATUSES = (
    "EXECUTION_ENABLED",
    "SIGNAL_ONLY",
    "SIGNAL_ONLY_MIN_SIZE",
    "DISABLED",
)
CREATE_ACTIONS = {
    "CREATE_DECISION",
    "ORDER_INSTANTIATED",
    "ROUTE_BLOCKED",
    "BLOCKED",
    "SIGNAL_ONLY",
    "SIGNAL_ONLY_MIN_SIZE",
}
CREATE_CATEGORIES = (
    "INSTANTIATED",
    "PORTFOLIO_RISK_BLOCK",
    "SIGNAL_ONLY",
    "SIGNAL_ONLY_MIN_SIZE",
    "PRE_CREATE_RISK_BLOCK",
    "PLAN_INVALID_BEFORE_CREATE",
    "MIN_SIZE",
    "MAKER_SAFETY",
    "DATA_CRITICAL",
    "DUPLICATE_LEVEL",
    "OTHER_EXPLICIT",
    "UNKNOWN_INTERNAL",
)


class Stage13StabilityConfig(BaseModel):
    """Opt-in controls for the bounded Stage 13 stability profile."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    regime_pause_entry_confirm_seconds: float = Field(default=0.0, ge=0, le=30)
    regime_pause_exit_confirm_seconds: float = Field(default=0.0, ge=0, le=30)
    preserve_existing_quotes_during_soft_pause_confirmation: bool = False
    suppress_new_entries_during_soft_pause_confirmation: bool = True
    use_incremental_pending_exposure_for_reconciliation: bool = False
    asset_execution_status: dict[str, str] = Field(default_factory=dict)
    parent_control_summary_path: str = "reports/stage12g/diagnostic_summary.json"

    @model_validator(mode="after")
    def validate_bounded_window(self) -> Stage13StabilityConfig:
        for name, value in (
            (
                "regime_pause_entry_confirm_seconds",
                self.regime_pause_entry_confirm_seconds,
            ),
            (
                "regime_pause_exit_confirm_seconds",
                self.regime_pause_exit_confirm_seconds,
            ),
        ):
            if value != 0 and not 5 <= value <= 30:
                raise ValueError(f"{name} must be 0 or bounded to 5-30 seconds")
        invalid = set(self.asset_execution_status.values()) - set(EXECUTION_STATUSES)
        if invalid:
            raise ValueError(f"unsupported asset execution status: {sorted(invalid)}")
        return self


def _row(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_record"):
        return dict(value.to_record())
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="python"))
    try:
        return dict(vars(value))
    except TypeError:
        return {}


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _epoch(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric / 1000 if numeric > 10_000_000_000 else numeric
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def _duration_bucket(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    if seconds < 1:
        return "<1s"
    if seconds < 5:
        return "1-5s"
    if seconds < 15:
        return "5-15s"
    if seconds < 30:
        return "15-30s"
    if seconds < 60:
        return "30-60s"
    if seconds < 300:
        return "1-5min"
    if seconds < 900:
        return "5-15min"
    return ">15min"


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
        return _safe(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if str(key) not in fields:
                fields.append(str(key))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_safe(row) for row in rows)


def effective_asset_status(
    config: Stage13StabilityConfig | Mapping[str, Any] | None,
    markets: Sequence[str],
    enabled_markets: Sequence[str],
) -> dict[str, str]:
    """Resolve explicit statuses without silently enabling a non-enabled pair."""

    if isinstance(config, Stage13StabilityConfig):
        stage13 = config
    else:
        stage13 = Stage13StabilityConfig.model_validate(config or {})
    explicit = stage13.asset_execution_status if stage13.enabled else {}
    enabled = set(enabled_markets)
    return {
        str(pair): (
            explicit.get(str(pair), "EXECUTION_ENABLED")
            if str(pair) in enabled
            else "SIGNAL_ONLY"
        )
        for pair in markets
    }


def _create_category(row: Mapping[str, Any]) -> str:
    action = str(row.get("raw_planned_action") or row.get("planned_action") or "").upper()
    status = str(row.get("asset_execution_status") or "").upper()
    reason_code = str(row.get("pre_create_block_category") or "").upper()
    reason = str(row.get("blocked_reason") or row.get("terminal_reason") or "").upper()
    if bool(row.get("order_instantiated")) or action == "ORDER_INSTANTIATED":
        return "INSTANTIATED"
    if status == "SIGNAL_ONLY_MIN_SIZE":
        return "SIGNAL_ONLY_MIN_SIZE"
    if status == "SIGNAL_ONLY":
        return "SIGNAL_ONLY"
    if reason_code in CREATE_CATEGORIES and reason_code not in {"INSTANTIATED", "UNKNOWN_INTERNAL"}:
        return reason_code
    if action == "ROUTE_BLOCKED" or row.get("portfolio_route_allowed") is False:
        return "PORTFOLIO_RISK_BLOCK"
    if "PORTFOLIO" in reason or "GROSS" in reason or "BETA" in reason:
        return "PORTFOLIO_RISK_BLOCK"
    if row.get("plan_valid") is False or "PLAN INVALID" in reason:
        return "PLAN_INVALID_BEFORE_CREATE"
    if "MINIMUM" in reason or "NOTIONAL" in reason or "AMOUNT BELOW" in reason:
        return "MIN_SIZE"
    if "MAKER" in reason or "CROSS" in reason:
        return "MAKER_SAFETY"
    if any(token in reason for token in ("DATA", "STALE", "CONFIDENCE", "RELATIONSHIP")):
        return "DATA_CRITICAL"
    if "DUPLICATE" in reason:
        return "DUPLICATE_LEVEL"
    if action in CREATE_ACTIONS:
        return "OTHER_EXPLICIT"
    return "UNKNOWN_INTERNAL"


def build_create_decision_reconciliation(
    rows: Sequence[Mapping[str, Any]],
    orders: Sequence[Any] = (),
) -> dict[str, Any]:
    """Reconcile every raw create decision to exactly one explicit category."""

    instantiated_ids = {
        str(_row(order).get("shadow_order_id"))
        for order in orders
        if _row(order).get("shadow_order_id")
    }
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        raw_action = str(row.get("raw_planned_action") or row.get("planned_action") or "")
        is_raw_create = raw_action.upper() in CREATE_ACTIONS or bool(
            row.get("order_instantiated")
        )
        if not is_raw_create:
            continue
        order_id = str(row.get("shadow_order_id") or "")
        if order_id in instantiated_ids:
            row["order_instantiated"] = True
        category = _create_category(row)
        row.update(
            {
                "raw_planned_action": raw_action,
                "final_eligibility": (
                    "INSTANTIATED" if category == "INSTANTIATED" else "BLOCKED"
                ),
                "reconciliation_category": category,
                "explicit_category": category != "UNKNOWN_INTERNAL",
            }
        )
        output.append(row)
    counts = {category: 0 for category in CREATE_CATEGORIES}
    for row in output:
        counts[str(row["reconciliation_category"])] += 1
    return {
        "rows": output,
        "counts": counts,
        "raw_create_decisions": len(output),
        "instantiated": counts["INSTANTIATED"],
        "unknown_internal": counts["UNKNOWN_INTERNAL"],
        "reconciles": sum(counts.values()) == len(output),
    }


def build_quote_survival(
    orders: Sequence[Any],
    *,
    end_timestamp: float,
) -> dict[str, Any]:
    """Measure conservative quote survival, excluding same-frame artifacts."""

    rows: list[dict[str, Any]] = []
    for value in orders:
        order = _row(value)
        if order.get("is_exit"):
            continue
        resting = _lifecycle_epoch(order, "created") or _epoch(
            order.get("resting_start_timestamp") or order.get("resting_start_epoch")
        )
        terminal = _lifecycle_epoch(order, "terminal") or _epoch(
            order.get("terminal_timestamp") or order.get("terminal_epoch")
        )
        same_frame = _same_cycle_create_cancel(order)
        if resting is None or same_frame:
            continue
        end = terminal if terminal is not None else end_timestamp
        lifetime = max(0.0, end - resting)
        rows.append(
            {
                "shadow_order_id": order.get("shadow_order_id"),
                "trading_pair": order.get("trading_pair"),
                "level_id": order.get("level_id"),
                "side": order.get("side"),
                "resting_start_timestamp": order.get("resting_start_timestamp"),
                "terminal_timestamp": order.get("terminal_timestamp"),
                "lifetime_seconds": lifetime,
                "terminal_status": order.get("status"),
                "still_resting_at_end": terminal is None,
            }
        )
    counts = {
        f"stayed_resting_ge_{seconds}s": sum(
            float(row["lifetime_seconds"]) >= seconds for row in rows
        )
        for seconds in (1, 5, 30, 60)
    }
    return {
        "rows": rows,
        "counts": counts,
        "evidence_sample_count": len(rows),
        "same_frame_excluded": sum(
            _same_cycle_create_cancel(_row(value))
            for value in orders
            if not _row(value).get("is_exit")
        ),
    }


def build_pause_hysteresis(cycles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Flatten pause-candidate, confirmation, and recovery observations."""

    rows: list[dict[str, Any]] = []
    episodes: dict[str, int] = defaultdict(int)
    active_episodes: dict[str, dict[str, Any]] = {}
    previous_signature: dict[str, tuple[str, str] | None] = {}
    episode_rows: list[dict[str, Any]] = []

    def close_episode(pair: str, end_epoch: float | None, *, closed: bool) -> None:
        episode = active_episodes.pop(pair, None)
        if episode is None:
            return
        start_epoch = episode.get("start_epoch")
        last_epoch = episode.get("last_epoch")
        finish = end_epoch if end_epoch is not None else last_epoch
        duration = (
            max(0.0, finish - start_epoch)
            if finish is not None and start_epoch is not None
            else None
        )
        episode_rows.append(
            {
                "trading_pair": pair,
                "pause_candidate_category": episode.get("category"),
                "pause_candidate_reason": episode.get("reason"),
                "start_timestamp": (
                    datetime.fromtimestamp(start_epoch, UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                    if start_epoch is not None
                    else None
                ),
                "end_timestamp": (
                    datetime.fromtimestamp(finish, UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                    if finish is not None
                    else None
                ),
                "duration_seconds": duration,
                "duration_bucket": _duration_bucket(duration),
                "confirmed": bool(episode.get("confirmed")),
                "closed": closed,
            }
        )

    for cycle in cycles:
        timestamp = cycle.get("timestamp")
        timestamp_epoch = _epoch(timestamp)
        decisions = cycle.get("decisions") or {}
        plans = cycle.get("plans") or {}
        for pair, raw_decision in decisions.items():
            decision = _row(raw_decision)
            plan = _row(plans.get(pair, {}))
            candidate = bool(
                decision.get("pause_candidate_active")
                or plan.get("pause_candidate_active")
            )
            confirmed = bool(decision.get("pause_confirmed") or plan.get("pause_confirmed"))
            category = str(
                decision.get("pause_candidate_category")
                or plan.get("pause_candidate_category")
                or "UNKNOWN"
            ).upper()
            reason = str(
                decision.get("pause_candidate_reason")
                or plan.get("pause_candidate_reason")
                or ""
            )
            pair_key = str(pair)
            signature = (category, reason) if candidate else None
            if signature is not None and signature != previous_signature.get(pair_key):
                close_episode(pair_key, timestamp_epoch, closed=True)
                active_episodes[pair_key] = {
                    "start_epoch": timestamp_epoch,
                    "last_epoch": timestamp_epoch,
                    "category": category,
                    "reason": reason,
                    "confirmed": confirmed and str(decision.get("mode") or "").lower() == "pause",
                }
                episodes[pair_key] += 1
            elif signature is not None:
                active = active_episodes.get(pair_key)
                if active is not None:
                    if timestamp_epoch is not None:
                        active["last_epoch"] = timestamp_epoch
                    active["confirmed"] = bool(active.get("confirmed")) or (
                        confirmed and str(decision.get("mode") or "").lower() == "pause"
                    )
            else:
                close_episode(pair_key, timestamp_epoch, closed=True)
            previous_signature[pair_key] = signature
            rows.append(
                {
                    "timestamp": timestamp,
                    "timestamp_epoch": timestamp_epoch,
                    "trading_pair": pair,
                    "mode": decision.get("mode"),
                    "pause_candidate_active": candidate,
                    "pause_candidate_category": decision.get(
                        "pause_candidate_category", plan.get("pause_candidate_category")
                    ),
                    "pause_candidate_age_seconds": decision.get(
                        "pause_candidate_age_seconds", plan.get("pause_candidate_age_seconds")
                    ),
                    "pause_confirmation_seconds": decision.get(
                        "pause_confirmation_seconds", plan.get("pause_confirmation_seconds", 0)
                    ),
                    "pause_confirmed": confirmed,
                    "recovery_candidate": decision.get(
                        "recovery_candidate", plan.get("recovery_candidate")
                    ),
                    "recovery_candidate_age_seconds": decision.get(
                        "recovery_candidate_age_seconds",
                        plan.get("recovery_candidate_age_seconds"),
                    ),
                    "recovery_confirmation_seconds": decision.get(
                        "recovery_confirmation_seconds",
                        plan.get("recovery_confirmation_seconds", 0),
                    ),
                }
            )
    for pair in list(active_episodes):
        close_episode(pair, None, closed=False)

    strategy_episodes = [
        row for row in episode_rows if row.get("pause_candidate_category") == "STRATEGY_REGIME"
    ]
    closed_transient = [
        row
        for row in strategy_episodes
        if row.get("closed") and not row.get("confirmed")
    ]
    closed_durations = [
        float(row["duration_seconds"])
        for row in strategy_episodes
        if row.get("closed") and row.get("duration_seconds") is not None
    ]
    return {
        "rows": rows,
        "episodes": episode_rows,
        "candidate_observations": sum(row["pause_candidate_active"] for row in rows),
        "confirmed_pause_observations": sum(
            row["pause_confirmed"] and row["mode"] == "pause" for row in rows
        ),
        "recovery_candidate_observations": sum(
            row["recovery_candidate"] is not None for row in rows
        ),
        "strategy_regime_pause_episodes": len(strategy_episodes),
        "by_asset_episodes": {
            pair: sum(
                row.get("trading_pair") == pair
                and row.get("pause_candidate_category") == "STRATEGY_REGIME"
                for row in strategy_episodes
            )
            for pair in sorted({str(row.get("trading_pair")) for row in strategy_episodes})
        },
        "transient_le_1s": sum(
            row.get("duration_seconds") is not None and row["duration_seconds"] <= 1
            for row in closed_transient
        ),
        "transient_le_5s": sum(
            row.get("duration_seconds") is not None and row["duration_seconds"] <= 5
            for row in closed_transient
        ),
        "transient_le_30s": sum(
            row.get("duration_seconds") is not None and row["duration_seconds"] <= 30
            for row in closed_transient
        ),
        "transient_le_60s": sum(
            row.get("duration_seconds") is not None and row["duration_seconds"] <= 60
            for row in closed_transient
        ),
        "transient_strategy_regime_pause_episodes": len(closed_transient),
        "pause_duration_buckets": dict(
            Counter(row.get("duration_bucket") for row in strategy_episodes if row.get("closed"))
        ),
        "median_strategy_pause_candidate_seconds": (
            sorted(closed_durations)[len(closed_durations) // 2]
            if closed_durations
            else None
        ),
        "current": rows[-1] if rows else {},
    }


def build_risk_delta_audit(
    cycles: Sequence[Mapping[str, Any]],
    engine: Any | None = None,
) -> list[dict[str, Any]]:
    """Collect incremental reservation records without inventing deltas."""

    rows = [dict(_row(row)) for row in (getattr(engine, "risk_delta_audit", []) or [])]
    if rows:
        return rows
    for cycle in cycles:
        risk = cycle.get("portfolio_risk") or {}
        rows.extend(dict(_row(row)) for row in risk.get("risk_delta_audit", []) or [])
    return rows


def build_asset_execution_status(
    config: Mapping[str, Any],
    min_rows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return the explicit Stage 13 execution/signal status per asset."""

    shadow = config.get("shadow", config)
    stage13 = shadow.get("stage13") or {}
    markets = list(shadow.get("markets") or [])
    enabled = set(shadow.get("enabled_markets") or [])
    explicit = stage13.get("asset_execution_status") or {}
    observed = Counter(str(row.get("trading_pair")) for row in min_rows)
    return [
        {
            "trading_pair": pair,
            "asset": str(pair).removesuffix("-USDC"),
            "status": (
                explicit.get(pair, "EXECUTION_ENABLED")
                if pair in enabled and bool(stage13.get("enabled", False))
                else "SIGNAL_ONLY"
                if pair not in enabled
                else "EXECUTION_ENABLED"
            ),
            "enabled_in_cycle": pair in enabled,
            "observed_minimum_size_rows": observed.get(pair, 0),
            "execution_mutations_allowed": False,
        }
        for pair in markets
    ]


def _control_metric(control: Mapping[str, Any], key: str, default: Any = None) -> Any:
    funnel = control.get("order_funnel") or {}
    if key in funnel:
        return funnel.get(key)
    if key == "zero_lifetime_total":
        return (control.get("zero_lifetime") or {}).get("total", default)
    if key == "median_lifetime_seconds":
        return (control.get("resting_lifetime") or {}).get(
            "median_lifetime_seconds", default
        )
    if key == "p90_lifetime_seconds":
        return (control.get("resting_lifetime") or {}).get("p90_lifetime_seconds", default)
    if key in {
        "strategy_regime_pause_episodes",
        "transient_le_1s",
        "transient_le_5s",
        "transient_le_30s",
        "transient_le_60s",
    }:
        pause = control.get("pause") or {}
        return pause.get(
            "unique_asset_episodes"
            if key == "strategy_regime_pause_episodes"
            else key,
            default,
        )
    if key == "pending_risk_oscillation":
        return (control.get("risk") or {}).get(key, default)
    if key == "risk_blocks":
        return (control.get("risk") or {}).get(key, control.get(key, default))
    return control.get(key, default)


def write_stage13_artifacts(
    *,
    project_root: str | Path,
    session_id: str,
    config: Mapping[str, Any],
    frames: Sequence[Any],
    model_metrics: Mapping[str, Any],
    cycles_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    start_timestamp: float,
    end_timestamp: float,
    stage12g_control_summary: Mapping[str, Any] | None = None,
    stage12f_summary: Mapping[str, Any] | None = None,
    stage12g_summary: Mapping[str, Any] | None = None,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Write Stage 13 stability evidence and return its machine summary."""

    project = Path(project_root).expanduser().resolve()
    root = project / "reports" / "stage13"
    root.mkdir(parents=True, exist_ok=True)
    conservative = model_metrics.get("CONSERVATIVE")
    orders = list(getattr(conservative, "orders", []) or []) if conservative else []
    if not orders and engine is not None:
        orders = list(getattr(engine, "orders", {}).values())
    eligibility = list(getattr(engine, "order_eligibility_audit", []) or []) if engine else []
    cycles = list(cycles_by_model.get("CONSERVATIVE", ()))
    funnel_rows = build_order_funnel(
        orders,
        eligibility,
        end_timestamp=end_timestamp,
    )
    funnel = {str(row["stage"]): row["count"] for row in funnel_rows}
    zero_rows = build_zero_lifetime_root_causes(orders)
    create_reconciliation = build_create_decision_reconciliation(eligibility, orders)
    survival = build_quote_survival(orders, end_timestamp=end_timestamp)
    pause = build_pause_hysteresis(cycles)
    risk_delta_rows = build_risk_delta_audit(cycles, engine)
    risk_reservation_rows = build_risk_reservation_audit(engine, cycles) if engine else []
    portfolio_risk_reservations = [
        row for row in risk_reservation_rows if row.get("scope") == "PORTFOLIO"
    ]
    pending_risk_oscillation_rows = [
        row for row in portfolio_risk_reservations if row.get("pending_reservation_oscillation")
    ]
    risk_invariant = all(
        bool(row.get("gross_reconciles", True))
        and bool(row.get("keep_double_count_invariant", True))
        and bool(row.get("ledger_pending_does_not_change_filled_inventory", True))
        for row in risk_reservation_rows
    )
    pending_self_invalidation_categories = {
        "ASSET_RISK_BLOCK",
        "COLLATERAL_BLOCK",
        "DRAWDOWN_BLOCK",
        "PORTFOLIO_RISK_BLOCK",
    }
    self_invalidation_rows = [
        row
        for row in orders
        if not row.get("is_exit")
        and row.get("cancel_timestamp")
        and str(row.get("cancel_reason_category") or "")
        in pending_self_invalidation_categories
        and not _same_cycle_create_cancel(row)
    ]
    conservative_metrics = (
        dict(getattr(conservative, "metrics", {}) or {}) if conservative else {}
    )
    stage12f = dict(stage12f_summary or {})
    stage12g = dict(stage12g_summary or {})
    asset_status = build_asset_execution_status(
        config,
        [
            {
                "trading_pair": row.get("trading_pair")
            }
            for row in eligibility
        ],
    )
    same_frame_rows = [
        {
            "shadow_order_id": row.get("shadow_order_id"),
            "trading_pair": row.get("trading_pair"),
            "level_id": row.get("level_id"),
            "root_cause": row.get("zero_lifetime_root_cause"),
            "same_cycle_create_cancel": row.get("same_cycle_create_cancel"),
            "created_timestamp": row.get("created_timestamp"),
            "terminal_timestamp": row.get("terminal_timestamp"),
        }
        for row in zero_rows
        if row.get("zero_lifetime_root_cause") == "RECONCILIATION_CANCEL_SAME_FRAME"
    ]
    safety = {
        "market_environment": str(config.get("market_environment", "mainnet")).lower(),
        "execution_mode": str(config.get("execution_mode", "SHADOW")).upper(),
        "execution_enabled": bool(config.get("execution_enabled", False)),
        "allow_mainnet_trading": bool(config.get("allow_mainnet_trading", False)),
        "real_exchange_mutation_calls": int(
            getattr(engine, "real_exchange_mutation_calls", 0) or 0
        ),
    }
    safety["status"] = (
        "PASS"
        if safety["market_environment"] == "mainnet"
        and safety["execution_mode"] == "SHADOW"
        and not safety["execution_enabled"]
        and not safety["allow_mainnet_trading"]
        and safety["real_exchange_mutation_calls"] == 0
        else "FAIL"
    )
    raw_create = int(create_reconciliation["raw_create_decisions"])
    instantiated = int(create_reconciliation["instantiated"])
    candidate_levels = int(funnel.get("candidate_grid_levels", 0) or 0)
    risk_eligible = int(funnel.get("risk_eligible", 0) or 0)
    entered_resting = int(funnel.get("entered_resting", 0) or 0)
    keep_count = sum(
        str(row.get("planned_action") or "").upper() == "KEEP" for row in eligibility
    )
    same_frame_count = len(same_frame_rows)
    same_frame_rate = same_frame_count / raw_create if raw_create else None
    risk_block_count = sum(
        int(create_reconciliation["counts"].get(category, 0) or 0)
        for category in ("PORTFOLIO_RISK_BLOCK", "PRE_CREATE_RISK_BLOCK")
    )

    lifetime_values = sorted(
        float(row["lifetime_seconds"])
        for row in survival["rows"]
        if _number(row.get("lifetime_seconds")) is not None
    )

    def percentile(values: Sequence[float], quantile: float) -> float | None:
        if not values:
            return None
        index = min(len(values) - 1, max(0, math.ceil(len(values) * quantile) - 1))
        return values[index]

    stage13_median_lifetime = _number(conservative_metrics.get("median_quote_lifetime"))
    if stage13_median_lifetime is None:
        stage13_median_lifetime = percentile(lifetime_values, 0.50)
    stage13_p90_lifetime = _number(conservative_metrics.get("p90_quote_lifetime"))
    if stage13_p90_lifetime is None:
        stage13_p90_lifetime = percentile(lifetime_values, 0.90)

    control = dict(stage12g_control_summary or {})
    parent_safety_statuses = [
        str((source.get("safety") or {}).get("status") or "").upper()
        for source in (stage12f, stage12g)
        if source
    ]
    hard_safety_regression = safety["status"] == "PASS" and risk_invariant and all(
        status == "PASS" for status in parent_safety_statuses
    )

    def first_status(*values: Any) -> str:
        for value in values:
            if value is not None and str(value).strip():
                return str(value).upper()
        return "NOT_PROVIDED"

    pnl_status = first_status(
        conservative_metrics.get("pnl_reconciliation_status"),
        (stage12g.get("accounting") or {}).get("pnl_reconciliation"),
    )
    fill_contract_status = first_status(
        (stage12f.get("fill_contract") or {}).get("status"),
    )
    trade_pipeline_status = first_status(
        (stage12f.get("trade_pipeline") or {}).get("classification"),
        (stage12g.get("trade_pipeline") or {}).get("classification"),
    )
    validation = {
        "hard_safety_regression": "PASS" if hard_safety_regression else "FAIL",
        "pnl_reconciliation": pnl_status,
        "fill_contract": fill_contract_status,
        "trade_pipeline": trade_pipeline_status,
    }
    pipeline_ok = trade_pipeline_status.startswith("HEALTHY")

    risk_delta_actions = Counter(
        str(row.get("action") or "UNKNOWN").upper() for row in risk_delta_rows
    )
    positive_deltas = [
        value
        for value in (_number(row.get("notional_delta")) for row in risk_delta_rows)
        if value is not None and value > 0
    ]
    keep_nonzero_deltas = sum(
        str(row.get("action") or "").upper() == "KEEP"
        and abs(_number(row.get("notional_delta"), 0.0) or 0.0) > 1e-9
        for row in risk_delta_rows
    )

    def max_reservation(*fields: str) -> float:
        values = [
            number
            for row in portfolio_risk_reservations
            for field in fields
            for number in [_number(row.get(field))]
            if number is not None
        ]
        return max(values, default=0.0)

    risk_summary = {
        "filled_gross_exposure": max_reservation(
            "filled_gross_exposure", "filled_inventory_notional"
        ),
        "pending_reserved_gross": max_reservation(
            "pending_reserved_gross_after_reconcile", "pending_reserved_gross"
        ),
        "worst_case_gross": max_reservation(
            "portfolio_gross_exposure_after_reconcile",
            "worst_case_gross",
            "portfolio_gross_exposure",
        ),
        "pending_risk_oscillation": "YES" if pending_risk_oscillation_rows else "NO",
        "pending_risk_oscillation_count": len(pending_risk_oscillation_rows),
        "self_invalidation_events": len(self_invalidation_rows),
        "keep_double_count_invariant": "PASS" if risk_invariant else "FAIL",
        "audit_rows": len(portfolio_risk_reservations),
        "risk_delta_audit_rows": len(risk_delta_rows),
        "risk_delta_actions": dict(risk_delta_actions),
        "max_positive_candidate_delta": max(positive_deltas, default=0.0),
        "keep_nonzero_delta_rows": keep_nonzero_deltas,
    }

    stability_reasons: list[str] = []
    if safety["status"] != "PASS":
        stability_reasons.append("shadow safety boundary failed")
    if not hard_safety_regression:
        stability_reasons.append("hard safety regression is not PASS")
    if pnl_status != "PASS":
        stability_reasons.append(f"PnL reconciliation is {pnl_status}")
    if fill_contract_status != "PASS":
        stability_reasons.append(f"fill contract is {fill_contract_status}")
    if not pipeline_ok:
        stability_reasons.append(f"public trade pipeline is {trade_pipeline_status}")
    if create_reconciliation["unknown_internal"]:
        stability_reasons.append("create decisions contain UNKNOWN_INTERNAL")
    if raw_create == 0:
        stability_reasons.append("no raw create decisions were observed")
    if not funnel.get("stayed_resting_ge_1s", 0):
        stability_reasons.append("no conservative quote survived at least one second")
    if keep_count == 0:
        stability_reasons.append("no KEEP decisions were observed")
    if not risk_invariant:
        stability_reasons.append("pending-risk reservation invariant failed")
    if self_invalidation_rows:
        stability_reasons.append("pending-risk self-invalidation events remain")
    if same_frame_rate is not None and same_frame_rate > 0.05:
        stability_reasons.append("same-frame create/cancel rate exceeds five percent")
    stage13_profile = config.get("stage13") or {}
    pause_windows_valid = all(
        value == 0 or 5 <= value <= 30
        for value in (
            _number(stage13_profile.get("regime_pause_entry_confirm_seconds"), 0.0) or 0.0,
            _number(stage13_profile.get("regime_pause_exit_confirm_seconds"), 0.0) or 0.0,
        )
    )
    if not pause_windows_valid:
        stability_reasons.append("pause confirmation window is outside the bounded 5-30s range")
    stability_pass = not stability_reasons
    baseline_reasons = list(stability_reasons)
    if survival["evidence_sample_count"] < 5:
        baseline_reasons.append(
            "quote survival evidence sample is "
            f"{survival['evidence_sample_count']}; at least 5 is required for the "
            "24-48h frozen baseline gate"
        )

    comparison_values = (
        ("candidates", candidate_levels, "candidate_grid_levels"),
        ("risk_eligible", risk_eligible, "risk_eligible"),
        ("create_decisions", raw_create, "create_decisions"),
        ("instantiated", instantiated, "shadow_order_objects_instantiated"),
        ("entered_resting", entered_resting, "entered_resting"),
        ("same_frame_create_cancel", same_frame_count, "zero_lifetime_total"),
        (
            "quote_survival_ge_1s",
            survival["counts"]["stayed_resting_ge_1s"],
            "stayed_resting_ge_1s",
        ),
        (
            "quote_survival_ge_5s",
            survival["counts"]["stayed_resting_ge_5s"],
            "stayed_resting_ge_5s",
        ),
        (
            "quote_survival_ge_30s",
            survival["counts"]["stayed_resting_ge_30s"],
            "stayed_resting_ge_30s",
        ),
        (
            "quote_survival_ge_60s",
            survival["counts"]["stayed_resting_ge_60s"],
            "stayed_resting_ge_60s",
        ),
        ("keep_decisions", keep_count, "keep_decisions"),
        ("median_resting_lifetime_seconds", stage13_median_lifetime, "median_lifetime_seconds"),
        ("p90_resting_lifetime_seconds", stage13_p90_lifetime, "p90_lifetime_seconds"),
        (
            "strategy_regime_pause_episodes",
            pause["strategy_regime_pause_episodes"],
            "strategy_regime_pause_episodes",
        ),
        ("transient_pause_le_5s", pause["transient_le_5s"], "transient_le_5s"),
        ("transient_pause_le_30s", pause["transient_le_30s"], "transient_le_30s"),
        (
            "pending_risk_oscillation",
            risk_summary["pending_risk_oscillation"],
            "pending_risk_oscillation",
        ),
        ("risk_blocks", risk_block_count, "risk_blocks"),
    )
    comparison_rows = []
    for label, stage13_value, control_key in comparison_values:
        control_value = _control_metric(control, control_key)
        numeric_values = (
            isinstance(stage13_value, (int, float))
            and not isinstance(stage13_value, bool)
            and isinstance(control_value, (int, float))
            and not isinstance(control_value, bool)
        )
        comparison_rows.append(
            {
                "metric": label,
                "stage12g_control": control_value,
                "stage13": stage13_value,
                "delta_stage13_minus_control": (
                    stage13_value - control_value if numeric_values else None
                ),
                "control_available": control_value is not None,
            }
        )
    summary = {
        "stage": "13",
        "session_id": session_id,
        "generated_at": datetime.fromtimestamp(end_timestamp, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "parent_stage12g_control_available": bool(control),
        "safety": safety,
        "order_funnel": funnel,
        "create_decision_reconciliation": {
            key: value
            for key, value in create_reconciliation.items()
            if key != "rows"
        },
        "same_frame_cancel": {
            "count": same_frame_count,
            "rate_of_raw_create_decisions": same_frame_rate,
            "root_causes": dict(
                Counter(str(row.get("zero_lifetime_root_cause")) for row in same_frame_rows)
            ),
        },
        "quote_survival": {
            key: value for key, value in survival.items() if key != "rows"
        },
        "pause_hysteresis": {
            key: value for key, value in pause.items() if key != "rows"
        },
        "risk_delta": {
            "audit_rows": len(risk_delta_rows),
            "actions": dict(risk_delta_actions),
            "unknown_delta_rows": sum(
                row.get("notional_delta") is None for row in risk_delta_rows
            ),
            "keep_decisions": keep_count,
            "max_positive_candidate_delta": max(positive_deltas, default=0.0),
            "keep_nonzero_delta_rows": keep_nonzero_deltas,
        },
        "risk_reservation": risk_summary,
        "validation": validation,
        "asset_execution_status": asset_status,
        "comparison": comparison_rows,
        "readiness": {
            "stability_optimization": "PASS" if stability_pass else "FAIL",
            "ready_for_24_48h_frozen_baseline": "YES"
            if stability_pass and survival["evidence_sample_count"] >= 5
            else "NO",
            "quote_optimization": "NO",
            "fill_optimization": "NO",
            "volume_optimization": "NO",
            "reasons": baseline_reasons,
            "next_action": (
                "Continue only to the bounded 24-48h frozen baseline gate."
                if stability_pass
                else (
                    "Keep strategy and economic parameters frozen; repair stability "
                    "evidence first."
                )
            ),
        },
        "artifact_root": str(root),
    }
    _write_csv(root / "stability_comparison.csv", comparison_rows)
    _write_csv(root / "same_frame_cancel_audit.csv", same_frame_rows)
    _write_csv(root / "risk_delta_audit.csv", risk_delta_rows)
    _write_csv(root / "risk_reservation_audit.csv", risk_reservation_rows)
    _write_csv(root / "pause_hysteresis.csv", pause["rows"])
    _write_csv(root / "pause_episodes.csv", pause["episodes"])
    _write_csv(root / "quote_survival.csv", survival["rows"])
    _write_csv(root / "asset_execution_status.csv", asset_status)
    _write_csv(root / "create_decision_reconciliation.csv", create_reconciliation["rows"])
    (root / "shadow_validation_summary.json").write_text(
        json.dumps(_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if control:
        (root / "parent_stage12g_summary.json").write_text(
            json.dumps(_safe(control), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    config_diff = {
        "parent_config_hash": (
            control.get("shadow_config_hash")
            or control.get("config_hash")
            or stage12g.get("shadow_config_hash")
        ),
        "parent_strategy_behavior_hash": (
            control.get("strategy_config_hash")
            or stage12g.get("strategy_config_hash")
        ),
        "stage13_config_hash": config.get("shadow_config_hash"),
        "stage13_strategy_behavior_hash": config.get("strategy_config_hash"),
        "stage13_profile": config.get("stage13", {}),
        "strategy_parameters_changed": False,
        "execution_enabled": False,
        "allow_mainnet_trading": False,
        "behavior_changes": [
            "final pre-create eligibility is audited before virtual instantiation",
            "existing pending reservations are reconciled with incremental risk deltas",
            "STRATEGY_REGIME pause entry and recovery use bounded confirmation windows",
            "BTC remains SIGNAL_ONLY",
            "ETH remains SIGNAL_ONLY_MIN_SIZE",
        ],
        "note": "Stage 13 changes only bounded stability controls and audit routing.",
    }
    (root / "config_diff.md").write_text(
        "# Stage 13 configuration boundary\n\n"
        + "```json\n"
        + json.dumps(_safe(config_diff), indent=2, sort_keys=True)
        + "\n```\n",
        encoding="utf-8",
    )
    markdown = [
        "# Stage 13 — Bounded Market-Making Stability",
        "",
        "DERIVE MAINNET PUBLIC DATA / SHADOW ORDERS / NO REAL EXCHANGE MUTATIONS",
        "",
        f"- Safety: **{safety['status']}**",
        f"- Raw create decisions: **{raw_create}**; instantiated: **{instantiated}**",
        f"- Entered resting: **{entered_resting}**",
        f"- Same-frame create/cancel: **{same_frame_count}** ({same_frame_rate})",
        "- Conservative quote survival >=1/5/30/60 sec: **"
        f"{survival['counts']['stayed_resting_ge_1s']} / "
        f"{survival['counts']['stayed_resting_ge_5s']} / "
        f"{survival['counts']['stayed_resting_ge_30s']} / "
        f"{survival['counts']['stayed_resting_ge_60s']}**",
        f"- KEEP decisions: **{keep_count}**",
        f"- Median/P90 resting lifetime (s): **{stage13_median_lifetime} / "
        f"{stage13_p90_lifetime}**",
        f"- Risk-delta audit rows: **{len(risk_delta_rows)}**",
        "",
        "## Validation",
        "",
    ]
    markdown.extend(
        f"- {key.replace('_', ' ').title()}: **{value}**" for key, value in validation.items()
    )
    markdown.extend(
        [
            "",
            "## Pause and risk controls",
            "",
            f"- Strategy-regime pause episodes: **{pause['strategy_regime_pause_episodes']}**",
            f"- Transient pause episodes <=5s / <=30s: **{pause['transient_le_5s']} / "
            f"{pause['transient_le_30s']}**",
            "- Entry / exit confirmation (s): **"
            f"{stage13_profile.get('regime_pause_entry_confirm_seconds', 0)} / "
            f"{stage13_profile.get('regime_pause_exit_confirm_seconds', 0)}**",
            f"- Pending reserved gross: **{risk_summary['pending_reserved_gross']}**",
            f"- Worst-case gross: **{risk_summary['worst_case_gross']}**",
            "- Pending-risk self-invalidation events: "
            f"**{risk_summary['self_invalidation_events']}**",
            f"- KEEP double-count invariant: **{risk_summary['keep_double_count_invariant']}**",
            f"- Pending-risk oscillation: **{risk_summary['pending_risk_oscillation']}** "
            "(broad reservation signal; self-invalidation is reported separately)",
            "",
            "## Stage 12G comparison",
            "",
            "| Metric | Stage 12G control | Stage 13 | Delta |",
            "|---|---:|---:|---:|",
        ]
    )
    markdown.extend(
        f"| {row['metric']} | {row['stage12g_control']} | {row['stage13']} | "
        f"{row['delta_stage13_minus_control']} |"
        for row in comparison_rows
    )
    markdown.extend(
        [
            "",
        "## Asset execution status",
        "",
        "| Pair | Status | Enabled in cycle | Mutations allowed |",
        "|---|---|---:|---:|",
        ]
    )
    markdown.extend(
        f"| {row['trading_pair']} | {row['status']} | {row['enabled_in_cycle']} | "
        f"{row['execution_mutations_allowed']} |"
        for row in asset_status
    )
    markdown.extend(
        [
            "",
            "## Readiness",
            "",
            f"- Stability optimization: **{summary['readiness']['stability_optimization']}**",
            (
                "- Ready for 24–48h frozen baseline: "
                f"**{summary['readiness']['ready_for_24_48h_frozen_baseline']}**"
            ),
            "- Quote optimization: **NO**",
            "- Fill optimization: **NO**",
            "- Volume optimization: **NO**",
            "",
            "Reasons:",
            *([f"- {reason}" for reason in baseline_reasons] or ["- None observed"]),
            "",
            "Stage 13 does not authorize live execution, PnL optimization, quote optimization, "
            "or volume optimization.",
            "",
        ]
    )
    report_path = project / "reports" / "stage13_market_making_stability.md"
    report_path.write_text("\n".join(markdown), encoding="utf-8")
    session_root = project / "reports" / "shadow_baseline" / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    (session_root / "stage13_market_making_stability.md").write_text(
        "\n".join(markdown), encoding="utf-8"
    )
    summary["report_path"] = str(report_path)
    (root / "diagnostic_summary.json").write_text(
        json.dumps(_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return _safe(summary)


__all__ = [
    "CREATE_CATEGORIES",
    "EXECUTION_STATUSES",
    "Stage13StabilityConfig",
    "build_asset_execution_status",
    "build_create_decision_reconciliation",
    "build_pause_hysteresis",
    "build_quote_survival",
    "build_risk_delta_audit",
    "effective_asset_status",
    "write_stage13_artifacts",
]
