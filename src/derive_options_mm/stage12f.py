"""Stage 12F public-trade repair and evidence-quality diagnostics.

Stage 12F is deliberately downstream of the strategy.  It repairs public
trade collection boundaries, reconciles REST and WebSocket evidence, and
measures the quality of the data used by the frozen shadow fill model.  It
does not change prices, sizes, modes, risk limits, or execution behavior.
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

from .stage12e import (
    HEALTHY_COLLECTION_STATUSES,
    PUBLIC_TRADE_STREAM_SUSPECT,
    TRADE_THROUGH_FILLED,
    TRADE_THROUGH_OBSERVED_NO_FILL,
    _get,
    _number,
    build_plan_invalid_rows,
    build_trade_stream_diagnostics,
    classify_fill_contract,
    order_evidence_coverage,
)

MISMATCH_REASONS = (
    "PRIMARY_MISSING_TRADE",
    "REST_MISSING_TRADE",
    "TIMESTAMP_WINDOW_BOUNDARY",
    "PAGINATION_LOSS",
    "DEDUPE_COLLISION",
    "SYMBOL_MAPPING",
    "TIMESTAMP_UNIT_ERROR",
    "OUT_OF_ORDER_EVENT",
    "DELAYED_EVENT",
    "RECONNECT_GAP",
    "RATE_LIMIT_GAP",
    "TRADE_ID_PARSE_ERROR",
    "OTHER_EXPLICIT",
    "UNKNOWN_INTERNAL",
)

ZERO_LIFETIME_REASONS = (
    "NEVER_RESTED",
    "CREATED_AND_REMOVED_SAME_FRAME",
    "SESSION_ARTIFACT",
    "RECONCILIATION_ARTIFACT",
    "PLAN_SUPPRESSION_BEFORE_RESTING",
    "TIMESTAMP_DEFECT",
    "IMMEDIATE_SAFETY_CANCEL",
    "SUB_INTERVAL_LIFETIME",
    "OTHER_EXPLICIT",
    "UNKNOWN_INTERNAL",
)

DATA_PAUSE_CATEGORIES = {
    "DATA_VALIDITY",
    "STATE_CONFIDENCE",
    "MARKET_SAFETY",
    "GLOBAL_IV_DATA",
    "RELATIONSHIP_NOT_VALID",
    "STARTUP_WARMUP",
}
STRATEGY_PAUSE_CATEGORIES = {
    "MIN_EXCHANGE_SIZE",
    "ASSET_RISK",
    "ASSET_INVENTORY_RISK",
    "PORTFOLIO_RISK",
    "GRID_VALIDATION",
    "NO_LEVELS",
    "MODE_PAUSE",
    "EXECUTION_GATE",
    "PLAN_VALIDATION",
    "STRATEGY_REGIME",
}


def _float(value: Any, default: float | None = None) -> float | None:
    number = _number(value)
    return default if number is None else number


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


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


def _trade_record(value: Any) -> dict[str, Any]:
    timestamp = _float(_get(value, "timestamp"))
    return {
        "trade_id": str(_get(value, "trade_id")) if _get(value, "trade_id") else None,
        "timestamp": timestamp,
        "price": _float(_get(value, "price", _get(value, "trade_price"))),
        "amount": _float(_get(value, "amount", _get(value, "trade_amount"))),
        "aggressor_side": str(
            _get(value, "aggressor_side", _get(value, "direction", "")) or ""
        ).lower()
        or None,
        "instrument_name": _get(value, "instrument_name"),
    }


def _trade_key(trade: Mapping[str, Any]) -> tuple[Any, ...]:
    if trade.get("trade_id"):
        return ("id", str(trade["trade_id"]))
    return (
        "row",
        trade.get("timestamp"),
        trade.get("price"),
        trade.get("amount"),
        trade.get("aggressor_side"),
    )


def _trade_attributes_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_timestamp = _float(left.get("timestamp"))
    right_timestamp = _float(right.get("timestamp"))
    if left_timestamp is None or right_timestamp is None:
        return False
    return (
        abs(left_timestamp - right_timestamp) <= 0.001
        and abs((_float(left.get("price")) or 0.0) - (_float(right.get("price")) or 0.0))
        <= 1e-9
        and abs((_float(left.get("amount")) or 0.0) - (_float(right.get("amount")) or 0.0))
        <= 1e-9
        and str(left.get("aggressor_side") or "") == str(right.get("aggressor_side") or "")
    )


def _discrete_percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, int((len(values) - 1) * percent / 100.0))
    return values[index]


def _mismatch_reason(
    *,
    missing_from_primary: bool,
    trade: Mapping[str, Any],
    metadata: Mapping[str, Any],
    window_start: float | None,
    window_end: float | None,
) -> str:
    timestamp = _float(trade.get("timestamp"))
    boundary = 1.0
    if timestamp is not None and (
        (window_start is not None and abs(timestamp - window_start) <= boundary)
        or (window_end is not None and abs(timestamp - window_end) <= boundary)
    ):
        return "TIMESTAMP_WINDOW_BOUNDARY"
    if str(metadata.get("rate_limit_status", "")).upper() in {"RATE_LIMITED", "429"}:
        return "RATE_LIMIT_GAP"
    page_size = _float(metadata.get("page_size"))
    raw_count = _float(metadata.get("raw_count"))
    if page_size and raw_count and raw_count >= page_size:
        return "PAGINATION_LOSS"
    if _float(metadata.get("reconnect_count"), 0.0) and missing_from_primary:
        return "RECONNECT_GAP"
    if str(metadata.get("timestamp_unit", "")).lower() not in {
        "milliseconds",
        "seconds",
        "microseconds",
        "nanoseconds",
        "iso8601",
        "",
        "none",
    }:
        return "TIMESTAMP_UNIT_ERROR"
    return "PRIMARY_MISSING_TRADE" if missing_from_primary else "REST_MISSING_TRADE"


def reconcile_trade_sets(
    primary: Sequence[Any],
    reference: Sequence[Any],
    *,
    metadata: Mapping[str, Any] | None = None,
    window_start: float | None = None,
    window_end: float | None = None,
) -> dict[str, Any]:
    """Compare two public trade sets without over-deduplicating no-ID rows."""

    context = dict(metadata or {})
    primary_rows = [_trade_record(row) for row in primary]
    reference_rows = [_trade_record(row) for row in reference]
    primary_by_key = {_trade_key(row): row for row in primary_rows}
    reference_by_key = {_trade_key(row): row for row in reference_rows}
    primary_ids = {key for key in primary_by_key if key[0] == "id"}
    reference_ids = {key for key in reference_by_key if key[0] == "id"}
    matched_keys = primary_ids & reference_ids
    mismatches: list[dict[str, Any]] = []
    for key in sorted(matched_keys):
        left, right = primary_by_key[key], reference_by_key[key]
        if not _trade_attributes_match(left, right):
            timestamp_difference = abs(
                (_float(left.get("timestamp")) or 0.0)
                - (_float(right.get("timestamp")) or 0.0)
            )
            reason = (
                "TIMESTAMP_UNIT_ERROR"
                if timestamp_difference > 100.0
                else "TRADE_ID_PARSE_ERROR"
            )
            mismatches.append(
                {
                    "trade_id": left.get("trade_id") or right.get("trade_id"),
                    "presence": "BOTH_ATTRIBUTE_MISMATCH",
                    "reason": reason,
                    "primary_timestamp": left.get("timestamp"),
                    "reference_timestamp": right.get("timestamp"),
                    "primary_price": left.get("price"),
                    "reference_price": right.get("price"),
                    "primary_amount": left.get("amount"),
                    "reference_amount": right.get("amount"),
                    "primary_side": left.get("aggressor_side"),
                    "reference_side": right.get("aggressor_side"),
                }
            )
    for key in sorted(
        key for key in reference_by_key.keys() - primary_by_key.keys() if key[0] == "id"
    ):
        trade = reference_by_key[key]
        mismatches.append(
            {
                "trade_id": trade.get("trade_id"),
                "presence": "REFERENCE_ONLY",
                "reason": _mismatch_reason(
                    missing_from_primary=True,
                    trade=trade,
                    metadata=context,
                    window_start=window_start,
                    window_end=window_end,
                ),
                "reference_timestamp": trade.get("timestamp"),
                "reference_price": trade.get("price"),
                "reference_amount": trade.get("amount"),
                "reference_side": trade.get("aggressor_side"),
            }
        )
    for key in sorted(
        key for key in primary_by_key.keys() - reference_by_key.keys() if key[0] == "id"
    ):
        trade = primary_by_key[key]
        mismatches.append(
            {
                "trade_id": trade.get("trade_id"),
                "presence": "PRIMARY_ONLY",
                "reason": _mismatch_reason(
                    missing_from_primary=False,
                    trade=trade,
                    metadata=context,
                    window_start=window_start,
                    window_end=window_end,
                ),
                "primary_timestamp": trade.get("timestamp"),
                "primary_price": trade.get("price"),
                "primary_amount": trade.get("amount"),
                "primary_side": trade.get("aggressor_side"),
            }
        )
    # No-ID rows are intentionally matched on the strongest available tuple;
    # same timestamp/price rows with different size or side remain distinct.
    no_id_primary = {key for key in primary_by_key if key[0] == "row"}
    no_id_reference = {key for key in reference_by_key if key[0] == "row"}
    matched_no_id = no_id_primary & no_id_reference
    for key in sorted(no_id_reference - no_id_primary):
        trade = reference_by_key[key]
        mismatches.append(
            {
                "trade_id": None,
                "presence": "REFERENCE_ONLY",
                "reason": "DEDUPE_COLLISION",
                "reference_timestamp": trade.get("timestamp"),
                "reference_price": trade.get("price"),
                "reference_amount": trade.get("amount"),
                "reference_side": trade.get("aggressor_side"),
            }
        )
    for key in sorted(no_id_primary - no_id_reference):
        trade = primary_by_key[key]
        mismatches.append(
            {
                "trade_id": None,
                "presence": "PRIMARY_ONLY",
                "reason": "DEDUPE_COLLISION",
                "primary_timestamp": trade.get("timestamp"),
                "primary_price": trade.get("price"),
                "primary_amount": trade.get("amount"),
                "primary_side": trade.get("aggressor_side"),
            }
        )
    matched = len(matched_keys) + len(matched_no_id)
    reference_count = len(reference_by_key)
    return {
        "primary_count": len(primary_by_key),
        "reference_count": reference_count,
        "matched_count": matched,
        "primary_missing_count": sum(row["presence"] == "REFERENCE_ONLY" for row in mismatches),
        "reference_missing_count": sum(row["presence"] == "PRIMARY_ONLY" for row in mismatches),
        "attribute_mismatch_count": sum(
            row["presence"] == "BOTH_ATTRIBUTE_MISMATCH" for row in mismatches
        ),
        "completeness_pct": matched / reference_count * 100.0 if reference_count else None,
        "mismatches": mismatches,
    }


def build_trade_crosscheck_rows(
    frames: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build frame-level cross-check rows and one row per persisted mismatch."""

    crosschecks: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for frame in frames:
        raw_status = _get(frame, "trade_crosscheck_raw_status") or _get(
            frame, "trade_crosscheck_status"
        )
        if not raw_status:
            continue
        pair = str(_get(frame, "trading_pair", ""))
        raw_collector = _get(
            frame,
            "trade_crosscheck_raw_collector_count",
            _get(frame, "trade_crosscheck_collector_count"),
        )
        raw_reference = _get(
            frame,
            "trade_crosscheck_raw_rest_count",
            _get(frame, "trade_crosscheck_rest_count"),
        )
        raw_primary_missing = _get(
            frame,
            "trade_crosscheck_raw_missing_from_collector",
            _get(frame, "trade_crosscheck_missing_from_collector"),
        )
        raw_reference_missing = _get(
            frame,
            "trade_crosscheck_raw_extra_in_collector",
            _get(frame, "trade_crosscheck_extra_in_collector"),
        )
        attribute_mismatch_count = _get(
            frame, "trade_crosscheck_attribute_mismatch_count", 0
        )
        matched = _get(frame, "trade_crosscheck_matched_count")
        if matched is None and raw_collector is not None and raw_reference_missing is not None:
            matched = max(0, int(raw_collector) - int(raw_reference_missing))
        crosschecks.append(
            {
                "timestamp": _get(frame, "timestamp"),
                "trading_pair": pair,
                "collector_source": _get(frame, "trade_source"),
                "independent_source": "REST_GET_TRADE_HISTORY",
                "raw_status": raw_status,
                "status": _get(frame, "trade_crosscheck_status"),
                "raw_collector_count": raw_collector,
                "raw_reference_count": raw_reference,
                "matched_count": matched,
                "primary_missing_count": raw_primary_missing,
                "reference_missing_count": raw_reference_missing,
                "attribute_mismatch_count": attribute_mismatch_count,
                "repaired": str(_get(frame, "trade_crosscheck_status", "")) == "REPAIRED",
                "timestamp_unit": _get(frame, "trade_timestamp_unit"),
                "window_start": _get(frame, "trade_crosscheck_window_start_epoch"),
                "window_end": _get(frame, "trade_crosscheck_window_end_epoch"),
                "error": _get(frame, "trade_crosscheck_error"),
            }
        )
        missing_ids = _string_set(_get(frame, "trade_crosscheck_missing_ids"))
        extra_ids = _string_set(_get(frame, "trade_crosscheck_extra_ids"))
        metadata = {
            "page_size": _get(frame, "trade_page_size"),
            "raw_count": _get(frame, "trade_raw_count"),
            "reconnect_count": _get(frame, "trade_reconnect_count"),
            "rate_limit_status": _get(frame, "trade_rate_limit_status"),
            "timestamp_unit": _get(frame, "trade_timestamp_unit"),
        }
        for index, trade_id in enumerate(sorted(missing_ids), 1):
            mismatches.append(
                {
                    "timestamp": _get(frame, "timestamp"),
                    "trading_pair": pair,
                    "trade_id": trade_id,
                    "presence": "REFERENCE_ONLY",
                    "reason": _mismatch_reason(
                        missing_from_primary=True,
                        trade={"trade_id": trade_id},
                        metadata=metadata,
                        window_start=_float(_get(frame, "trade_crosscheck_window_start_epoch")),
                        window_end=_float(_get(frame, "trade_crosscheck_window_end_epoch")),
                    ),
                    "repaired": str(_get(frame, "trade_crosscheck_status", "")) == "REPAIRED",
                    "mismatch_ordinal": index,
                }
            )
        for index, trade_id in enumerate(sorted(extra_ids), 1):
            mismatches.append(
                {
                    "timestamp": _get(frame, "timestamp"),
                    "trading_pair": pair,
                    "trade_id": trade_id,
                    "presence": "PRIMARY_ONLY",
                    "reason": _mismatch_reason(
                        missing_from_primary=False,
                        trade={"trade_id": trade_id},
                        metadata=metadata,
                        window_start=_float(_get(frame, "trade_crosscheck_window_start_epoch")),
                        window_end=_float(_get(frame, "trade_crosscheck_window_end_epoch")),
                    ),
                    "repaired": str(_get(frame, "trade_crosscheck_status", "")) == "REPAIRED",
                    "mismatch_ordinal": index,
                }
            )
        attribute_ids = _string_set(
            _get(frame, "trade_crosscheck_attribute_mismatch_ids")
        )
        for index, trade_id in enumerate(sorted(attribute_ids), 1):
            mismatches.append(
                {
                    "timestamp": _get(frame, "timestamp"),
                    "trading_pair": pair,
                    "trade_id": trade_id,
                    "presence": "BOTH_ATTRIBUTE_MISMATCH",
                    "reason": "OTHER_EXPLICIT",
                    "repaired": str(_get(frame, "trade_crosscheck_status", ""))
                    == "REPAIRED",
                    "mismatch_ordinal": index,
                    "detail": (
                        "same stable trade ID had different timestamp, price, amount, or "
                        "aggressor side"
                    ),
                }
            )
        if not missing_ids and not extra_ids and not attribute_ids and raw_status == "MISMATCH":
            total = int(raw_primary_missing or 0) + int(raw_reference_missing or 0)
            for index in range(max(1, total)):
                mismatches.append(
                    {
                        "timestamp": _get(frame, "timestamp"),
                        "trading_pair": pair,
                        "trade_id": None,
                        "presence": "COUNTS_ONLY_LEGACY",
                        "reason": "OTHER_EXPLICIT",
                        "repaired": False,
                        "mismatch_ordinal": index + 1,
                        "detail": "legacy session persisted counts but not trade IDs",
                    }
                )
    return crosschecks, mismatches


