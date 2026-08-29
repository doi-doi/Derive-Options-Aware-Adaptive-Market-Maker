"""Stage 12E evidence reconciliation and root-cause diagnostics.

This module is deliberately downstream of the strategy stages.  It does not
choose a price, size, mode, or risk limit.  It only normalizes public-trade
evidence, reconciles order lifecycles, and writes auditable diagnostic rows.

The most important contract in this module is intentionally strict:
``TRADED_THROUGH_FILLED`` means that a real conservative ``ShadowFill`` event
exists for the same order.  A qualifying public trade without that event is
reported as ``TRADE_THROUGH_OBSERVED_NO_FILL`` and is never promoted to a
paper fill.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .stage12c import FILL_ELIGIBILITY_STATUSES

TRADE_THROUGH_FILLED = "TRADED_THROUGH_FILLED"
TRADE_THROUGH_OBSERVED_NO_FILL = "TRADE_THROUGH_OBSERVED_NO_FILL"
TOUCHED_FILLED = "TOUCHED_FILLED"
TOUCHED_NOT_TRADED_THROUGH = "TOUCHED_NOT_TRADED_THROUGH"
NEVER_REACHED_PRICE = "NEVER_REACHED_PRICE"
INSUFFICIENT_TRADE_EVIDENCE = "INSUFFICIENT_TRADE_EVIDENCE"
PUBLIC_TRADE_STREAM_SUSPECT = "PUBLIC_TRADE_STREAM_SUSPECT"

TIMESTAMP_UNITS = ("seconds", "milliseconds", "microseconds", "nanoseconds")
HEALTHY_COLLECTION_STATUSES = frozenset(
    {
        "OK",
        "CONNECTED",
        "CONNECTED_NO_TRADES",
        "REST_FALLBACK",
        "WEBSOCKET",
        "WS_CONNECTED",
    }
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def timestamp_unit(value: Any) -> str | None:
    """Return the unit implied by a numeric Derive timestamp.

    Derive public REST/WebSocket timestamps are milliseconds.  The thresholds
    below also make seconds, microseconds, and nanoseconds explicit so a
    feed-unit regression cannot silently shift evidence by 1,000x.
    """

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            return "iso8601" if "T" in text or "-" in text else None
    else:
        numeric = _number(value)
    if numeric is None:
        return None
    magnitude = abs(numeric)
    if magnitude < 100_000_000_000:
        return "seconds"
    if magnitude < 100_000_000_000_000:
        return "milliseconds"
    if magnitude < 100_000_000_000_000_000:
        return "microseconds"
    return "nanoseconds"


def normalize_timestamp(value: Any, unit: str | None = None) -> tuple[float | None, str | None]:
    """Normalize a timestamp and return ``(epoch_seconds, detected_unit)``."""

    if isinstance(value, str):
        text = value.strip()
        try:
            numeric = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None, None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).timestamp(), "iso8601"
    else:
        numeric = _number(value)
    if numeric is None:
        return None, None
    selected = str(unit or timestamp_unit(numeric) or "").strip().lower()
    aliases = {
        "s": "seconds",
        "sec": "seconds",
        "seconds": "seconds",
        "ms": "milliseconds",
        "millis": "milliseconds",
        "milliseconds": "milliseconds",
        "us": "microseconds",
        "micros": "microseconds",
        "microseconds": "microseconds",
        "ns": "nanoseconds",
        "nanos": "nanoseconds",
        "nanoseconds": "nanoseconds",
    }
    selected = aliases.get(selected, selected)
    divisors = {
        "seconds": 1.0,
        "milliseconds": 1_000.0,
        "microseconds": 1_000_000.0,
        "nanoseconds": 1_000_000_000.0,
    }
    divisor = divisors.get(selected)
    if divisor is None:
        return None, None
    return numeric / divisor, selected


def _direction(row: Mapping[str, Any]) -> str | None:
    value = row.get("direction", row.get("aggressor_side", row.get("side")))
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"buy", "sell"} else None


def canonical_trade_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    instrument_name: str | None = None,
    timestamp_unit: str | None = None,
) -> dict[str, Any]:
    """Canonicalize public REST rows without over-deduplicating trades.

    Derive REST can return one maker and one taker row for the same
    ``trade_id``.  Those rows represent one economic trade, so the taker row is
    preferred because its direction is the aggressor direction used by the
    public trade WebSocket.  Rows without a stable ID are retained rather than
    collapsed merely because price, time, and amount happen to match.
    """

    raw_rows = list(rows)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    rejected = 0
    units: Counter[str] = Counter()
    accepted = 0
    for index, original in enumerate(raw_rows):
        if not isinstance(original, Mapping):
            rejected += 1
            continue
        row = dict(original)
        row_instrument = row.get("instrument_name", row.get("instrumentName"))
        if instrument_name and row_instrument and str(row_instrument) != instrument_name:
            rejected += 1
            continue
        epoch, detected = normalize_timestamp(
            row.get("timestamp"), row.get("timestamp_unit", timestamp_unit)
        )
        price = _number(row.get("trade_price", row.get("price")))
        amount = _number(row.get("trade_amount", row.get("amount")))
        if epoch is None or price is None or amount is None or price <= 0 or amount <= 0:
            rejected += 1
            continue
        if detected:
            units[detected] += 1
        trade_id = row.get("trade_id", row.get("tradeId"))
        stable_id = str(trade_id) if trade_id not in (None, "") else None
        parsed = {
            "trade_id": stable_id,
            "timestamp": epoch,
            "timestamp_unit": detected,
            "price": price,
            "amount": amount,
            "aggressor_side": _direction(row),
            "instrument_name": str(row_instrument) if row_instrument else instrument_name,
            "liquidity_role": str(row.get("liquidity_role", "")).strip().lower() or None,
            "_source_index": index,
        }
        key = ("id", stable_id) if stable_id is not None else ("row", str(index))
        groups[key].append(parsed)
        accepted += 1

    canonical: list[dict[str, Any]] = []
    for _key, candidates in groups.items():
        selected = min(
            candidates,
            key=lambda row: (
                0 if row.get("liquidity_role") == "taker" else 1,
                0 if row.get("aggressor_side") in {"buy", "sell"} else 1,
                float(row["timestamp"]),
                str(row.get("trade_id") or ""),
                int(row.get("_source_index", 0)),
            ),
        )
        canonical.append(
            {
                key_name: value
                for key_name, value in selected.items()
                if not key_name.startswith("_")
            }
            | {"raw_row_count": len(candidates)}
        )
    canonical.sort(key=lambda row: (float(row["timestamp"]), str(row.get("trade_id") or "")))
    return {
        "rows": canonical,
        "raw_count": len(raw_rows),
        "accepted_count": accepted,
        "canonical_count": len(canonical),
        "duplicate_count": max(0, accepted - len(canonical)),
        "rejected_count": rejected,
        "timestamp_units": dict(units),
        "sort_order": "timestamp_ascending_then_trade_id",
        "dedup_key": "trade_id; no-id rows retained",
    }


def _trade_record(trade: Any, trading_pair: str | None = None) -> dict[str, Any]:
    timestamp, detected_unit = normalize_timestamp(
        _get(trade, "timestamp"), _get(trade, "timestamp_unit")
    )
    return {
        "trade_id": _get(trade, "trade_id"),
        "timestamp": timestamp,
        "timestamp_unit": detected_unit,
        "price": _number(_get(trade, "price")),
        "amount": _number(_get(trade, "amount")),
        "aggressor_side": str(_get(trade, "aggressor_side", "") or "").lower() or None,
        "instrument_name": _get(trade, "instrument_name", trading_pair),
        "raw_row_count": 1,
    }


def _frame_collection_ok(frame: Any) -> bool:
    status = str(_get(frame, "trade_collection_status", "") or "").upper()
    if status in HEALTHY_COLLECTION_STATUSES:
        return True
    # Synthetic/unit-test frames predate the provenance fields.  A non-empty
    # public trade tuple is still usable evidence, but an empty unknown frame
    # must not be called healthy by the Stage 12E audit.
    return status in {"", "UNKNOWN"} and bool(_get(frame, "trades", ()))


def _trade_qualifies(order: Any, trade: Mapping[str, Any]) -> bool:
    price = _number(_get(order, "price"))
    side = str(_get(order, "side", "") or "").lower()
    if price is None:
        return False
    direction = str(trade.get("aggressor_side") or "").lower()
    trade_price = _number(trade.get("price"))
    if trade_price is None or direction not in {"buy", "sell"}:
        return False
    return (side == "buy" and direction == "sell" and trade_price < price) or (
        side == "sell" and direction == "buy" and trade_price > price
    )


def _union_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    ordered = sorted((left, right) for left, right in intervals if right > left)
    total = 0.0
    current_left: float | None = None
    current_right: float | None = None
    for left, right in ordered:
        if current_left is None:
            current_left, current_right = left, right
        elif current_right is not None and left <= current_right:
            current_right = max(current_right, right)
        else:
            total += (current_right or current_left) - current_left
            current_left, current_right = left, right
    if current_left is not None:
        total += (current_right or current_left) - current_left
    return max(0.0, total)


def _frame_interval(frame: Any, end_timestamp: float) -> tuple[float, float]:
    start = _number(_get(frame, "timestamp")) or 0.0
    explicit_start = _number(_get(frame, "trade_collection_start_epoch"))
    explicit_end = _number(_get(frame, "trade_collection_end_epoch"))
    if explicit_start is not None and explicit_end is not None and explicit_end > explicit_start:
        return explicit_start, min(end_timestamp, explicit_end)
    sample = _number(_get(frame, "trade_sample_interval_seconds")) or 5.0
    return start, min(end_timestamp, start + max(0.001, sample))


def _frame_market_activity(frame: Any, previous: Any | None) -> dict[str, Any]:
    """Classify trade silence against independently changing market fields."""

    best_bid = _number(_get(frame, "best_bid"))
    best_ask = _number(_get(frame, "best_ask"))
    bbo_valid = bool(best_bid and best_ask and best_bid > 0 and best_ask > best_bid)
    bid_depth = _number(_get(frame, "bid_depth"))
    ask_depth = _number(_get(frame, "ask_depth"))
    depth_source = "five_percent_depth"
    if bid_depth is None or ask_depth is None:
        bid_depth = _number(_get(frame, "best_bid_size"))
        ask_depth = _number(_get(frame, "best_ask_size"))
        depth_source = "top_of_book_size"
    depth_available = bool(
        bid_depth is not None
        and ask_depth is not None
        and bid_depth >= 0
        and ask_depth >= 0
        and (bid_depth > 0 or ask_depth > 0)
    )
    mid = (best_bid + best_ask) / 2.0 if bbo_valid else None
    previous_bid = _number(_get(previous, "best_bid")) if previous is not None else None
    previous_ask = _number(_get(previous, "best_ask")) if previous is not None else None
    previous_mid = (
        (previous_bid + previous_ask) / 2.0
        if previous_bid and previous_ask and previous_ask > previous_bid
        else None
    )
    previous_bid_depth = _number(_get(previous, "bid_depth")) if previous is not None else None
    previous_ask_depth = _number(_get(previous, "ask_depth")) if previous is not None else None
    if previous_bid_depth is None or previous_ask_depth is None:
        previous_bid_depth = (
            _number(_get(previous, "best_bid_size")) if previous is not None else None
        )
        previous_ask_depth = (
            _number(_get(previous, "best_ask_size")) if previous is not None else None
        )
    bbo_changed = bool(
        previous is not None
        and bbo_valid
        and (best_bid != previous_bid or best_ask != previous_ask)
    )
    mid_changed = bool(previous is not None and mid is not None and mid != previous_mid)
    depth_changed = bool(
        previous is not None
        and depth_available
        and (
            bid_depth != previous_bid_depth
            or ask_depth != previous_ask_depth
        )
    )
    collection_status = str(_get(frame, "trade_collection_status", "UNKNOWN") or "UNKNOWN").upper()
    collection_healthy = _frame_collection_ok(frame)
    has_trade_event = bool(_get(frame, "trades", ()) or ())
    if not collection_healthy:
        classification = "COLLECTION_UNHEALTHY"
    elif has_trade_event:
        classification = "TRADE_EVENTS_OBSERVED"
    elif bbo_valid and depth_available and (bbo_changed or mid_changed or depth_changed):
        classification = PUBLIC_TRADE_STREAM_SUSPECT
    elif bbo_valid:
        classification = "FUNCTIONING_BUT_MARKET_SPARSE"
    else:
        classification = "MARKET_DATA_INVALID"
    return {
        "market_bbo_valid": bbo_valid,
        "depth_data_available": depth_available,
        "depth_source": depth_source if depth_available else None,
        "bbo_updated": bbo_changed,
        "mid_price_updated": mid_changed,
        "depth_updated": depth_changed,
        "mid_move": (
            abs(mid - previous_mid)
            if mid is not None and previous_mid is not None
            else 0.0
        ),
        "trade_event_present": has_trade_event,
        "trade_silence_classification": classification,
        "trade_collection_status": collection_status,
    }


def build_trade_stream_diagnostics(frames: Sequence[Any]) -> list[dict[str, Any]]:
    """Return one quiet-vs-suspect classification per market frame."""

    by_pair: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    for index, frame in enumerate(frames):
        by_pair[str(_get(frame, "trading_pair", ""))].append((index, frame))
    diagnostics: list[dict[str, Any]] = [{} for _ in frames]
    for values in by_pair.values():
        values.sort(key=lambda item: _number(_get(item[1], "timestamp")) or 0.0)
        previous = None
        for index, frame in values:
            diagnostics[index] = _frame_market_activity(frame, previous)
            previous = frame
    return diagnostics


def order_evidence_coverage(
    order: Any,
    frames: Sequence[Any],
    *,
    end_timestamp: float,
) -> dict[str, Any]:
    """Return healthy public-trade collection coverage for one order window."""

    start = _number(_get(order, "resting_start_epoch"))
    if start is None:
        start = _number(_get(order, "created_epoch")) or end_timestamp
    terminal = _number(_get(order, "terminal_epoch"))
    active_end = min(end_timestamp, terminal) if terminal is not None else end_timestamp
    expected = max(0.0, active_end - start)
    intervals: list[tuple[float, float]] = []
    event_intervals: list[tuple[float, float]] = []
    for frame in frames:
        if str(_get(frame, "trading_pair", "")) != str(_get(order, "trading_pair", "")):
            continue
        left, right = _frame_interval(frame, end_timestamp)
        left, right = max(left, start), min(right, active_end)
        if right <= left:
            continue
        if _frame_collection_ok(frame):
            intervals.append((left, right))
            if _get(frame, "trades", ()):
                event_intervals.append((left, right))
    covered = min(expected, _union_seconds(intervals))
    event_covered = min(expected, _union_seconds(event_intervals))
    return {
        "expected_seconds": expected,
        "covered_seconds": covered,
        "event_observation_seconds": event_covered,
        "coverage_pct": covered / expected * 100.0 if expected else None,
        "event_observation_coverage_pct": event_covered / expected * 100.0 if expected else None,
        "healthy_intervals": len(intervals),
        "connection_status": (
            "HEALTHY" if covered >= expected and expected > 0 else "PARTIAL" if covered else "NONE"
        ),
    }


def classify_fill_contract(
    order: Any,
    frames: Sequence[Any],
    fills: Sequence[Any],
    *,
    end_timestamp: float,
    model: str = "CONSERVATIVE",
) -> dict[str, Any]:
    """Reconcile one order against actual fills and bounded evidence windows."""

    start = _number(_get(order, "resting_start_epoch"))
    if start is None:
        start = _number(_get(order, "created_epoch")) or end_timestamp
    terminal = _number(_get(order, "terminal_epoch"))
    active_end = min(end_timestamp, terminal) if terminal is not None else end_timestamp
    pair = str(_get(order, "trading_pair", ""))
    relevant_frames = sorted(
        [
            frame
            for frame in frames
            if str(_get(frame, "trading_pair", "")) == pair
            and (_number(_get(frame, "timestamp")) or 0.0) > start
            and (_number(_get(frame, "timestamp")) or 0.0) <= end_timestamp
        ],
        key=lambda frame: _number(_get(frame, "timestamp")) or 0.0,
    )
    usable_active: list[dict[str, Any]] = []
    qualifying_active: list[dict[str, Any]] = []
    qualifying_after_terminal: list[dict[str, Any]] = []
    for frame in relevant_frames:
        frame_epoch = _number(_get(frame, "timestamp")) or 0.0
        for raw_trade in (_get(frame, "trades", ()) or ()):
            trade = _trade_record(raw_trade, pair)
            trade_epoch = _number(trade.get("timestamp"))
            if trade_epoch is None or trade_epoch <= start:
                continue
            if str(trade.get("aggressor_side") or "").lower() not in {"buy", "sell"}:
                continue
            trade["frame_timestamp"] = frame_epoch
            if trade_epoch <= active_end:
                usable_active.append(trade)
                if _trade_qualifies(order, trade):
                    qualifying_active.append(trade)
            elif (
                terminal is not None
                and trade_epoch <= end_timestamp
                and _trade_qualifies(order, trade)
            ):
                qualifying_after_terminal.append(trade)

    actual_fills = [
        fill
        for fill in fills
        if str(_get(fill, "shadow_order_id", "")) == str(_get(order, "shadow_order_id", ""))
    ]
    coverage = order_evidence_coverage(order, frames, end_timestamp=end_timestamp)
    touched = any(
        (
            (_number(_get(frame, "best_ask")) or math.inf)
            <= (_number(_get(order, "price")) or -math.inf)
            if str(_get(order, "side", "")).lower() == "buy"
            else (_number(_get(frame, "best_bid")) or -math.inf)
            >= (_number(_get(order, "price")) or math.inf)
        )
        for frame in relevant_frames
        if (_number(_get(frame, "timestamp")) or 0.0) <= active_end
    )
    is_conservative = str(model).upper() in {"CONSERVATIVE", "CONSERVATIVE_TRADE_THROUGH"}
    if actual_fills:
        status = TRADE_THROUGH_FILLED if is_conservative else TOUCHED_FILLED
        reason = (
            "actual conservative ShadowFill event reconciled"
            if is_conservative
            else "actual touch ShadowFill event reconciled"
        )
    elif is_conservative and qualifying_after_terminal:
        status = TRADE_THROUGH_OBSERVED_NO_FILL
        reason = (
            "qualifying trade observed after order terminal; "
            "no conservative ShadowFill event"
        )
    elif is_conservative and qualifying_active:
        status = TRADE_THROUGH_OBSERVED_NO_FILL
        reason = (
            "qualifying trade observed during active order window; "
            "no conservative ShadowFill event"
        )
    elif not usable_active or coverage["covered_seconds"] <= 0:
        status = INSUFFICIENT_TRADE_EVIDENCE
        reason = "no healthy public-trade collection coverage overlapped the active order window"
    elif touched:
        status = TOUCHED_NOT_TRADED_THROUGH
        reason = "BBO touched but no public trade crossed the conservative threshold"
    else:
        status = NEVER_REACHED_PRICE
        reason = "usable public trades were observed but neither BBO nor trades reached price"
    return {
        "shadow_order_id": _get(order, "shadow_order_id"),
        "trading_pair": pair,
        "level_id": _get(order, "level_id"),
        "side": _get(order, "side"),
        "is_exit": bool(_get(order, "is_exit", False)),
        "status": status,
        "reason": reason,
        "model": model,
        "actual_shadow_fill_count": len(actual_fills),
        "actual_shadow_fill_ids": [str(_get(fill, "fill_id")) for fill in actual_fills],
        "trade_count": len(usable_active),
        "qualifying_trade_count": len(qualifying_active),
        "qualifying_trade_after_terminal_count": len(qualifying_after_terminal),
        "qualifying_trade_ids": [
            str(row.get("trade_id")) for row in qualifying_active if row.get("trade_id")
        ],
        "qualifying_trade_after_terminal_ids": [
            str(row.get("trade_id")) for row in qualifying_after_terminal if row.get("trade_id")
        ],
        "bbo_touched": touched,
        "resting_start_timestamp": _get(order, "resting_start_timestamp"),
        "terminal_timestamp": _get(order, "terminal_timestamp"),
        "resting_start_epoch": start,
        "terminal_epoch": terminal,
        "coverage_expected_seconds": coverage["expected_seconds"],
        "coverage_covered_seconds": coverage["covered_seconds"],
        "coverage_pct": coverage["coverage_pct"],
        "event_observation_seconds": coverage["event_observation_seconds"],
        "connection_status": coverage["connection_status"],
    }


def _plan_reason(
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    decision: Mapping[str, Any],
    global_risk: Mapping[str, Any],
    frame: Mapping[str, Any] | None,
    reconciliation: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """Return an explicit, normalized plan/pause root cause."""

    state_reasons = [str(value) for value in state.get("reasons", []) if value]
    plan_reasons = [str(value) for value in plan.get("reasons", []) if value]
    decision_reasons = [str(value) for value in decision.get("reasons", []) if value]
    all_reasons = [*plan_reasons, *decision_reasons, *state_reasons]
    if frame is None or not bool(frame.get("data_valid", True)):
        return "DATA_VALIDITY", "market snapshot missing or invalid"
    if state.get("state_valid") is False:
        return "DATA_VALIDITY", state_reasons[0] if state_reasons else "market state invalid"
    relation = state.get("btc_transmission") or {}
    if isinstance(relation, Mapping) and relation.get("relationship_valid") is False:
        if str(state.get("trading_pair")) != "BTC-USDC":
            return "RELATIONSHIP_NOT_VALID", "BTC relationship has not reached its validity gate"
    if (
        global_risk.get("btc_iv_available") is False
        and str(state.get("trading_pair")) != "BTC-USDC"
    ):
        if any("IV" in reason.upper() or "VOLATILITY" in reason.upper() for reason in all_reasons):
            return "GLOBAL_IV_DATA", "shared BTC options signal unavailable or stale"
    if not plan.get("valid", False):
        lowered = " ".join(all_reasons).lower()
        if "failed closed" in lowered or "validation" in lowered or "quantiz" in lowered:
            return (
                "GRID_VALIDATION",
                plan_reasons[0] if plan_reasons else "grid validation failed closed",
            )
        if "level" in lowered and (
            "no" in lowered
            or "zero" in lowered
            or not plan.get("buy_levels")
            and not plan.get("sell_levels")
        ):
            return "NO_LEVELS", plan_reasons[0] if plan_reasons else "no executable levels"
        return "PLAN_VALIDATION", plan_reasons[0] if plan_reasons else "plan validity gate failed"
    mode = str(decision.get("mode", plan.get("mode", ""))).lower()
    if mode == "pause" or not bool(plan.get("enabled", True)):
        detail = (
            decision_reasons[0]
            if decision_reasons
            else plan_reasons[0]
            if plan_reasons
            else "mode selector pause"
        )
        return "MODE_PAUSE", detail
    if reconciliation and reconciliation.get("pause_reason"):
        return "EXECUTION_GATE", str(reconciliation["pause_reason"])
    route_blocked = bool((reconciliation or {}).get("skip_count"))
    if route_blocked:
        return "PORTFOLIO_RISK", "portfolio route or risk gate blocked desired levels"
    if not plan.get("buy_levels") and not plan.get("sell_levels"):
        return "NO_LEVELS", "valid plan contains no executable levels"
    return "NONE", ""


def _plan_level_ids(plan: Mapping[str, Any]) -> list[str]:
    """Return stable level identities from a serialized GridPlan."""

    values: list[str] = []
    for side in ("buy_levels", "sell_levels", "levels"):
        levels = plan.get(side) or []
        if not isinstance(levels, Sequence) or isinstance(levels, (str, bytes)):
            continue
        for level in levels:
            if isinstance(level, Mapping):
                value = level.get("level_id")
                if (
                    value is None
                    and level.get("side") is not None
                    and level.get("level_index") is not None
                ):
                    value = f"{level['side']}_{level['level_index']}"
            else:
                value = getattr(level, "level_id", None)
            if value is not None:
                values.append(str(value))
    return sorted(set(values))


def build_plan_invalid_rows(
    cycles: Sequence[Mapping[str, Any]],
    frames: Sequence[Any],
    reconciliation: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Trace every plan-valid transition with its upstream decision inputs."""

    frame_by_pair: dict[str, list[Any]] = defaultdict(list)
    for frame in frames:
        frame_by_pair[str(_get(frame, "trading_pair", ""))].append(frame)
    for values in frame_by_pair.values():
        values.sort(key=lambda value: _number(_get(value, "timestamp")) or 0.0)
    reconciliation_by_key = {
        (str(row.get("trading_pair")), str(row.get("timestamp"))): row for row in reconciliation
    }
    previous: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for cycle in sorted(cycles, key=lambda row: _number(row.get("timestamp_epoch")) or 0.0):
        cycle_epoch, _ = normalize_timestamp(cycle.get("timestamp"))
        cycle_epoch = cycle_epoch or _number(cycle.get("timestamp_epoch")) or 0.0
        for pair, plan_value in (cycle.get("plans") or {}).items():
            plan = dict(plan_value) if isinstance(plan_value, Mapping) else {}
            state = dict((cycle.get("states") or {}).get(pair) or {})
            decision = dict((cycle.get("decisions") or {}).get(pair) or {})
            global_risk = dict(cycle.get("global_risk") or {})
            matching_frames = frame_by_pair.get(str(pair), [])
            frame = min(
                matching_frames,
                key=lambda value: abs((_number(_get(value, "timestamp")) or 0.0) - cycle_epoch),
                default=None,
            )
            frame_record = frame.to_strategy_snapshot() if frame is not None else None
            reconciliation_row = reconciliation_by_key.get((str(pair), str(cycle.get("timestamp"))))
            valid = bool(plan.get("valid", False))
            desired_level_ids = _plan_level_ids(plan)
            previous_row = previous.get(str(pair))
            old = previous_row.get("plan_valid") if previous_row else None
            if old is None:
                transition = "INITIAL_VALID" if valid else "INITIAL_INVALID"
            elif old and not valid:
                transition = "VALID_TO_INVALID"
            elif not old and valid:
                transition = "INVALID_TO_VALID"
            elif not valid:
                transition = "INVALID_CONTINUED"
            else:
                transition = "VALID_CONTINUED"
            category, detail = _plan_reason(
                plan, state, decision, global_risk, frame_record, reconciliation_row
            )
            is_paused = category != "NONE" or str(decision.get("mode", "")).lower() == "pause"
            rows.append(
                {
                    "timestamp": cycle.get("timestamp") or _iso(cycle_epoch),
                    "timestamp_epoch": cycle_epoch,
                    "cycle_id": cycle.get("cycle_id"),
                    "trading_pair": pair,
                    "transition": transition,
                    "previous_plan_valid": old,
                    "plan_valid": valid,
                    "is_paused": is_paused,
                    "reason_category": category,
                    "reason": detail,
                    "plan_version": plan.get("plan_version"),
                    "previous_plan_version": (
                        previous_row.get("plan_version") if previous_row else None
                    ),
                    "mode": decision.get("mode", plan.get("mode")),
                    "previous_mode": previous_row.get("mode") if previous_row else None,
                    "plan_enabled": plan.get("enabled"),
                    "previous_plan_enabled": (
                        previous_row.get("plan_enabled") if previous_row else None
                    ),
                    "desired_level_ids": desired_level_ids,
                    "previous_desired_level_ids": (
                        previous_row.get("desired_level_ids", []) if previous_row else []
                    ),
                    "removed_level_ids": sorted(
                        set(previous_row.get("desired_level_ids", []) if previous_row else [])
                        - set(desired_level_ids)
                    ),
                    "added_level_ids": sorted(
                        set(desired_level_ids)
                        - set(previous_row.get("desired_level_ids", []) if previous_row else [])
                    ),
                    "buy_levels_count": len(plan.get("buy_levels") or []),
                    "sell_levels_count": len(plan.get("sell_levels") or []),
                    "state_valid": state.get("state_valid"),
                    "state_confidence": state.get("confidence"),
                    "state_reasons": state.get("reasons", []),
                    "market_data_valid": frame_record.get("data_valid") if frame_record else None,
                    "market_environment": (
                        frame_record.get("market_environment") if frame_record else None
                    ),
                    "global_iv_available": global_risk.get("btc_iv_available"),
                    "global_iv_age_seconds": global_risk.get("btc_iv_age_seconds"),
                    "global_iv_regime": global_risk.get("global_risk_regime"),
                    "relationship_valid": (
                        (state.get("btc_transmission") or {}).get("relationship_valid")
                        if isinstance(state.get("btc_transmission"), Mapping)
                        else None
                    ),
                    "relationship_confidence": state.get("relationship_confidence"),
                    "inventory_ratio": decision.get(
                        "inventory_ratio", state.get("inventory_ratio")
                    ),
                    "risk_reasons": (cycle.get("portfolio_risk") or {}).get("reasons", []),
                    "risk_ratio": (cycle.get("portfolio_risk") or {}).get("portfolio_risk_ratio"),
                    "portfolio_gross_notional": (cycle.get("portfolio_risk") or {}).get(
                        "gross_notional"
                    ),
                    "pending_entries": cycle.get("pending_entries"),
                    "freshness_seconds": (
                        (cycle.get("portfolio_risk") or {}).get("timestamp")
                    ),
                    "validation_gates": plan.get(
                        "validation_gates", plan.get("gates", plan.get("validation"))
                    ),
                    "market_data_age_seconds": (
                        frame_record.get("market_data_age_seconds") if frame_record else None
                    ),
                    "option_data_available": (
                        frame_record.get("iv_data_available") if frame_record else None
                    ),
                    "option_data_age_seconds": (
                        frame_record.get("option_data_age_seconds") if frame_record else None
                    ),
                    "relationship_data_available": (
                        (state.get("btc_transmission") or {}).get("relationship_valid")
                        if isinstance(state.get("btc_transmission"), Mapping)
                        else None
                    ),
                    "account_inventory_valid": state.get("account_state_valid"),
                    "active_level_ids": (
                        reconciliation_row.get("active_level_ids")
                        if reconciliation_row
                        else []
                    ),
                    "create_count": (
                        reconciliation_row.get("create_count") if reconciliation_row else 0
                    ),
                    "keep_count": reconciliation_row.get("keep_count") if reconciliation_row else 0,
                    "stop_count": reconciliation_row.get("stop_count") if reconciliation_row else 0,
                    "skip_count": reconciliation_row.get("skip_count") if reconciliation_row else 0,
                    "pause_reason": (
                        reconciliation_row.get("pause_reason") if reconciliation_row else None
                    ),
                }
            )
            previous[str(pair)] = rows[-1]
    return rows


