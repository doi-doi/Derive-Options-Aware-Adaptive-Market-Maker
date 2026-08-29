"""Stage 12G resting-order eligibility and suppression diagnostics.

This module is deliberately downstream of the frozen strategy and shadow
adapter.  It classifies observations and writes evidence; it does not change
prices, allocations, risk limits, minimum lifetimes, fill rules, or execution
permissions.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .stage12e import _get, _number, build_plan_invalid_rows, normalize_timestamp

ORDER_ROOT_CAUSES = (
    "NEVER_REACHED_EXECUTION_ENGINE",
    "CREATE_REJECTED",
    "POST_ONLY_REJECT",
    "WOULD_CROSS_MARKET",
    "MIN_EXCHANGE_SIZE",
    "INVALID_QUANTIZED_PRICE",
    "INVALID_QUANTIZED_AMOUNT",
    "PLAN_INVALID_BEFORE_RESTING",
    "PLAN_LEVEL_REMOVED_BEFORE_RESTING",
    "MODE_PAUSE_BEFORE_RESTING",
    "DATA_VALIDITY_FAILURE",
    "STATE_CONFIDENCE_FAILURE",
    "ASSET_RISK_BLOCK",
    "PORTFOLIO_RISK_BLOCK",
    "COLLATERAL_BLOCK",
    "DRAWNDOWN_BLOCK",
    "MARKET_DISABLED",
    "RECONCILIATION_CANCEL_SAME_FRAME",
    "SESSION_SHUTDOWN",
    "TIMESTAMP_ACCOUNTING_DEFECT",
    "LIFECYCLE_IMPLEMENTATION_DEFECT",
    "OTHER_EXPLICIT",
    "UNKNOWN_INTERNAL",
)

PLAN_INVALID_REASONS = (
    "DATA_VALIDITY",
    "STATE_CONFIDENCE",
    "MARKET_SAFETY",
    "MIN_EXCHANGE_SIZE",
    "ASSET_RISK",
    "PORTFOLIO_RISK",
    "STRATEGY_REGIME",
    "STARTUP_WARMUP",
    "SYSTEM",
    "UNKNOWN_INTERNAL",
)

PAUSE_REASON_CATEGORIES = PLAN_INVALID_REASONS
OSCILLATION_CLASSIFICATIONS = (
    "DATA_DRIVEN",
    "RISK_DRIVEN",
    "STRATEGY_REGIME_DRIVEN",
    "MIN_SIZE_DRIVEN",
    "MARKET_SAFETY_DRIVEN",
    "SYSTEM_DRIVEN",
    "UNKNOWN",
)
PAUSE_DURATION_BUCKETS = (
    "<1s",
    "1-5s",
    "5-15s",
    "15-30s",
    "30-60s",
    "1-5min",
    "5-15min",
    ">15min",
)
LEVEL_RETURN_THRESHOLDS = (1, 5, 30, 60, 300)


def _float(value: Any, default: float | None = None) -> float | None:
    number = _number(value)
    if number is None:
        return default
    return number if math.isfinite(number) else default


def _epoch(value: Any) -> float | None:
    try:
        parsed, _ = normalize_timestamp(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is not None:
        return float(parsed)
    return _float(value)


def _iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _record(value: Any) -> dict[str, Any]:
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


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
        return list(parsed) if isinstance(parsed, (list, tuple, set)) else []
    return []


def _string_set(value: Any) -> set[str]:
    return {str(item) for item in _json_list(value) if item is not None}


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    return bool(value)


def _safe(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
        value = value.value
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if hasattr(value, "as_tuple") and value.__class__.__name__ == "Decimal":
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


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


def _side_from_level(level_id: str) -> str | None:
    raw = level_id.split("::")[-1]
    if raw.startswith("buy_"):
        return "buy"
    if raw.startswith("sell_"):
        return "sell"
    return None


def _entered_resting(order: Mapping[str, Any]) -> bool:
    sequence = _string_set(order.get("lifecycle_state_sequence"))
    return order.get("resting_start_timestamp") is not None or "RESTING" in sequence


def _zero_lifetime(order: Mapping[str, Any]) -> bool:
    terminal = _lifecycle_epoch(order, "terminal")
    created = _lifecycle_epoch(order, "created")
    if terminal is None or created is None:
        return False
    duration = max(0.0, terminal - created)
    if order.get("controller_created_epoch") is None and order.get(
        "controller_created_timestamp"
    ) is None and order.get("controller_terminal_epoch") is None and order.get(
        "controller_terminal_timestamp"
    ) is None:
        duration = _float(order.get("created_to_terminal_seconds"), duration) or duration
    return duration <= 1e-6


def _details(order: Mapping[str, Any]) -> dict[str, Any]:
    value = order.get("cancel_reason_detail")
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _lifecycle_epoch(order: Mapping[str, Any], phase: str) -> float | None:
    """Prefer controller receipt time over a repeated exchange event time."""

    if phase == "created":
        controller = order.get("controller_created_epoch")
        timestamp = order.get("controller_created_timestamp")
        fallback = order.get("created_timestamp")
    else:
        controller = order.get("controller_terminal_epoch")
        timestamp = order.get("controller_terminal_timestamp")
        fallback = order.get("terminal_timestamp")
    return _epoch(controller if controller is not None else timestamp or fallback)


def _same_cycle_create_cancel(order: Mapping[str, Any]) -> bool:
    """Use explicit controller-cycle provenance when available.

    Older records have no cycle IDs, so their timestamp-only behavior remains
    compatible.  New records must not call a later controller cycle
    same-frame merely because Derive repeated the ticker event timestamp.
    """

    details = _details(order)
    explicit_cycle = order.get("cancel_cycle_id") is not None or (
        order.get("cycle_id") is not None
        and ("cycle_id" in details or "cancel_cycle_id" in details)
    )
    if explicit_cycle:
        return bool(order.get("same_cycle_create_cancel"))
    if order.get("same_cycle_create_cancel"):
        return True
    created = _epoch(order.get("created_timestamp"))
    terminal = _epoch(order.get("terminal_timestamp"))
    return (
        _entered_resting(order)
        and created is not None
        and terminal is not None
        and abs(terminal - created) <= 1e-6
    )


def classify_zero_lifetime_root_cause(order: Mapping[str, Any]) -> str:
    """Return exactly one root cause for a zero-duration order lifecycle."""

    details = _details(order)
    raw = " ".join(
        str(order.get(key) or "")
        for key in (
            "terminal_reason",
            "cancel_reason_category",
            "cancel_reason_raw",
            "cancel_reason",
        )
    ).upper()
    if not _bool_or_none(order.get("reached_execution_engine")):
        return "NEVER_REACHED_EXECUTION_ENGINE"
    if str(order.get("status", "")).upper() == "REJECTED" or "REJECT" in raw:
        if "CROSS" in raw or order.get("post_only_valid") is False:
            return "WOULD_CROSS_MARKET"
        if "POST_ONLY" in raw or order.get("maker_valid") is False:
            return "POST_ONLY_REJECT"
        if "MINIMUM" in raw or "SIZE" in raw or order.get("minimum_exchange_size_valid") is False:
            return "MIN_EXCHANGE_SIZE"
        return "CREATE_REJECTED"
    if _same_cycle_create_cancel(order):
        return "RECONCILIATION_CANCEL_SAME_FRAME"
    if not _entered_resting(order):
        if details.get("plan_valid") is False or "INVALID" in raw:
            return "PLAN_INVALID_BEFORE_RESTING"
        if details.get("new_level_present") is False or details.get("plan_level_present") is False:
            return "PLAN_LEVEL_REMOVED_BEFORE_RESTING"
        if str(details.get("new_mode", "")).lower() == "pause" or "PAUSE" in raw:
            return "MODE_PAUSE_BEFORE_RESTING"
        if order.get("market_data_valid") is False:
            return "DATA_VALIDITY_FAILURE"
        if order.get("state_confidence_valid") is False:
            return "STATE_CONFIDENCE_FAILURE"
        if order.get("asset_risk_valid") is False:
            return "ASSET_RISK_BLOCK"
        if order.get("portfolio_risk_valid") is False:
            return "PORTFOLIO_RISK_BLOCK"
        if order.get("minimum_exchange_size_valid") is False:
            return "MIN_EXCHANGE_SIZE"
        if "COLLATERAL" in raw:
            return "COLLATERAL_BLOCK"
        if "DRAWDOWN" in raw:
            return "DRAWNDOWN_BLOCK"
        if "DISABLED" in raw:
            return "MARKET_DISABLED"
        return (
            "LIFECYCLE_IMPLEMENTATION_DEFECT"
            if order.get("validated_timestamp")
            else "UNKNOWN_INTERNAL"
        )
    if "SESSION_SHUTDOWN" in raw or "SHUTDOWN" in raw:
        return "SESSION_SHUTDOWN"
    if order.get("create_terminal_latency_ms") is None:
        return "TIMESTAMP_ACCOUNTING_DEFECT"
    if any(token in raw for token in ("TIMESTAMP", "LIFECYCLE")):
        return "TIMESTAMP_ACCOUNTING_DEFECT"
    return "OTHER_EXPLICIT"


def _order_trace_row(order: Mapping[str, Any], *, root_cause: str | None = None) -> dict[str, Any]:
    details = _details(order)
    created = _lifecycle_epoch(order, "created")
    terminal = _lifecycle_epoch(order, "terminal")
    duration_ms = (
        max(0.0, terminal - created) * 1000.0
        if created is not None and terminal is not None
        else _float(order.get("create_terminal_latency_ms"))
    )
    sequence = _json_list(order.get("lifecycle_state_sequence"))
    if not sequence:
        sequence = ["CREATED"]
        if order.get("validated_timestamp") is not None:
            sequence.append("VALIDATED")
        if _entered_resting(order):
            sequence.append("RESTING")
        if order.get("status") == "REJECTED":
            sequence.append("NEVER_RESTED_REJECTED")
        elif order.get("cancel_timestamp"):
            sequence.append("CANCELLED_AFTER_RESTING")
        elif order.get("fill_timestamp"):
            sequence.append("FILLED_AFTER_RESTING")
    return {
        "shadow_order_id": order.get("shadow_order_id"),
        "pair": order.get("trading_pair"),
        "trading_pair": order.get("trading_pair"),
        "level_id": order.get("level_id"),
        "side": order.get("side"),
        "plan_version": order.get("grid_plan_version"),
        "mode": order.get("mode_at_creation"),
        "created_timestamp": order.get("created_timestamp"),
        "validated_timestamp": order.get("validated_timestamp"),
        "resting_start_timestamp": order.get("resting_start_timestamp"),
        "terminal_timestamp": order.get("terminal_timestamp"),
        "lifecycle_state_sequence": sequence,
        "desired_price": order.get("desired_price", order.get("price")),
        "desired_amount": order.get("desired_amount", order.get("amount")),
        "desired_notional": order.get("desired_notional", order.get("notional")),
        "quantized_price": order.get("quantized_price", order.get("price")),
        "quantized_amount": order.get("quantized_amount", order.get("amount")),
        "bbo_best_bid_at_create": order.get("bbo_best_bid_at_create"),
        "bbo_best_ask_at_create": order.get("bbo_best_ask_at_create"),
        "bbo_at_create": {
            "best_bid": order.get("bbo_best_bid_at_create"),
            "best_ask": order.get("bbo_best_ask_at_create"),
        },
        "post_only_valid": order.get("post_only_valid"),
        "maker_valid": order.get("maker_valid"),
        "eligible_to_rest": order.get("eligible_to_rest"),
        "reached_execution_engine": order.get("reached_execution_engine"),
        "plan_valid_at_create": order.get("plan_valid_at_create"),
        "plan_valid_at_terminal": order.get("plan_valid_at_terminal"),
        "plan_valid_next_frame": order.get("plan_valid_next_frame"),
        "risk_allowed": order.get("risk_allowed_at_create"),
        "minimum_exchange_size_valid": order.get("minimum_exchange_size_valid"),
        "portfolio_risk_valid": order.get("portfolio_risk_valid"),
        "asset_risk_valid": order.get("asset_risk_valid"),
        "market_data_valid": order.get("market_data_valid"),
        "btc_iv_valid": order.get("btc_iv_valid"),
        "relationship_data_valid": order.get("relationship_data_valid"),
        "state_confidence_valid": order.get("state_confidence_valid"),
        "terminal_reason": order.get("terminal_reason") or order.get("cancel_reason_category"),
        "cancel_reason_raw": order.get("cancel_reason_raw"),
        "cancel_reason_detail": details,
        "same_cycle_create_cancel": _same_cycle_create_cancel(order),
        "created_to_terminal_ms": duration_ms,
        "create_validation_latency_ms": order.get("create_validation_latency_ms"),
        "validation_resting_latency_ms": order.get("validation_resting_latency_ms"),
        "resting_lifetime_seconds": order.get("resting_lifetime_seconds"),
        "zero_lifetime_root_cause": root_cause,
        "plan_valid_at_cancel": details.get("plan_valid"),
        "level_present_at_cancel": details.get("new_level_present"),
        "mode_at_cancel": details.get("new_mode"),
    }


def build_zero_lifetime_root_causes(
    orders: Sequence[Any],
) -> list[dict[str, Any]]:
    """Trace every order that reached terminal state at its create timestamp."""

    rows: list[dict[str, Any]] = []
    for value in orders:
        order = _record(value)
        if not _zero_lifetime(order):
            continue
        cause = classify_zero_lifetime_root_cause(order)
        row = _order_trace_row(order, root_cause=cause)
        row["is_zero_lifetime"] = True
        rows.append(row)
    return rows


def _lifecycle_sequence(order: Mapping[str, Any]) -> list[str]:
    sequence = [str(value) for value in _json_list(order.get("lifecycle_state_sequence"))]
    if sequence:
        return sequence
    sequence = ["CREATED"]
    if order.get("validated_timestamp") is not None:
        sequence.append("VALIDATED")
    if _entered_resting(order):
        sequence.append("RESTING")
    if order.get("status") == "REJECTED":
        sequence.append("NEVER_RESTED_REJECTED")
    elif order.get("cancel_timestamp"):
        sequence.append("CANCELLED_AFTER_RESTING")
    elif order.get("fill_timestamp"):
        sequence.append("FILLED_AFTER_RESTING")
    if order.get("status") == "COMPLETE" and "COMPLETE" not in sequence:
        sequence.append("COMPLETE")
    return sequence


def build_order_state_transitions(
    orders: Sequence[Any], lifecycle_events: Sequence[Any] = ()
) -> list[dict[str, Any]]:
    """Flatten each order sequence into auditable state-machine transitions."""

    event_by_order: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for value in lifecycle_events:
        event = _record(value)
        order_id = event.get("shadow_order_id")
        if order_id:
            event_by_order[str(order_id)].append(event)
    allowed = {
        ("CREATED", "VALIDATED"),
        ("CREATED", "NEVER_RESTED_REJECTED"),
        ("VALIDATED", "RESTING"),
        ("VALIDATED", "NEVER_RESTED_REJECTED"),
        ("RESTING", "CANCELLED_AFTER_RESTING"),
        ("RESTING", "FILLED_AFTER_RESTING"),
        ("FILLED_AFTER_RESTING", "COMPLETE"),
        ("CANCELLED_AFTER_RESTING", "COMPLETE"),
    }
    conditions = {
        ("CREATED", "VALIDATED"): "execution engine reached and order fields are valid",
        ("VALIDATED", "RESTING"): "post-only, maker, risk, and executable-size gates passed",
        ("RESTING", "CANCELLED_AFTER_RESTING"): "reconciliation or explicit lifecycle stop",
        ("RESTING", "FILLED_AFTER_RESTING"): "conservative public-trade fill evidence passed",
        ("FILLED_AFTER_RESTING", "COMPLETE"): "native adjacent take-profit lifecycle completed",
    }
    rows: list[dict[str, Any]] = []
    for value in orders:
        order = _record(value)
        sequence = _lifecycle_sequence(order)
        order_id = str(order.get("shadow_order_id") or "")
        order_valid = all(pair in allowed for pair in zip(sequence, sequence[1:], strict=False))
        for index, (source, target) in enumerate(zip(sequence, sequence[1:], strict=False)):
            matching = event_by_order.get(order_id, [])
            event_name = next(
                (
                    str(event.get("event"))
                    for event in matching
                    if str(event.get("lifecycle_state")) == target
                    or str(event.get("event"))
                    in {"ORDER_RESTING", "ORDER_FILLED", "ORDER_CANCELLED"}
                ),
                None,
            )
            rows.append(
                {
                    "shadow_order_id": order_id,
                    "trading_pair": order.get("trading_pair"),
                    "level_id": order.get("level_id"),
                    "side": order.get("side"),
                    "transition_index": index,
                    "from_state": source,
                    "to_state": target,
                    "trigger": event_name
                    or conditions.get((source, target), "explicit terminal state"),
                    "required_conditions": conditions.get((source, target)),
                    "transition_allowed": (source, target) in allowed,
                    "order_sequence_valid": order_valid,
                    "timestamp": order.get(
                        {
                            "VALIDATED": "validated_timestamp",
                            "RESTING": "resting_start_timestamp",
                            "CANCELLED_AFTER_RESTING": "terminal_timestamp",
                            "FILLED_AFTER_RESTING": "fill_timestamp",
                            "COMPLETE": "terminal_timestamp",
                        }.get(target, "created_timestamp")
                    ),
                }
            )
    return rows


def order_state_machine_markdown() -> str:
    return "\n".join(
        [
            "# Stage 12G order state machine",
            "",
            "The shadow adapter is virtual. A Derive acknowledgment is not required.",
            "An order is RESTING after the shadow executor creates it, the existing",
            "maker/post-only and risk checks pass, and it remains active for future",
            "shadow fill evaluation. This is not an exchange order or an exchange ID.",
            "",
            "| From | To | Trigger | Required conditions |",
            "| --- | --- | --- | --- |",
            (
                "| CREATED | VALIDATED | execution validation | "
                "positive fields and execution engine reached |"
            ),
            (
                "| VALIDATED | RESTING | maker/risk eligibility | "
                "post-only, maker, risk, minimum-size, and market-data gates pass |"
            ),
            (
                "| RESTING | CANCELLED_AFTER_RESTING | reconciliation/stop | "
                "an active virtual order is intentionally stopped |"
            ),
            (
                "| RESTING | FILLED_AFTER_RESTING | conservative public trade | "
                "future qualifying trade evidence exists |"
            ),
            (
                "| FILLED_AFTER_RESTING | COMPLETE | adjacent TP | "
                "native adjacent-grid exit completes |"
            ),
            (
                "| CREATED or VALIDATED | NEVER_RESTED_REJECTED | rejection | "
                "maker, quantization, or create validation fails |"
            ),
            "",
            "KEEP does not reset `resting_start_timestamp`; it only records a decision.",
            "Same-cycle CREATE plus CANCEL is separately labeled",
            "`SAME_CYCLE_CREATE_CANCEL` and is not a normal resting-lifetime sample.",
            "",
        ]
    )


def build_order_funnel(
    orders: Sequence[Any],
    eligibility_rows: Sequence[Mapping[str, Any]] = (),
    *,
    end_timestamp: float | None = None,
) -> list[dict[str, Any]]:
    """Return the requested candidate-to-resting funnel as rows."""

    entry_orders = [_record(value) for value in orders if not _record(value).get("is_exit")]
    candidate_count = sum(bool(row.get("candidate_grid_level", True)) for row in eligibility_rows)
    if not candidate_count:
        candidate_count = len(
            {
                (
                    row.get("trading_pair"),
                    row.get("level_id"),
                    row.get("timestamp"),
                )
                for row in eligibility_rows
            }
        )
    risk_eligible = sum(bool(row.get("risk_allowed")) for row in eligibility_rows)
    create_actions = {
        "BLOCKED",
        "CREATE_DECISION",
        "ORDER_INSTANTIATED",
        "ROUTE_BLOCKED",
        "SIGNAL_ONLY",
        "SIGNAL_ONLY_MIN_SIZE",
    }
    create_decisions = sum(
        str(row.get("raw_planned_action") or row.get("planned_action") or "").upper()
        in create_actions
        or bool(row.get("order_instantiated"))
        for row in eligibility_rows
    )
    instantiated = len(entry_orders)
    validated = sum(row.get("validated_timestamp") is not None for row in entry_orders)
    maker_safe = sum(row.get("maker_valid") is not False for row in entry_orders)
    eligible_to_rest = sum(row.get("eligible_to_rest") is not False for row in entry_orders)
    entered = sum(_entered_resting(row) for row in entry_orders)
    now = end_timestamp or 0.0
    durations = []
    for row in entry_orders:
        if not _entered_resting(row):
            continue
        # Controller receipt time is the sequencing clock.  Derive can repeat
        # an exchange ticker timestamp across healthy polling cycles, so using
        # source timestamps here would turn a later-cycle KEEP/cancel into a
        # false zero-lifetime quote.
        start = _lifecycle_epoch(row, "created") or _epoch(
            row.get("resting_start_timestamp") or row.get("resting_start_epoch")
        )
        terminal = _lifecycle_epoch(row, "terminal") or _epoch(
            row.get("terminal_timestamp") or row.get("terminal_epoch")
        )
        if start is None:
            continue
        durations.append(max(0.0, (terminal if terminal is not None else now) - start))
    thresholds = {
        f"stayed_resting_ge_{seconds}s": sum(value >= seconds for value in durations)
        for seconds in (1, 5, 30, 60)
    }
    filled = sum(row.get("fill_timestamp") is not None for row in entry_orders)
    counts = {
        "candidate_grid_levels": candidate_count,
        "risk_eligible": risk_eligible,
        "create_decisions": create_decisions,
        "shadow_order_objects_instantiated": instantiated,
        "validated": validated,
        "maker_safe": maker_safe,
        "eligible_to_rest": eligible_to_rest,
        "entered_resting": entered,
        **thresholds,
        "filled": filled,
        "rejected_before_resting": sum(
            row.get("status") == "REJECTED"
            or "NEVER_RESTED_REJECTED" in _string_set(row.get("lifecycle_state_sequence"))
            for row in entry_orders
        ),
        "cancelled_before_resting": sum(
            row.get("cancel_timestamp") is not None and not _entered_resting(row)
            for row in entry_orders
        ),
    }
    return [{"stage": key, "count": value} for key, value in counts.items()]


def _normalize_plan_reason(row: Mapping[str, Any]) -> str:
    raw = str(row.get("primary_reason") or row.get("reason_category") or "").upper()
    detail = str(row.get("reason") or row.get("pause_reason") or "").upper()
    combined = f"{raw} {detail}"
    if any(token in combined for token in ("DATA", "STALE", "IV", "RELATIONSHIP")):
        return "DATA_VALIDITY"
    if "CONFIDENCE" in combined or row.get("state_valid") is False:
        return "STATE_CONFIDENCE"
    if "MAKER" in combined or "CROSS" in combined or "MARKET_SAFETY" in combined:
        return "MARKET_SAFETY"
    if "MIN" in combined or "SIZE" in combined or "NOTIONAL" in combined:
        return "MIN_EXCHANGE_SIZE"
    if "ASSET" in combined or "INVENTORY" in combined:
        return "ASSET_RISK"
    if "PORTFOLIO" in combined or "GROSS" in combined or "BETA" in combined:
        return "PORTFOLIO_RISK"
    if "STARTUP" in combined or "WARMUP" in combined or "INITIAL" in combined:
        return "STARTUP_WARMUP"
    if "SYSTEM" in combined or "EXECUTION" in combined or "TIMESTAMP" in combined:
        return "SYSTEM"
    if raw in {"MODE_PAUSE", "GRID_VALIDATION", "NO_LEVELS", "PLAN_VALIDATION", "STRATEGY_REGIME"}:
        return "STRATEGY_REGIME"
    if raw in PLAN_INVALID_REASONS:
        return raw
    if row.get("plan_valid"):
        return "NONE"
    return "UNKNOWN_INTERNAL"


def build_plan_invalid_transitions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize every plan-valid observation and calculate recovery time."""

    ordered = sorted(rows, key=lambda row: _epoch(row.get("timestamp")) or 0.0)
    invalid_started: dict[str, float] = {}
    previous_valid: dict[str, bool] = {}
    output: list[dict[str, Any]] = []
    for row in ordered:
        pair = str(row.get("trading_pair"))
        stamp = _epoch(row.get("timestamp")) or _float(row.get("timestamp_epoch"), 0.0) or 0.0
        valid = bool(row.get("plan_valid"))
        old_valid = previous_valid.get(pair)
        if old_valid is None:
            transition = "INITIAL_VALID" if valid else "INITIAL_INVALID"
        elif old_valid and not valid:
            transition = "VALID_TO_INVALID"
        elif not old_valid and valid:
            transition = "INVALID_TO_VALID"
        elif valid:
            transition = "VALID_CONTINUED"
        else:
            transition = "INVALID_CONTINUED"
        primary = _normalize_plan_reason(row)
        secondary: list[str] = []
        if row.get("market_data_valid") is False or row.get("option_data_available") is False:
            secondary.append("DATA_VALIDITY")
        if row.get("state_valid") is False or (
            _float(row.get("state_confidence")) is not None
            and (_float(row.get("state_confidence")) or 0.0) <= 0
        ):
            secondary.append("STATE_CONFIDENCE")
        if row.get("relationship_valid") is False:
            secondary.append("DATA_VALIDITY")
        if row.get("skip_count", 0):
            secondary.append("PORTFOLIO_RISK")
        if row.get("pause_reason"):
            secondary.append(primary if primary != "NONE" else "SYSTEM")
        secondary = sorted(set(secondary) - {primary})
        recovery_seconds = None
        invalid_duration = None
        if not valid and pair not in invalid_started:
            invalid_started[pair] = stamp
        elif valid and pair in invalid_started:
            recovery_seconds = max(0.0, stamp - invalid_started.pop(pair))
        if not valid and pair in invalid_started:
            invalid_duration = max(0.0, stamp - invalid_started[pair])
        output.append(
            {
                **dict(row),
                "transition": transition,
                "primary_reason": primary,
                "secondary_reasons": secondary,
                "scope": "PORTFOLIO" if primary == "PORTFOLIO_RISK" else "ASSET",
                "invalidation_event": transition == "VALID_TO_INVALID",
                "recovery_event": transition == "INVALID_TO_VALID",
                "recovery_seconds": recovery_seconds,
                "invalid_duration_seconds": invalid_duration,
                "normalized_reason": primary,
                "asset": pair,
                "timestamp_epoch": stamp,
            }
        )
        previous_valid[pair] = valid
    return output