def build_trade_collector_audit(
    frames: Sequence[Any], *, start_timestamp: float, end_timestamp: float
) -> list[dict[str, Any]]:
    """Summarize collector architecture and health separately by asset."""

    grouped: dict[str, list[Any]] = defaultdict(list)
    for frame in frames:
        grouped[str(_get(frame, "trading_pair", ""))].append(frame)
    diagnostics = build_trade_stream_diagnostics(frames)
    diagnostics_by_identity = {
        id(frame): diagnostic
        for frame, diagnostic in zip(frames, diagnostics, strict=True)
    }
    rows: list[dict[str, Any]] = []
    for pair, values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda row: _float(_get(row, "timestamp")) or 0.0)
        unique_ids: set[str] = set()
        unique_no_id: set[tuple[Any, ...]] = set()
        healthy_seconds = 0.0
        event_frames = 0
        suspect_frames = 0
        mismatched_frames = 0
        recovered_frames = 0
        unrecovered_frames = 0
        request_gaps = 0
        overlaps: list[float] = []
        reference_count = 0
        matched_count = 0
        primary_missing = 0
        reference_missing = 0
        for frame in ordered:
            sample = _float(_get(frame, "trade_sample_interval_seconds"), 5.0) or 5.0
            if (
                str(_get(frame, "trade_collection_status", "")).upper()
                in HEALTHY_COLLECTION_STATUSES
            ):
                healthy_seconds += sample
            trades = _get(frame, "trades", ()) or ()
            if trades:
                event_frames += 1
            if _get(frame, "trade_crosscheck_raw_status") == "MISMATCH":
                mismatched_frames += 1
                if _get(frame, "trade_crosscheck_status") == "REPAIRED":
                    recovered_frames += 1
                else:
                    unrecovered_frames += 1
            diagnostic = diagnostics_by_identity.get(id(frame), {})
            if (
                diagnostic.get("trade_silence_classification")
                == PUBLIC_TRADE_STREAM_SUSPECT
            ):
                suspect_frames += 1
            for trade in trades:
                record = _trade_record(trade)
                if record.get("trade_id"):
                    unique_ids.add(str(record["trade_id"]))
                else:
                    unique_no_id.add(_trade_key(record))
            if _get(frame, "trade_poll_gap_seconds"):
                request_gaps += 1
            overlap = _float(_get(frame, "trade_request_overlap_seconds"))
            if overlap is not None:
                overlaps.append(overlap)
            raw_reference = _float(
                _get(
                    frame,
                    "trade_crosscheck_raw_rest_count",
                    _get(frame, "trade_crosscheck_rest_count"),
                )
            )
            raw_matched = _float(_get(frame, "trade_crosscheck_matched_count"))
            raw_primary_missing = _float(
                _get(
                    frame,
                    "trade_crosscheck_raw_missing_from_collector",
                    _get(frame, "trade_crosscheck_missing_from_collector"),
                )
            )
            raw_reference_missing = _float(
                _get(
                    frame,
                    "trade_crosscheck_raw_extra_in_collector",
                    _get(frame, "trade_crosscheck_extra_in_collector"),
                )
            )
            if raw_reference is not None:
                reference_count += int(raw_reference)
            if raw_matched is not None:
                matched_count += int(raw_matched)
            if raw_primary_missing is not None:
                primary_missing += int(raw_primary_missing)
            if raw_reference_missing is not None:
                reference_missing += int(raw_reference_missing)
        session_seconds = max(0.0, end_timestamp - start_timestamp)
        collection_pct = (
            min(100.0, healthy_seconds / session_seconds * 100.0)
            if session_seconds
            else None
        )
        completeness = matched_count / reference_count * 100.0 if reference_count else None
        errors = sum(bool(_get(frame, "trade_collection_error")) for frame in ordered)
        classification = (
            "INCOMPLETE"
            if errors or unrecovered_frames
            else "RECOVERED_WITH_SUSPECT_STREAM"
            if recovered_frames and suspect_frames
            else PUBLIC_TRADE_STREAM_SUSPECT
            if suspect_frames
            else "HEALTHY_WITH_SMALL_RECOVERED_GAPS"
            if recovered_frames
            else "PIPELINE_MISMATCH"
            if mismatched_frames
            else "MARKET_GENUINELY_SPARSE"
            if not event_frames
            else "HEALTHY"
        )
        sample = ordered[-1] if ordered else None
        rows.append(
            {
                "record_type": "ASSET",
                "trading_pair": pair,
                "collector_type": (
                    "WEBSOCKET"
                    if _get(sample, "trade_source") == "websocket"
                    else "REST_AUTHORITATIVE"
                    if _get(sample, "trade_source") == "rest_fallback"
                    else _get(sample, "trade_source")
                ),
                "endpoint": _get(sample, "trade_endpoint") or "wss://api.lyra.finance/ws",
                "channel": _get(sample, "trade_channel") or f"trades.perp.{pair.split('-', 1)[0]}",
                "symbol": pair.split("-", 1)[0],
                "poll_interval_seconds": _get(sample, "trade_sample_interval_seconds"),
                "lookback_window_seconds": None,
                "page_size": _get(sample, "trade_page_size"),
                "pagination_observed": any(_get(frame, "trade_page_count", 0) for frame in ordered),
                "timestamp_unit": _get(sample, "trade_timestamp_unit"),
                "sort_order": _get(sample, "trade_sort_order"),
                "dedupe_key": _get(sample, "trade_dedup_key"),
                "reconnect_count": max(
                    (_float(_get(frame, "trade_reconnect_count"), 0.0) or 0.0 for frame in ordered),
                    default=0.0,
                ),
                "rate_limit_status": _get(sample, "trade_rate_limit_status"),
                "request_gap_count": request_gaps,
                "overlap_seconds_min": min(overlaps) if overlaps else None,
                "overlap_seconds_max": max(overlaps) if overlaps else None,
                "stream_health_coverage_pct": collection_pct,
                "primary_trade_count": len(unique_ids) + len(unique_no_id),
                "reference_trade_count": reference_count or None,
                "matched_trade_count": matched_count or None,
                "primary_missing_count": primary_missing or None,
                "reference_missing_count": reference_missing or None,
                "rest_crosscheck_completeness_pct": completeness,
                "suspect_frame_count": suspect_frames,
                "mismatch_frame_count": mismatched_frames,
                "recovered_gap_count": recovered_frames,
                "unrecovered_gap_count": unrecovered_frames,
                "classification": classification,
                "error_count": errors,
            }
        )
    return rows