def build_pause_episodes(
    rows: Sequence[Mapping[str, Any]], continuity_gap_seconds: float = 15.0
) -> list[dict[str, Any]]:
    """Group raw pause rows into explicit pair/cause episodes."""

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("is_paused"):
            grouped[(str(row.get("trading_pair")), str(row.get("reason_category")))].append(row)
    episodes: list[dict[str, Any]] = []
    for (pair, category), values in grouped.items():
        values = sorted(values, key=lambda row: _number(row.get("timestamp_epoch")) or 0.0)
        current: dict[str, Any] | None = None
        for row in values:
            stamp = _number(row.get("timestamp_epoch")) or 0.0
            if (
                current is None
                or stamp - float(current["last_timestamp_epoch"]) > continuity_gap_seconds
            ):
                if current is not None:
                    episodes.append(current)
                current = {
                    "episode_id": f"pause-{len(episodes) + 1:05d}",
                    "trading_pair": pair,
                    "reason_category": category,
                    "reason": row.get("reason"),
                    "first_timestamp": row.get("timestamp") or _iso(stamp),
                    "first_timestamp_epoch": stamp,
                    "last_timestamp": row.get("timestamp") or _iso(stamp),
                    "last_timestamp_epoch": stamp,
                    "raw_cycle_count": 1,
                    "plan_versions": [row.get("plan_version")],
                    "modes": [row.get("mode")],
                }
            else:
                current["last_timestamp"] = row.get("timestamp") or _iso(stamp)
                current["last_timestamp_epoch"] = stamp
                current["raw_cycle_count"] = int(current["raw_cycle_count"]) + 1
                current["plan_versions"].append(row.get("plan_version"))
                current["modes"].append(row.get("mode"))
        if current is not None:
            episodes.append(current)
    for row in episodes:
        row["duration_seconds"] = max(
            0.0, float(row["last_timestamp_epoch"]) - float(row["first_timestamp_epoch"])
        )
        row["plan_versions"] = sorted(
            {value for value in row["plan_versions"] if value is not None}
        )
        row["modes"] = sorted({str(value) for value in row["modes"] if value is not None})
        row["data_driven"] = row["reason_category"] in {
            "DATA_VALIDITY",
            "GLOBAL_IV_DATA",
            "RELATIONSHIP_NOT_VALID",
        }
        row["strategy_or_gate_driven"] = row["reason_category"] in {
            "GRID_VALIDATION",
            "NO_LEVELS",
            "MODE_PAUSE",
            "EXECUTION_GATE",
            "PORTFOLIO_RISK",
        }
    return sorted(episodes, key=lambda row: float(row["first_timestamp_epoch"]))