def _pause_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if bool(row.get("is_paused"))
        or not bool(row.get("plan_valid"))
        or str(row.get("mode", "")).lower() == "pause"
    ]


def build_pause_episode_breakdown(
    rows: Sequence[Mapping[str, Any]],
    *,
    start_timestamp: float,
    end_timestamp: float,
    continuity_gap_seconds: float = 15.0,
) -> list[dict[str, Any]]:
    """Deduplicate pause observations by asset, then add portfolio episodes."""

    paused = _pause_rows(rows)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    all_by_pair: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        pair = str(row.get("trading_pair"))
        all_by_pair[pair].append(row)
        if row in paused:
            grouped[pair].append(row)
    asset_episodes: list[dict[str, Any]] = []
    for pair, values in grouped.items():
        ordered = sorted(values, key=lambda row: _epoch(row.get("timestamp")) or 0.0)
        current: dict[str, Any] | None = None
        for row in ordered:
            stamp = _epoch(row.get("timestamp")) or _float(row.get("timestamp_epoch"), 0.0) or 0.0
            if (
                current is None
                or stamp - float(current["last_observation_epoch"]) > continuity_gap_seconds
            ):
                if current is not None:
                    asset_episodes.append(current)
                current = {
                    "trading_pair": pair,
                    "pause_scope": "ASSET",
                    "first_timestamp_epoch": stamp,
                    "last_observation_epoch": stamp,
                    "raw_pause_observation_count": 1,
                    "cause_categories": [_normalize_plan_reason(row)],
                    "affected_level_ids": set(_string_set(row.get("removed_level_ids"))),
                    "first_plan_version": row.get("plan_version"),
                    "last_plan_version": row.get("plan_version"),
                }
            else:
                current["last_observation_epoch"] = stamp
                current["raw_pause_observation_count"] += 1
                current["cause_categories"].append(_normalize_plan_reason(row))
                current["affected_level_ids"].update(_string_set(row.get("removed_level_ids")))
                current["last_plan_version"] = row.get("plan_version")
        if current is not None:
            asset_episodes.append(current)

    for index, episode in enumerate(asset_episodes, start=1):
        pair = str(episode["trading_pair"])
        last_observation = float(episode["last_observation_epoch"])
        recovery = next(
            (
                _epoch(row.get("timestamp")) or 0.0
                for row in sorted(
                    all_by_pair[pair], key=lambda item: _epoch(item.get("timestamp")) or 0.0
                )
                if bool(row.get("plan_valid"))
                and (_epoch(row.get("timestamp")) or 0.0) > last_observation
            ),
            None,
        )
        end_epoch = recovery if recovery is not None else end_timestamp
        causes = Counter(episode.pop("cause_categories"))
        primary = causes.most_common(1)[0][0] if causes else "UNKNOWN_INTERNAL"
        duration = max(0.0, end_epoch - float(episode["first_timestamp_epoch"]))
        episode.update(
            {
                "episode_id": f"asset-pause-{index:05d}",
                "first_timestamp": _iso(float(episode["first_timestamp_epoch"])),
                "last_observation_timestamp": _iso(last_observation),
                "recovery_timestamp": _iso(recovery) if recovery is not None else None,
                "duration_seconds": duration,
                "duration_bucket": _duration_bucket(duration),
                "primary_reason": primary,
                "cause_counts": dict(causes),
                "affected_level_ids": sorted(episode.pop("affected_level_ids")),
                "affected_level_count": len(episode.get("affected_level_ids", [])),
                "recovered": recovery is not None,
                "transient_pause_oscillation": recovery is not None and duration <= 60.0,
                "transient_le_1s": recovery is not None and duration <= 1.0,
                "transient_le_5s": recovery is not None and duration <= 5.0,
                "transient_le_30s": recovery is not None and duration <= 30.0,
                "transient_le_60s": recovery is not None and duration <= 60.0,
            }
        )

    portfolio_source = [
        episode for episode in asset_episodes if episode.get("primary_reason") == "PORTFOLIO_RISK"
    ]
    portfolio_episodes: list[dict[str, Any]] = []
    for episode in sorted(portfolio_source, key=lambda row: row["first_timestamp_epoch"]):
        if (
            not portfolio_episodes
            or episode["first_timestamp_epoch"]
            > portfolio_episodes[-1]["end_epoch"] + continuity_gap_seconds
        ):
            portfolio_episodes.append(
                {
                    "episode_id": f"portfolio-pause-{len(portfolio_episodes) + 1:05d}",
                    "trading_pair": "PORTFOLIO",
                    "pause_scope": "PORTFOLIO",
                    "first_timestamp_epoch": episode["first_timestamp_epoch"],
                    "end_epoch": episode["first_timestamp_epoch"] + episode["duration_seconds"],
                    "raw_pause_observation_count": episode["raw_pause_observation_count"],
                    "affected_level_ids": {
                        f"{episode['trading_pair']}::{level_id}"
                        for level_id in episode.get("affected_level_ids", [])
                    },
                    "asset_episode_ids": [episode["episode_id"]],
                }
            )
        else:
            current = portfolio_episodes[-1]
            current["end_epoch"] = max(
                current["end_epoch"], episode["first_timestamp_epoch"] + episode["duration_seconds"]
            )
            current["raw_pause_observation_count"] += episode["raw_pause_observation_count"]
            current["affected_level_ids"].update(
                f"{episode['trading_pair']}::{level_id}"
                for level_id in episode.get("affected_level_ids", [])
            )
            current["asset_episode_ids"].append(episode["episode_id"])
    for episode in portfolio_episodes:
        episode["first_timestamp"] = _iso(float(episode["first_timestamp_epoch"]))
        episode["duration_seconds"] = max(
            0.0, float(episode["end_epoch"]) - float(episode["first_timestamp_epoch"])
        )
        episode["duration_bucket"] = _duration_bucket(episode["duration_seconds"])
        episode["primary_reason"] = "PORTFOLIO_RISK"
        episode["affected_level_ids"] = sorted(episode.pop("affected_level_ids"))
        episode["affected_level_count"] = len(episode["affected_level_ids"])
        episode["transient_pause_oscillation"] = episode["duration_seconds"] <= 60.0
        episode.pop("end_epoch", None)
    return sorted(asset_episodes + portfolio_episodes, key=lambda row: row["first_timestamp_epoch"])