def build_trade_gap_recovery(frames: Sequence[Any]) -> list[dict[str, Any]]:
    """Persist backfill attempts, poll gaps, and repair outcomes."""

    rows: list[dict[str, Any]] = []
    for frame in frames:
        attempted = bool(_get(frame, "trade_backfill_attempted"))
        poll_gap = _float(_get(frame, "trade_poll_gap_seconds"), 0.0) or 0.0
        recovery = _get(frame, "trade_recovery_status")
        if not attempted and poll_gap <= 0 and recovery not in {
            "REST_AUTHORITATIVE_REPAIR",
            "REST_REPAIR_INCOMPLETE",
        }:
            continue
        rows.append(
            {
                "timestamp": _get(frame, "timestamp"),
                "trading_pair": _get(frame, "trading_pair"),
                "event": "BACKFILL_ATTEMPT" if attempted else "POLL_WINDOW_AUDIT",
                "status": recovery or "BACKFILL_NOT_REQUIRED",
                "window_start": _get(frame, "trade_previous_request_end_epoch"),
                "window_end": _get(frame, "trade_request_window_end_epoch"),
                "overlap_seconds": _get(frame, "trade_request_overlap_seconds"),
                "poll_gap_seconds": poll_gap,
                "trades_found": _get(frame, "trade_backfill_trades_found", 0),
                "complete": _get(frame, "trade_backfill_complete"),
                "error": _get(frame, "trade_backfill_error"),
                "reconnect_count": _get(frame, "trade_reconnect_count"),
            }
        )
    return rows