def build_plan_oscillation(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Summarize level disappearance/reappearance and validity oscillation."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("trading_pair"))].append(row)
    output: list[dict[str, Any]] = []
    for pair, values in grouped.items():
        ordered = sorted(values, key=lambda row: _number(row.get("timestamp_epoch")) or 0.0)
        valid_to_invalid = sum(row.get("transition") == "VALID_TO_INVALID" for row in ordered)
        invalid_to_valid = sum(row.get("transition") == "INVALID_TO_VALID" for row in ordered)
        invalid = [row for row in ordered if not row.get("plan_valid")]
        levels_removed = sum(
            bool(row.get("previous_plan_valid")) and not row.get("plan_valid") for row in ordered
        )
        longest = 0
        current = 0
        for row in ordered:
            if not row.get("plan_valid"):
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        categories = Counter(str(row.get("reason_category")) for row in invalid)
        data_cycles = sum(
            row.get("reason_category")
            in {"DATA_VALIDITY", "GLOBAL_IV_DATA", "RELATIONSHIP_NOT_VALID"}
            for row in invalid
        )
        strategy_cycles = sum(
            row.get("reason_category")
            in {
                "GRID_VALIDATION",
                "NO_LEVELS",
                "MODE_PAUSE",
                "EXECUTION_GATE",
                "PORTFOLIO_RISK",
            }
            for row in invalid
        )
        output.append(
            {
                "trading_pair": pair,
                "cycle_count": len(ordered),
                "invalid_cycle_count": len(invalid),
                "valid_to_invalid_count": valid_to_invalid,
                "invalid_to_valid_count": invalid_to_valid,
                "level_removed_count": levels_removed,
                "longest_invalid_streak_cycles": longest,
                "data_driven_cycles": data_cycles,
                "strategy_or_gate_driven_cycles": strategy_cycles,
                "dominant_reason": categories.most_common(1)[0][0] if categories else "NONE",
                "reason_counts": dict(categories),
                "oscillation_classification": (
                    "DATA_DRIVEN" if data_cycles > strategy_cycles else
                    "STRATEGY_OR_GATE_DRIVEN" if strategy_cycles > data_cycles else
                    "NONE_OR_MIXED"
                ),
            }
        )
    return output


def build_risk_root_causes(
    risk_events: Sequence[Mapping[str, Any]], risk_episodes: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Aggregate raw risk checks while retaining pending/filled exposure traces."""

    episodes_by_key: Counter[tuple[str, str, str]] = Counter()
    for row in risk_episodes:
        episodes_by_key[
            (
                str(row.get("trading_pair")),
                str(row.get("reason")),
                str(row.get("model", "")),
            )
        ] += 1
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in risk_events:
        grouped[
            (
                str(row.get("model", "")),
                str(row.get("trading_pair", "")),
                str(row.get("category", "OTHER")),
            )
        ].append(row)
    output: list[dict[str, Any]] = []
    for (model, pair, category), values in sorted(grouped.items()):
        candidates = [_number(row.get("candidate_notional")) or 0.0 for row in values]
        before = [_number(row.get("exposure_before")) or 0.0 for row in values]
        after = [_number(row.get("exposure_after_candidate")) for row in values]
        trace_consistent = all(
            after_value is None or abs(after_value - before_value - candidate) <= 1e-8
            for before_value, candidate, after_value in zip(before, candidates, after, strict=False)
        )
        output.append(
            {
                "model": model,
                "trading_pair": pair,
                "category": category,
                "raw_block_count": len(values),
                "episode_count": episodes_by_key.get((pair, category, model), 0),
                "candidate_notional_sum": sum(candidates),
                "candidate_notional_max": max(candidates, default=0.0),
                "exposure_before_max": max(before, default=0.0),
                "exposure_after_candidate_max": max((value or 0.0 for value in after), default=0.0),
                "pending_exposure_in_trace": any(
                    any(
                        key in row
                        for key in (
                            "pending_buy_notional",
                            "pending_sell_notional",
                            "pending_entries",
                        )
                    )
                    for row in values
                ),
                "filled_exposure_in_trace": any(
                    any(
                        key in row
                        for key in (
                            "filled_exposure",
                            "position_notional",
                            "inventory_notional",
                        )
                    )
                    for row in values
                ),
                "candidate_trace_consistent": trace_consistent,
                "isolation_check": (
                    "PASS" if model in {"CONSERVATIVE", "TOUCH_OPTIMISTIC", ""} else "UNKNOWN"
                ),
            }
        )
    return output


def build_trade_coverage(
    frames: Sequence[Any],
    orders: Sequence[Mapping[str, Any]],
    *,
    start_timestamp: float,
    end_timestamp: float,
) -> list[dict[str, Any]]:
    """Produce wall-clock, asset, event, and order-weighted coverage rows."""

    pairs = sorted(
        {str(_get(frame, "trading_pair", "")) for frame in frames}
        | {str(row.get("trading_pair")) for row in orders}
    )
    output: list[dict[str, Any]] = []
    for pair in pairs:
        selected = [frame for frame in frames if str(_get(frame, "trading_pair", "")) == pair]
        selected.sort(key=lambda frame: _number(_get(frame, "timestamp")) or 0.0)
        activity = build_trade_stream_diagnostics(selected)
        healthy_intervals = []
        event_intervals = []
        observed_intervals = []
        for frame in selected:
            left, right = _frame_interval(frame, end_timestamp)
            left, right = max(start_timestamp, left), min(end_timestamp, right)
            if right <= left:
                continue
            observed_intervals.append((left, right))
            if _frame_collection_ok(frame):
                healthy_intervals.append((left, right))
            if _get(frame, "trades", ()):
                event_intervals.append((left, right))
        pair_orders = [
            row
            for row in orders
            if str(row.get("trading_pair")) == pair and not row.get("is_exit")
        ]
        expected_weight = sum(_number(row.get("notional")) or 0.0 for row in pair_orders)
        covered_weight = 0.0
        expected_seconds = 0.0
        covered_seconds = 0.0
        for order in pair_orders:
            order_coverage = order_evidence_coverage(
                order, frames, end_timestamp=end_timestamp
            )
            expected = _number(order_coverage.get("expected_seconds")) or 0.0
            covered = _number(order_coverage.get("covered_seconds")) or 0.0
            weight = _number(order.get("notional")) or 0.0
            expected_seconds += expected
            covered_seconds += covered
            covered_weight += covered / expected * weight if expected > 0 else 0.0
        healthy = _union_seconds(healthy_intervals)
        events = _union_seconds(event_intervals)
        observed = _union_seconds(observed_intervals)
        gaps: list[tuple[float, float]] = []
        cursor = start_timestamp
        for left, right in sorted(healthy_intervals):
            if left > cursor:
                gaps.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < end_timestamp:
            gaps.append((cursor, end_timestamp))
        largest = sorted(
            gaps, key=lambda pair_value: pair_value[1] - pair_value[0], reverse=True
        )[:10]
        classification_counts = Counter(
            row.get("trade_silence_classification") for row in activity
        )
        suspect_count = classification_counts.get(PUBLIC_TRADE_STREAM_SUSPECT, 0)
        trade_count = sum(len(_get(frame, "trades", ()) or ()) for frame in selected)
        if any(
            _get(frame, "trade_collection_status", "UNKNOWN")
            not in HEALTHY_COLLECTION_STATUSES
            for frame in selected
        ):
            stream_classification = "COLLECTION_INCOMPLETE"
        elif suspect_count:
            stream_classification = "STREAM_SUSPECT"
        elif trade_count:
            stream_classification = "GENUINELY_SPARSE"
        else:
            stream_classification = "FUNCTIONING_BUT_MARKET_SPARSE"
        output.append(
            {
                "record_type": "ASSET_SUMMARY",
                "asset": pair,
                "session_wall_clock_seconds": max(0.0, end_timestamp - start_timestamp),
                "asset_observed_seconds": observed,
                "collection_healthy_seconds": healthy,
                "event_observation_seconds": events,
                "collection_coverage_pct": healthy
                / max(1e-9, end_timestamp - start_timestamp)
                * 100.0
                if end_timestamp > start_timestamp else None,
                "event_observation_coverage_pct": events
                / max(1e-9, end_timestamp - start_timestamp)
                * 100.0
                if end_timestamp > start_timestamp else None,
                "order_weighted_expected_seconds": expected_seconds,
                "order_weighted_covered_seconds": covered_seconds,
                "order_weighted_coverage_pct": covered_seconds / expected_seconds * 100.0
                if expected_seconds else None,
                "order_weighted_notional_coverage_pct": covered_weight / expected_weight * 100.0
                if expected_weight else None,
                "order_count": len(pair_orders),
                "trade_count": trade_count,
                "bbo_update_count": sum(
                    row.get("market_bbo_valid", False) for row in activity
                ),
                "mid_price_update_count": sum(
                    row.get("mid_price_updated", False) for row in activity
                ),
                "depth_update_count": sum(
                    row.get("depth_updated", False) for row in activity
                ),
                "stream_suspect_count": suspect_count,
                "stream_classification": stream_classification,
                "stream_classification_counts": dict(classification_counts),
                "gap_count": len(gaps),
                "largest_gap_seconds": max((right - left for left, right in gaps), default=0.0),
            }
        )
        for rank, (left, right) in enumerate(largest, 1):
            frame = min(
                selected,
                key=lambda candidate: abs((_number(_get(candidate, "timestamp")) or 0.0) - left),
                default=None,
            )
            gap_activity = [
                activity[index]
                for index, candidate in enumerate(selected)
                if left <= (_number(_get(candidate, "timestamp")) or 0.0) <= right
            ]
            gap_frames = [
                candidate
                for candidate in selected
                if left <= (_number(_get(candidate, "timestamp")) or 0.0) <= right
            ]
            output.append(
                {
                    "record_type": "LARGEST_GAP",
                    "asset": pair,
                    "gap_rank": rank,
                    "gap_start": _iso(left),
                    "gap_end": _iso(right),
                    "gap_start_epoch": left,
                    "gap_end_epoch": right,
                    "gap_seconds": right - left,
                    "best_bid": _get(frame, "best_bid") if frame else None,
                    "best_ask": _get(frame, "best_ask") if frame else None,
                    "mid_price": (
                        ((_number(_get(frame, "best_bid")) or 0.0)
                        + (_number(_get(frame, "best_ask")) or 0.0))
                        / 2.0
                        if frame
                        else None
                    ),
                    "bid_depth": _get(frame, "bid_depth") if frame else None,
                    "ask_depth": _get(frame, "ask_depth") if frame else None,
                    "trades_observed": len(_get(frame, "trades", ()) or ()) if frame else 0,
                    "bbo_updates_during_gap": sum(
                        row.get("market_bbo_valid", False) for row in gap_activity
                    ),
                    "mid_price_updates_during_gap": sum(
                        row.get("mid_price_updated", False) for row in gap_activity
                    ),
                    "max_mid_move_during_gap": max(
                        (row.get("mid_move", 0.0) for row in gap_activity), default=0.0
                    ),
                    "depth_updates_during_gap": sum(
                        row.get("depth_updated", False) for row in gap_activity
                    ),
                    "public_historical_trades_found": sum(
                        _number(_get(candidate, "trade_crosscheck_rest_count")) or 0.0
                        for candidate in gap_frames
                    ),
                    "trade_silence_classification": (
                        PUBLIC_TRADE_STREAM_SUSPECT
                        if any(
                            row.get("trade_silence_classification")
                            == PUBLIC_TRADE_STREAM_SUSPECT
                            for row in gap_activity
                        )
                        else "FUNCTIONING_BUT_MARKET_SPARSE"
                    ),
                    "connection_status": _get(frame, "trade_connection_status") if frame else None,
                }
            )
    return output


def summarize_order_evidence_coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize conservative resting-order evidence coverage distributions."""

    order_rows = [
        row
        for row in rows
        if row.get("record_type") == "ORDER"
        and str(row.get("model", "")).upper() == "CONSERVATIVE"
    ]
    values = sorted(
        value
        for row in order_rows
        if (value := _number(row.get("coverage_pct"))) is not None
    )

    def percentile(percent: float) -> float | None:
        if not values:
            return None
        position = (len(values) - 1) * percent / 100.0
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return values[lower]
        fraction = position - lower
        return values[lower] + (values[upper] - values[lower]) * fraction

    buckets = {
        "ge_95_pct": sum(value >= 95.0 for value in values),
        "80_to_95_pct": sum(80.0 <= value < 95.0 for value in values),
        "50_to_80_pct": sum(50.0 <= value < 80.0 for value in values),
        "lt_50_pct": sum(value < 50.0 for value in values),
    }
    total = len(values)
    return {
        "orders_total": len(order_rows),
        "orders_measured": total,
        "orders_unmeasured": len(order_rows) - total,
        "zero_lifetime_orders": sum(
            (expected := _number(row.get("expected_seconds"))) is not None and expected <= 0.0
            for row in order_rows
        ),
        "median_coverage_pct": percentile(50.0),
        "p25_coverage_pct": percentile(25.0),
        "p75_coverage_pct": percentile(75.0),
        "p90_coverage_pct": percentile(90.0),
        "orders_ge_95_pct": buckets["ge_95_pct"],
        "orders_80_to_95_pct": buckets["80_to_95_pct"],
        "orders_50_to_80_pct": buckets["50_to_80_pct"],
        "orders_lt_50_pct": buckets["lt_50_pct"],
        "bucket_percentages": {
            key: value / total * 100.0 if total else None for key, value in buckets.items()
        },
    }


def legacy_fill_reconciliation(legacy_root: Path | None) -> list[dict[str, Any]]:
    """Reconcile old eligibility rows without fabricating missing raw evidence."""

    if legacy_root is None:
        return []
    orders_path = legacy_root / "orders.csv"
    fills_path = legacy_root / "fills.csv"
    if not orders_path.is_file():
        return []
    with orders_path.open(newline="", encoding="utf-8") as handle:
        orders = list(csv.DictReader(handle))
    fills = []
    if fills_path.is_file():
        with fills_path.open(newline="", encoding="utf-8") as handle:
            fills = list(csv.DictReader(handle))
    filled_ids = {
        str(row.get("shadow_order_id"))
        for row in fills
        if str(row.get("model", "")).upper() == "CONSERVATIVE"
    }
    rows: list[dict[str, Any]] = []
    for order in orders:
        if str(order.get("model", "")).upper() != "CONSERVATIVE":
            continue
        if order.get("fill_eligibility_status") != TRADE_THROUGH_FILLED:
            continue
        order_id = str(order.get("shadow_order_id"))
        has_fill = order_id in filled_ids
        rows.append(
            {
                "record_scope": "LEGACY",
                "legacy_session_id": legacy_root.name,
                "model": "CONSERVATIVE",
                "shadow_order_id": order_id,
                "trading_pair": order.get("trading_pair"),
                "level_id": order.get("level_id"),
                "side": order.get("side"),
                "legacy_status": order.get("fill_eligibility_status"),
                "status": TRADE_THROUGH_FILLED if has_fill else TRADE_THROUGH_OBSERVED_NO_FILL,
                "reason": (
                    "legacy conservative ShadowFill event found"
                    if has_fill
                    else (
                        "legacy row had no conservative ShadowFill event; raw trade frames "
                        "were not persisted"
                    )
                ),
                "actual_shadow_fill_count": int(has_fill),
                "raw_trade_trace_available": False,
                "audit_disposition": "RECONCILED" if not has_fill else "CONFIRMED",
            }
        )
    return rows


def _csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        json.dumps(value, sort_keys=True)
                        if isinstance(value, (dict, list, tuple))
                        else value
                    )
                    for key, value in row.items()
                }
            )


