"""Read-only Stage 3 mode selection from normalized market state.

The selector answers only how a later grid generator should behave.  It does
not calculate prices, sizes, levels, or execution instructions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from .state_engine import (
    DirectionState,
    InventoryState,
    MarketState,
    VolatilityState,
    parse_timestamp,
)


class GridMode(StrEnum):
    """Symbolic behavior modes consumed by the later grid stage."""

    NORMAL = "normal"
    DEFENSIVE = "defensive"
    LONG_BIAS = "long_bias"
    SHORT_BIAS = "short_bias"
    PAUSE = "pause"


class ModeSelectorConfig(BaseModel):
    """Compact, explicit thresholds for deterministic mode selection."""

    minimum_mode_confidence: float = Field(default=0.75, ge=0, le=1)
    minimum_bias_confidence: float = Field(default=0.85, ge=0, le=1)
    critical_confidence: float = Field(default=0.50, ge=0, le=1)

    bias_direction_score_threshold: float = Field(default=0.25, ge=0, le=1)
    bias_inventory_limit: float = Field(default=0.40, ge=0)
    inventory_soft_limit: float = Field(default=0.60, ge=0)
    inventory_hard_limit: float = Field(default=0.90, gt=0)

    defensive_volatility_score: float = Field(default=1.50, gt=0)
    extreme_volatility_score: float = Field(default=3.00, gt=0)
    defensive_iv_ratio_threshold: float = Field(default=1.25, gt=0)

    mode_confirmation_samples: int = Field(default=2, ge=1)
    minimum_mode_duration_seconds: float = Field(default=10.0, ge=0)
    pause_recovery_samples: int = Field(default=3, ge=1)
    pause_recovery_seconds: float = Field(default=0.0, ge=0)
    defensive_exit_confirmation_samples: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> ModeSelectorConfig:
        if not (
            self.critical_confidence
            <= self.minimum_mode_confidence
            <= self.minimum_bias_confidence
        ):
            raise ValueError(
                "critical_confidence <= minimum_mode_confidence "
                "<= minimum_bias_confidence is required"
            )
        if not (
            self.bias_inventory_limit
            <= self.inventory_soft_limit
            <= self.inventory_hard_limit
        ):
            raise ValueError(
                "bias_inventory_limit <= inventory_soft_limit <= inventory_hard_limit "
                "is required"
            )
        if self.defensive_volatility_score >= self.extreme_volatility_score:
            raise ValueError(
                "defensive_volatility_score must be below extreme_volatility_score"
            )
        return self


class GridModeDecision(BaseModel):
    """One explainable mode decision derived from one ``MarketState``."""

    timestamp: str
    trading_pair: str
    market_environment: str = "testnet"
    mode: GridMode
    previous_mode: GridMode | None = None
    transition_occurred: bool = False

    volatility_state: VolatilityState
    volatility_score: float | None = None
    direction_state: DirectionState
    direction_score: float | None = None
    inventory_state: InventoryState
    inventory_ratio: float | None = None

    confidence: float = Field(ge=0, le=1)
    valid: bool
    reasons: list[str] = Field(default_factory=list)
    recommended_profile: str


@dataclass(frozen=True)
class ModeEvaluation:
    """Stateless candidate mode and its deterministic reasons."""

    mode: GridMode
    reasons: tuple[str, ...]


_PROFILE_BY_MODE = {
    GridMode.NORMAL: "standard",
    GridMode.DEFENSIVE: "risk_reduced",
    GridMode.LONG_BIAS: "long_bias",
    GridMode.SHORT_BIAS: "short_bias",
    GridMode.PAUSE: "disabled",
}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _signed(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:+.4g}"


def _dedupe(reasons: list[str]) -> list[str]:
    return list(dict.fromkeys(reasons))


def determine_candidate_mode(
    state: MarketState,
    config: ModeSelectorConfig | None = None,
) -> ModeEvaluation:
    """Apply the ordered risk hierarchy without mutating selector state."""

    cfg = config or ModeSelectorConfig()

    if not state.state_valid:
        return ModeEvaluation(GridMode.PAUSE, ("market state invalid",))

    confidence = _finite(state.confidence)
    if confidence is None:
        return ModeEvaluation(GridMode.PAUSE, ("confidence unavailable",))
    if confidence < cfg.critical_confidence:
        return ModeEvaluation(
            GridMode.PAUSE,
            (
                f"confidence {confidence:.2f} below critical threshold "
                f"{cfg.critical_confidence:.2f}",
            ),
        )

    inventory_ratio = _finite(state.inventory_ratio)
    if state.inventory_state is InventoryState.UNKNOWN or inventory_ratio is None:
        return ModeEvaluation(GridMode.PAUSE, ("inventory data unavailable",))
    if abs(inventory_ratio) >= cfg.inventory_hard_limit:
        return ModeEvaluation(
            GridMode.PAUSE,
            (
                f"inventory ratio {_signed(inventory_ratio)} exceeds hard limit "
                f"+/-{cfg.inventory_hard_limit:.2f}",
            ),
        )

    volatility_score = _finite(state.volatility_score)
    if volatility_score is None or volatility_score < 0:
        return ModeEvaluation(GridMode.PAUSE, ("volatility score unavailable",))
    if volatility_score >= cfg.extreme_volatility_score:
        return ModeEvaluation(
            GridMode.PAUSE,
            (
                f"extreme volatility score {volatility_score:.2f} reaches pause "
                f"threshold {cfg.extreme_volatility_score:.2f}",
            ),
        )
    if state.volatility_state is VolatilityState.INITIALIZING:
        return ModeEvaluation(GridMode.PAUSE, ("volatility state initializing",))

    defensive_reasons: list[str] = []
    if state.volatility_state is VolatilityState.HIGH:
        defensive_reasons.append("high volatility regime")
    if volatility_score >= cfg.defensive_volatility_score:
        defensive_reasons.append(
            f"volatility score {volatility_score:.2f} reaches defensive threshold "
            f"{cfg.defensive_volatility_score:.2f}"
        )
    iv_ratio = _finite(state.iv_ratio)
    if iv_ratio is not None and iv_ratio >= cfg.defensive_iv_ratio_threshold:
        defensive_reasons.append(
            f"ATM IV ratio {iv_ratio:.2f}x baseline reaches defensive threshold "
            f"{cfg.defensive_iv_ratio_threshold:.2f}x"
        )
    if abs(inventory_ratio) >= cfg.inventory_soft_limit:
        defensive_reasons.append(
            f"inventory ratio {_signed(inventory_ratio)} reaches soft limit "
            f"+/-{cfg.inventory_soft_limit:.2f}"
        )
    if confidence < cfg.minimum_mode_confidence:
        defensive_reasons.append(
            f"confidence {confidence:.2f} below normal-operation threshold "
            f"{cfg.minimum_mode_confidence:.2f}"
        )
    if defensive_reasons:
        return ModeEvaluation(GridMode.DEFENSIVE, tuple(defensive_reasons))

    direction_score = _finite(state.direction_score)
    if (
        state.direction_state is DirectionState.BULLISH
        and direction_score is not None
        and direction_score >= cfg.bias_direction_score_threshold
    ):
        if confidence >= cfg.minimum_bias_confidence and inventory_ratio < cfg.bias_inventory_limit:
            return ModeEvaluation(
                GridMode.LONG_BIAS,
                (
                    f"bullish direction confirmed at {_signed(direction_score)}; "
                    f"inventory {_signed(inventory_ratio)} permits long bias",
                ),
            )
        if inventory_ratio >= cfg.bias_inventory_limit:
            return ModeEvaluation(
                GridMode.NORMAL,
                (
                    f"long bias blocked: inventory {_signed(inventory_ratio)} reaches "
                    f"bias limit +{cfg.bias_inventory_limit:.2f}",
                ),
            )
        return ModeEvaluation(
            GridMode.NORMAL,
            (
                f"long bias blocked: confidence {confidence:.2f} below bias threshold "
                f"{cfg.minimum_bias_confidence:.2f}",
            ),
        )

    if (
        state.direction_state is DirectionState.BEARISH
        and direction_score is not None
        and direction_score <= -cfg.bias_direction_score_threshold
    ):
        if (
            confidence >= cfg.minimum_bias_confidence
            and inventory_ratio > -cfg.bias_inventory_limit
        ):
            return ModeEvaluation(
                GridMode.SHORT_BIAS,
                (
                    f"bearish direction confirmed at {_signed(direction_score)}; "
                    f"inventory {_signed(inventory_ratio)} permits short bias",
                ),
            )
        if inventory_ratio <= -cfg.bias_inventory_limit:
            return ModeEvaluation(
                GridMode.NORMAL,
                (
                    f"short bias blocked: inventory {_signed(inventory_ratio)} reaches "
                    f"bias limit -{cfg.bias_inventory_limit:.2f}",
                ),
            )
        return ModeEvaluation(
            GridMode.NORMAL,
            (
                f"short bias blocked: confidence {confidence:.2f} below bias threshold "
                f"{cfg.minimum_bias_confidence:.2f}",
            ),
        )

    direction_text = state.direction_state.value.replace("_", " ")
    return ModeEvaluation(
        GridMode.NORMAL,
        (
            f"normal volatility; direction {direction_text}; "
            f"inventory {_signed(inventory_ratio)} within target range",
        ),
    )


def format_grid_mode_summary(decision: GridModeDecision) -> str:
    """Format one concise mode report for Condor logs."""

    previous = decision.previous_mode.name if decision.previous_mode else "NONE"
    volatility_score = (
        "unavailable"
        if decision.volatility_score is None
        else f"{decision.volatility_score:.4g}"
    )
    transition = f"{previous} -> {decision.mode.name}"
    if not decision.transition_occurred:
        transition = "none"
    return "\n".join(
        [
            "[GRID MODE]",
            f"Pair: {decision.trading_pair}",
            f"Previous: {previous}",
            f"Current: {decision.mode.name}",
            "Volatility:",
            f"  {decision.volatility_state.name}",
            f"  Score: {volatility_score}",
            "Direction:",
            f"  {decision.direction_state.name}",
            f"  Score: {_signed(decision.direction_score)}",
            "Inventory:",
            f"  {decision.inventory_state.name}",
            f"  Ratio: {_signed(decision.inventory_ratio)}",
            f"Confidence: {decision.confidence:.2f}",
            f"Valid: {str(decision.valid).lower()}",
            f"Transition: {transition}",
            "Reasons:",
            *[f"  {reason}" for reason in decision.reasons],
        ]
    )


class ModeSelector:
    """Stateful selector with confirmation, duration, and safe recovery gates."""

    def __init__(self, config: ModeSelectorConfig | None = None) -> None:
        self.config = config or ModeSelectorConfig()
        self._current_mode: GridMode | None = None
        self._candidate_mode: GridMode | None = None
        self._candidate_count = 0
        self._mode_entered_at: float | None = None
        self._last_timestamp_seconds: float | None = None
        self._pause_safe_since: float | None = None

    @property
    def current_mode(self) -> GridMode | None:
        return self._current_mode

    @property
    def candidate_mode(self) -> GridMode | None:
        return self._candidate_mode

    @property
    def candidate_count(self) -> int:
        return self._candidate_count

    def _reset_candidate(self) -> None:
        self._candidate_mode = None
        self._candidate_count = 0

    def _track_candidate(self, mode: GridMode) -> int:
        if self._candidate_mode is mode:
            self._candidate_count += 1
        else:
            self._candidate_mode = mode
            self._candidate_count = 1
        return self._candidate_count

    def _duration_satisfied(self, timestamp_seconds: float) -> bool:
        if self._mode_entered_at is None:
            return True
        return (
            timestamp_seconds - self._mode_entered_at
            >= self.config.minimum_mode_duration_seconds
        )

    def _activate(self, mode: GridMode, timestamp_seconds: float) -> None:
        self._current_mode = mode
        self._mode_entered_at = timestamp_seconds
        self._reset_candidate()
        self._pause_safe_since = None

    def _decision(
        self,
        state: MarketState,
        *,
        previous_mode: GridMode | None,
        reasons: list[str],
    ) -> GridModeDecision:
        assert self._current_mode is not None
        return GridModeDecision(
            timestamp=state.timestamp,
            trading_pair=state.trading_pair,
            mode=self._current_mode,
            previous_mode=previous_mode,
            transition_occurred=previous_mode is not self._current_mode,
            volatility_state=state.volatility_state,
            volatility_score=state.volatility_score,
            direction_state=state.direction_state,
            direction_score=state.direction_score,
            inventory_state=state.inventory_state,
            inventory_ratio=state.inventory_ratio,
            confidence=state.confidence,
            valid=state.state_valid,
            reasons=_dedupe(reasons),
            recommended_profile=_PROFILE_BY_MODE[self._current_mode],
        )

    def _force_pause(
        self,
        state: MarketState,
        reasons: list[str],
        timestamp_seconds: float | None,
    ) -> GridModeDecision:
        previous_mode = self._current_mode
        if timestamp_seconds is None:
            timestamp_seconds = self._mode_entered_at
        if timestamp_seconds is None:
            timestamp_seconds = 0.0
        self._activate(GridMode.PAUSE, timestamp_seconds)
        return self._decision(
            state,
            previous_mode=previous_mode,
            reasons=[*reasons, "PAUSE is an immediate safety mode"],
        )

    def _pause_recovery(
        self,
        state: MarketState,
        evaluation: ModeEvaluation,
        timestamp_seconds: float,
        previous_mode: GridMode | None,
    ) -> GridModeDecision:
        recoverable_modes = {
            GridMode.NORMAL,
            GridMode.DEFENSIVE,
            GridMode.LONG_BIAS,
            GridMode.SHORT_BIAS,
        }
        if evaluation.mode not in recoverable_modes:
            self._reset_candidate()
            self._pause_safe_since = None
            return self._decision(
                state,
                previous_mode=previous_mode,
                reasons=[
                    *evaluation.reasons,
                    "PAUSE recovery requires a normal, defensive, or directional-risk candidate",
                ],
            )

        count = self._track_candidate(evaluation.mode)
        if self._pause_safe_since is None:
            self._pause_safe_since = timestamp_seconds
        elapsed = timestamp_seconds - self._pause_safe_since
        ready = (
            count >= self.config.pause_recovery_samples
            and elapsed >= self.config.pause_recovery_seconds
        )
        if ready:
            self._activate(evaluation.mode, timestamp_seconds)
            reasons = [
                *evaluation.reasons,
                f"PAUSE recovery complete to {evaluation.mode.name} after "
                f"{count} safe observations",
            ]
        else:
            reasons = [
                *evaluation.reasons,
                f"PAUSE recovery pending: {count}/{self.config.pause_recovery_samples} "
                "safe observations",
            ]
            if self.config.pause_recovery_seconds > 0:
                reasons.append(
                    f"PAUSE recovery time {elapsed:.1f}/{self.config.pause_recovery_seconds:.1f}s"
                )
        return self._decision(
            state,
            previous_mode=previous_mode,
            reasons=reasons,
        )

    def _defensive_exit(
        self,
        state: MarketState,
        evaluation: ModeEvaluation,
        timestamp_seconds: float,
        previous_mode: GridMode | None,
    ) -> GridModeDecision:
        if evaluation.mode is GridMode.DEFENSIVE:
            self._reset_candidate()
            return self._decision(
                state,
                previous_mode=previous_mode,
                reasons=list(evaluation.reasons),
            )

        count = self._track_candidate(GridMode.NORMAL)
        if count >= self.config.defensive_exit_confirmation_samples and self._duration_satisfied(
            timestamp_seconds
        ):
            self._activate(GridMode.NORMAL, timestamp_seconds)
            reasons = [
                *evaluation.reasons,
                "defensive exit confirmed; returning to NORMAL",
            ]
        else:
            reasons = [
                *evaluation.reasons,
                f"defensive exit pending: {count}/"
                f"{self.config.defensive_exit_confirmation_samples} safe observations",
            ]
        return self._decision(
            state,
            previous_mode=previous_mode,
            reasons=reasons,
        )

    def _apply_evaluation(
        self,
        state: MarketState,
        evaluation: ModeEvaluation,
        timestamp_seconds: float,
    ) -> GridModeDecision:
        previous_mode = self._current_mode
        if evaluation.mode is GridMode.PAUSE:
            return self._force_pause(state, list(evaluation.reasons), timestamp_seconds)

        if self._current_mode is None:
            self._activate(GridMode.PAUSE, timestamp_seconds)
            previous_mode = None

        assert self._current_mode is not None
        if self._current_mode is GridMode.PAUSE:
            return self._pause_recovery(
                state, evaluation, timestamp_seconds, previous_mode
            )
        if self._current_mode is GridMode.DEFENSIVE:
            return self._defensive_exit(
                state, evaluation, timestamp_seconds, previous_mode
            )
        if evaluation.mode is self._current_mode:
            self._reset_candidate()
            return self._decision(
                state,
                previous_mode=previous_mode,
                reasons=list(evaluation.reasons),
            )

        count = self._track_candidate(evaluation.mode)
        reasons = list(evaluation.reasons)
        if count >= self.config.mode_confirmation_samples and self._duration_satisfied(
            timestamp_seconds
        ):
            old_mode = self._current_mode
            self._activate(evaluation.mode, timestamp_seconds)
            reasons.append(
                f"mode transition {old_mode.name} -> {evaluation.mode.name} "
                f"confirmed for {count} observations"
            )
        else:
            reasons.append(
                f"candidate {evaluation.mode.name} pending: {count}/"
                f"{self.config.mode_confirmation_samples} confirmations"
            )
            if not self._duration_satisfied(timestamp_seconds):
                entered = self._mode_entered_at or timestamp_seconds
                reasons.append(
                    f"minimum mode duration pending: "
                    f"{timestamp_seconds - entered:.1f}/"
                    f"{self.config.minimum_mode_duration_seconds:.1f}s"
                )
        return self._decision(
            state,
            previous_mode=previous_mode,
            reasons=reasons,
        )

    def _malformed_decision(self, raw_state: Any) -> GridModeDecision:
        raw = raw_state if isinstance(raw_state, Mapping) else {}
        timestamp = str(raw.get("timestamp", ""))
        trading_pair = str(raw.get("trading_pair", "unknown")) or "unknown"
        state = MarketState(
            timestamp=timestamp,
            trading_pair=trading_pair,
            volatility_state=VolatilityState.INITIALIZING,
            direction_state=DirectionState.INITIALIZING,
            inventory_state=InventoryState.UNKNOWN,
            state_valid=False,
            confidence=0.0,
        )
        return self._force_pause(
            state,
            ["market state malformed; PAUSE required"],
            parse_timestamp(timestamp),
        )

    def update(self, state: MarketState | Mapping[str, Any]) -> GridModeDecision:
        """Consume one state observation without performing external I/O."""

        try:
            market_state = (
                state
                if isinstance(state, MarketState)
                else MarketState.model_validate(state)
            )
        except (TypeError, ValueError, ValidationError):
            return self._malformed_decision(state)

        timestamp_seconds = parse_timestamp(market_state.timestamp)
        if timestamp_seconds is None:
            return self._force_pause(
                market_state,
                ["market state timestamp unavailable"],
                None,
            )
        if (
            self._last_timestamp_seconds is not None
            and timestamp_seconds <= self._last_timestamp_seconds
        ):
            return self._force_pause(
                market_state,
                ["market state timestamp is not newer than selector history"],
                timestamp_seconds,
            )
        self._last_timestamp_seconds = timestamp_seconds
        evaluation = determine_candidate_mode(market_state, self.config)
        return self._apply_evaluation(market_state, evaluation, timestamp_seconds)


__all__ = [
    "GridMode",
    "GridModeDecision",
    "ModeEvaluation",
    "ModeSelector",
    "ModeSelectorConfig",
    "determine_candidate_mode",
    "format_grid_mode_summary",
]