def _zero_lifetime_reason(order: Any, *, start: float | None, terminal: float | None) -> str:
    lifecycle = str(_get(order, "lifecycle_state", "") or "").upper()
    status = str(_get(order, "status", "") or "").upper()
    category = str(_get(order, "cancel_reason_category", "") or "").upper()
    raw_reason = str(
        _get(order, "cancel_reason_raw", _get(order, "cancel_reason", "")) or ""
    ).upper()
    if start is None or lifecycle == "NEVER_RESTED_REJECTED" or status == "REJECTED":
        return "NEVER_RESTED"
    if terminal is not None and terminal < start:
        return "TIMESTAMP_DEFECT"
    if category in {"WOULD_CROSS_MARKET", "POST_ONLY_SAFETY", "MIN_LIFETIME_SAFETY_OVERRIDE"}:
        return "IMMEDIATE_SAFETY_CANCEL"
    if "PLAN" in category or "PAUSE" in category or "GRIDPLAN" in raw_reason:
        return "PLAN_SUPPRESSION_BEFORE_RESTING"
    if "SESSION" in category or "MANUAL" in category:
        return "SESSION_ARTIFACT"
    if status in {"CANCELLED", "COMPLETE"} and lifecycle == "CANCELLED_AFTER_RESTING":
        return "CREATED_AND_REMOVED_SAME_FRAME"
    return "SUB_INTERVAL_LIFETIME"


def build_order_evidence_quality(
    orders: Sequence[Any],
    frames: Sequence[Any],
    *,
    end_timestamp: float,
    minimum_samples: int = 5,
) -> dict[str, Any]:
    """Measure meaningful resting-order evidence and explain zero lifetimes."""

    rows: list[dict[str, Any]] = []
    zero_rows: list[dict[str, Any]] = []
    measured: list[float] = []
    for order in orders:
        if bool(_get(order, "is_exit", False)):
            continue
        start = _float(_get(order, "resting_start_epoch"))
        terminal = _float(_get(order, "terminal_epoch"))
        if start is None:
            lifetime = 0.0
        else:
            lifetime = max(0.0, (terminal if terminal is not None else end_timestamp) - start)
        coverage = order_evidence_coverage(order, frames, end_timestamp=end_timestamp)
        zero_reason = None if lifetime > 0 else _zero_lifetime_reason(
            order, start=start, terminal=terminal
        )
        row = {
            "record_type": "ORDER",
            "shadow_order_id": _get(order, "shadow_order_id"),
            "trading_pair": _get(order, "trading_pair"),
            "level_id": _get(order, "level_id"),
            "side": _get(order, "side"),
            "status": _get(order, "status"),
            "lifecycle_state": _get(order, "lifecycle_state"),
            "resting_start_timestamp": _get(order, "resting_start_timestamp"),
            "terminal_timestamp": _get(order, "terminal_timestamp"),
            "resting_lifetime_seconds": lifetime,
            "trustworthy_trade_evidence_seconds": (
                coverage.get("covered_seconds") if lifetime > 0 else None
            ),
            "trade_evidence_coverage_pct": (
                coverage.get("coverage_pct") if lifetime > 0 else None
            ),
            "trade_event_observation_seconds": coverage.get("event_observation_seconds")
            if lifetime > 0
            else None,
            "zero_lifetime_reason": zero_reason,
            "zero_lifetime": lifetime <= 0,
            "cancel_reason_category": _get(order, "cancel_reason_category"),
            "cancel_reason_raw": _get(order, "cancel_reason_raw"),
        }
        rows.append(row)
        if lifetime > 0:
            measured.append(float(coverage.get("coverage_pct") or 0.0))
        else:
            zero_rows.append(row)
    measured.sort()

    def percentile(percent: float) -> float | None:
        if len(measured) < minimum_samples:
            return None
        position = (len(measured) - 1) * percent / 100.0
        lower, upper = math.floor(position), math.ceil(position)
        if lower == upper:
            return measured[lower]
        fraction = position - lower
        return measured[lower] + (measured[upper] - measured[lower]) * fraction

    bucket_counts = {
        "ge_95_pct": sum(value >= 95.0 for value in measured),
        "80_to_95_pct": sum(80.0 <= value < 95.0 for value in measured),
        "50_to_80_pct": sum(50.0 <= value < 80.0 for value in measured),
        "lt_50_pct": sum(value < 50.0 for value in measured),
    }
    return {
        "rows": rows,
        "zero_rows": zero_rows,
        "total_orders": len(rows),
        "orders_rested": sum(row["resting_lifetime_seconds"] > 0 for row in rows),
        "zero_lifetime_orders": len(zero_rows),
        "coverage_sample_n": len(measured),
        "minimum_samples": minimum_samples,
        "coverage_health": "PASS" if len(measured) >= minimum_samples else "INSUFFICIENT SAMPLE",
        "median_coverage_pct": percentile(50.0),
        "p25_coverage_pct": percentile(25.0),
        "p75_coverage_pct": percentile(75.0),
        "p90_coverage_pct": percentile(90.0),
        "bucket_counts": bucket_counts,
        "zero_lifetime_counts": dict(
            Counter(row.get("zero_lifetime_reason") for row in zero_rows)
        ),
        "unknown_zero_lifetime_count": sum(
            row.get("zero_lifetime_reason") == "UNKNOWN_INTERNAL" for row in zero_rows
        ),
    }


