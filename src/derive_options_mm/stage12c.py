"""Small, dependency-free observability primitives for Stage 12C.

The functions in this module describe execution evidence.  They do not choose
prices, sizes, modes, or risk limits and therefore cannot change Stage 1--4
strategy behaviour.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

CANCEL_TAXONOMY = (
    "PRICE_DEVIATION",
    "AMOUNT_DEVIATION",
    "MODE_CHANGE",
    "PLAN_LEVEL_REMOVED",
    "PLAN_DISABLED",
    "PLAN_STALE",
    "MAX_AGE",
    "MIN_LIFETIME_SAFETY_OVERRIDE",
    "REPLACEMENT_COOLDOWN_OVERRIDE",
    "POST_ONLY_SAFETY",
    "WOULD_CROSS_MARKET",
    "STALE_MARKET_DATA",
    "STALE_GLOBAL_RISK",
    "ACCOUNT_STATE_INVALID",
    "ASSET_INVENTORY_RISK",
    "PORTFOLIO_GROSS_RISK",
    "PORTFOLIO_BETA_RISK",
    "DRAWDOWN_RISK",
    "PAUSE",
    "CONFIG_CHANGE",
    "SESSION_SHUTDOWN",
    "MANUAL_STOP",
    "EXECUTOR_STATE_CHANGE",
    "FILL_TRANSITION",
    "UNKNOWN_INTERNAL",
)

LIFECYCLE_STATES = (
    "CREATED",
    "VALIDATED",
    "RESTING",
    "NEVER_RESTED_REJECTED",
    "CANCELLED_AFTER_RESTING",
    "FILLED_AFTER_RESTING",
)

LIFECYCLE_EVENTS = (
    "ORDER_CREATED",
    "ORDER_RESTING",
    "ORDER_KEEP",
    "ORDER_REPLACE_DEFERRED",
    "ORDER_CANCEL_REQUESTED",
    "ORDER_CANCELLED",
    "ORDER_FILLED",
    "ORDER_TP_CREATED",
    "ORDER_COMPLETE",
)

FILL_ELIGIBILITY_STATUSES = (
    "TRADED_THROUGH_FILLED",
    "TRADE_THROUGH_OBSERVED_NO_FILL",
    "TOUCHED_FILLED",
    "TOUCHED_NOT_TRADED_THROUGH",
    "NEVER_REACHED_PRICE",
    "INSUFFICIENT_TRADE_EVIDENCE",
)

RESTING_LIFETIME_BUCKETS = (
    "<5s",
    "5-30s",
    "30-60s",
    "1-2m",
    "2-5m",
    "5-15m",
    ">15m",
)

REPLACEMENT_DEVIATION_BUCKETS = (
    "<2bps",
    "2-5bps",
    "5-8bps",
    "8-12bps",
    "12-20bps",
    "20+bps",
    "UNKNOWN",
)


def _text(value: Any) -> str:
    return str(value or "").strip().upper()


def classify_cancel_reason(
    reason: Any,
    *,
    reason_code: Any = "",
    context: dict[str, Any] | None = None,
) -> str:
    """Return one explicit Stage 12C category for a cancel decision.

    ``context`` is intentionally inspected before broad text matching.  This
    lets a caller distinguish a crossed maker quote from a merely stale quote,
    and a removed plan level from a plan that became invalid.
    """

    details = context or {}
    raw = f"{_text(reason_code)} {_text(reason)}".strip()
    if details.get("safety_override"):
        override = _text(details.get("safety_override_reason"))
        if "COOLDOWN" in override:
            return "REPLACEMENT_COOLDOWN_OVERRIDE"
        if "LIFETIME" in override or "AGE" in override:
            return "MIN_LIFETIME_SAFETY_OVERRIDE"
    if "SESSION_SHUTDOWN" in raw or "SHUTDOWN" in raw or "INTERRUPT" in raw:
        return "SESSION_SHUTDOWN"
    if "MANUAL" in raw or "KILL_SWITCH" in raw or "MANUAL_KILL" in raw:
        return "MANUAL_STOP"
    if "CONFIG" in raw or details.get("config_changed"):
        return "CONFIG_CHANGE"
    if "FILL_TRANSITION" in raw or details.get("fill_transition"):
        return "FILL_TRANSITION"
    if "EXECUTOR" in raw or "DUPLICATE" in raw or details.get("executor_state_change"):
        return "EXECUTOR_STATE_CHANGE"
    if details.get("would_cross_market") or "WOULD_CROSS_MARKET" in raw:
        return "WOULD_CROSS_MARKET"
    if "MAKER_SAFETY" in raw or "POST_ONLY" in raw:
        return "POST_ONLY_SAFETY"
    if details.get("plan_level_present") is False or "LEVEL NO LONGER" in raw:
        return "PLAN_LEVEL_REMOVED"
    if details.get("plan_enabled") is False or "PLAN PAUSE" in raw or "DISABLED" in raw:
        return "PLAN_DISABLED"
    if details.get("plan_stale") or "STALE PLAN" in raw or "PLAN STALE" in raw:
        return "PLAN_STALE"
    if details.get("market_stale") or "STALE MARKET" in raw:
        return "STALE_MARKET_DATA"
    if details.get("global_risk_stale") or "STALE GLOBAL" in raw:
        return "STALE_GLOBAL_RISK"
    if details.get("account_state_invalid") or "GRIDPLAN INVALID" in raw:
        return "ACCOUNT_STATE_INVALID"
    if "INVENTORY" in raw or "POSITION" in raw or "ASSET" in raw and "RISK" in raw:
        return "ASSET_INVENTORY_RISK"
    if "BETA" in raw:
        return "PORTFOLIO_BETA_RISK"
    if "PORTFOLIO" in raw or "GROSS" in raw:
        return "PORTFOLIO_GROSS_RISK"
    if "DRAWDOWN" in raw:
        return "DRAWDOWN_RISK"
    if "PAUSE" in raw:
        return "PAUSE"
    if "MAXIMUM" in raw or "MAX_AGE" in raw or "EXPIRED" in raw:
        return "MAX_AGE"
    if "MODE" in raw:
        return "MODE_CHANGE"
    if "AMOUNT" in raw or details.get("amount_deviation_bps") is not None:
        return "AMOUNT_DEVIATION"
    if "PRICE" in raw or "MATERIAL" in raw:
        return "PRICE_DEVIATION"
    return "UNKNOWN_INTERNAL"


def normalize_risk_reason(reason: Any) -> str:
    """Map a raw risk-block message to a stable reporting reason."""

    value = _text(reason)
    if "AMOUNT BELOW" in value or "MINIMUM" in value or "EXCHANGE SIZE" in value:
        return "MIN_EXCHANGE_SIZE"
    if "COLLATERAL" in value or "RESERVE" in value:
        return "COLLATERAL_RESERVE"
    if "DRAWDOWN" in value:
        return "DRAWDOWN_RISK"
    if "BETA" in value or "CORRELAT" in value:
        return "PORTFOLIO_BETA_RISK"
    if "GROSS" in value or "TOTAL POSITION" in value or "PORTFOLIO" in value:
        return "PORTFOLIO_GROSS_RISK"
    if "PER-ASSET" in value or "ASSET" in value or "INVENTORY" in value:
        return "ASSET_INVENTORY_RISK"
    if "ACTIVE EXECUTOR" in value or "CAPACITY" in value or "LEVEL CAP" in value:
        return "EXECUTOR_CAPACITY"
    if "STALE" in value:
        return "STALE_DATA"
    if "PAUSE" in value or "INVALID" in value:
        return "PLAN_SUPPRESSION"
    return "OTHER"


def resting_lifetime_bucket(value: Any) -> str | None:
    """Return the requested resting-lifetime bucket for a finite duration."""

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    if seconds < 5:
        return "<5s"
    if seconds < 30:
        return "5-30s"
    if seconds < 60:
        return "30-60s"
    if seconds < 120:
        return "1-2m"
    if seconds < 300:
        return "2-5m"
    if seconds <= 900:
        return "5-15m"
    return ">15m"


def replacement_deviation_bucket(value: Any) -> str:
    """Return the exact operational replacement-deviation bucket."""

    try:
        bps = float(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if not math.isfinite(bps) or bps < 0:
        return "UNKNOWN"
    if bps < 2:
        return "<2bps"
    if bps < 5:
        return "2-5bps"
    if bps < 8:
        return "5-8bps"
    if bps < 12:
        return "8-12bps"
    if bps <= 20:
        return "12-20bps"
    return "20+bps"


@dataclass
class RiskEpisodeTracker:
    """Deduplicate repeated risk blocks into continuous episodes."""

    continuity_gap_seconds: float = 15.0
    raw_blocks_total: int = 0
    risk_checks_total: int = 0
    _episodes: list[dict[str, Any]] = field(default_factory=list)
    _active: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record_check(self, count: int = 1) -> None:
        self.risk_checks_total += max(0, int(count))

    def record(
        self,
        timestamp: float,
        *,
        reason: Any,
        trading_pair: str | None = None,
        level_id: str | None = None,
        side: str | None = None,
        assets: list[str] | tuple[str, ...] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = float(timestamp)
        category = normalize_risk_reason(reason)
        pair = str(trading_pair or "UNKNOWN")
        level = str(level_id or "UNKNOWN")
        side_value = str(side or "UNKNOWN")
        # The episode key is based on normalized identity, not the raw message.
        # Different diagnostic wording for the same candidate must not create
        # a second episode.
        key = "|".join((pair, level, side_value, category))
        active = self._active.get(key)
        if (
            active is None
            or timestamp - float(active["last_timestamp_epoch"]) > self.continuity_gap_seconds
        ):
            if active is not None:
                self._episodes.append(active)
            episode = {
                "episode_id": f"risk-episode-{uuid.uuid4().hex[:12]}",
                "episode_key": key,
                "reason": category,
                "raw_reason": str(reason or ""),
                "trading_pair": trading_pair,
                "level_id": level_id,
                "side": side,
                "first_timestamp_epoch": timestamp,
                "last_timestamp_epoch": timestamp,
                "raw_block_count": 1,
                "blocked_seconds": 0.0,
                "assets": sorted(set(assets or ([pair] if pair != "UNKNOWN" else []))),
                "candidate_trace": dict(context or {}),
            }
            self._active[key] = episode
        else:
            active["last_timestamp_epoch"] = timestamp
            active["raw_block_count"] = int(active["raw_block_count"]) + 1
            active["blocked_seconds"] = max(0.0, timestamp - float(active["first_timestamp_epoch"]))
            active["assets"] = sorted(set(active.get("assets", [])) | set(assets or []))
            if context:
                active["candidate_trace"] = dict(context)
            episode = active
        self.raw_blocks_total += 1
        return episode

    def rows(self, end_timestamp: float | None = None) -> list[dict[str, Any]]:
        end = float(end_timestamp) if end_timestamp is not None else None
        rows = [dict(row) for row in self._episodes]
        rows.extend(dict(row) for row in self._active.values())
        active_episode_ids = {str(row["episode_id"]) for row in self._active.values()}
        for row in rows:
            last = (
                end
                if end is not None and str(row["episode_id"]) in active_episode_ids
                else float(row["last_timestamp_epoch"])
            )
            row["blocked_seconds"] = max(0.0, last - float(row["first_timestamp_epoch"]))
            row["first_timestamp"] = row.pop(
                "first_timestamp", _iso_or_number(row["first_timestamp_epoch"])
            )
            row["last_timestamp"] = row.pop(
                "last_timestamp", _iso_or_number(row["last_timestamp_epoch"])
            )
        return sorted(rows, key=lambda row: float(row["first_timestamp_epoch"]))

    def summary(self, end_timestamp: float | None = None) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"raw_blocks": 0, "unique_episodes": 0, "blocked_seconds": 0.0, "assets": set()}
        )
        for row in self.rows(end_timestamp):
            bucket = grouped[str(row.get("reason", "OTHER"))]
            bucket["raw_blocks"] += int(row.get("raw_block_count", 0) or 0)
            bucket["unique_episodes"] += 1
            bucket["blocked_seconds"] += float(row.get("blocked_seconds", 0.0) or 0.0)
            bucket["assets"].update(row.get("assets") or [])
        return [
            {
                "reason": reason,
                "raw_blocks": values["raw_blocks"],
                "unique_episodes": values["unique_episodes"],
                "blocked_seconds": values["blocked_seconds"],
                "assets": sorted(values["assets"]),
            }
            for reason, values in sorted(grouped.items())
        ]


def _iso_or_number(value: float) -> float:
    # Keep this helper numeric so the module remains independent of the shadow
    # module's timestamp utilities.  Report callers add their ISO rendering.
    return value


def calculate_trade_coverage(
    timestamps: list[float] | tuple[float, ...],
    *,
    start_timestamp: float,
    end_timestamp: float,
    sample_interval_seconds: float = 5.0,
    trade_count: int | None = None,
    evidence_minutes: int | None = None,
) -> dict[str, Any]:
    """Calculate duration/gap coverage from deduplicated public-trade times.

    Each evidence point covers one expected polling interval.  This is a
    conservative availability measure, not a claim that every trade inside an
    interval was observed.
    """

    start = float(start_timestamp)
    end = max(start, float(end_timestamp))
    expected_duration = end - start
    interval = max(0.001, float(sample_interval_seconds))
    points = sorted(
        {
            point
            for point in timestamps
            if (
                isinstance(point, (int, float))
                and math.isfinite(float(point))
                and start <= point <= end
            )
        }
    )
    if not points:
        return {
            "expected_duration_seconds": expected_duration,
            "covered_duration_seconds": 0.0,
            "coverage_pct": 0.0 if expected_duration else None,
            "trade_count": int(trade_count or 0),
            "evidence_minutes": int(evidence_minutes or 0),
            "no_evidence_minutes": math.ceil(expected_duration / 60.0) if expected_duration else 0,
            "gap_count": 1 if expected_duration else 0,
            "max_gap_seconds": expected_duration if expected_duration else 0.0,
            "median_gap_seconds": expected_duration if expected_duration else 0.0,
            "p95_gap_seconds": expected_duration if expected_duration else 0.0,
        }
    windows: list[tuple[float, float]] = []
    for point in points:
        windows.append((max(start, point), min(end, point + interval)))
    merged: list[list[float]] = []
    for left, right in windows:
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    covered = sum(right - left for left, right in merged)
    gaps: list[float] = []
    cursor = start
    for left, right in merged:
        if left > cursor:
            gaps.append(left - cursor)
        cursor = max(cursor, right)
    if cursor < end:
        gaps.append(end - cursor)
    return {
        "expected_duration_seconds": expected_duration,
        "covered_duration_seconds": covered,
        "coverage_pct": covered / expected_duration * 100.0 if expected_duration else None,
        "trade_count": int(trade_count if trade_count is not None else len(points)),
        "evidence_minutes": int(
            evidence_minutes
            if evidence_minutes is not None
            else len({int(point // 60) for point in points})
        ),
        "no_evidence_minutes": math.ceil(max(0.0, expected_duration - covered) / 60.0),
        "gap_count": len(gaps),
        "max_gap_seconds": max(gaps, default=0.0),
        "median_gap_seconds": _percentile(gaps, 0.50),
        "p95_gap_seconds": _percentile(gaps, 0.95),
    }


trade_coverage = calculate_trade_coverage


def _percentile(values: list[float], percentile: float) -> float | None:
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


__all__ = [
    "CANCEL_TAXONOMY",
    "FILL_ELIGIBILITY_STATUSES",
    "LIFECYCLE_EVENTS",
    "LIFECYCLE_STATES",
    "REPLACEMENT_DEVIATION_BUCKETS",
    "RESTING_LIFETIME_BUCKETS",
    "RiskEpisodeTracker",
    "calculate_trade_coverage",
    "classify_cancel_reason",
    "normalize_risk_reason",
    "replacement_deviation_bucket",
    "resting_lifetime_bucket",
    "trade_coverage",
]