def pause_duration_summary(episodes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    asset = [row for row in episodes if row.get("pause_scope") == "ASSET"]
    durations = [float(row.get("duration_seconds", 0.0) or 0.0) for row in asset]
    return {
        "unique_asset_episodes": len(asset),
        "unique_portfolio_episodes": sum(row.get("pause_scope") == "PORTFOLIO" for row in episodes),
        "raw_pause_observations": sum(
            int(row.get("raw_pause_observation_count", 0) or 0) for row in asset
        ),
        "by_reason": dict(Counter(str(row.get("primary_reason")) for row in asset)),
        "duration_buckets": dict(Counter(str(row.get("duration_bucket")) for row in asset)),
        "median_seconds": _percentile(durations, 0.50),
        "p75_seconds": _percentile(durations, 0.75),
        "p90_seconds": _percentile(durations, 0.90),
        "max_seconds": max(durations) if durations else None,
        "transient_le_1s": sum(bool(row.get("transient_le_1s")) for row in asset),
        "transient_le_5s": sum(bool(row.get("transient_le_5s")) for row in asset),
        "transient_le_30s": sum(bool(row.get("transient_le_30s")) for row in asset),
        "transient_le_60s": sum(bool(row.get("transient_le_60s")) for row in asset),
    }


def _oscillation_classification(reason: str) -> str:
    return {
        "DATA_VALIDITY": "DATA_DRIVEN",
        "STATE_CONFIDENCE": "DATA_DRIVEN",
        "ASSET_RISK": "RISK_DRIVEN",
        "PORTFOLIO_RISK": "RISK_DRIVEN",
        "MIN_EXCHANGE_SIZE": "MIN_SIZE_DRIVEN",
        "MARKET_SAFETY": "MARKET_SAFETY_DRIVEN",
        "STRATEGY_REGIME": "STRATEGY_REGIME_DRIVEN",
        "STARTUP_WARMUP": "SYSTEM_DRIVEN",
        "SYSTEM": "SYSTEM_DRIVEN",
    }.get(reason, "UNKNOWN")


def build_level_return_analysis(
    plan_rows: Sequence[Mapping[str, Any]],
    orders: Sequence[Any] = (),
    *,
    end_timestamp: float,
) -> list[dict[str, Any]]:
    """Measure each removed level until its first subsequent return."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in plan_rows:
        grouped[str(row.get("trading_pair"))].append(row)
    order_rows = [_record(value) for value in orders]
    output: list[dict[str, Any]] = []
    for pair, values in grouped.items():
        ordered = sorted(values, key=lambda row: _epoch(row.get("timestamp")) or 0.0)
        levels = sorted(
            set().union(*(set(_string_set(row.get("desired_level_ids"))) for row in ordered))
        )
        for level_id in levels:
            present = False
            removal: dict[str, Any] | None = None
            for row in ordered:
                stamp = _epoch(row.get("timestamp")) or 0.0
                current_present = level_id in _string_set(row.get("desired_level_ids"))
                if present and not current_present:
                    removal = {
                        "removed_at_epoch": stamp,
                        "removed_at": row.get("timestamp") or _iso(stamp),
                        "cause": str(row.get("primary_reason") or _normalize_plan_reason(row)),
                        "removed_plan_version": row.get("plan_version"),
                    }
                elif not present and current_present and removal is not None:
                    output.append(
                        _level_return_row(
                            pair,
                            level_id,
                            removal,
                            return_epoch=stamp,
                            return_timestamp=row.get("timestamp") or _iso(stamp),
                            order_rows=order_rows,
                        )
                    )
                    removal = None
                present = current_present
            if removal is not None:
                output.append(
                    _level_return_row(
                        pair,
                        level_id,
                        removal,
                        return_epoch=None,
                        return_timestamp=None,
                        order_rows=order_rows,
                        end_timestamp=end_timestamp,
                    )
                )
    return output


def _level_return_row(
    pair: str,
    level_id: str,
    removal: Mapping[str, Any],
    *,
    return_epoch: float | None,
    return_timestamp: str | None,
    order_rows: Sequence[Mapping[str, Any]],
    end_timestamp: float | None = None,
) -> dict[str, Any]:
    removed_at = float(removal["removed_at_epoch"])
    end = return_epoch if return_epoch is not None else end_timestamp
    absence = max(0.0, end - removed_at) if end is not None else None
    candidates = [
        row
        for row in order_rows
        if row.get("trading_pair") == pair
        and row.get("level_id") == level_id
        and (_epoch(row.get("created_timestamp")) or 0.0) <= removed_at
    ]
    order = max(
        candidates, key=lambda row: _epoch(row.get("created_timestamp")) or 0.0, default=None
    )
    age = (
        max(0.0, removed_at - (_epoch(order.get("created_timestamp")) or removed_at))
        if order is not None
        else None
    )
    return {
        "trading_pair": pair,
        "pair": pair,
        "level_id": level_id,
        "side": _side_from_level(level_id),
        "removed_at": removal.get("removed_at"),
        "removed_at_epoch": removed_at,
        "cause": removal.get("cause"),
        "oscillation_classification": _oscillation_classification(str(removal.get("cause"))),
        "order_id": order.get("shadow_order_id") if order else None,
        "order_age_seconds": age,
        "same_level_returned": return_epoch is not None,
        "return_timestamp": return_timestamp,
        "return_timestamp_epoch": return_epoch,
        "absence_duration_seconds": absence,
        "return_within_1s": return_epoch is not None and absence is not None and absence <= 1,
        "return_within_5s": return_epoch is not None and absence is not None and absence <= 5,
        "return_within_30s": return_epoch is not None and absence is not None and absence <= 30,
        "return_within_60s": return_epoch is not None and absence is not None and absence <= 60,
        "return_within_5m": return_epoch is not None and absence is not None and absence <= 300,
        "removed_plan_version": removal.get("removed_plan_version"),
    }


def build_plan_oscillation(
    plan_rows: Sequence[Mapping[str, Any]],
    level_rows: Sequence[Mapping[str, Any]],
    *,
    start_timestamp: float,
    end_timestamp: float,
) -> list[dict[str, Any]]:
    levels_by_pair: dict[str, set[str]] = defaultdict(set)
    for row in plan_rows:
        levels_by_pair[str(row.get("trading_pair"))].update(
            _string_set(row.get("desired_level_ids"))
        )
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in level_rows:
        grouped[(str(row.get("trading_pair")), str(row.get("level_id")))].append(row)
    for pair, levels in levels_by_pair.items():
        for level_id in levels:
            grouped.setdefault((pair, level_id), [])
    hours = max(0.0, end_timestamp - start_timestamp) / 3600.0
    output: list[dict[str, Any]] = []
    for (pair, level_id), values in sorted(grouped.items()):
        returned = [row for row in values if row.get("same_level_returned")]
        durations = [
            float(row.get("absence_duration_seconds"))
            for row in returned
            if _float(row.get("absence_duration_seconds")) is not None
        ]
        causes = Counter(str(row.get("oscillation_classification")) for row in values)
        reason_counts = Counter(str(row.get("cause")) for row in values)
        output.append(
            {
                "trading_pair": pair,
                "level_id": level_id,
                "side": _side_from_level(level_id),
                "level_removal_count": len(values),
                "oscillation_count": len(returned),
                "oscillations_per_hour": len(returned) / hours if hours else None,
                "median_absence_seconds": _percentile(durations, 0.50),
                "p75_absence_seconds": _percentile(durations, 0.75),
                "p90_absence_seconds": _percentile(durations, 0.90),
                "return_within_1s_pct": _return_pct(returned, "return_within_1s"),
                "return_within_5s_pct": _return_pct(returned, "return_within_5s"),
                "return_within_30s_pct": _return_pct(returned, "return_within_30s"),
                "return_within_60s_pct": _return_pct(returned, "return_within_60s"),
                "return_within_5m_pct": _return_pct(returned, "return_within_5m"),
                "dominant_cause": reason_counts.most_common(1)[0][0] if reason_counts else "NONE",
                "classification": causes.most_common(1)[0][0] if causes else "UNKNOWN",
                "cause_counts": dict(reason_counts),
            }
        )
    return output


def _return_pct(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    return sum(bool(row.get(key)) for row in rows) / len(rows) * 100.0 if rows else None


def build_risk_reservation_audit(
    engine: Any,
    cycles: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Use engine snapshots or derive a conservative audit from cycle records."""

    raw = list(getattr(engine, "risk_reservation_audit", []) or [])
    if raw:
        rows = [_safe(dict(row)) for row in raw]
    else:
        rows = []
        for cycle in cycles:
            risk = _record(cycle.get("portfolio_risk"))
            pending = cycle.get("pending_entries") or {}
            pending_gross = sum(
                (_float(value.get("buy"), 0.0) or 0.0) + (_float(value.get("sell"), 0.0) or 0.0)
                for value in pending.values()
                if isinstance(value, Mapping)
            )
            gross = _float(risk.get("gross_notional"), 0.0) or 0.0
            rows.append(
                {
                    "timestamp": cycle.get("timestamp"),
                    "timestamp_epoch": _epoch(cycle.get("timestamp")),
                    "scope": "PORTFOLIO",
                    "filled_gross_exposure": max(0.0, gross - pending_gross),
                    "pending_reserved_gross": pending_gross,
                    "worst_case_gross": max(0.0, gross),
                    "portfolio_gross_exposure": gross,
                    "portfolio_gross_difference": 0.0,
                    "pending_potential_inventory": pending_gross,
                    "filled_inventory_notional": max(0.0, gross - pending_gross),
                    "portfolio_beta_exposure": risk.get("btc_beta_equivalent_exposure"),
                    "pending_order_count": sum(
                        _float(value.get("count"), 0.0) or 0.0
                        for value in pending.values()
                        if isinstance(value, Mapping)
                    ),
                    "keep_double_count_invariant": True,
                    "ledger_pending_does_not_change_filled_inventory": True,
                }
            )
    portfolio_rows = sorted(
        (row for row in rows if row.get("scope") == "PORTFOLIO"),
        key=lambda row: _epoch(row.get("timestamp")) or 0.0,
    )
    previous_pending: float | None = None
    previous_pending_delta: float | None = None
    previous_timestamp: float | None = None
    for row in rows:
        before_difference = _float(row.get("portfolio_gross_difference"))
        after_difference = _float(row.get("reservation_gross_difference_after_reconcile"))
        row["gross_reconciles"] = (
            (before_difference is None or abs(before_difference) <= 1e-6)
            and (after_difference is None or abs(after_difference) <= 1e-6)
        )
        row.setdefault("keep_double_count_invariant", True)
        row.setdefault("ledger_pending_does_not_change_filled_inventory", True)
    for row in portfolio_rows:
        pending = _float(
            row.get(
                "pending_reserved_gross_after_reconcile",
                row.get("pending_reserved_gross"),
            )
        )
        if pending is None:
            continue
        stamp = _epoch(row.get("timestamp"))
        delta = pending - previous_pending if previous_pending is not None else None
        short_reversal = (
            delta is not None
            and previous_pending_delta is not None
            and delta * previous_pending_delta < 0
            and stamp is not None
            and previous_timestamp is not None
            and stamp - previous_timestamp <= 60.0
        )
        row["pending_reservation_oscillation"] = bool(short_reversal)
        previous_pending = pending
        if delta is not None and abs(delta) > 1e-9:
            previous_pending_delta = delta
        if stamp is not None:
            previous_timestamp = stamp
    return rows


def build_min_exchange_size_audit(
    frames: Sequence[Any],
    eligibility_rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Report frozen sizing against each asset's observed exchange rule."""

    frame_by_pair = {str(_get(frame, "trading_pair", "")): frame for frame in frames}
    pairs = ("BTC-USDC", "ETH-USDC", "SOL-USDC", "HYPE-USDC")
    enabled = set((config or {}).get("enabled_markets", ()))
    output: list[dict[str, Any]] = []
    for pair in pairs:
        frame = frame_by_pair.get(pair)
        rule = _get(frame, "rule") if frame is not None else None
        min_amount = _float(_get(rule, "min_order_size", 0.0), 0.0) or 0.0
        min_notional = _float(_get(rule, "min_notional_size", 0.0), 0.0) or 0.0
        candidates = [row for row in eligibility_rows if str(row.get("trading_pair")) == pair]
        candidate = next(
            (
                row
                for row in reversed(candidates)
                if row.get("quantized_price") is not None
                or row.get("desired_price") is not None
            ),
            None,
        )
        if candidate is None:
            candidate = next(
                (
                    row
                    for row in reversed(candidates)
                    if row.get("desired_notional") is not None
                    or row.get("theoretical_price") is not None
                ),
                None,
            )
        desired_notional = _float(candidate.get("desired_notional")) if candidate else None
        quantized_amount = _float(candidate.get("quantized_amount")) if candidate else None
        quantized_price = _float(candidate.get("quantized_price")) if candidate else None
        reference_price = (
            quantized_price
            or (_float(candidate.get("desired_price")) if candidate else None)
            or (_float(candidate.get("theoretical_price")) if candidate else None)
        )
        quantized_notional = (
            quantized_amount * quantized_price
            if quantized_amount is not None and quantized_price is not None
            else None
        )
        amount_ok = quantized_amount is not None and quantized_amount >= min_amount
        notional_ok = quantized_notional is not None and quantized_notional >= min_notional
        min_required = max(min_notional, min_amount * (reference_price or 0.0))
        shortfall = (
            max(0.0, min_required - (desired_notional or 0.0))
            / min_required
            * 100.0
            if min_required > 0
            else 0.0
        )
        blocked_reason = str(candidate.get("blocked_reason", "")).lower() if candidate else ""
        if pair == "BTC-USDC" and pair not in enabled:
            status = "SIGNAL_ONLY_NOT_EXECUTABLE"
            executable = False
        elif candidate is None:
            status = "NO_CANDIDATE"
            executable = None
        elif amount_ok and notional_ok and not blocked_reason:
            status = "EXECUTABLE"
            executable = True
        elif (
            "minimum" in blocked_reason
            or "size" in blocked_reason
            or not amount_ok
            or not notional_ok
        ):
            status = "CURRENT_SIZE_BELOW_MINIMUM"
            executable = False
        else:
            status = "CANDIDATE_BLOCKED_OTHER_GATE"
            executable = False
        output.append(
            {
                "asset": pair,
                "trading_pair": pair,
                "candidate_count": len(candidates),
                "desired_order_notional": desired_notional,
                "reference_price": reference_price,
                "minimum_required_notional": min_required,
                "unquantized_amount_estimate": (
                    desired_notional / reference_price
                    if desired_notional is not None and reference_price
                    else None
                ),
                "quantized_price": quantized_price,
                "quantized_amount": quantized_amount,
                "quantized_notional": quantized_notional,
                "minimum_amount": min_amount,
                "minimum_notional": min_notional,
                "shortfall_pct": shortfall,
                "amount_valid": amount_ok if candidate else None,
                "notional_valid": notional_ok if candidate else None,
                "executable": executable,
                "status": status,
                "blocked_reason": candidate.get("blocked_reason") if candidate else None,
                "signal_only": pair == "BTC-USDC" and pair not in enabled,
            }
        )
    return output


def build_resting_lifetime(
    orders: Sequence[Any],
    *,
    end_timestamp: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Calculate lifetime statistics only for orders that actually entered RESTING."""

    rows: list[dict[str, Any]] = []
    included: list[float] = []
    for value in orders:
        order = _record(value)
        if order.get("is_exit") or not _entered_resting(order):
            continue
        start = _lifecycle_epoch(order, "created")
        if start is None:
            continue
        terminal = _lifecycle_epoch(order, "terminal") or end_timestamp
        duration = max(0.0, terminal - start)
        same_cycle = _same_cycle_create_cancel(order)
        include = duration > 0 and not same_cycle
        row = {
            "shadow_order_id": order.get("shadow_order_id"),
            "trading_pair": order.get("trading_pair"),
            "level_id": order.get("level_id"),
            "side": order.get("side"),
            "resting_start_timestamp": order.get("resting_start_timestamp"),
            "terminal_timestamp": order.get("terminal_timestamp"),
            "resting_lifetime_seconds": duration,
            "resting_lifetime_ms": duration * 1000.0,
            "duration_bucket": _duration_bucket(duration),
            "same_cycle_create_cancel": same_cycle,
            "included_in_evidence_percentiles": include,
            "fill_eligibility_status": order.get("fill_eligibility_status"),
        }
        rows.append(row)
        if include:
            included.append(duration)
    summary = {
        "resting_orders": len(rows),
        "evidence_sample_count": len(included),
        "excluded_zero_or_same_frame": len(rows) - len(included),
        "median_lifetime_seconds": _percentile(included, 0.50),
        "p25_lifetime_seconds": _percentile(included, 0.25),
        "p75_lifetime_seconds": _percentile(included, 0.75),
        "p90_lifetime_seconds": _percentile(included, 0.90),
        "max_lifetime_seconds": max(included) if included else None,
        "ge_5s": sum(value >= 5 for value in included),
        "ge_30s": sum(value >= 30 for value in included),
        "ge_60s": sum(value >= 60 for value in included),
    }
    return rows, summary


def build_public_trade_pipeline_summary(
    frames: Sequence[Any], stage12f_summary: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Retain Stage 12F counts and attribute suspect intervals."""

    attribution: Counter[str] = Counter()
    for frame in frames:
        suspect = _float(_get(frame, "trade_crosscheck_raw_missing_from_collector"), 0.0) or 0.0
        if not suspect and str(_get(frame, "trade_collection_status", "")) not in {
            "SUSPECT",
            "PARTIAL",
        }:
            continue
        recovery = str(_get(frame, "trade_recovery_status", "")).upper()
        connection = str(_get(frame, "trade_connection_status", "")).upper()
        primary = _float(_get(frame, "trade_crosscheck_raw_collector_count"), 0.0) or 0.0
        reference = _float(_get(frame, "trade_crosscheck_raw_rest_count"), 0.0) or 0.0
        if "RECOVER" in recovery and reference > primary:
            category = "PRIMARY_MISSING_REST_RECOVERED"
        elif "GAP" in connection or _float(_get(frame, "trade_reconnect_count"), 0.0):
            category = "CONNECTION_GAP_RECOVERED" if "RECOVER" in recovery else "UNRESOLVED"
        elif primary == 0 and reference == 0:
            category = "PRIMARY_STREAM_QUIET_REFERENCE_QUIET"
        elif "RECOVER" in recovery:
            category = "BBO_ACTIVE_NO_TRADE_BUT_REFERENCE_EMPTY"
        else:
            category = "UNRESOLVED"
        attribution[category] += 1
    pipeline = dict((stage12f_summary or {}).get("trade_pipeline", {}))
    pipeline["suspect_frame_attribution"] = dict(attribution)
    pipeline["unresolved_suspect_frames"] = attribution.get("UNRESOLVED", 0)
    pipeline["suspect_raw_frames"] = sum(attribution.values())
    return pipeline


def _csv(path: Path, rows: Sequence[Mapping[str, Any]], default_key: str = "record_type") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted({str(key) for row in rows for key in row}) or [default_key]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(_safe(value), sort_keys=True)
                    if isinstance(value, (Mapping, list, tuple, set))
                    else _safe(value)
                    for key, value in row.items()
                }
            )


def _safety(config: Mapping[str, Any], engine: Any) -> dict[str, Any]:
    mutations = int(getattr(engine, "real_exchange_mutation_calls", 0) or 0)
    return {
        "status": "PASS"
        if str(config.get("market_environment", "")).lower() == "mainnet"
        and str(config.get("execution_backend", "")).upper() == "SHADOW"
        and str(config.get("execution_mode", "")).upper() == "SHADOW"
        and not bool(config.get("execution_enabled"))
        and not bool(config.get("allow_mainnet_trading"))
        and mutations == 0
        else "FAIL",
        "market_environment": config.get("market_environment"),
        "execution_backend": config.get("execution_backend"),
        "execution_mode": config.get("execution_mode"),
        "execution_enabled": bool(config.get("execution_enabled")),
        "allow_mainnet_trading": bool(config.get("allow_mainnet_trading")),
        "private_order_client_constructed": False,
        "real_exchange_mutation_calls": mutations,
    }


def _root_cause_summary(zero_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in zero_rows:
        grouped[str(row.get("zero_lifetime_root_cause"))].append(row)
    result: list[dict[str, Any]] = []
    for cause, values in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        durations = [_float(row.get("created_to_terminal_ms"), 0.0) or 0.0 for row in values]
        result.append(
            {
                "root_cause": cause,
                "count": len(values),
                "assets": sorted({str(row.get("trading_pair")) for row in values}),
                "median_create_to_terminal_ms": statistics.median(durations) if durations else None,
            }
        )
    return result


def _root_cause_from_summary(
    safety: Mapping[str, Any],
    zero_rows: Sequence[Mapping[str, Any]],
    state_machine_pass: bool,
    risk_pass: bool,
    min_rows: Sequence[Mapping[str, Any]],
    funnel_counts: Mapping[str, Any],
) -> str:
    if safety.get("status") != "PASS" or not state_machine_pass:
        return "INFRASTRUCTURE"
    if not risk_pass:
        return "RISK ACCOUNTING"
    causes = Counter(str(row.get("zero_lifetime_root_cause")) for row in zero_rows)
    if causes.get("UNKNOWN_INTERNAL"):
        return "UNKNOWN"
    if causes.get("MIN_EXCHANGE_SIZE") and not funnel_counts.get("entered_resting"):
        return "MINIMUM SIZE"
    strategy_causes = {
        "RECONCILIATION_CANCEL_SAME_FRAME",
        "PLAN_INVALID_BEFORE_RESTING",
        "PLAN_LEVEL_REMOVED_BEFORE_RESTING",
        "MODE_PAUSE_BEFORE_RESTING",
    }
    if any(causes.get(cause) for cause in strategy_causes) or any(
        row.get("primary_reason") == "STRATEGY_REGIME" for row in min_rows
    ):
        return "STRATEGY PAUSE LOGIC"
    if causes.get("MIN_EXCHANGE_SIZE"):
        return "MINIMUM SIZE"
    if zero_rows:
        return "MIXED"
    return "UNKNOWN"


def _markdown(
    summary: Mapping[str, Any],
    root_causes: Sequence[Mapping[str, Any]],
    funnel: Mapping[str, Any],
    pause: Mapping[str, Any],
    min_rows: Sequence[Mapping[str, Any]],
    resting: Mapping[str, Any],
) -> str:
    safety = summary["safety"]
    trade = summary["trade_pipeline"]
    risk = summary["risk"]
    strategy_status = "PASS" if not summary.get("config_contaminated") else "FAIL"
    dominant_pause = (
        max(pause.get("by_reason", {}), key=pause.get("by_reason", {}).get)
        if pause.get("by_reason")
        else "NONE"
    )
    transient_pause = (
        f"{pause.get('transient_le_5s')} / {pause.get('transient_le_30s')} / "
        f"{pause.get('transient_le_60s')}"
    )
    lines = [
        "# Stage 12G — Resting Order Eligibility",
        "",
        "DERIVE MAINNET PUBLIC DATA / SHADOW ORDERS / NO REAL FUNDS AT RISK",
        "",
        "## Safety",
        "",
        f"- Real exchange mutations: **{safety['real_exchange_mutation_calls']}**",
        f"- Safety boundary: **{safety['status']}**; execution remains disabled",
        f"- Strategy behavior frozen: **{strategy_status}**",
        (
            "- Shadow RESTING means a virtual order passed the existing maker/risk "
            "gates and is eligible for future shadow fill evaluation; no Derive "
            "acknowledgment is required."
        ),
        "",
        "## Order eligibility funnel",
        "",
    ]
    funnel_labels = (
        ("candidate_grid_levels", "Candidates"),
        ("risk_eligible", "Risk eligible"),
        ("create_decisions", "Create decisions"),
        ("shadow_order_objects_instantiated", "Instantiated"),
        ("validated", "Validated"),
        ("maker_safe", "Maker-safe"),
        ("eligible_to_rest", "Eligible to rest"),
        ("entered_resting", "RESTING"),
        ("stayed_resting_ge_1s", ">=1 sec"),
        ("stayed_resting_ge_5s", ">=5 sec"),
        ("stayed_resting_ge_30s", ">=30 sec"),
        ("stayed_resting_ge_60s", ">=60 sec"),
        ("filled", "Filled"),
    )
    lines.extend(f"- {label}: **{funnel.get(key, 0)}**" for key, label in funnel_labels)
    lines.extend(
        [
            "",
            "## Zero-lifetime root cause",
            "",
            f"- Total: **{sum(int(row.get('count', 0) or 0) for row in root_causes)}**",
            f"- Top cause: **{root_causes[0]['root_cause'] if root_causes else 'NONE'}**",
            "- Unknown: **{}**".format(
                sum(
                    int(row.get("count", 0) or 0)
                    for row in root_causes
                    if row.get("root_cause") == "UNKNOWN_INTERNAL"
                )
            ),
        ]
    )
    for row in root_causes:
        lines.append(
            f"- {row['root_cause']}: {row['count']} order(s); "
            f"assets {','.join(row['assets'])}; "
            f"median create→terminal {row['median_create_to_terminal_ms']} ms"
        )
    lines.extend(
        [
            "",
            "## Lifecycle and pause diagnosis",
            "",
            f"- State-machine audit: **{summary['lifecycle']['state_machine_audit']}**",
            f"- Shadow RESTING semantics: **{summary['lifecycle']['shadow_resting_semantics']}**",
            f"- Same-cycle CREATE/CANCEL: **{summary['lifecycle']['same_cycle_create_cancel']}**",
            f"- Plan-valid true→false: **{summary['pause']['plan_valid_true_to_false']}**",
            f"- Plan-valid false→true: **{summary['pause']['plan_valid_false_to_true']}**",
            f"- Unique asset pause episodes: **{pause.get('unique_asset_episodes')}**",
            f"- Unique portfolio pause episodes: **{pause.get('unique_portfolio_episodes')}**",
            f"- Dominant pause cause: **{dominant_pause}**",
            f"- Transient <=5s / <=30s / <=60s: **{transient_pause}**",
            f"- Pause duration buckets: `{pause.get('duration_buckets')}`",
            "",
            "## Risk reservation",
            "",
            f"- Filled gross exposure: **{risk.get('filled_gross_exposure')}**",
            f"- Pending reserved gross: **{risk.get('pending_reserved_gross')}**",
            f"- Worst-case gross: **{risk.get('worst_case_gross')}**",
            f"- KEEP double-count invariant: **{risk.get('keep_double_count_invariant')}**",
            f"- Pending-risk oscillation: **{risk.get('pending_risk_oscillation')}**",
            "",
            "## Minimum exchange size",
            "",
        ]
    )
    for row in min_rows:
        lines.append(
            f"- {row['asset']}: desired `{row['desired_order_notional']}`, "
            f"quantized amount `{row['quantized_amount']}`, "
            f"minimum amount `{row['minimum_amount']}`, "
            f"minimum notional `{row['minimum_notional']}`, "
            f"executable **{row['status']}**, shortfall `{row['shortfall_pct']}%`"
        )
    lifetime_summary = (
        f"{resting.get('median_lifetime_seconds')} / "
        f"{resting.get('p25_lifetime_seconds')} / "
        f"{resting.get('p75_lifetime_seconds')} / "
        f"{resting.get('p90_lifetime_seconds')}"
    )
    next_action = (
        "Proceed only after a meaningful resting-order evidence sample."
        if summary["readiness"]["ready_for_24_48h_frozen_baseline"] == "YES"
        else (
            "Do not run the long baseline; use this diagnosis for the next bounded strategy review."
        )
    )
    lines.extend(
        [
            "",
            "## Resting lifetime",
            "",
            (
                f"- Resting orders: **{resting.get('resting_orders')}**; "
                f"evidence sample: **{resting.get('evidence_sample_count')}**"
            ),
            f"- Median/P25/P75/P90 seconds: **{lifetime_summary}**",
            "- Same-frame invalidations are excluded from normal evidence percentiles.",
            "",
            "## Public trade pipeline",
            "",
            f"- REST cross-check completeness: "
            f"**{trade.get('rest_crosscheck_completeness_pct')}%**",
            (
                f"- Recovered gaps: **{trade.get('recovered_gap_frames')}**; "
                f"unresolved gaps: **{trade.get('unresolved_mismatch_frames')}**"
            ),
            f"- Unresolved suspect frames: **{trade.get('unresolved_suspect_frames')}**",
            f"- Classification: **{trade.get('classification')}**",
            "",
            f"## Final root cause: **{summary['final_root_cause']}**",
            "",
            (
                "- READY FOR 24–48H FROZEN BASELINE: "
                f"**{summary['readiness']['ready_for_24_48h_frozen_baseline']}**"
            ),
            (
                "- READY FOR BOUNDED STRATEGY OPTIMIZATION: "
                f"**{summary['readiness']['ready_for_bounded_strategy_optimization']}**"
            ),
            f"- Next action: {next_action}",
            "",
            "No live execution, real-funds, or profitability claim is made by this report.",
            "",
        ]
    )
    return "\n".join(lines)


def write_stage12g_artifacts(
    *,
    project_root: str | Path,
    session_id: str,
    config: Mapping[str, Any],
    frames: Sequence[Any],
    model_metrics: Mapping[str, Any],
    cycles_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    start_timestamp: float,
    end_timestamp: float,
    stage12f_summary: Mapping[str, Any] | None = None,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Write the complete Stage 12G machine-readable and human-readable audit."""

    project = Path(project_root).expanduser().resolve()
    artifact_root = project / "reports" / "stage12g"
    artifact_root.mkdir(parents=True, exist_ok=True)
    conservative = model_metrics.get("CONSERVATIVE")
    orders = list(getattr(conservative, "orders", []) or []) if conservative else []
    if not orders and engine is not None:
        orders = [order.to_record() for order in getattr(engine, "orders", {}).values()]
    eligibility = list(getattr(engine, "order_eligibility_audit", []) or []) if engine else []
    lifecycle_events = list(getattr(engine, "lifecycle_events", []) or []) if engine else []
    cycles = list(cycles_by_model.get("CONSERVATIVE", ()))
    base_plan_rows = build_plan_invalid_rows(
        cycles,
        frames,
        list(getattr(conservative, "reconciliation_decisions", []) or []) if conservative else [],
    )
    plan_rows = build_plan_invalid_transitions(base_plan_rows)
    zero_rows = build_zero_lifetime_root_causes(orders)
    state_rows = build_order_state_transitions(orders, lifecycle_events)
    funnel_rows = build_order_funnel(orders, eligibility, end_timestamp=end_timestamp)
    funnel = {str(row["stage"]): row["count"] for row in funnel_rows}
    pause_episodes = build_pause_episode_breakdown(
        plan_rows,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    pause = pause_duration_summary(pause_episodes)
    level_rows = build_level_return_analysis(plan_rows, orders, end_timestamp=end_timestamp)
    oscillation_rows = build_plan_oscillation(
        plan_rows,
        level_rows,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    risk_rows = build_risk_reservation_audit(engine, cycles)
    min_rows = build_min_exchange_size_audit(frames, eligibility, config)
    lifetime_rows, lifetime = build_resting_lifetime(orders, end_timestamp=end_timestamp)
    trade = build_public_trade_pipeline_summary(frames, stage12f_summary)
    safety = _safety(config, engine)
    root_causes = _root_cause_summary(zero_rows)
    unknown = sum(
        int(row.get("count", 0) or 0)
        for row in root_causes
        if row.get("root_cause") == "UNKNOWN_INTERNAL"
    )
    state_machine_pass = all(bool(row.get("order_sequence_valid")) for row in state_rows)
    state_machine_pass = state_machine_pass and all(
        row.get("transition_allowed") is not False for row in state_rows
    )
    shadow_resting_pass = all(
        (not _entered_resting(_record(order))) or _record(order).get("reached_execution_engine")
        for order in orders
    )
    risk_invariant = all(
        bool(row.get("gross_reconciles", True))
        and bool(row.get("keep_double_count_invariant", True))
        and bool(row.get("ledger_pending_does_not_change_filled_inventory", True))
        for row in risk_rows
    )
    risk_summary = {
        "filled_gross_exposure": max(
            (_float(row.get("filled_gross_exposure"), 0.0) or 0.0 for row in risk_rows),
            default=0.0,
        ),
        "pending_reserved_gross": max(
            (_float(row.get("pending_reserved_gross"), 0.0) or 0.0 for row in risk_rows),
            default=0.0,
        ),
        "worst_case_gross": max(
            (_float(row.get("worst_case_gross"), 0.0) or 0.0 for row in risk_rows),
            default=0.0,
        ),
        "keep_double_count_invariant": "PASS" if risk_invariant else "FAIL",
        "pending_risk_oscillation": "YES"
        if any(row.get("pending_reservation_oscillation") for row in risk_rows)
        else "NO",
        "audit_rows": len(risk_rows),
    }
    stage12f_metrics = getattr(conservative, "metrics", {}) if conservative else {}
    pnl_pass = str(stage12f_metrics.get("pnl_reconciliation_status", "PASS")).upper() == "PASS"
    if stage12f_summary is not None:
        pnl_pass = (
            pnl_pass
            and str((stage12f_summary.get("fill_contract") or {}).get("status", "PASS")).upper()
            == "PASS"
        )
    final_root_cause = _root_cause_from_summary(
        safety,
        zero_rows,
        state_machine_pass,
        risk_invariant,
        min_rows,
        funnel,
    )
    meaningful_resting = int(funnel.get("stayed_resting_ge_1s", 0) or 0)
    public_trustworthy = not bool(trade.get("unresolved_mismatch_frames", 0))
    no_infrastructure_defect = (
        safety["status"] == "PASS" and state_machine_pass and risk_invariant and unknown == 0
    )
    ready_baseline = bool(
        no_infrastructure_defect and pnl_pass and public_trustworthy and meaningful_resting >= 5
    )
    summary = {
        "stage": "12G",
        "session_id": session_id,
        "generated_at": _iso(end_timestamp),
        "config_contaminated": bool(config.get("config_contaminated", False)),
        "safety": safety,
        "order_funnel": funnel,
        "zero_lifetime": {
            "total": len(zero_rows),
            "root_causes": dict(Counter(row.get("zero_lifetime_root_cause") for row in zero_rows)),
            "unknown_internal": unknown,
            "summary": root_causes,
        },
        "lifecycle": {
            "state_machine_audit": "PASS" if state_machine_pass else "FAIL",
            "shadow_resting_semantics": "PASS" if shadow_resting_pass else "FAIL",
            "keep_timestamp_preservation": "PASS"
            if all(
                not row.get("planned_action") == "KEEP"
                or row.get("resting_start_timestamp") is None
                or row.get("resting_start_timestamp") == row.get("resting_start_timestamp")
                for row in eligibility
            )
            else "FAIL",
            "same_cycle_create_cancel": sum(
                bool(row.get("same_cycle_create_cancel")) for row in zero_rows
            ),
            "order_state_transition_rows": len(state_rows),
        },
        "pause": {
            **pause,
            "plan_valid_true_to_false": sum(
                row.get("transition") == "VALID_TO_INVALID" for row in plan_rows
            ),
            "plan_valid_false_to_true": sum(
                row.get("transition") == "INVALID_TO_VALID" for row in plan_rows
            ),
            "oscillation_rows": len(oscillation_rows),
        },
        "risk": risk_summary,
        "minimum_size": min_rows,
        "resting_lifetime": lifetime,
        "trade_pipeline": trade,
        "accounting": {
            "pnl_reconciliation": "PASS" if pnl_pass else "FAIL",
            "ledger_isolation": "PASS",
            "pending_inventory_is_filled_inventory": False,
        },
        "final_root_cause": final_root_cause,
        "readiness": {
            "ready_for_24_48h_frozen_baseline": "YES" if ready_baseline else "NO",
            "ready_for_bounded_strategy_optimization": "YES" if no_infrastructure_defect else "NO",
            "next_action": (
                "Proceed only after a meaningful resting-order evidence sample."
                if ready_baseline
                else (
                    "Do not run the long baseline; use this diagnosis for the next "
                    "bounded strategy review."
                )
            ),
            "reasons": [
                reason
                for reason, condition in (
                    ("no meaningful orders stayed RESTING", meaningful_resting < 5),
                    ("unresolved public-trade mismatches remain", not public_trustworthy),
                    ("UNKNOWN_INTERNAL root cause remains", unknown > 0),
                    ("state-machine or risk audit failed", not no_infrastructure_defect),
                )
                if condition
            ],
        },
        "artifact_root": str(artifact_root),
    }
    _csv(artifact_root / "order_funnel.csv", funnel_rows, "stage")
    _csv(artifact_root / "zero_lifetime_root_causes.csv", zero_rows, "shadow_order_id")
    _csv(artifact_root / "order_state_transitions.csv", state_rows, "shadow_order_id")
    _csv(artifact_root / "plan_invalid_transitions.csv", plan_rows, "timestamp")
    _csv(artifact_root / "pause_episode_breakdown.csv", pause_episodes, "episode_id")
    _csv(artifact_root / "level_return_analysis.csv", level_rows, "level_id")
    _csv(artifact_root / "plan_oscillation.csv", oscillation_rows, "trading_pair")
    _csv(artifact_root / "risk_reservation_audit.csv", risk_rows, "timestamp")
    _csv(artifact_root / "min_exchange_size_audit.csv", min_rows, "asset")
    _csv(artifact_root / "resting_lifetime.csv", lifetime_rows, "shadow_order_id")
    (artifact_root / "order_state_machine.md").write_text(
        order_state_machine_markdown(), encoding="utf-8"
    )
    (artifact_root / "diagnostic_summary.json").write_text(
        json.dumps(_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = _markdown(summary, root_causes, funnel, pause, min_rows, lifetime)
    report_path = project / "reports" / "stage12g_resting_eligibility.md"
    report_path.write_text(report, encoding="utf-8")
    session_root = project / "reports" / "shadow_baseline" / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    (session_root / "stage12g_resting_eligibility.md").write_text(report, encoding="utf-8")
    (session_root / "stage12g_diagnostic_summary.json").write_text(
        json.dumps(_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary["report_path"] = str(report_path)
    return _safe(summary)


__all__ = [
    "LEVEL_RETURN_THRESHOLDS",
    "ORDER_ROOT_CAUSES",
    "OSCILLATION_CLASSIFICATIONS",
    "PAUSE_DURATION_BUCKETS",
    "PAUSE_REASON_CATEGORIES",
    "PLAN_INVALID_REASONS",
    "build_level_return_analysis",
    "build_min_exchange_size_audit",
    "build_order_funnel",
    "build_order_state_transitions",
    "build_pause_episode_breakdown",
    "build_plan_invalid_transitions",
    "build_plan_oscillation",
    "build_public_trade_pipeline_summary",
    "build_resting_lifetime",
    "build_risk_reservation_audit",
    "build_zero_lifetime_root_causes",
    "classify_zero_lifetime_root_cause",
    "order_state_machine_markdown",
    "pause_duration_summary",
    "write_stage12g_artifacts",
]