def build_pause_episodes_stage12f(
    rows: Sequence[Mapping[str, Any]], *, continuity_gap_seconds: float = 15.0
) -> list[dict[str, Any]]:
    """Group asset-scope pauses while retaining level and cancellation facts."""

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("is_paused"):
            grouped[(str(row.get("trading_pair")), str(row.get("reason_category")))].append(row)
    episodes: list[dict[str, Any]] = []
    for (pair, category), values in grouped.items():
        ordered = sorted(values, key=lambda row: _float(row.get("timestamp_epoch"), 0.0) or 0.0)
        current: dict[str, Any] | None = None
        for row in ordered:
            stamp = _float(row.get("timestamp_epoch"), 0.0) or 0.0
            if (
                current is None
                or stamp - float(current["last_timestamp_epoch"]) > continuity_gap_seconds
            ):
                if current is not None:
                    episodes.append(current)
                current = {
                    "episode_id": f"pause-{len(episodes) + 1:05d}",
                    "trading_pair": pair,
                    "pause_scope": "PORTFOLIO" if category == "PORTFOLIO_RISK" else "ASSET",
                    "reason_category": category,
                    "reason": row.get("reason"),
                    "first_timestamp": row.get("timestamp") or _iso(stamp),
                    "first_timestamp_epoch": stamp,
                    "last_timestamp": row.get("timestamp") or _iso(stamp),
                    "last_timestamp_epoch": stamp,
                    "raw_pause_observation_count": 1,
                    "affected_level_ids": set(_string_set(row.get("removed_level_ids"))),
                    "orders_cancelled": int(_float(row.get("stop_count"), 0.0) or 0.0),
                }
            else:
                current["last_timestamp"] = row.get("timestamp") or _iso(stamp)
                current["last_timestamp_epoch"] = stamp
                current["raw_pause_observation_count"] += 1
                current["affected_level_ids"].update(_string_set(row.get("removed_level_ids")))
                current["orders_cancelled"] += int(_float(row.get("stop_count"), 0.0) or 0.0)
        if current is not None:
            episodes.append(current)
    all_rows = list(rows)
    for episode in episodes:
        episode["duration_seconds"] = max(
            0.0,
            float(episode["last_timestamp_epoch"]) - float(episode["first_timestamp_epoch"]),
        )
        affected = sorted(episode.pop("affected_level_ids"))
        episode["affected_level_ids"] = affected
        episode["affected_level_count"] = len(affected)
        later_valid = [
            row
            for row in all_rows
            if str(row.get("trading_pair")) == episode["trading_pair"]
            and bool(row.get("plan_valid"))
            and (_float(row.get("timestamp_epoch"), 0.0) or 0.0)
            > float(episode["last_timestamp_epoch"])
        ]
        returned = sorted(
            set().union(*(set(_string_set(row.get("desired_level_ids"))) for row in later_valid))
            & set(affected)
        )
        episode["returned_level_ids"] = returned
        episode["same_level_returned_afterward"] = bool(returned)
        episode["return_after_seconds"] = (
            min(
                (_float(row.get("timestamp_epoch"), 0.0) or 0.0)
                - float(episode["last_timestamp_epoch"])
                for row in later_valid
                if set(_string_set(row.get("desired_level_ids"))) & set(affected)
            )
            if returned
            else None
        )
        episode["data_driven"] = episode["reason_category"] in DATA_PAUSE_CATEGORIES
        episode["strategy_driven"] = episode["reason_category"] in STRATEGY_PAUSE_CATEGORIES
    return sorted(episodes, key=lambda row: float(row["first_timestamp_epoch"]))