def write_stage12e_artifacts(
    *,
    project_root: str | Path,
    session_id: str,
    config: Mapping[str, Any],
    frames: Sequence[Any],
    model_metrics: Mapping[str, Any],
    cycles_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    start_timestamp: float,
    end_timestamp: float,
    legacy_root: Path | None = None,
) -> dict[str, Any]:
    """Write the complete Stage 12E artifact contract and return its summary."""

    root = Path(project_root).expanduser().resolve() / "reports" / "stage12e"
    root.mkdir(parents=True, exist_ok=True)
    current_fill_rows: list[dict[str, Any]] = []
    pipeline_rows: list[dict[str, Any]] = []
    crosscheck_rows: list[dict[str, Any]] = []
    all_orders: list[dict[str, Any]] = []
    frame_diagnostics = build_trade_stream_diagnostics(frames)
    conservative = model_metrics.get("CONSERVATIVE")
    for model, item in model_metrics.items():
        orders = list(getattr(item, "orders", []) or [])
        fills = list(getattr(item, "fills", []) or [])
        all_orders.extend(orders)
        for order in orders:
            if order.get("is_exit"):
                continue
            current_fill_rows.append(
                classify_fill_contract(
                    order,
                    frames,
                    fills,
                    end_timestamp=end_timestamp,
                    model=str(model),
                )
                | {"record_scope": "CURRENT", "session_id": session_id}
            )
    for frame_index, frame in enumerate(frames):
        pair = str(_get(frame, "trading_pair", ""))
        activity = frame_diagnostics[frame_index]
        pipeline_rows.append(
            {
                "session_id": session_id,
                "timestamp": _get(frame, "timestamp"),
                "trading_pair": pair,
                "source": _get(frame, "trade_source"),
                "collection_status": _get(frame, "trade_collection_status"),
                "endpoint": _get(frame, "trade_endpoint"),
                "channel": _get(frame, "trade_channel"),
                "request_window_start": _get(
                    frame, "trade_request_window_start_epoch"
                ),
                "request_window_end": _get(frame, "trade_request_window_end_epoch"),
                "raw_row_count": _get(frame, "trade_raw_count"),
                "canonical_row_count": _get(
                    frame, "trade_canonical_count", len(_get(frame, "trades", ()) or ())
                ),
                "duplicate_row_count": _get(frame, "trade_duplicate_count"),
                "rejected_row_count": _get(frame, "trade_rejected_count"),
                "page_count": _get(frame, "trade_page_count"),
                "page_size": _get(frame, "trade_page_size"),
                "pagination_count": _get(frame, "trade_pagination_count"),
                "timestamp_unit": _get(frame, "trade_timestamp_unit"),
                "sort_order": _get(frame, "trade_sort_order"),
                "dedup_key": _get(frame, "trade_dedup_key"),
                "connection_status": _get(frame, "trade_connection_status"),
                "reconnect_count": _get(frame, "trade_reconnect_count"),
                "rate_limit_status": _get(frame, "trade_rate_limit_status"),
                "error": _get(frame, "trade_collection_error"),
                "trade_event_count": len(_get(frame, "trades", ()) or ()),
                "market_bbo_valid": activity.get("market_bbo_valid"),
                "depth_data_available": activity.get("depth_data_available"),
                "depth_source": activity.get("depth_source"),
                "bbo_updated": activity.get("bbo_updated"),
                "mid_price_updated": activity.get("mid_price_updated"),
                "depth_updated": activity.get("depth_updated"),
                "trade_silence_classification": activity.get(
                    "trade_silence_classification"
                ),
            }
        )
        if _get(frame, "trade_crosscheck_status"):
            crosscheck_rows.append(
                {
                    "session_id": session_id,
                    "timestamp": _get(frame, "timestamp"),
                    "trading_pair": pair,
                    "collector_source": _get(frame, "trade_source"),
                    "independent_source": "REST_GET_TRADE_HISTORY",
                    "status": _get(frame, "trade_crosscheck_status"),
                    "collector_count": _get(frame, "trade_crosscheck_collector_count"),
                    "rest_count": _get(frame, "trade_crosscheck_rest_count"),
                    "missing_from_collector": _get(
                        frame, "trade_crosscheck_missing_from_collector"
                    ),
                    "extra_in_collector": _get(frame, "trade_crosscheck_extra_in_collector"),
                    "timestamp_unit": _get(frame, "trade_timestamp_unit"),
                    "error": _get(frame, "trade_crosscheck_error"),
                    "window_start": _get(frame, "trade_crosscheck_window_start_epoch"),
                    "window_end": _get(frame, "trade_crosscheck_window_end_epoch"),
                }
            )
    primary_orders = list(getattr(conservative, "orders", []) or []) if conservative else []
    coverage_rows = build_trade_coverage(
        frames,
        primary_orders,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    pause_rows = build_plan_invalid_rows(
        list(cycles_by_model.get("CONSERVATIVE", ())),
        frames,
        getattr(conservative, "reconciliation_decisions", []) if conservative else (),
    )
    pause_episodes = build_pause_episodes(pause_rows)
    oscillation_rows = build_plan_oscillation(pause_rows)
    risk_events = [
        {"model": model, **row}
        for model, item in model_metrics.items()
        for row in (getattr(item, "risk_events", []) or [])
    ]
    risk_episodes = [
        {"model": model, **row}
        for model, item in model_metrics.items()
        for row in (getattr(item, "risk_episodes", []) or [])
    ]
    risk_rows = build_risk_root_causes(risk_events, risk_episodes)
    order_coverage_rows = []
    for row in current_fill_rows:
        order_coverage_rows.append(
            {
                "record_type": "ORDER",
                "session_id": session_id,
                "model": row.get("model"),
                "shadow_order_id": row.get("shadow_order_id"),
                "trading_pair": row.get("trading_pair"),
                "level_id": row.get("level_id"),
                "side": row.get("side"),
                "notional": next(
                    (
                        order.get("notional")
                        for order in all_orders
                        if order.get("shadow_order_id") == row.get("shadow_order_id")
                    ),
                    None,
                ),
                "expected_seconds": row.get("coverage_expected_seconds"),
                "covered_seconds": row.get("coverage_covered_seconds"),
                "coverage_pct": row.get("coverage_pct"),
                "event_observation_seconds": row.get("event_observation_seconds"),
                "connection_status": row.get("connection_status"),
                "trade_count": row.get("trade_count"),
                "qualifying_trade_count": row.get("qualifying_trade_count"),
                "status": row.get("status"),
            }
        )
    order_coverage_summary = summarize_order_evidence_coverage(order_coverage_rows)
    legacy_rows = legacy_fill_reconciliation(legacy_root)
    fill_audit_rows = [*current_fill_rows, *legacy_rows]
    conservative_actual_fills = (
        sum(
            row.get("entry_exit", "entry") == "entry"
            for row in (getattr(conservative, "fills", []) or [])
        )
        if conservative
        else 0
    )
    conservative_filled_statuses = sum(
        row.get("status") == TRADE_THROUGH_FILLED and row.get("record_scope") == "CURRENT"
        for row in fill_audit_rows
        if row.get("model") == "CONSERVATIVE"
    )
    current_observed_no_fill = sum(
        row.get("status") == TRADE_THROUGH_OBSERVED_NO_FILL and row.get("record_scope") == "CURRENT"
        for row in fill_audit_rows
        if row.get("model") == "CONSERVATIVE"
    )
    legacy_reconciled = sum(row.get("audit_disposition") == "RECONCILED" for row in legacy_rows)
    invariant = conservative_actual_fills == conservative_filled_statuses
    pipeline_mismatches = sum(row.get("status") == "MISMATCH" for row in crosscheck_rows)
    pipeline_collection_incomplete = any(
        not _frame_collection_ok(frame) for frame in frames
    )
    suspect_by_asset = Counter(
        row.get("trading_pair")
        for row in pipeline_rows
        if row.get("trade_silence_classification") == PUBLIC_TRADE_STREAM_SUSPECT
    )
    if pipeline_collection_incomplete:
        pipeline_classification = "COLLECTION_INCOMPLETE"
    elif pipeline_mismatches:
        pipeline_classification = "PIPELINE_MISMATCH"
    elif suspect_by_asset:
        pipeline_classification = "STREAM_SUSPECT"
    elif any(row.get("trade_event_count", 0) for row in pipeline_rows):
        pipeline_classification = "GENUINELY_SPARSE"
    else:
        pipeline_classification = "FUNCTIONING_BUT_MARKET_SPARSE"
    safety_pass = (
        str(config.get("market_environment", "")).lower() == "mainnet"
        and str(config.get("execution_backend", "")).upper() == "SHADOW"
        and str(config.get("execution_mode", "")).upper() == "SHADOW"
        and not bool(config.get("execution_enabled"))
        and not bool(config.get("allow_mainnet_trading"))
        and all(
            int(row.get("real_exchange_mutation_calls", 0) or 0) == 0
            for row in risk_events
        )
    )
    summary = {
        "stage": "12E",
        "session_id": session_id,
        "generated_at": _iso(end_timestamp),
        "safety": {
            "status": "PASS" if safety_pass else "FAIL",
            "market_environment": config.get("market_environment"),
            "execution_mode": config.get("execution_mode"),
            "execution_backend": config.get("execution_backend"),
            "execution_enabled": bool(config.get("execution_enabled")),
            "allow_mainnet_trading": bool(config.get("allow_mainnet_trading")),
            "real_exchange_mutation_calls": 0,
        },
        "fill_contract": {
            "status": "PASS" if invariant else "FAIL",
            "conservative_shadow_fill_events": conservative_actual_fills,
            "conservative_filled_statuses": conservative_filled_statuses,
            "current_trade_through_observed_no_fill": current_observed_no_fill,
        "legacy_rows_reconciled_no_fill": legacy_reconciled,
            "legacy_raw_trade_trace_available": False if legacy_rows else None,
        },
        "trade_pipeline": {
            "status": (
                "PASS"
                if pipeline_rows
                and not pipeline_collection_incomplete
                and not pipeline_mismatches
                else "INCOMPLETE"
            ),
            "classification": pipeline_classification,
            "collector_rows": len(pipeline_rows),
            "crosscheck_rows": len(crosscheck_rows),
            "websocket_rows": sum(row.get("source") == "websocket" for row in pipeline_rows),
            "rest_fallback_rows": sum(
                row.get("source") == "rest_fallback" for row in pipeline_rows
            ),
            "crosscheck_mismatches": pipeline_mismatches,
            "stream_suspect": bool(suspect_by_asset),
            "stream_suspect_frames": sum(suspect_by_asset.values()),
            "stream_suspect_by_asset": dict(suspect_by_asset),
            "silence_classification_counts": dict(
                Counter(row.get("trade_silence_classification") for row in pipeline_rows)
            ),
        },
        "coverage": {
            "assets": [row for row in coverage_rows if row.get("record_type") == "ASSET_SUMMARY"],
            "largest_gap_rows": [
                row for row in coverage_rows if row.get("record_type") == "LARGEST_GAP"
            ],
            "order_evidence": order_coverage_summary,
        },
        "pause": {
            "plan_rows": len(pause_rows),
            "invalid_rows": sum(not row.get("plan_valid") for row in pause_rows),
            "valid_to_invalid": sum(
                row.get("transition") == "VALID_TO_INVALID" for row in pause_rows
            ),
            "pause_episodes": len(pause_episodes),
            "oscillation": oscillation_rows,
            "categories": dict(
                Counter(
                    row.get("reason_category")
                    for row in pause_rows
                    if row.get("is_paused")
                )
            ),
        },
        "risk": {
            "root_causes": risk_rows,
            "raw_events": len(risk_events),
            "episodes": len(risk_episodes),
        },
        "readiness": "NOT READY FOR OPTIMIZATION",
        "readiness_reasons": [
            "Stage 12E is diagnostic remediation only; no strategy tuning or live execution "
            "is authorized",
            "a bounded diagnostic must be interpreted with conservative public-trade evidence "
            "and queue uncertainty",
        ],
    }
    artifact_rows = {
        "fill_contract_audit.csv": fill_audit_rows,
        "trade_pipeline_audit.csv": pipeline_rows,
        "trade_gap_crosscheck.csv": crosscheck_rows,
        "order_evidence_coverage.csv": [*order_coverage_rows, *coverage_rows],
        "plan_invalid_transitions.csv": pause_rows,
        "pause_episodes.csv": pause_episodes,
        "plan_oscillation.csv": oscillation_rows,
        "risk_root_causes.csv": risk_rows,
    }
    default_fields = {
        "fill_contract_audit.csv": "status",
        "trade_pipeline_audit.csv": "timestamp",
        "trade_gap_crosscheck.csv": "timestamp",
        "order_evidence_coverage.csv": "record_type",
        "plan_invalid_transitions.csv": "timestamp",
        "pause_episodes.csv": "episode_id",
        "plan_oscillation.csv": "trading_pair",
        "risk_root_causes.csv": "category",
    }
    for name, rows in artifact_rows.items():
        fields = sorted({key for row in rows for key in row} or {default_fields[name]})
        _csv(root / name, rows, fields)
    (root / "diagnostic_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = [
        "# Stage 12E root-cause diagnostic",
        "",
        "PUBLIC DERIVE MAINNET DATA / SHADOW ORDERS / NO REAL FUNDS AT RISK",
        "",
        f"- Session: `{session_id}`",
        f"- Safety: **{summary['safety']['status']}**",
        f"- Fill contract: **{summary['fill_contract']['status']}**",
        f"- Trade pipeline: **{summary['trade_pipeline']['status']}**",
        f"- Trade pipeline classification: **{summary['trade_pipeline']['classification']}**",
        f"- Conservative ShadowFill events: **{conservative_actual_fills}**",
        f"- Conservative `TRADED_THROUGH_FILLED` statuses: **{conservative_filled_statuses}**",
        f"- Current `TRADE_THROUGH_OBSERVED_NO_FILL` rows: **{current_observed_no_fill}**",
        f"- Legacy contradictory rows reconciled without fabricated fills: **{legacy_reconciled}**",
        f"- Plan-valid to invalid transitions: **{summary['pause']['valid_to_invalid']}**",
        f"- Pause episodes: **{summary['pause']['pause_episodes']}**",
        f"- Risk root-cause rows: **{len(risk_rows)}**",
        "- Independent REST cross-check mismatches: "
        f"**{pipeline_mismatches}/{len(crosscheck_rows)}**",
        "- Public-trade stream suspect frames: "
        f"**{summary['trade_pipeline']['stream_suspect_frames']}**",
        "- Conservative order-evidence coverage: "
        f"median **{order_coverage_summary['median_coverage_pct']}%**, "
        f"P25 **{order_coverage_summary['p25_coverage_pct']}%**, "
        f"P75 **{order_coverage_summary['p75_coverage_pct']}%**, "
        f"P90 **{order_coverage_summary['p90_coverage_pct']}%**",
        "",
        "## Interpretation",
        "",
        "A qualifying public trade and an actual conservative paper fill are separate events. "
        "Only the latter is counted as `TRADED_THROUGH_FILLED`; the former is retained as "
        "`TRADE_THROUGH_OBSERVED_NO_FILL` with its bounded order-window reason.",
        "",
        "Missing or unhealthy public-trade coverage remains `INSUFFICIENT_TRADE_EVIDENCE`. "
        "BBO touch is not promoted to a conservative fill.",
        "",
        "No strategy parameters were changed. No exchange mutation path is enabled.",
        "",
        "Required artifacts are in `reports/stage12e/`.",
        "",
        "READINESS: **NOT READY FOR OPTIMIZATION**",
    ]
    report_text = "\n".join(report) + "\n"
    root_report = root.parent / "stage12e_root_cause.md"
    root_report.write_text(report_text, encoding="utf-8")
    (root / "stage12e_root_cause.md").write_text(report_text, encoding="utf-8")
    report_root = Path(config.get("report_root", "reports/shadow_baseline")).expanduser()
    if not report_root.is_absolute():
        report_root = Path(project_root).expanduser().resolve() / report_root
    session_root = report_root / session_id / "stage12e"
    session_root.mkdir(parents=True, exist_ok=True)
    for name in (
        "fill_contract_audit.csv",
        "trade_pipeline_audit.csv",
        "trade_gap_crosscheck.csv",
        "order_evidence_coverage.csv",
        "plan_invalid_transitions.csv",
        "pause_episodes.csv",
        "plan_oscillation.csv",
        "risk_root_causes.csv",
        "diagnostic_summary.json",
        "stage12e_root_cause.md",
    ):
        source = root / name
        (session_root / name).write_bytes(source.read_bytes())
    return summary


__all__ = [
    "FILL_ELIGIBILITY_STATUSES",
    "HEALTHY_COLLECTION_STATUSES",
    "INSUFFICIENT_TRADE_EVIDENCE",
    "NEVER_REACHED_PRICE",
    "PUBLIC_TRADE_STREAM_SUSPECT",
    "TIMESTAMP_UNITS",
    "TOUCHED_FILLED",
    "TOUCHED_NOT_TRADED_THROUGH",
    "TRADE_THROUGH_FILLED",
    "TRADE_THROUGH_OBSERVED_NO_FILL",
    "build_pause_episodes",
    "build_plan_invalid_rows",
    "build_plan_oscillation",
    "build_risk_root_causes",
    "build_trade_coverage",
    "build_trade_stream_diagnostics",
    "canonical_trade_rows",
    "classify_fill_contract",
    "legacy_fill_reconciliation",
    "normalize_timestamp",
    "order_evidence_coverage",
    "summarize_order_evidence_coverage",
    "timestamp_unit",
    "write_stage12e_artifacts",
]
