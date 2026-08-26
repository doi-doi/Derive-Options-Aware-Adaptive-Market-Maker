"""Read-only Stage 2 market-state calculations built on Stage 1 snapshots.

The state engine deliberately knows nothing about grids, orders, executors, or
Hummingbot connectors.  It accepts a Stage 1 ``MarketSnapshot`` instance (or
its JSON mapping), keeps a bounded in-memory history, and returns one
explainable ``MarketState`` observation.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

_EPSILON = 1e-12
_ZERO_BASELINE_SCORE = 4.0


class VolatilityState(StrEnum):
    """Explainable volatility regime labels."""

    INITIALIZING = "initializing"
    NORMAL = "normal"
    HIGH = "high"


class DirectionState(StrEnum):
    """Explainable short-term pressure labels."""

    INITIALIZING = "initializing"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class InventoryState(StrEnum):
    """Explainable signed inventory labels."""

    UNKNOWN = "unknown"
    LONG = "long"
    NEUTRAL = "neutral"
    SHORT = "short"


class StateEngineConfig(BaseModel):
    """Small, explicit parameter surface for the read-only state engine."""

    history_window_seconds: float = Field(default=900.0, gt=0)
    minimum_history_samples: int = Field(default=20, ge=2)

    realized_vol_window_seconds: float = Field(default=60.0, gt=0)
    realized_vol_baseline_seconds: float = Field(default=300.0, gt=0)
    realized_vol_weight: float = Field(default=0.75, ge=0)
    iv_weight: float = Field(default=0.25, ge=0)
    iv_history_window_seconds: float = Field(default=900.0, gt=0)
    iv_minimum_samples: int = Field(default=5, ge=1)
    high_vol_enter_threshold: float = Field(default=1.50, gt=0)
    high_vol_exit_threshold: float = Field(default=1.25, gt=0)

    direction_return_window_seconds: float = Field(default=30.0, gt=0)
    direction_price_scale: float = Field(
        default=0.001,
        gt=0,
        description="Log-return magnitude mapped to a price signal of one",
    )
    direction_book_weight: float = Field(default=0.45, ge=0)
    direction_flow_weight: float = Field(default=0.30, ge=0)
    direction_price_weight: float = Field(default=0.25, ge=0)
    bullish_enter_threshold: float = Field(default=0.25, ge=0, le=1)
    bullish_exit_threshold: float = Field(default=0.15, ge=0, le=1)
    bearish_enter_threshold: float = Field(default=-0.25, ge=-1, le=0)
    bearish_exit_threshold: float = Field(default=-0.15, ge=-1, le=0)
    direction_confirmation_samples: int = Field(default=3, ge=1)

    max_position_notional: float = Field(
        default=100_000.0,
        gt=0,
        description="Configured exposure reference used only for diagnostics",
    )
    inventory_neutral_threshold: float = Field(default=0.10, ge=0)

    @model_validator(mode="after")
    def validate_thresholds_and_weights(self) -> StateEngineConfig:
        if self.high_vol_exit_threshold > self.high_vol_enter_threshold:
            raise ValueError("high_vol_exit_threshold must not exceed high_vol_enter_threshold")
        if self.bullish_exit_threshold > self.bullish_enter_threshold:
            raise ValueError("bullish_exit_threshold must not exceed bullish_enter_threshold")
        if self.bearish_exit_threshold < self.bearish_enter_threshold:
            raise ValueError("bearish_exit_threshold must not be below bearish_enter_threshold")
        if (
            self.realized_vol_weight == 0
            and self.iv_weight == 0
        ):
            raise ValueError("at least one volatility weight must be positive")
        if (
            self.direction_book_weight == 0
            and self.direction_flow_weight == 0
            and self.direction_price_weight == 0
        ):
            raise ValueError("at least one direction weight must be positive")
        return self


class MarketState(BaseModel):
    """One normalized, explainable state observation."""

    timestamp: str
    trading_pair: str
    market_environment: str = "testnet"

    volatility_state: VolatilityState
    volatility_score: float | None = None

    direction_state: DirectionState
    direction_score: float | None = None

    inventory_state: InventoryState
    inventory_ratio: float | None = None

    realized_volatility: float | None = None
    realized_volatility_ratio: float | None = None
    atm_iv: float | None = None
    atm_call_iv: float | None = None
    atm_put_iv: float | None = None
    atm_iv_confidence: float = Field(default=0.0, ge=0, le=1)
    iv_ratio: float | None = None
    iv_change: float | None = None
    iv_history_samples: int = Field(default=0, ge=0)
    iv_history_ready: bool = False
    option_expiry: str | None = None
    option_expiry_dte: float | None = None
    atm_strike: float | None = None
    atm_distance_pct: float | None = None
    option_data_age_seconds: float | None = None
    option_data_source: str | None = None
    option_data_errors: list[str] = Field(default_factory=list)

    book_imbalance: float | None = None
    order_flow_imbalance: float | None = None
    short_return: float | None = None
    position_notional: float | None = None

    state_valid: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _SnapshotPoint:
    """Only the Stage 1 fields needed for state calculations."""

    timestamp: str
    timestamp_seconds: float | None
    trading_pair: str
    mid_price: float | None
    book_imbalance: float | None
    order_flow_imbalance: float | None
    atm_iv: float | None
    atm_call_iv: float | None
    atm_put_iv: float | None
    atm_iv_confidence: float
    option_expiry: str | None
    option_expiry_dte: float | None
    atm_strike: float | None
    atm_distance_pct: float | None
    option_data_age_seconds: float | None
    option_data_source: str | None
    option_data_errors: tuple[str, ...]
    current_position: float | None
    position_notional: float | None
    account_data_available: bool
    data_valid: bool
    validation_errors: tuple[str, ...]


def _read(snapshot: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(field_name, default)
    return getattr(snapshot, field_name, default)


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_timestamp(value: Any) -> float | None:
    """Parse Stage 1 ISO timestamps or numeric timestamps into UTC seconds."""

    numeric = _finite_float(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def _iso_utc(seconds: float | None = None) -> str:
    value = datetime.now(UTC).timestamp() if seconds is None else seconds
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _clamp(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def calculate_log_return(previous_mid: float, current_mid: float) -> float:
    """Calculate a finite log return from two positive midpoint prices."""

    previous = _finite_float(previous_mid)
    current = _finite_float(current_mid)
    if previous is None or current is None or previous <= 0 or current <= 0:
        raise ValueError("mid prices must be finite and positive")
    return math.log(current / previous)


def calculate_realized_volatility(log_returns: Sequence[float]) -> float | None:
    """Return RMS log-return volatility without annualization.

    RMS is intentionally used instead of an annualized estimator: Stage 2
    needs a transparent relative short-term measure, not a time-scale claim.
    """

    finite_returns = [value for value in log_returns if math.isfinite(value)]
    if not finite_returns:
        return None
    return math.sqrt(sum(value * value for value in finite_returns) / len(finite_returns))


def calculate_normalized_volatility(
    current_realized_volatility: float | None,
    baseline_realized_volatility: float | None,
    *,
    zero_baseline_score: float = _ZERO_BASELINE_SCORE,
) -> float | None:
    """Compare current volatility with its prior baseline."""

    current = _finite_float(current_realized_volatility)
    baseline = _finite_float(baseline_realized_volatility)
    cap = _finite_float(zero_baseline_score)
    if current is None or baseline is None or current < 0 or baseline < 0:
        return None
    if baseline <= _EPSILON:
        return 1.0 if current <= _EPSILON else (cap or _ZERO_BASELINE_SCORE)
    return current / baseline


def calculate_iv_regime(
    current_atm_iv: float | None,
    historical_atm_iv: Sequence[float],
    *,
    zero_baseline_score: float = _ZERO_BASELINE_SCORE,
) -> float | None:
    """Compare current ATM IV with the recent median, if available."""

    current = _finite_float(current_atm_iv)
    history = sorted(
        value
        for value in (_finite_float(item) for item in historical_atm_iv)
        if value is not None and value > 0
    )
    if current is None or current <= 0 or not history:
        return None
    baseline = statistics.median(history)
    if baseline <= _EPSILON:
        return 1.0 if current <= _EPSILON else (zero_baseline_score or _ZERO_BASELINE_SCORE)
    return current / baseline


def calculate_iv_change(
    current_atm_iv: float | None, previous_atm_iv: float | None
) -> float | None:
    """Return the current minus previous decimal ATM IV when both are valid."""

    current = _finite_float(current_atm_iv)
    previous = _finite_float(previous_atm_iv)
    if current is None or previous is None or current <= 0 or previous <= 0:
        return None
    return current - previous


def calculate_combined_volatility_score(
    normalized_realized_volatility: float | None,
    normalized_iv: float | None,
    *,
    realized_vol_weight: float,
    iv_weight: float,
) -> float | None:
    """Combine available relative volatility signals with weight renormalization."""

    components: list[tuple[float, float]] = []
    realized = _finite_float(normalized_realized_volatility)
    iv = _finite_float(normalized_iv)
    if realized is not None and realized_vol_weight > 0:
        components.append((realized, realized_vol_weight))
    if iv is not None and iv_weight > 0:
        components.append((iv, iv_weight))
    if not components:
        return None
    total_weight = sum(weight for _, weight in components)
    return sum(value * weight for value, weight in components) / total_weight


def classify_volatility(
    volatility_score: float | None,
    current_state: VolatilityState,
    *,
    enter_threshold: float,
    exit_threshold: float,
) -> VolatilityState:
    """Classify volatility with separate enter/exit thresholds."""

    score = _finite_float(volatility_score)
    if score is None:
        return VolatilityState.INITIALIZING
    if current_state is VolatilityState.HIGH:
        return VolatilityState.HIGH if score >= exit_threshold else VolatilityState.NORMAL
    return VolatilityState.HIGH if score >= enter_threshold else VolatilityState.NORMAL


def calculate_price_signal(short_return: float | None, scale: float) -> float | None:
    """Map a log return to a bounded direction component."""

    value = _finite_float(short_return)
    divisor = _finite_float(scale)
    if value is None or divisor is None or divisor <= 0:
        return None
    return _clamp(value / divisor)


def calculate_direction_score(
    book_imbalance: float | None,
    order_flow_imbalance: float | None,
    price_signal: float | None,
    *,
    book_weight: float,
    flow_weight: float,
    price_weight: float,
) -> float | None:
    """Combine available bounded microstructure components and renormalize."""

    book_value = _finite_float(book_imbalance)
    flow_value = _finite_float(order_flow_imbalance)
    price_value = _finite_float(price_signal)
    values = (
        (_clamp(book_value), book_weight) if book_value is not None else None,
        (_clamp(flow_value), flow_weight) if flow_value is not None else None,
        (_clamp(price_value), price_weight) if price_value is not None else None,
    )
    components = [
        component for component in values if component is not None and component[1] > 0
    ]
    if not components:
        return None
    total_weight = sum(weight for _, weight in components)
    return _clamp(sum(value * weight for value, weight in components) / total_weight)


def classify_direction(
    direction_score: float | None,
    *,
    bullish_threshold: float,
    bearish_threshold: float,
) -> DirectionState:
    """Apply stateless direction thresholds to a bounded score."""

    score = _finite_float(direction_score)
    if score is None:
        return DirectionState.INITIALIZING
    if score >= bullish_threshold:
        return DirectionState.BULLISH
    if score <= bearish_threshold:
        return DirectionState.BEARISH
    return DirectionState.NEUTRAL


def calculate_inventory_ratio(
    current_position: float | None,
    position_notional: float | None,
    mid_price: float | None,
    max_position_notional: float,
) -> tuple[float | None, float | None]:
    """Return signed inventory ratio and signed notional.

    Stage 1 stores ``position_notional`` as an absolute notional.  The signed
    Stage 2 value is reconstructed from Stage 1's signed ``current_position``.
    """

    position = _finite_float(current_position)
    maximum = _finite_float(max_position_notional)
    if position is None or maximum is None or maximum <= 0:
        return None, None
    if abs(position) <= _EPSILON:
        return 0.0, 0.0

    notional = _finite_float(position_notional)
    price = _finite_float(mid_price)
    if notional is None:
        if price is None or price <= 0:
            return None, None
        notional = abs(position) * price
    signed_notional = math.copysign(abs(notional), position)
    return signed_notional / maximum, signed_notional


def classify_inventory(
    inventory_ratio: float | None, neutral_threshold: float
) -> InventoryState:
    """Classify signed inventory without treating unavailable data as neutral."""

    ratio = _finite_float(inventory_ratio)
    threshold = _finite_float(neutral_threshold)
    if ratio is None or threshold is None or threshold < 0:
        return InventoryState.UNKNOWN
    if ratio > threshold:
        return InventoryState.LONG
    if ratio < -threshold:
        return InventoryState.SHORT
    return InventoryState.NEUTRAL


def calculate_confidence(
    *,
    snapshot_valid: bool,
    history_ready: bool,
    realized_volatility_available: bool,
    book_available: bool,
    flow_available: bool,
    iv_available: bool,
    iv_confidence: float | None = None,
) -> float:
    """Calculate transparent completeness confidence in the range [0, 1]."""

    score = 0.0
    score += 0.30 if snapshot_valid else 0.0
    score += 0.30 if history_ready else 0.0
    score += 0.15 if realized_volatility_available else 0.0
    score += 0.10 if book_available else 0.0
    score += 0.075 if flow_available else 0.0
    iv_completeness = 1.0 if iv_confidence is None else _clamp(iv_confidence, 0.0, 1.0)
    score += 0.075 * iv_completeness if iv_available else 0.0
    if not snapshot_valid:
        score = min(score, 0.20)
    elif not history_ready:
        score = min(score, 0.39)
    return round(_clamp(score, 0.0, 1.0), 3)


def _format_optional(value: float | None, fmt: str = ".4g") -> str:
    return "unavailable" if value is None else format(value, fmt)


def _format_signed(value: float | None, fmt: str = ".4g") -> str:
    return "unavailable" if value is None else f"{value:+{fmt}}"


def format_state_summary(state: MarketState) -> str:
    """Format one concise state report for the Condor log."""

    rv_text = (
        "unavailable"
        if state.realized_volatility_ratio is None
        else f"{state.realized_volatility_ratio:.4g}x baseline"
    )
    atm_iv_text = (
        "unavailable" if state.atm_iv is None else f"{state.atm_iv:.2%}"
    )
    iv_ratio_text = (
        "unavailable" if state.iv_ratio is None else f"{state.iv_ratio:.4g}x baseline"
    )
    iv_ratio_display = (
        "initializing"
        if state.atm_iv is not None and not state.iv_history_ready
        else iv_ratio_text
    )
    atm_strike_text = _format_optional(state.atm_strike, ".8g")
    return "\n".join(
        [
            "[DERIVE STATE]",
            f"Pair: {state.trading_pair}",
            "Volatility:",
            f"  {state.volatility_state.name}",
            f"  Score: {_format_optional(state.volatility_score)}",
            f"  RV: {rv_text}",
            f"  ATM IV: {atm_iv_text}",
            f"  IV Ratio: {iv_ratio_display}",
            f"  IV Expiry: {state.option_expiry or 'unavailable'}",
            f"  IV DTE: {_format_optional(state.option_expiry_dte, '.2f')}",
            f"  ATM Strike: {atm_strike_text}",
            "Direction:",
            f"  {state.direction_state.name}",
            f"  Score: {_format_signed(state.direction_score)}",
            f"  Book: {_format_signed(state.book_imbalance)}",
            f"  OFI: {_format_signed(state.order_flow_imbalance)}",
            f"  Return: {_format_signed(state.short_return, '.3%')}",
            "Inventory:",
            f"  {state.inventory_state.name}",
            f"  Ratio: {_format_signed(state.inventory_ratio)}",
            f"Confidence: {state.confidence:.2f}",
            f"Valid: {str(state.state_valid).lower()}",
            "Reasons:",
            *[f"  {reason}" for reason in state.reasons],
        ]
    )


def _coerce_snapshot(snapshot: Any) -> _SnapshotPoint:
    raw_timestamp = _read(snapshot, "timestamp", "")
    timestamp = str(raw_timestamp) if raw_timestamp is not None else ""
    timestamp_seconds = parse_timestamp(raw_timestamp)
    trading_pair = str(_read(snapshot, "trading_pair", ""))

    book_imbalance = _finite_float(_read(snapshot, "depth_imbalance"))
    if book_imbalance is None:
        book_imbalance = _finite_float(_read(snapshot, "top_level_imbalance"))

    flow = _finite_float(_read(snapshot, "order_flow_imbalance"))
    if _read(snapshot, "trade_data_available", False) is not True:
        flow = None

    iv = _finite_float(_read(snapshot, "atm_iv"))
    if _read(snapshot, "iv_data_available", False) is not True or iv is None or iv <= 0:
        iv = None

    call_iv = _finite_float(_read(snapshot, "atm_call_iv"))
    put_iv = _finite_float(_read(snapshot, "atm_put_iv"))
    raw_iv_confidence = _finite_float(_read(snapshot, "iv_confidence"))
    iv_confidence = (
        _clamp(raw_iv_confidence, 0.0, 1.0)
        if raw_iv_confidence is not None
        else (1.0 if iv is not None else 0.0)
    )

    raw_option_errors = _read(snapshot, "option_data_errors", [])
    option_errors = (
        tuple(str(error) for error in raw_option_errors)
        if isinstance(raw_option_errors, (list, tuple))
        else ()
    )

    raw_errors = _read(snapshot, "validation_errors", [])
    errors = tuple(str(error) for error in raw_errors) if isinstance(raw_errors, list) else ()
    return _SnapshotPoint(
        timestamp=timestamp,
        timestamp_seconds=timestamp_seconds,
        trading_pair=trading_pair,
        mid_price=_finite_float(_read(snapshot, "mid_price")),
        book_imbalance=book_imbalance,
        order_flow_imbalance=flow,
        atm_iv=iv,
        atm_call_iv=call_iv,
        atm_put_iv=put_iv,
        atm_iv_confidence=iv_confidence,
        option_expiry=_read(snapshot, "option_expiry"),
        option_expiry_dte=_finite_float(_read(snapshot, "option_expiry_dte")),
        atm_strike=_finite_float(_read(snapshot, "atm_strike")),
        atm_distance_pct=_finite_float(_read(snapshot, "atm_distance_pct")),
        option_data_age_seconds=_finite_float(_read(snapshot, "option_data_age_seconds")),
        option_data_source=_read(snapshot, "option_data_source"),
        option_data_errors=option_errors,
        current_position=_finite_float(_read(snapshot, "current_position")),
        position_notional=_finite_float(_read(snapshot, "position_notional")),
        account_data_available=_read(snapshot, "account_data_available", False) is True,
        data_valid=_read(snapshot, "data_valid", False) is True,
        validation_errors=errors,
    )


def _log_returns(points: Sequence[_SnapshotPoint]) -> list[tuple[float, float]]:
    returns: list[tuple[float, float]] = []
    for previous, current in zip(points, points[1:], strict=False):
        if (
            previous.timestamp_seconds is None
            or current.timestamp_seconds is None
            or current.timestamp_seconds <= previous.timestamp_seconds
            or previous.mid_price is None
            or current.mid_price is None
        ):
            continue
        try:
            value = calculate_log_return(previous.mid_price, current.mid_price)
        except ValueError:
            continue
        returns.append((current.timestamp_seconds, value))
    return returns


def _values_in_window(
    values: Iterable[tuple[float, float]], start: float, end: float
) -> list[float]:
    return [value for timestamp, value in values if start < timestamp <= end]


def _find_mid_at_or_before(
    points: Sequence[_SnapshotPoint], cutoff: float
) -> float | None:
    for point in reversed(points):
        if point.timestamp_seconds is not None and point.timestamp_seconds <= cutoff:
            return point.mid_price
    return None


class StateEngine:
    """Convert a stream of Stage 1 snapshots into explainable market states."""

    def __init__(self, config: StateEngineConfig | None = None) -> None:
        self.config = config or StateEngineConfig()
        self._history: deque[_SnapshotPoint] = deque(maxlen=10_000)
        self._last_timestamp_seconds: float | None = None
        self._trading_pair: str | None = None
        self._volatility_state = VolatilityState.INITIALIZING
        self._direction_state = DirectionState.INITIALIZING
        self._pending_direction: DirectionState | None = None
        self._pending_direction_count = 0

    @property
    def history_size(self) -> int:
        """Expose bounded history size for diagnostics and tests."""

        return len(self._history)

    def _prune_history(self, latest_timestamp: float) -> None:
        cutoff = latest_timestamp - max(
            self.config.history_window_seconds,
            self.config.realized_vol_window_seconds,
            self.config.realized_vol_baseline_seconds,
            self.config.iv_history_window_seconds,
            self.config.direction_return_window_seconds,
        )
        while self._history and (
            self._history[0].timestamp_seconds is None
            or self._history[0].timestamp_seconds < cutoff
        ):
            self._history.popleft()

    def _inventory_values(
        self, point: _SnapshotPoint
    ) -> tuple[InventoryState, float | None, float | None]:
        if not point.account_data_available:
            return InventoryState.UNKNOWN, None, None
        ratio, signed_notional = calculate_inventory_ratio(
            point.current_position,
            point.position_notional,
            point.mid_price,
            self.config.max_position_notional,
        )
        return (
            classify_inventory(ratio, self.config.inventory_neutral_threshold),
            ratio,
            signed_notional,
        )

    def _base_reasons(self, point: _SnapshotPoint) -> list[str]:
        reasons: list[str] = []
        if point.book_imbalance is None:
            reasons.append("book imbalance unavailable")
        else:
            reasons.append(f"book imbalance {_format_signed(point.book_imbalance)}")
        if point.order_flow_imbalance is None:
            reasons.append("OFI unavailable")
        else:
            reasons.append(f"OFI {_format_signed(point.order_flow_imbalance)}")
        if point.atm_iv is None:
            reasons.append("ATM IV unavailable")
        else:
            reasons.append(
                f"ATM IV {point.atm_iv:.2%} (confidence {point.atm_iv_confidence:.2f})"
            )
        reasons.extend(f"options: {error}" for error in point.option_data_errors)
        if point.account_data_available:
            ratio, _ = calculate_inventory_ratio(
                point.current_position,
                point.position_notional,
                point.mid_price,
                self.config.max_position_notional,
            )
            if ratio is not None:
                reasons.append(f"inventory ratio {_format_signed(ratio)}")
        else:
            reasons.append("inventory unavailable")
        return reasons

    def _state(
        self,
        point: _SnapshotPoint,
        *,
        volatility_state: VolatilityState,
        volatility_score: float | None = None,
        direction_state: DirectionState,
        direction_score: float | None = None,
        inventory_state: InventoryState,
        inventory_ratio: float | None = None,
        realized_volatility: float | None = None,
        realized_volatility_ratio: float | None = None,
        iv_ratio: float | None = None,
        iv_change: float | None = None,
        iv_history_samples: int = 0,
        iv_history_ready: bool = False,
        short_return: float | None = None,
        signed_position_notional: float | None = None,
        state_valid: bool,
        confidence: float,
        reasons: Iterable[str],
    ) -> MarketState:
        return MarketState(
            timestamp=point.timestamp or _iso_utc(point.timestamp_seconds),
            trading_pair=point.trading_pair,
            volatility_state=volatility_state,
            volatility_score=volatility_score,
            direction_state=direction_state,
            direction_score=direction_score,
            inventory_state=inventory_state,
            inventory_ratio=inventory_ratio,
            realized_volatility=realized_volatility,
            realized_volatility_ratio=realized_volatility_ratio,
            atm_iv=point.atm_iv,
            atm_call_iv=point.atm_call_iv,
            atm_put_iv=point.atm_put_iv,
            atm_iv_confidence=point.atm_iv_confidence,
            iv_ratio=iv_ratio,
            iv_change=iv_change,
            iv_history_samples=iv_history_samples,
            iv_history_ready=iv_history_ready,
            option_expiry=point.option_expiry,
            option_expiry_dte=point.option_expiry_dte,
            atm_strike=point.atm_strike,
            atm_distance_pct=point.atm_distance_pct,
            option_data_age_seconds=point.option_data_age_seconds,
            option_data_source=point.option_data_source,
            option_data_errors=list(point.option_data_errors),
            book_imbalance=point.book_imbalance,
            order_flow_imbalance=point.order_flow_imbalance,
            short_return=short_return,
            position_notional=signed_position_notional,
            state_valid=state_valid,
            confidence=confidence,
            reasons=list(reasons),
        )

    def _invalid_state(self, point: _SnapshotPoint, errors: Iterable[str]) -> MarketState:
        inventory_state, inventory_ratio, signed_notional = self._inventory_values(point)
        reasons = list(dict.fromkeys([*point.validation_errors, *errors]))
        if not point.data_valid:
            reasons.append("Stage 1 snapshot is invalid; state update skipped")
        if point.mid_price is None:
            reasons.append("mid price unavailable")
        if point.timestamp_seconds is None:
            reasons.append("timestamp unavailable")
        return self._state(
            point,
            volatility_state=self._volatility_state,
            direction_state=self._direction_state,
            inventory_state=inventory_state,
            inventory_ratio=inventory_ratio,
            signed_position_notional=signed_notional,
            state_valid=False,
            confidence=0.0,
            reasons=reasons,
        )

    def _confirmed_direction(self, desired: DirectionState) -> DirectionState:
        if desired is self._direction_state:
            self._pending_direction = None
            self._pending_direction_count = 0
            return self._direction_state
        if desired is DirectionState.INITIALIZING:
            self._pending_direction = None
            self._pending_direction_count = 0
            return self._direction_state
        if desired is self._pending_direction:
            self._pending_direction_count += 1
        else:
            self._pending_direction = desired
            self._pending_direction_count = 1
        if self._pending_direction_count >= self.config.direction_confirmation_samples:
            self._direction_state = desired
            self._pending_direction = None
            self._pending_direction_count = 0
        return self._direction_state

    def _direction_target(self, score: float) -> DirectionState:
        if (
            self._direction_state is DirectionState.BULLISH
            and score >= self.config.bullish_exit_threshold
        ):
            return DirectionState.BULLISH
        if (
            self._direction_state is DirectionState.BEARISH
            and score <= self.config.bearish_exit_threshold
        ):
            return DirectionState.BEARISH
        return classify_direction(
            score,
            bullish_threshold=self.config.bullish_enter_threshold,
            bearish_threshold=self.config.bearish_enter_threshold,
        )

    def update(self, snapshot: Any) -> MarketState:
        """Consume one Stage 1 snapshot without performing any external I/O."""

        point = _coerce_snapshot(snapshot)
        if self._trading_pair is None and point.trading_pair:
            self._trading_pair = point.trading_pair
        elif self._trading_pair and point.trading_pair != self._trading_pair:
            return self._invalid_state(
                point,
                [
                    f"trading pair changed from {self._trading_pair} to {point.trading_pair}",
                ],
            )

        if (
            not point.data_valid
            or point.timestamp_seconds is None
            or point.mid_price is None
            or point.mid_price <= 0
        ):
            return self._invalid_state(point, ())
        if (
            self._last_timestamp_seconds is not None
            and point.timestamp_seconds <= self._last_timestamp_seconds
        ):
            return self._invalid_state(point, ["snapshot timestamp is not newer than history"])

        self._history.append(point)
        self._last_timestamp_seconds = point.timestamp_seconds
        self._prune_history(point.timestamp_seconds)

        inventory_state, inventory_ratio, signed_notional = self._inventory_values(point)
        reasons = self._base_reasons(point)
        history_ready = len(self._history) >= self.config.minimum_history_samples
        if not history_ready:
            reasons.insert(
                0,
                "initializing: "
                f"{len(self._history)}/{self.config.minimum_history_samples} valid snapshots",
            )
            confidence = calculate_confidence(
                snapshot_valid=True,
                history_ready=False,
                realized_volatility_available=False,
                book_available=point.book_imbalance is not None,
                flow_available=point.order_flow_imbalance is not None,
                iv_available=point.atm_iv is not None,
                iv_confidence=point.atm_iv_confidence if point.atm_iv is not None else None,
            )
            return self._state(
                point,
                volatility_state=VolatilityState.INITIALIZING,
                direction_state=DirectionState.INITIALIZING,
                inventory_state=inventory_state,
                inventory_ratio=inventory_ratio,
                signed_position_notional=signed_notional,
                state_valid=False,
                confidence=confidence,
                reasons=reasons,
            )

        history = list(self._history)
        returns = _log_returns(history)
        current_start = point.timestamp_seconds - self.config.realized_vol_window_seconds
        baseline_start = point.timestamp_seconds - self.config.realized_vol_baseline_seconds
        baseline_end = current_start
        current_returns = _values_in_window(
            returns, current_start, point.timestamp_seconds
        )
        baseline_returns = _values_in_window(returns, baseline_start, baseline_end)
        current_rv = calculate_realized_volatility(current_returns)
        baseline_rv = calculate_realized_volatility(baseline_returns)
        rv_ratio = calculate_normalized_volatility(current_rv, baseline_rv)
        historical_iv = [
            item.atm_iv
            for item in history
            if (
                item.timestamp_seconds is not None
                and item.timestamp_seconds < point.timestamp_seconds
                and item.timestamp_seconds
                >= point.timestamp_seconds - self.config.iv_history_window_seconds
                and item.atm_iv is not None
            )
        ]
        iv_history_samples = len(historical_iv)
        iv_history_ready = iv_history_samples >= self.config.iv_minimum_samples
        iv_ratio = (
            calculate_iv_regime(point.atm_iv, historical_iv)
            if iv_history_ready
            else None
        )
        previous_atm_iv = next(
            (
                item.atm_iv
                for item in reversed(history[:-1])
                if (
                    item.timestamp_seconds is not None
                    and item.timestamp_seconds
                    >= point.timestamp_seconds - self.config.iv_history_window_seconds
                    and item.atm_iv is not None
                )
            ),
            None,
        )
        iv_change = calculate_iv_change(point.atm_iv, previous_atm_iv)
        if not iv_history_ready:
            reasons.insert(
                0,
                "initializing: "
                f"{iv_history_samples}/{self.config.iv_minimum_samples} prior ATM IV "
                "observations for IV ratio",
            )
        volatility_score = calculate_combined_volatility_score(
            rv_ratio,
            iv_ratio,
            realized_vol_weight=self.config.realized_vol_weight,
            iv_weight=self.config.iv_weight,
        )
        if volatility_score is None:
            reasons.insert(0, "initializing: realized-volatility baseline unavailable")
            volatility_state = VolatilityState.INITIALIZING
        else:
            volatility_state = classify_volatility(
                volatility_score,
                self._volatility_state,
                enter_threshold=self.config.high_vol_enter_threshold,
                exit_threshold=self.config.high_vol_exit_threshold,
            )
            self._volatility_state = volatility_state
            if rv_ratio is not None:
                reasons.insert(0, f"realized volatility {rv_ratio:.2f}x baseline")
            if iv_ratio is not None:
                reasons.insert(1, f"ATM IV {iv_ratio:.2f}x recent median")

        past_mid = _find_mid_at_or_before(
            history, point.timestamp_seconds - self.config.direction_return_window_seconds
        )
        short_return = (
            calculate_log_return(past_mid, point.mid_price)
            if past_mid is not None
            else None
        )
        price_signal = calculate_price_signal(short_return, self.config.direction_price_scale)
        direction_score = calculate_direction_score(
            point.book_imbalance,
            point.order_flow_imbalance,
            price_signal,
            book_weight=self.config.direction_book_weight,
            flow_weight=self.config.direction_flow_weight,
            price_weight=self.config.direction_price_weight,
        )
        if short_return is not None:
            reasons.append(f"short return {_format_signed(short_return, '.3%')}")
        if direction_score is None:
            reasons.insert(0, "initializing: no direction components available")
            direction_state = DirectionState.INITIALIZING
        else:
            direction_state = self._confirmed_direction(self._direction_target(direction_score))

        state_valid = (
            current_rv is not None
            and baseline_rv is not None
            and direction_score is not None
            and volatility_score is not None
        )
        confidence = calculate_confidence(
            snapshot_valid=True,
            history_ready=True,
            realized_volatility_available=state_valid,
            book_available=point.book_imbalance is not None,
            flow_available=point.order_flow_imbalance is not None,
            iv_available=point.atm_iv is not None,
            iv_confidence=point.atm_iv_confidence if point.atm_iv is not None else None,
        )
        if not state_valid:
            reasons.insert(0, "state is incomplete until required rolling history is available")
        return self._state(
            point,
            volatility_state=volatility_state,
            volatility_score=volatility_score,
            direction_state=direction_state,
            direction_score=direction_score,
            inventory_state=inventory_state,
            inventory_ratio=inventory_ratio,
            realized_volatility=current_rv,
            realized_volatility_ratio=rv_ratio,
            iv_ratio=iv_ratio,
            iv_change=iv_change,
            iv_history_samples=iv_history_samples,
            iv_history_ready=iv_history_ready,
            short_return=short_return,
            signed_position_notional=signed_notional,
            state_valid=state_valid,
            confidence=confidence,
            reasons=reasons,
        )


__all__ = [
    "DirectionState",
    "InventoryState",
    "MarketState",
    "StateEngine",
    "StateEngineConfig",
    "VolatilityState",
    "calculate_combined_volatility_score",
    "calculate_confidence",
    "calculate_direction_score",
    "calculate_inventory_ratio",
    "calculate_iv_regime",
    "calculate_log_return",
    "calculate_normalized_volatility",
    "calculate_price_signal",
    "calculate_realized_volatility",
    "classify_direction",
    "classify_inventory",
    "classify_volatility",
    "format_state_summary",
    "parse_timestamp",
]