def build_pause_count_reconciliation(
    rows: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]],
    *,
    start_timestamp: float,
    end_timestamp: float,
) -> list[dict[str, Any]]:
    """Reconcile observations, transitions, episodes, and level removals."""

    pairs = sorted({str(row.get("trading_pair")) for row in rows})
    output: list[dict[str, Any]] = []
    for pair in pairs:
        values = [row for row in rows if str(row.get("trading_pair")) == pair]
        pair_episodes = [row for row in episodes if str(row.get("trading_pair")) == pair]
        removed = [
            level
            for row in values
            for level in _string_set(row.get("removed_level_ids"))
        ]
        recreated = [
            level
            for row in values
            for level in _string_set(row.get("added_level_ids"))
        ]
        raw_observations = sum(bool(row.get("is_paused")) for row in values)
        episode_observations = sum(
            int(_float(row.get("raw_pause_observation_count"), 0.0) or 0.0)
            for row in pair_episodes
        )
        durations = sorted(_float(row.get("duration_seconds"), 0.0) or 0.0 for row in pair_episodes)
        output.append(
            {
                "asset": pair,
                "plan_valid_true_to_false": sum(
                    row.get("transition") == "VALID_TO_INVALID" for row in values
                ),
                "plan_valid_false_to_true": sum(
                    row.get("transition") == "INVALID_TO_VALID" for row in values
                ),
                "pause_observations": raw_observations,
                "unique_asset_pause_episodes": len(pair_episodes),
                "portfolio_pause_episodes": sum(
                    row.get("pause_scope") == "PORTFOLIO" for row in pair_episodes
                ),
                "level_removal_events": len(removed),
                "levels_recreated": len(recreated),
                "median_pause_duration_seconds": (
                    durations[len(durations) // 2] if durations else None
                ),
                "pause_observations_from_episodes": episode_observations,
                "count_reconciliation_pass": raw_observations == episode_observations,
                "session_start": _iso(start_timestamp),
                "session_end": _iso(end_timestamp),
                "explanation": (
                    "episode count is lower than observations when consecutive cycles share "
                    "the same asset/cause; valid-to-invalid counts only episode starts; level "
                    "removals can exceed both when one pause removes multiple levels"
                ),
                "data_driven_observations": sum(
                    row.get("is_paused") and row.get("reason_category") in DATA_PAUSE_CATEGORIES
                    for row in values
                ),
                "strategy_driven_observations": sum(
                    row.get("is_paused") and row.get("reason_category") in STRATEGY_PAUSE_CATEGORIES
                    for row in values
                ),
            }
        )
    return output


def build_plan_oscillation_stage12f(
    rows: Sequence[Mapping[str, Any]], *, start_timestamp: float, end_timestamp: float
) -> list[dict[str, Any]]:
    """Measure present -> removed -> present by asset and level."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("trading_pair"))].append(row)
    output: list[dict[str, Any]] = []
    session_hours = max(0.0, end_timestamp - start_timestamp) / 3600.0
    for pair, values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda row: _float(row.get("timestamp_epoch"), 0.0) or 0.0)
        levels = sorted(
            set().union(
                *(set(_string_set(row.get("desired_level_ids"))) for row in ordered)
            )
            or {"__PLAN__"}
        )
        for level in levels:
            previous_present = False
            removed_at: float | None = None
            absence_durations: list[float] = []
            absence_causes: Counter[str] = Counter()
            removal_count = 0
            return_count = 0
            for row in ordered:
                desired = _string_set(row.get("desired_level_ids"))
                present = level in desired if level != "__PLAN__" else bool(desired)
                stamp = _float(row.get("timestamp_epoch"), 0.0) or 0.0
                if previous_present and not present:
                    removal_count += 1
                    removed_at = stamp
                elif not previous_present and present and removed_at is not None:
                    duration = max(0.0, stamp - removed_at)
                    absence_durations.append(duration)
                    return_count += 1
                    absence_causes.update(
                        str(row.get("reason_category"))
                        for row in ordered
                        if removed_at <= (_float(row.get("timestamp_epoch"), 0.0) or 0.0) <= stamp
                        and row.get("reason_category")
                    )
                    removed_at = None
                if not present:
                    category = str(row.get("reason_category"))
                    if category:
                        absence_causes[category] += 1
                previous_present = present
            absence_durations.sort()

            data_count = sum(
                value for key, value in absence_causes.items() if key in DATA_PAUSE_CATEGORIES
            )
            strategy_count = sum(
                value for key, value in absence_causes.items() if key in STRATEGY_PAUSE_CATEGORIES
            )
            classification = (
                "DATA_DRIVEN_PLAN_OSCILLATION"
                if return_count and data_count > strategy_count
                else "STRATEGY_DRIVEN_PLAN_OSCILLATION"
                if return_count and strategy_count > data_count
                else "MIXED"
                if return_count and data_count and strategy_count
                else "NONE"
            )
            output.append(
                {
                    "record_type": "LEVEL",
                    "trading_pair": pair,
                    "level_id": level,
                    "oscillation_count": return_count,
                    "oscillations_per_hour": (
                        return_count / session_hours if session_hours else None
                    ),
                    "level_removal_count": removal_count,
                    "median_absence_seconds": _discrete_percentile(absence_durations, 50.0),
                    "p90_absence_seconds": _discrete_percentile(absence_durations, 90.0),
                    "return_within_5s_pct": (
                        sum(value <= 5.0 for value in absence_durations) / return_count * 100.0
                        if return_count
                        else None
                    ),
                    "return_within_30s_pct": (
                        sum(value <= 30.0 for value in absence_durations) / return_count * 100.0
                        if return_count
                        else None
                    ),
                    "return_within_60s_pct": (
                        sum(value <= 60.0 for value in absence_durations) / return_count * 100.0
                        if return_count
                        else None
                    ),
                    "return_within_5m_pct": (
                        sum(value <= 300.0 for value in absence_durations) / return_count * 100.0
                        if return_count
                        else None
                    ),
                    "data_driven_observations": data_count,
                    "strategy_driven_observations": strategy_count,
                    "dominant_reason": absence_causes.most_common(1)[0][0]
                    if absence_causes
                    else "NONE",
                    "oscillation_classification": classification,
                }
            )
    return output


def build_fill_contract_summary(
    model_metrics: Mapping[str, Any],
    frames: Sequence[Any],
    *,
    end_timestamp: float,
    stage12e_summary: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Create an explicit current/legacy fill contract reconciliation table."""

    rows: list[dict[str, Any]] = []
    conservative = model_metrics.get("CONSERVATIVE")
    current_rows: list[dict[str, Any]] = []
    actual_fills = 0
    if conservative is not None:
        fills = list(getattr(conservative, "fills", []) or [])
        actual_fills = sum(str(_get(fill, "entry_exit", "entry")) == "entry" for fill in fills)
        for order in list(getattr(conservative, "orders", []) or []):
            if bool(_get(order, "is_exit", False)):
                continue
            current_rows.append(
                classify_fill_contract(
                    order,
                    frames,
                    fills,
                    end_timestamp=end_timestamp,
                    model="CONSERVATIVE",
                )
            )
    status_counts = Counter(str(row.get("status")) for row in current_rows)
    summary_fill = (stage12e_summary or {}).get("fill_contract", {})
    for status in sorted(
        set(status_counts) | {TRADE_THROUGH_FILLED, TRADE_THROUGH_OBSERVED_NO_FILL}
    ):
        rows.append(
            {
                "record_type": "STATUS",
                "scope": "CURRENT",
                "model": "CONSERVATIVE",
                "status": status,
                "count": status_counts.get(status, 0),
                "actual_conservative_shadow_fill_events": actual_fills,
                "invariant": status != TRADE_THROUGH_FILLED
                or status_counts.get(status, 0) == actual_fills,
            }
        )
    legacy_no_fill = int(summary_fill.get("legacy_rows_reconciled_no_fill", 0) or 0)
    rows.append(
        {
            "record_type": "LEGACY_RECONCILIATION",
            "scope": "LEGACY",
            "model": "CONSERVATIVE",
            "status": TRADE_THROUGH_OBSERVED_NO_FILL,
            "count": legacy_no_fill,
            "actual_conservative_shadow_fill_events": 0,
            "invariant": True,
            "reason": (
                "legacy raw trade frames were not persisted and no conservative fill events "
                "existed"
            ),
        }
    )
    rows.append(
        {
            "record_type": "INVARIANT",
            "scope": "ALL",
            "model": "CONSERVATIVE",
            "status": "PASS"
            if actual_fills == status_counts.get(TRADE_THROUGH_FILLED, 0)
            else "FAIL",
            "count": status_counts.get(TRADE_THROUGH_FILLED, 0),
            "actual_conservative_shadow_fill_events": actual_fills,
            "invariant": actual_fills == status_counts.get(TRADE_THROUGH_FILLED, 0),
        }
    )
    return rows


def _csv(path: Path, rows: Sequence[Mapping[str, Any]], default_field: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row} or {default_field})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, sort_keys=True)
                    if isinstance(value, (dict, list, tuple, set))
                    else value
                    for key, value in row.items()
                }
            )


def _architecture_markdown(frames: Sequence[Any]) -> str:
    sample = next(iter(frames), None)
    source = _get(sample, "trade_source") if sample is not None else None
    channel = _get(sample, "trade_channel") if sample is not None else None
    return "\n".join(
        [
            "# Stage 12F public trade collector architecture",
            "",
            (
                "This document describes the public-only collector used by the frozen mainnet "
                "shadow diagnostic."
            ),
            "",
            (
                f"- Collector: **{source or 'unknown'}** (WebSocket live path with public REST "
                "recovery/reference)"
            ),
            "- WebSocket endpoint: `wss://api.lyra.finance/ws`",
            f"- Example channel: `{channel or 'trades.perp.<CURRENCY>'}`",
            (
                "- REST reference: `public/get_trade_history` with `currency`, `instrument_name`, "
                "`instrument_type=perp`, `from_timestamp`, `to_timestamp`, `page`, and `page_size`."
            ),
            (
                "- Symbols: strategy `BTC-USDC`/`ETH-USDC`/`SOL-USDC`/`HYPE-USDC` map to Derive "
                "`BTC-PERP`/`ETH-PERP`/`SOL-PERP`/`HYPE-PERP` and `trades.perp.<CURRENCY>`."
            ),
            (
                "- Poll interval: configured snapshot interval; request windows are bounded and "
                "record overlap/gaps."
            ),
            (
                "- REST page size: at most 1000; pagination follows `pagination.num_pages` and "
                "is bounded to 20 pages."
            ),
            (
                "- Timestamp units: Derive public trade timestamps are normalized to epoch seconds "
                "from milliseconds; seconds, microseconds, nanoseconds, and ISO-8601 are tested "
                "explicitly."
            ),
            "- Sort order: canonical ascending timestamp then trade ID.",
            (
                "- Deduplication: stable `trade_id`; no-ID rows use timestamp, price, amount, and "
                "aggressor side without collapsing different sizes/sides."
            ),
            (
                "- Reconnects: WebSocket reconnects are counted; the next live frame requests a "
                "bounded REST overlap/backfill."
            ),
            (
                "- REST authority: checked windows retain raw mismatch counts/IDs, then use "
                "canonical REST rows for the repaired shadow frame."
            ),
            (
                "- Rate limits/errors: retained as explicit metadata; failed backfills are not "
                "called complete and do not reuse stale rows."
            ),
            (
                "- Authentication: no private Derive order/account client is constructed; this "
                "path is read-only and SHADOW-only."
            ),
            "",
        ]
    )


def write_stage12f_artifacts(
    *,
    project_root: str | Path,
    session_id: str,
    config: Mapping[str, Any],
    frames: Sequence[Any],
    model_metrics: Mapping[str, Any],
    cycles_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    start_timestamp: float,
    end_timestamp: float,
    stage12e_summary: Mapping[str, Any] | None = None,
    minimum_coverage_samples: int = 5,
) -> dict[str, Any]:
    """Write all Stage 12F artifacts and return a JSON-safe diagnostic summary."""

    root = Path(project_root).expanduser().resolve() / "reports" / "stage12f"
    root.mkdir(parents=True, exist_ok=True)
    conservative = model_metrics.get("CONSERVATIVE")
    reconciliation = getattr(conservative, "reconciliation_decisions", []) if conservative else []
    plan_rows = build_plan_invalid_rows(
        list(cycles_by_model.get("CONSERVATIVE", ())), frames, reconciliation
    )
    pause_episodes = build_pause_episodes_stage12f(plan_rows)
    pause_reconciliation = build_pause_count_reconciliation(
        plan_rows,
        pause_episodes,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    oscillation_rows = build_plan_oscillation_stage12f(
        plan_rows, start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    orders = list(getattr(conservative, "orders", []) or []) if conservative else []
    order_quality = build_order_evidence_quality(
        orders,
        frames,
        end_timestamp=end_timestamp,
        minimum_samples=minimum_coverage_samples,
    )
    trade_audit = build_trade_collector_audit(
        frames, start_timestamp=start_timestamp, end_timestamp=end_timestamp
    )
    trade_crosscheck, trade_mismatches = build_trade_crosscheck_rows(frames)
    gap_recovery = build_trade_gap_recovery(frames)
    fill_summary = build_fill_contract_summary(
        model_metrics,
        frames,
        end_timestamp=end_timestamp,
        stage12e_summary=stage12e_summary,
    )
    safety_pass = (
        str(config.get("market_environment", "")).lower() == "mainnet"
        and str(config.get("execution_backend", "")).upper() == "SHADOW"
        and str(config.get("execution_mode", "")).upper() == "SHADOW"
        and not bool(config.get("execution_enabled"))
        and not bool(config.get("allow_mainnet_trading"))
    )
    raw_unresolved = sum(
        row.get("raw_status") == "MISMATCH" and not row.get("repaired")
        for row in trade_crosscheck
    )
    recovered = sum(row.get("repaired") is True for row in trade_crosscheck)
    suspect_frames = sum(
        int(row.get("suspect_frame_count", 0) or 0) for row in trade_audit
    )
    pipeline_classification = (
        "PIPELINE_MISMATCH"
        if raw_unresolved
        else "HEALTHY_WITH_SMALL_RECOVERED_GAPS"
        if recovered
        else "MARKET_GENUINELY_SPARSE"
        if not any(row.get("primary_trade_count") for row in trade_audit)
        else "HEALTHY"
    )
    invariant = all(bool(row.get("invariant")) for row in fill_summary)
    pause_count_pass = all(
        bool(row.get("count_reconciliation_pass")) for row in pause_reconciliation
    )
    unknown_zero = int(order_quality["unknown_zero_lifetime_count"])
    readiness_reasons = [
        (
            "Stage 12F is collection and measurement remediation only; strategy optimization "
            "remains blocked"
        ),
    ]
    if raw_unresolved:
        readiness_reasons.append("unresolved primary/reference public-trade mismatches remain")
    if order_quality["coverage_health"] != "PASS":
        readiness_reasons.append("order-evidence coverage sample is below the configured minimum")
    if unknown_zero:
        readiness_reasons.append("zero-lifetime orders still have UNKNOWN_INTERNAL root causes")
    if suspect_frames:
        readiness_reasons.append(
            "public-trade stream suspect intervals require attribution/recovery"
        )
    summary = {
        "stage": "12F",
        "session_id": session_id,
        "generated_at": _iso(end_timestamp),
        "baseline_config_version": config.get("baseline_config_version"),
        "shadow_config_hash": config.get("shadow_config_hash"),
        "strategy_config_hash": config.get("strategy_config_hash"),
        "safety": {
            "status": "PASS" if safety_pass else "FAIL",
            "market_environment": config.get("market_environment"),
            "execution_backend": config.get("execution_backend"),
            "execution_mode": config.get("execution_mode"),
            "execution_enabled": bool(config.get("execution_enabled")),
            "allow_mainnet_trading": bool(config.get("allow_mainnet_trading")),
            "private_order_client_constructed": False,
            "real_exchange_mutation_calls": 0,
        },
        "trade_pipeline": {
            "classification": pipeline_classification,
            "collector_rows": len(trade_audit),
            "primary_trade_count": sum(
                int(row.get("primary_trade_count", 0) or 0) for row in trade_audit
            ),
            "reference_trade_count": sum(
                int(row.get("reference_trade_count", 0) or 0) for row in trade_audit
            ),
            "matched_trade_count": sum(
                int(row.get("matched_trade_count", 0) or 0) for row in trade_audit
            ),
            "primary_missing_count": sum(
                int(row.get("primary_missing_count", 0) or 0) for row in trade_audit
            ),
            "reference_missing_count": sum(
                int(row.get("reference_missing_count", 0) or 0) for row in trade_audit
            ),
            "rest_crosscheck_completeness_pct": (
                sum(int(row.get("matched_trade_count", 0) or 0) for row in trade_audit)
                / sum(int(row.get("reference_trade_count", 0) or 0) for row in trade_audit)
                * 100.0
                if sum(int(row.get("reference_trade_count", 0) or 0) for row in trade_audit)
                else None
            ),
            "stream_health_coverage_pct": (
                sum(
                    float(row.get("stream_health_coverage_pct", 0.0) or 0.0)
                    for row in trade_audit
                )
                / len(trade_audit)
                if trade_audit
                else None
            ),
            "crosscheck_rows": len(trade_crosscheck),
            "mismatch_rows": len(trade_mismatches),
            "unresolved_mismatch_frames": raw_unresolved,
            "recovered_gap_frames": recovered,
            "gap_recovery_rows": len(gap_recovery),
            "suspect_frames": suspect_frames,
            "per_asset": trade_audit,
        },
        "order_evidence": {
            key: value
            for key, value in order_quality.items()
            if key not in {"rows", "zero_rows"}
        },
        "zero_lifetime": {
            "total": order_quality["zero_lifetime_orders"],
            "root_causes": order_quality["zero_lifetime_counts"],
            "unknown_internal": unknown_zero,
        },
        "fill_contract": {
            "status": "PASS" if invariant else "FAIL",
            "conservative_shadow_fill_events": sum(
                int(_get(fill, "entry_exit", "entry") == "entry")
                for fill in (getattr(conservative, "fills", []) or [])
            )
            if conservative
            else 0,
            "trade_through_filled_statuses": sum(
                int(row.get("count", 0) or 0)
                for row in fill_summary
                if row.get("status") == TRADE_THROUGH_FILLED
                and row.get("scope") == "CURRENT"
            ),
            "trade_through_observed_no_fill": sum(
                int(row.get("status") == TRADE_THROUGH_OBSERVED_NO_FILL)
                * int(row.get("count", 0) or 0)
                for row in fill_summary
            ),
            "invariant": "PASS" if invariant else "FAIL",
        },
        "pause": {
            "plan_valid_true_to_false": sum(
                row.get("plan_valid_true_to_false", 0) for row in pause_reconciliation
            ),
            "plan_valid_false_to_true": sum(
                row.get("plan_valid_false_to_true", 0) for row in pause_reconciliation
            ),
            "raw_pause_observations": sum(
                row.get("pause_observations", 0) for row in pause_reconciliation
            ),
            "unique_pause_episodes": len(pause_episodes),
            "level_removal_events": sum(
                row.get("level_removal_events", 0) for row in pause_reconciliation
            ),
            "count_reconciliation": "PASS" if pause_count_pass else "FAIL",
            "data_driven_observations": sum(
                row.get("data_driven_observations", 0) for row in pause_reconciliation
            ),
            "strategy_driven_observations": sum(
                row.get("strategy_driven_observations", 0) for row in pause_reconciliation
            ),
            "unknown_observations": sum(
                row.get("pause_observations", 0)
                - row.get("data_driven_observations", 0)
                - row.get("strategy_driven_observations", 0)
                for row in pause_reconciliation
            ),
        },
        "risk": (stage12e_summary or {}).get("risk", {}),
        "readiness": "READY FOR 24–48H FROZEN BASELINE"
        if safety_pass
        and invariant
        and not raw_unresolved
        and order_quality["coverage_health"] == "PASS"
        and unknown_zero == 0
        and pause_count_pass
        else "NOT READY FOR 24–48H FROZEN BASELINE",
        "readiness_reasons": readiness_reasons,
    }
    artifacts = {
        "trade_collector_audit.csv": (trade_audit, "record_type"),
        "trade_crosscheck.csv": (trade_crosscheck, "timestamp"),
        "trade_mismatch_reasons.csv": (trade_mismatches, "reason"),
        "trade_gap_recovery.csv": (gap_recovery, "event"),
        "order_evidence_coverage.csv": (order_quality["rows"], "record_type"),
        "zero_lifetime_root_causes.csv": (order_quality["zero_rows"], "zero_lifetime_reason"),
        "pause_count_reconciliation.csv": (pause_reconciliation, "asset"),
        "pause_episodes.csv": (pause_episodes, "episode_id"),
        "plan_oscillation.csv": (oscillation_rows, "trading_pair"),
        "fill_contract_summary.csv": (fill_summary, "record_type"),
    }
    for name, (rows, default) in artifacts.items():
        _csv(root / name, rows, default)
    architecture = _architecture_markdown(frames)
    (root / "trade_collector_architecture.md").write_text(architecture, encoding="utf-8")
    (root / "diagnostic_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = "\n".join(
        [
            "# Stage 12F trade pipeline and evidence quality",
            "",
            "PUBLIC DERIVE MAINNET DATA / SHADOW ORDERS / NO REAL FUNDS AT RISK",
            "",
            f"- Session: `{session_id}`",
            f"- Strategy config hash: `{summary['strategy_config_hash']}`",
            f"- Safety: **{summary['safety']['status']}**; real exchange mutations: **0**",
            "- Strategy behavior: **frozen**; no thresholds or allocation parameters changed",
            "",
            "## Public trade pipeline",
            "",
            f"- Classification: **{pipeline_classification}**",
            f"- Primary trades: **{summary['trade_pipeline']['primary_trade_count']}**",
            f"- Reference trades: **{summary['trade_pipeline']['reference_trade_count']}**",
            f"- Matched: **{summary['trade_pipeline']['matched_trade_count']}**",
            f"- Primary missing: **{summary['trade_pipeline']['primary_missing_count']}**",
            f"- Reference missing: **{summary['trade_pipeline']['reference_missing_count']}**",
            (
                "- REST cross-check completeness: "
                f"**{summary['trade_pipeline']['rest_crosscheck_completeness_pct']}%**"
            ),
            (
                "- Stream-health coverage: "
                f"**{summary['trade_pipeline']['stream_health_coverage_pct']}%**"
            ),
            (
                f"- Recovered gap frames: **{recovered}**; unresolved mismatch frames: "
                f"**{raw_unresolved}**"
            ),
            f"- Suspect frames: **{suspect_frames}**",
            "",
            "## Order trade-evidence quality",
            "",
            (
                f"- Total orders: **{order_quality['total_orders']}**; orders that rested: "
                f"**{order_quality['orders_rested']}**"
            ),
            f"- Zero-lifetime orders: **{order_quality['zero_lifetime_orders']}**",
            f"- Coverage sample n: **{order_quality['coverage_sample_n']}**",
            f"- Coverage health: **{order_quality['coverage_health']}**",
            (
                "- Median/P25/P75/P90: "
                f"**{order_quality['median_coverage_pct']} / "
                f"{order_quality['p25_coverage_pct']} / {order_quality['p75_coverage_pct']} / "
                f"{order_quality['p90_coverage_pct']}**"
            ),
            (
                "- Buckets >=95 / 80–95 / 50–80 / <50: "
                f"**{order_quality['bucket_counts']['ge_95_pct']} / "
                f"{order_quality['bucket_counts']['80_to_95_pct']} / "
                f"{order_quality['bucket_counts']['50_to_80_pct']} / "
                f"{order_quality['bucket_counts']['lt_50_pct']}**"
            ),
            "",
            "## Fill contract and pause accounting",
            "",
            f"- Fill invariant: **{summary['fill_contract']['invariant']}**",
            (
                f"- Plan-valid true→false: **{summary['pause']['plan_valid_true_to_false']}**; "
                f"false→true: **{summary['pause']['plan_valid_false_to_true']}**"
            ),
            (
                f"- Raw pause observations: **{summary['pause']['raw_pause_observations']}**; "
                f"unique episodes: **{summary['pause']['unique_pause_episodes']}**"
            ),
            f"- Count reconciliation: **{summary['pause']['count_reconciliation']}**",
            "",
            f"READINESS: **{summary['readiness']}**",
            "",
            *[f"- {reason}" for reason in summary["readiness_reasons"]],
            "",
        ]
    )
    (root / "stage12f_trade_pipeline.md").write_text(report, encoding="utf-8")
    project_reports_root = Path(project_root).expanduser().resolve() / "reports"
    project_reports_root.mkdir(parents=True, exist_ok=True)
    (project_reports_root / "stage12f_trade_pipeline.md").write_text(
        report, encoding="utf-8"
    )
    report_root = Path(config.get("report_root", "reports/shadow_baseline")).expanduser()
    if not report_root.is_absolute():
        report_root = Path(project_root).expanduser().resolve() / report_root
    session_root = report_root / session_id / "stage12f"
    session_root.mkdir(parents=True, exist_ok=True)
    for path in (*root.glob("*.csv"), *root.glob("*.json"), *root.glob("*.md")):
        (session_root / path.name).write_bytes(path.read_bytes())
    return summary


__all__ = [
    "MISMATCH_REASONS",
    "ZERO_LIFETIME_REASONS",
    "build_fill_contract_summary",
    "build_order_evidence_quality",
    "build_pause_count_reconciliation",
    "build_pause_episodes_stage12f",
    "build_plan_oscillation_stage12f",
    "build_trade_collector_audit",
    "build_trade_crosscheck_rows",
    "build_trade_gap_recovery",
    "reconcile_trade_sets",
    "write_stage12f_artifacts",
]
