"""Deterministic multi-asset risk and grid orchestration for Stage 8.

The original Stage 1--7 engines remain the source of truth for one asset.  This
module composes those engines instead of copying their calculations:

* one :class:`GlobalRiskEngine` consumes BTC ATM IV;
* one rolling relationship engine estimates each asset's BTC correlation and
  beta from synchronized returns;
* one existing ``StateEngine``, ``ModeSelector``, and ``GridParameterEngine``
  run independently for every configured pair; and
* :class:`PortfolioRiskGovernor` evaluates proposed entries before a later
  execution adapter can create an executor.

There is intentionally no network, Hummingbot, or order surface here.  The
module is suitable for deterministic replay and dry-run Condor presentation.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .grid_engine import GridParameterConfig, GridParameterEngine, GridPlan
from .mode_selector import GridModeDecision, ModeSelector, ModeSelectorConfig
from .state_engine import (
    DirectionState,
    InventoryState,
    MarketState,
    StateEngine,
    StateEngineConfig,
    VolatilityState,
    calculate_combined_volatility_score,
    classify_volatility,
    parse_timestamp,
)

SUPPORTED_TRADING_PAIRS = ("BTC-USDC", "ETH-USDC", "SOL-USDC", "HYPE-USDC")
BTC_TRADING_PAIR = "BTC-USDC"
_EPSILON = 1e-12


def _read(value: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(field_name, default)
    return getattr(value, field_name, default)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _timestamp(value: Any, fallback: float | None = None) -> tuple[str, float | None]:
    seconds = parse_timestamp(value)
    if seconds is None:
        seconds = time.time() if fallback is None else fallback
    text = datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    return text, seconds


def pair_level_id(trading_pair: str, side: str, level_index: int) -> str:
    """Return a stable level key that cannot collide across markets."""

    return f"{trading_pair}::{str(side).lower()}_{int(level_index)}"


class GlobalRiskRegime(StrEnum):
    INITIALIZING = "initializing"
    NORMAL = "normal"
    ELEVATED = "elevated"
    EXTREME = "extreme"


class GlobalRiskSettings(BaseModel):
    """Shared BTC-options controls; they are not copied per asset."""

    source_pair: str = BTC_TRADING_PAIR
    iv_weight: float = Field(default=0.25, ge=0, le=1)
    stale_seconds: float = Field(default=15.0, gt=0)
    missing_behavior: Literal["rv_only", "defensive", "pause"] = "rv_only"
    elevated_ratio: float = Field(default=1.25, gt=0)
    extreme_ratio: float = Field(default=1.50, gt=0)
    history_window_seconds: float = Field(default=900.0, gt=0)
    minimum_history_samples: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def validate_regime_thresholds(self) -> GlobalRiskSettings:
        if self.extreme_ratio <= self.elevated_ratio:
            raise ValueError("extreme_ratio must be above elevated_ratio")
        if self.source_pair != BTC_TRADING_PAIR:
            raise ValueError("the shared options source must be BTC-USDC")
        return self


class RelationshipSettings(BaseModel):
    """Rolling synchronized BTC relationship estimator controls."""

    window_seconds: float = Field(default=3600.0, gt=0)
    short_window_seconds: float = Field(default=900.0, gt=0)
    medium_window_seconds: float = Field(default=1800.0, gt=0)
    sensitivity_windows_seconds: tuple[float, ...] = (900.0, 1800.0, 3600.0)
    minimum_observations: int = Field(default=15, ge=3)
    beta_clip: float = Field(default=3.0, gt=0)
    transmission_max: float = Field(default=1.5, gt=0)
    stale_seconds: float = Field(default=120.0, gt=0)
    max_gap_seconds: float = Field(default=180.0, gt=0)

    @model_validator(mode="after")
    def validate_windows(self) -> RelationshipSettings:
        if self.short_window_seconds > self.medium_window_seconds:
            raise ValueError("short_window_seconds must not exceed medium_window_seconds")
        if self.medium_window_seconds > self.window_seconds:
            raise ValueError("medium_window_seconds must not exceed window_seconds")
        if not self.sensitivity_windows_seconds:
            raise ValueError("at least one relationship sensitivity window is required")
        return self


class PortfolioRiskSettings(BaseModel):
    """Conservative portfolio limits used by the dry-run governor."""

    portfolio_max_gross_notional: float = Field(default=10_000.0, gt=0)
    portfolio_soft_beta_exposure: float = Field(default=2_000.0, gt=0)
    portfolio_hard_beta_exposure: float = Field(default=4_000.0, gt=0)
    portfolio_max_long_beta_exposure: float = Field(default=3_000.0, gt=0)
    portfolio_max_short_beta_exposure: float = Field(default=3_000.0, gt=0)
    per_asset_max_position_notional: float = Field(default=2_000.0, gt=0)
    max_active_executors_per_asset: int = Field(default=4, ge=1)
    max_active_executors_portfolio: int = Field(default=8, ge=1)
    soft_limit_ratio: float = Field(default=1.0, gt=0, le=1)

    @model_validator(mode="after")
    def validate_limits(self) -> PortfolioRiskSettings:
        if self.portfolio_soft_beta_exposure >= self.portfolio_hard_beta_exposure:
            raise ValueError("portfolio_soft_beta_exposure must be below hard beta exposure")
        if self.portfolio_soft_beta_exposure > self.portfolio_max_gross_notional:
            raise ValueError("soft beta exposure must fit within gross notional")
        return self


class MultiAssetConfig(BaseModel):
    """Shared configuration for the four-asset dry-run architecture."""

    market_environment: Literal["testnet", "mainnet"] = "testnet"
    execution_mode: Literal["TESTNET", "LIVE", "SHADOW", "REPLAY"] = "TESTNET"
    supported_markets: tuple[str, ...] = SUPPORTED_TRADING_PAIRS
    enabled_markets: tuple[str, ...] = SUPPORTED_TRADING_PAIRS
    global_options: GlobalRiskSettings = Field(default_factory=GlobalRiskSettings)
    relationship: RelationshipSettings = Field(default_factory=RelationshipSettings)
    portfolio_risk: PortfolioRiskSettings = Field(default_factory=PortfolioRiskSettings)
    state: StateEngineConfig = Field(default_factory=StateEngineConfig)
    mode: ModeSelectorConfig = Field(default_factory=ModeSelectorConfig)
    grid: GridParameterConfig = Field(default_factory=GridParameterConfig)
    local_rv_weight: float = Field(default=0.75, ge=0)
    transmitted_btc_iv_weight: float = Field(default=0.25, ge=0)
    execution_enabled: bool = False
    allow_mainnet_trading: bool = False
    per_asset_max_levels_per_side: int = Field(default=1, ge=1)
    execution_status_by_market: dict[str, str] = Field(default_factory=dict)
    use_incremental_pending_exposure_for_reconciliation: bool = False

    @model_validator(mode="after")
    def validate_safe_scope(self) -> MultiAssetConfig:
        supported = set(self.supported_markets)
        enabled = set(self.enabled_markets)
        if not enabled:
            raise ValueError("at least one market must be enabled")
        if not enabled.issubset(supported):
            raise ValueError("enabled_markets must be a subset of supported_markets")
        invalid_statuses = set(self.execution_status_by_market.values()) - {
            "EXECUTION_ENABLED",
            "SIGNAL_ONLY",
            "SIGNAL_ONLY_MIN_SIZE",
            "DISABLED",
        }
        if invalid_statuses:
            raise ValueError(f"unsupported execution status: {sorted(invalid_statuses)}")
        if not set(self.execution_status_by_market).issubset(supported):
            raise ValueError("execution status keys must be supported markets")
        if BTC_TRADING_PAIR not in supported:
            raise ValueError("BTC-USDC must remain a supported market")
        if self.global_options.source_pair != BTC_TRADING_PAIR:
            raise ValueError("global options source must remain BTC-USDC")
        if self.market_environment == "mainnet" and self.execution_mode != "SHADOW":
            raise ValueError(
                "mainnet market data is allowed only with execution_mode=SHADOW; "
                "mainnet order execution remains a separate authorization boundary"
            )
        if self.execution_mode == "SHADOW" and self.market_environment != "mainnet":
            raise ValueError("execution_mode=SHADOW requires market_environment=mainnet")
        if self.execution_mode == "SHADOW" and self.allow_mainnet_trading:
            raise ValueError("shadow mode must not enable allow_mainnet_trading")
        if self.execution_enabled:
            raise ValueError("multi-asset development config must keep execution disabled")
        if self.local_rv_weight == 0 and self.transmitted_btc_iv_weight == 0:
            raise ValueError("at least one volatility component must be enabled")
        return self


class GlobalRiskState(BaseModel):
    """One shared, immutable-in-practice BTC options systematic-risk state."""

    model_config = ConfigDict(frozen=True)

    timestamp: str
    source_asset: str = "BTC"
    source_pair: str = BTC_TRADING_PAIR
    btc_atm_iv: float | None = None
    btc_iv_ratio: float | None = None
    btc_iv_change: float | None = None
    btc_iv_score: float | None = None
    btc_options_confidence: float = Field(default=0.0, ge=0, le=1)
    btc_iv_age_seconds: float | None = None
    btc_iv_available: bool = False
    btc_iv_stale: bool = False
    global_risk_score: float | None = None
    global_risk_regime: GlobalRiskRegime = GlobalRiskRegime.INITIALIZING
    reasons: list[str] = Field(default_factory=list)


class BTCTransmissionState(BaseModel):
    """Measured BTC relationship used to scale shared IV risk."""

    model_config = ConfigDict(frozen=True)

    timestamp: str
    trading_pair: str
    btc_correlation: float | None = None
    btc_beta: float | None = None
    relationship_observations: int = Field(default=0, ge=0)
    relationship_confidence: float = Field(default=0.0, ge=0, le=1)
    relationship_valid: bool = False
    correlation_age_seconds: float | None = None
    transmission_coefficient: float = Field(default=0.0, ge=0)
    residual_volatility: float | None = None
    window_seconds: float | None = None
    reasons: list[str] = Field(default_factory=list)


class AssetMarketState(MarketState):
    """Stage 2 state with shared-risk and relationship annotations."""

    market_environment: str = "testnet"
    local_realized_volatility_ratio: float | None = None
    transmitted_btc_iv_component: float | None = None
    btc_transmission_coefficient: float = 0.0
    btc_correlation: float | None = None
    btc_beta: float | None = None
    relationship_confidence: float = 0.0
    residual_volatility: float | None = None
    global_risk_score: float | None = None
    global_risk_regime: GlobalRiskRegime = GlobalRiskRegime.INITIALIZING
    global_risk_state: GlobalRiskState | None = None
    btc_transmission: BTCTransmissionState | None = None
    global_iv_fallback: bool = False


class AssetGridModeDecision(GridModeDecision):
    """Stage 3 decision with explicit environment and portfolio scope."""

    market_environment: str = "testnet"
    global_risk_regime: GlobalRiskRegime = GlobalRiskRegime.INITIALIZING
    portfolio_risk_scope: str = "asset"


class ProposedEntry(BaseModel):
    """One prospective entry considered by the portfolio governor."""

    model_config = ConfigDict(frozen=True)

    trading_pair: str
    level_id: str
    side: Literal["buy", "sell"]
    quote_notional: float = Field(ge=0)


class PortfolioRiskDecision(BaseModel):
    """Portfolio-level result before Stage 5 entry creation."""

    timestamp: str
    gross_notional: float = 0.0
    net_notional: float = 0.0
    btc_beta_equivalent_exposure: float = 0.0
    long_beta_exposure: float = 0.0
    short_beta_exposure: float = 0.0
    per_asset_exposure: dict[str, float] = Field(default_factory=dict)
    portfolio_risk_ratio: float = 0.0
    soft_limit_triggered: bool = False
    hard_limit_triggered: bool = False
    blocked_pairs: list[str] = Field(default_factory=list)
    blocked_sides: dict[str, list[str]] = Field(default_factory=dict)
    global_pause_new_exposure: bool = False
    risk_reducing_sides: dict[str, list[str]] = Field(default_factory=dict)
    allowed_level_ids: dict[str, list[str]] = Field(default_factory=dict)
    blocked_level_ids: dict[str, list[str]] = Field(default_factory=dict)
    active_executors: int = 0
    per_asset_active_executors: dict[str, int] = Field(default_factory=dict)
    active_executor_input_count: int = 0
    pending_executor_count: int = 0
    active_pending_executor_overlap_count: int = 0
    pre_proposal_active_executors: int = 0
    executor_cap_triggered: bool = False
    risk_delta_audit: list[dict[str, Any]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class PortfolioPlanRoute(BaseModel):
    """Pair-safe dry-run routing result for a GridPlan."""

    model_config = ConfigDict(frozen=True)

    trading_pair: str
    allowed_level_ids: tuple[str, ...] = ()
    blocked_level_ids: tuple[str, ...] = ()
    blocked_sides: tuple[str, ...] = ()
    executor_namespace: str


def _global_snapshot_values(
    snapshot: Any, now_seconds: float
) -> tuple[float | None, float | None, float]:
    iv = _finite(_read(snapshot, "btc_atm_iv", _read(snapshot, "atm_iv")))
    available_raw = _read(snapshot, "btc_iv_available", _read(snapshot, "iv_data_available"))
    available = available_raw is True or (
        available_raw is None
        and iv is not None
        and _read(snapshot, "data_available", False) is True
    )
    if iv is None or iv <= 0 or not available:
        return None, None, 0.0
    age = _finite(_read(snapshot, "btc_iv_age_seconds", _read(snapshot, "option_data_age_seconds")))
    if age is None:
        option_timestamp = parse_timestamp(
            _read(snapshot, "option_data_timestamp", _read(snapshot, "timestamp"))
        )
        if option_timestamp is not None:
            age = max(0.0, now_seconds - option_timestamp)
    confidence = _finite(
        _read(
            snapshot,
            "btc_options_confidence",
            _read(snapshot, "iv_confidence", _read(snapshot, "confidence")),
        )
    )
    return iv, age, max(0.0, min(1.0, confidence if confidence is not None else 0.0))


class GlobalRiskEngine:
    """Build one BTC GlobalRiskState per common clock tick."""

    def __init__(self, config: GlobalRiskSettings | None = None) -> None:
        self.config = config or GlobalRiskSettings()
        self._history: deque[tuple[float, float]] = deque(maxlen=10_000)
        self._last_iv: float | None = None
        self._fetch_count = 0

    @property
    def fetch_count(self) -> int:
        """Number of shared BTC option observations accepted for processing."""

        return self._fetch_count

    @property
    def history_size(self) -> int:
        return len(self._history)

    def update(self, snapshot: Any, *, now_seconds: float | None = None) -> GlobalRiskState:
        """Consume one BTC option observation; never fabricate a missing IV."""

        timestamp, timestamp_seconds = _timestamp(
            _read(snapshot, "timestamp"), now_seconds
        )
        now = timestamp_seconds if timestamp_seconds is not None else time.time()
        self._fetch_count += 1
        iv, age, confidence = _global_snapshot_values(snapshot, now)
        stale = age is None or age > self.config.stale_seconds
        reasons: list[str] = []
        history_cutoff = now - self.config.history_window_seconds
        while self._history and self._history[0][0] < history_cutoff:
            self._history.popleft()

        if iv is None:
            reasons.append("BTC ATM IV unavailable; no IV was fabricated")
        if age is None:
            reasons.append("BTC ATM IV age unavailable")
        elif age > self.config.stale_seconds:
            reasons.append(
                f"BTC ATM IV stale at {age:.1f}s; limit {self.config.stale_seconds:.1f}s"
            )
        if confidence <= 0:
            reasons.append("BTC options confidence unavailable")

        iv_available = iv is not None and not stale and confidence > 0
        historical = [value for _, value in self._history]
        ratio = None
        if iv_available and len(historical) >= self.config.minimum_history_samples:
            baseline = statistics.median(historical)
            if baseline > _EPSILON:
                ratio = iv / baseline
            else:
                reasons.append("BTC IV baseline was zero")
        elif iv_available:
            reasons.append(
                f"BTC IV ratio initializing: {len(historical)}/"
                f"{self.config.minimum_history_samples} prior observations"
            )
        change = None if iv is None or self._last_iv is None else iv - self._last_iv
        if iv_available:
            self._history.append((now, iv))
            self._last_iv = iv

        score = ratio if ratio is not None else None
        if score is None:
            regime = GlobalRiskRegime.INITIALIZING
        elif score >= self.config.extreme_ratio:
            regime = GlobalRiskRegime.EXTREME
        elif score >= self.config.elevated_ratio:
            regime = GlobalRiskRegime.ELEVATED
        else:
            regime = GlobalRiskRegime.NORMAL
        if iv_available and ratio is not None:
            reasons.append(f"BTC ATM IV ratio {ratio:.3f}x recent median")
        if change is not None:
            reasons.append(f"BTC ATM IV change {change:+.4f}")

        return GlobalRiskState(
            timestamp=timestamp,
            btc_atm_iv=iv,
            btc_iv_ratio=ratio,
            btc_iv_change=change,
            btc_iv_score=score,
            btc_options_confidence=confidence,
            btc_iv_age_seconds=age,
            btc_iv_available=iv_available,
            btc_iv_stale=stale,
            global_risk_score=score,
            global_risk_regime=regime,
            reasons=list(dict.fromkeys(reasons)),
        )


@dataclass(frozen=True)
class _RelationshipReturn:
    timestamp_seconds: float
    asset_return: float
    btc_return: float


class RollingBTCRelationshipEngine:
    """Estimate synchronized rolling BTC correlation, beta, and residual RV."""

    def __init__(
        self,
        config: RelationshipSettings | None = None,
        *,
        trading_pairs: Iterable[str] = SUPPORTED_TRADING_PAIRS,
    ) -> None:
        self.config = config or RelationshipSettings()
        self.trading_pairs = tuple(dict.fromkeys(trading_pairs))
        self._last_prices: dict[str, tuple[float, float]] = {}
        self._returns: dict[str, deque[_RelationshipReturn]] = {
            pair: deque(maxlen=10_000)
            for pair in self.trading_pairs
            if pair != BTC_TRADING_PAIR
        }

    @staticmethod
    def _log_return(previous: float | None, current: float | None) -> float | None:
        if previous is None or current is None or previous <= 0 or current <= 0:
            return None
        value = math.log(current / previous)
        return value if math.isfinite(value) else None

    def _state(self, pair: str, timestamp: str, timestamp_seconds: float) -> BTCTransmissionState:
        if pair == BTC_TRADING_PAIR:
            return BTCTransmissionState(
                timestamp=timestamp,
                trading_pair=pair,
                btc_correlation=1.0,
                btc_beta=1.0,
                relationship_observations=0,
                relationship_confidence=1.0,
                relationship_valid=True,
                correlation_age_seconds=0.0,
                transmission_coefficient=1.0,
                window_seconds=self.config.window_seconds,
                reasons=["BTC is the identity relationship by definition"],
            )
        rows = [
            row
            for row in self._returns.get(pair, ())
            if timestamp_seconds - self.config.window_seconds
            < row.timestamp_seconds
            <= timestamp_seconds
        ]
        observations = len(rows)
        confidence = min(1.0, observations / self.config.minimum_observations)
        if not rows:
            return BTCTransmissionState(
                timestamp=timestamp,
                trading_pair=pair,
                relationship_observations=0,
                relationship_confidence=0.0,
                relationship_valid=False,
                window_seconds=self.config.window_seconds,
                reasons=["insufficient synchronized BTC returns"],
            )
        age = max(0.0, timestamp_seconds - rows[-1].timestamp_seconds)
        btc_values = [row.btc_return for row in rows]
        asset_values = [row.asset_return for row in rows]
        btc_mean = statistics.fmean(btc_values)
        asset_mean = statistics.fmean(asset_values)
        btc_var = statistics.fmean((value - btc_mean) ** 2 for value in btc_values)
        asset_var = statistics.fmean((value - asset_mean) ** 2 for value in asset_values)
        reasons: list[str] = []
        if observations < self.config.minimum_observations:
            reasons.append(
                f"relationship observations {observations}/"
                f"{self.config.minimum_observations}"
            )
        if btc_var <= _EPSILON:
            reasons.append("BTC return variance is zero")
        if asset_var <= _EPSILON:
            reasons.append("asset return variance is zero")
        if age > self.config.stale_seconds:
            reasons.append(
                f"relationship stale at {age:.1f}s; limit {self.config.stale_seconds:.1f}s"
            )
        if btc_var <= _EPSILON or asset_var <= _EPSILON:
            return BTCTransmissionState(
                timestamp=timestamp,
                trading_pair=pair,
                relationship_observations=observations,
                relationship_confidence=confidence,
                relationship_valid=False,
                correlation_age_seconds=age,
                window_seconds=self.config.window_seconds,
                reasons=reasons,
            )
        covariance = statistics.fmean(
            (asset - asset_mean) * (btc - btc_mean)
            for asset, btc in zip(asset_values, btc_values, strict=True)
        )
        raw_beta = covariance / btc_var
        beta = max(-self.config.beta_clip, min(self.config.beta_clip, raw_beta))
        correlation = covariance / math.sqrt(asset_var * btc_var)
        correlation = max(-1.0, min(1.0, correlation))
        residual = math.sqrt(
            statistics.fmean(
                (asset - beta * btc) ** 2
                for asset, btc in zip(asset_values, btc_values, strict=True)
            )
        )
        valid = (
            observations >= self.config.minimum_observations
            and age <= self.config.stale_seconds
        )
        transmission = (
            min(
                self.config.transmission_max,
                confidence * abs(correlation) * abs(beta),
            )
            if valid
            else 0.0
        )
        reasons.extend(
            [
                f"{self.config.window_seconds:.0f}s BTC correlation {correlation:+.3f}",
                f"BTC beta {beta:+.3f} (raw {raw_beta:+.3f}; "
                f"clipped at +/-{self.config.beta_clip:.2f})",
                f"transmission coefficient {transmission:.3f}",
                f"residual volatility {residual:.6g}",
            ]
        )
        return BTCTransmissionState(
            timestamp=timestamp,
            trading_pair=pair,
            btc_correlation=correlation,
            btc_beta=beta,
            relationship_observations=observations,
            relationship_confidence=confidence,
            relationship_valid=valid,
            correlation_age_seconds=age,
            transmission_coefficient=transmission,
            residual_volatility=residual,
            window_seconds=self.config.window_seconds,
            reasons=list(dict.fromkeys(reasons)),
        )

    def update(
        self,
        prices: Mapping[str, Any],
        *,
        timestamp: Any,
    ) -> dict[str, BTCTransmissionState]:
        """Add one common-clock price frame and return current pair states."""

        timestamp_text, timestamp_seconds = _timestamp(timestamp)
        assert timestamp_seconds is not None
        btc_price = _finite(prices.get(BTC_TRADING_PAIR))
        if btc_price is not None and btc_price > 0:
            previous_btc = self._last_prices.get(BTC_TRADING_PAIR)
            self._last_prices[BTC_TRADING_PAIR] = (timestamp_seconds, btc_price)
        else:
            previous_btc = None
        for pair in self._returns:
            current_price = _finite(prices.get(pair))
            previous_asset = self._last_prices.get(pair)
            if current_price is None or current_price <= 0 or previous_btc is None:
                if current_price is not None and current_price > 0:
                    self._last_prices[pair] = (timestamp_seconds, current_price)
                continue
            previous_asset_price = previous_asset[1] if previous_asset else None
            asset_return = self._log_return(previous_asset_price, current_price)
            btc_return = self._log_return(previous_btc[1], btc_price)
            previous_asset_time = previous_asset[0] if previous_asset else timestamp_seconds
            previous_btc_time = previous_btc[0]
            synchronized = (
                asset_return is not None
                and btc_return is not None
                and abs(previous_asset_time - previous_btc_time) <= self.config.max_gap_seconds
            )
            if synchronized:
                self._returns[pair].append(
                    _RelationshipReturn(timestamp_seconds, asset_return, btc_return)
                )
            self._last_prices[pair] = (timestamp_seconds, current_price)
        return {
            pair: self._state(pair, timestamp_text, timestamp_seconds)
            for pair in self.trading_pairs
        }

    def window_sensitivity(self, trading_pair: str) -> dict[str, dict[str, float | int | None]]:
        """Report relationship stability for configured windows without tuning."""

        if trading_pair == BTC_TRADING_PAIR:
            return {
                str(window): {"correlation": 1.0, "beta": 1.0, "observations": 0}
                for window in self.config.sensitivity_windows_seconds
            }
        rows = list(self._returns.get(trading_pair, ()))
        result: dict[str, dict[str, float | int | None]] = {}
        for window in self.config.sensitivity_windows_seconds:
            selected = [
                row
                for row in rows
                if rows and rows[-1].timestamp_seconds - row.timestamp_seconds <= window
            ]
            if len(selected) < 2:
                result[str(window)] = {
                    "correlation": None,
                    "beta": None,
                    "observations": len(selected),
                }
                continue
            btc_values = [row.btc_return for row in selected]
            asset_values = [row.asset_return for row in selected]
            btc_mean = statistics.fmean(btc_values)
            asset_mean = statistics.fmean(asset_values)
            btc_var = statistics.fmean((value - btc_mean) ** 2 for value in btc_values)
            asset_var = statistics.fmean((value - asset_mean) ** 2 for value in asset_values)
            covariance = statistics.fmean(
                (asset - asset_mean) * (btc - btc_mean)
                for asset, btc in zip(asset_values, btc_values, strict=True)
            )
            result[str(window)] = {
                "correlation": (
                    covariance / math.sqrt(asset_var * btc_var)
                    if btc_var > _EPSILON and asset_var > _EPSILON
                    else None
                ),
                "beta": covariance / btc_var if btc_var > _EPSILON else None,
                "observations": len(selected),
            }
        return result


def _strip_global_iv(snapshot: Any) -> dict[str, Any]:
    """Keep local asset inputs while preventing non-BTC IV duplication."""

    if isinstance(snapshot, BaseModel):
        values = snapshot.model_dump(mode="python")
    elif isinstance(snapshot, Mapping):
        values = dict(snapshot)
    else:
        values = {key: getattr(snapshot, key) for key in dir(snapshot) if not key.startswith("_")}
    for key in (
        "atm_iv",
        "atm_call_iv",
        "atm_put_iv",
        "atm_strike",
        "atm_distance_pct",
        "option_instrument",
        "option_call_instrument",
        "option_put_instrument",
        "option_expiry",
        "option_expiry_dte",
        "option_data_timestamp",
        "option_data_age_seconds",
        "option_data_source",
        "option_environment",
    ):
        if key in values:
            values[key] = None
    values["iv_confidence"] = 0.0
    values["iv_data_available"] = False
    values["option_data_errors"] = []
    return values


@dataclass(frozen=True)
class MultiAssetStateResult:
    global_risk: GlobalRiskState
    relationships: dict[str, BTCTransmissionState]
    states: dict[str, AssetMarketState]
    enabled_markets: tuple[str, ...]
    disabled_markets: tuple[str, ...]


class MultiAssetStateEngine:
    """Run local Stage 2 state engines with one shared BTC options signal."""

    def __init__(self, config: MultiAssetConfig | None = None) -> None:
        self.config = config or MultiAssetConfig()
        self.global_risk_engine = GlobalRiskEngine(self.config.global_options)
        self.relationship_engine = RollingBTCRelationshipEngine(
            self.config.relationship,
            trading_pairs=self.config.enabled_markets,
        )
        self._engines = {
            pair: StateEngine(self.config.state) for pair in self.config.enabled_markets
        }
        self._volatility_states = {
            pair: VolatilityState.INITIALIZING for pair in self.config.enabled_markets
        }

    @property
    def options_update_count(self) -> int:
        return self.global_risk_engine.fetch_count

    def _invalid_state(
        self,
        pair: str,
        timestamp: str,
        global_risk: GlobalRiskState,
        relationship: BTCTransmissionState,
        reason: str,
    ) -> AssetMarketState:
        return AssetMarketState(
            timestamp=timestamp,
            trading_pair=pair,
            volatility_state=VolatilityState.INITIALIZING,
            direction_state=DirectionState.INITIALIZING,
            inventory_state=InventoryState.UNKNOWN,
            state_valid=False,
            confidence=0.0,
            market_environment=self.config.market_environment,
            global_risk_score=global_risk.global_risk_score,
            global_risk_regime=global_risk.global_risk_regime,
            global_risk_state=global_risk,
            btc_transmission=relationship,
            btc_correlation=relationship.btc_correlation,
            btc_beta=relationship.btc_beta,
            btc_transmission_coefficient=relationship.transmission_coefficient,
            relationship_confidence=relationship.relationship_confidence,
            reasons=[reason],
        )

    def update(
        self,
        snapshots: Mapping[str, Any],
        *,
        global_risk_state: GlobalRiskState | None = None,
    ) -> MultiAssetStateResult:
        """Process one common timestamp of independent asset snapshots."""

        btc_snapshot = snapshots.get(self.config.global_options.source_pair)
        if global_risk_state is None:
            if btc_snapshot is None:
                now = time.time()
                global_risk = self.global_risk_engine.update(
                    {"timestamp": now, "iv_data_available": False}, now_seconds=now
                )
            else:
                global_risk = self.global_risk_engine.update(btc_snapshot)
        else:
            global_risk = global_risk_state
        timestamps = [
            parse_timestamp(_read(snapshot, "timestamp"))
            for snapshot in snapshots.values()
        ]
        timestamp_seconds = max(
            (value for value in timestamps if value is not None), default=time.time()
        )
        timestamp = (
            datetime.fromtimestamp(timestamp_seconds, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        prices = {
            pair: _read(snapshot, "mid_price")
            for pair, snapshot in snapshots.items()
        }
        relationships = self.relationship_engine.update(prices, timestamp=timestamp)
        states: dict[str, AssetMarketState] = {}
        disabled: list[str] = []
        for pair in self.config.enabled_markets:
            relationship = relationships[pair]
            snapshot = snapshots.get(pair)
            if snapshot is None:
                states[pair] = self._invalid_state(
                    pair,
                    timestamp,
                    global_risk,
                    relationship,
                    "market snapshot unavailable; market disabled",
                )
                disabled.append(pair)
                continue
            local_snapshot = snapshot if pair == BTC_TRADING_PAIR else _strip_global_iv(snapshot)
            local_state = self._engines[pair].update(local_snapshot)
            local_rv = local_state.realized_volatility_ratio
            transmitted = None
            fallback = False
            reasons = list(local_state.reasons)
            if pair == BTC_TRADING_PAIR:
                final_score = local_state.volatility_score
                volatility_state = local_state.volatility_state
                global_used = global_risk.btc_iv_available and local_state.iv_ratio is not None
            else:
                if global_risk.btc_iv_available and global_risk.btc_iv_ratio is not None:
                    transmitted = global_risk.btc_iv_ratio * relationship.transmission_coefficient
                    global_used = (
                        transmitted is not None
                        and self.config.transmitted_btc_iv_weight > 0
                    )
                    if global_used:
                        reasons.append(
                            f"BTC IV {global_risk.btc_iv_ratio:.3f}x median * transmission "
                            f"{relationship.transmission_coefficient:.3f}"
                        )
                else:
                    global_used = False
                    fallback = True
                    reasons.append("BTC IV unavailable/stale; local RV fallback applied")
                final_score = calculate_combined_volatility_score(
                    local_rv,
                    transmitted if global_used else None,
                    realized_vol_weight=self.config.local_rv_weight,
                    iv_weight=self.config.transmitted_btc_iv_weight,
                )
                if fallback and self.config.global_options.missing_behavior == "defensive":
                    final_score = max(
                        final_score or 0.0,
                        self.config.mode.defensive_volatility_score,
                    )
                    reasons.append("global IV missing behavior=defensive")
                if fallback and self.config.global_options.missing_behavior == "pause":
                    reasons.append("global IV missing behavior=pause")
                    local_state = local_state.model_copy(update={"state_valid": False})
                if final_score is None:
                    volatility_state = VolatilityState.INITIALIZING
                else:
                    volatility_state = classify_volatility(
                        final_score,
                        self._volatility_states[pair],
                        enter_threshold=self.config.state.high_vol_enter_threshold,
                        exit_threshold=self.config.state.high_vol_exit_threshold,
                    )
                    self._volatility_states[pair] = volatility_state
            confidence = local_state.confidence
            if fallback:
                confidence = round(confidence * 0.80, 3)
            if pair != BTC_TRADING_PAIR and not relationship.relationship_valid:
                confidence = round(
                    confidence * (0.85 + 0.15 * relationship.relationship_confidence),
                    3,
                )
                reasons.append("BTC relationship is not yet valid; transmission held at zero")
            if final_score is not None:
                reasons.append(f"combined asset volatility score {final_score:.3f}")
            if relationship.btc_correlation is not None:
                reasons.append(
                    f"BTC correlation {relationship.btc_correlation:+.3f}; beta "
                    f"{relationship.btc_beta:+.3f}"
                )
            state_values = local_state.model_dump(mode="python")
            state_values.update(
                {
                    "volatility_state": volatility_state,
                    "volatility_score": final_score,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "state_valid": local_state.state_valid
                    and not (
                        fallback and self.config.global_options.missing_behavior == "pause"
                    ),
                    "market_environment": self.config.market_environment,
                    "local_realized_volatility_ratio": local_rv,
                    "transmitted_btc_iv_component": transmitted,
                    "btc_transmission_coefficient": relationship.transmission_coefficient,
                    "btc_correlation": relationship.btc_correlation,
                    "btc_beta": relationship.btc_beta,
                    "relationship_confidence": relationship.relationship_confidence,
                    "residual_volatility": relationship.residual_volatility,
                    "global_risk_score": global_risk.global_risk_score,
                    "global_risk_regime": global_risk.global_risk_regime,
                    "global_risk_state": global_risk,
                    "btc_transmission": relationship,
                    "global_iv_fallback": fallback,
                    "reasons": list(dict.fromkeys(reasons)),
                }
            )
            asset_state = AssetMarketState(**state_values)
            states[pair] = asset_state
        return MultiAssetStateResult(
            global_risk=global_risk,
            relationships=relationships,
            states=states,
            enabled_markets=tuple(
                pair for pair in self.config.enabled_markets if pair not in disabled
            ),
            disabled_markets=tuple(disabled),
        )


def _proposal_from_level(pair: str, level: Any) -> ProposedEntry:
    side = str(getattr(getattr(level, "side", None), "value", _read(level, "side", ""))).lower()
    index = int(_read(level, "level_index", 0))
    return ProposedEntry(
        trading_pair=pair,
        level_id=pair_level_id(pair, side, index),
        side="buy" if side == "buy" else "sell",
        quote_notional=max(0.0, _finite(_read(level, "quote_amount")) or 0.0),
    )


class PortfolioRiskGovernor:
    """Evaluate aggregate exposure and preserve risk-reducing actions."""

    def __init__(self, config: PortfolioRiskSettings | None = None) -> None:
        self.config = config or PortfolioRiskSettings()

    @staticmethod
    def _signed_beta_exposure(notional: float, beta: float) -> float:
        return notional * beta

    @staticmethod
    def _normalise_pending(
        pending_entries: Mapping[str, Any] | None,
    ) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for pair, raw in (pending_entries or {}).items():
            if isinstance(raw, Mapping):
                buy = _finite(raw.get("buy", raw.get("pending_buy_notional"))) or 0.0
                sell = _finite(raw.get("sell", raw.get("pending_sell_notional"))) or 0.0
                count = _finite(raw.get("count", raw.get("pending_count"))) or 0.0
            else:
                buy = sell = count = 0.0
            result[str(pair)] = {
                "buy": max(0.0, buy),
                "sell": max(0.0, sell),
                "count": max(0.0, count),
            }
        return result

    @staticmethod
    def _normalise_existing_entries(
        existing_entries: Mapping[str, Any] | None,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for pair, raw_levels in (existing_entries or {}).items():
            if not isinstance(raw_levels, Mapping):
                continue
            pair_levels: dict[str, dict[str, Any]] = {}
            for level_id, raw in raw_levels.items():
                if isinstance(raw, Mapping):
                    notional = _finite(
                        raw.get("notional", raw.get("quote_notional"))
                    ) or 0.0
                    side = str(raw.get("side", "")).lower() or None
                else:
                    notional = _finite(raw) or 0.0
                    side = None
                pair_levels[str(level_id)] = {
                    "notional": max(0.0, notional),
                    "side": side,
                }
            result[str(pair)] = pair_levels
        return result

    def _base_exposure(
        self,
        positions: Mapping[str, Any] | None,
        pending_entries: Mapping[str, Any] | None,
        betas: Mapping[str, Any] | None,
    ) -> tuple[dict[str, float], dict[str, dict[str, float]], float, float, float, float, float]:
        pending = self._normalise_pending(pending_entries)
        pairs = set(str(pair) for pair in (positions or {})) | set(pending)
        asset_exposure: dict[str, float] = {}
        gross = 0.0
        net = 0.0
        beta_equivalent = 0.0
        long_beta = 0.0
        short_beta = 0.0
        for pair in pairs:
            position = _finite((positions or {}).get(pair)) or 0.0
            buy = pending.get(pair, {}).get("buy", 0.0)
            sell = pending.get(pair, {}).get("sell", 0.0)
            effective = position + buy - sell
            exposure = abs(position) + buy + sell
            beta = _finite((betas or {}).get(pair))
            beta = 1.0 if beta is None else beta
            beta_value = self._signed_beta_exposure(effective, beta)
            asset_exposure[pair] = exposure
            gross += exposure
            net += effective
            beta_equivalent += beta_value
            long_beta += max(0.0, beta_value)
            short_beta += max(0.0, -beta_value)
        return asset_exposure, pending, gross, net, beta_equivalent, long_beta, short_beta

    def evaluate(
        self,
        *,
        timestamp: str,
        positions: Mapping[str, Any] | None = None,
        pending_entries: Mapping[str, Any] | None = None,
        proposed_entries: Mapping[str, Sequence[ProposedEntry | Mapping[str, Any]]] | None = None,
        betas: Mapping[str, Any] | None = None,
        active_executors: Mapping[str, Any] | None = None,
        existing_entries: Mapping[str, Any] | None = None,
        use_incremental_pending_exposure: bool = False,
    ) -> PortfolioRiskDecision:
        """Evaluate current plus pending exposure and proposed entry sides."""

        asset_exposure, pending, gross, net, beta_value, long_beta, short_beta = (
            self._base_exposure(positions, pending_entries, betas)
        )
        soft = (
            gross >= self.config.portfolio_max_gross_notional * self.config.soft_limit_ratio
            or max(long_beta, short_beta) >= self.config.portfolio_soft_beta_exposure
        )
        hard = (
            gross >= self.config.portfolio_max_gross_notional
            or abs(beta_value) >= self.config.portfolio_hard_beta_exposure
            or long_beta >= self.config.portfolio_max_long_beta_exposure
            or short_beta >= self.config.portfolio_max_short_beta_exposure
        )
        blocked_sides: dict[str, list[str]] = {}
        risk_reducing: dict[str, list[str]] = {}
        allowed_ids: dict[str, list[str]] = {}
        blocked_ids: dict[str, list[str]] = {}
        risk_delta_audit: list[dict[str, Any]] = []
        reasons: list[str] = []
        existing_by_pair = self._normalise_existing_entries(existing_entries)
        current_positions = {
            str(pair): _finite(value) or 0.0
            for pair, value in (positions or {}).items()
        }
        for pair in set(asset_exposure) | set(pending):
            current_positions[pair] = (
                current_positions.get(pair, 0.0)
                + pending.get(pair, {}).get("buy", 0.0)
                - pending.get(pair, {}).get("sell", 0.0)
            )
        working_asset_exposure = dict(asset_exposure)
        working_gross = gross
        working_beta = beta_value
        active_executor_input_count = sum(
            int(_finite(value) or 0.0) for value in (active_executors or {}).values()
        )
        pending_executor_count = sum(
            int(values.get("count", 0.0)) for values in pending.values()
        )
        active_pending_executor_overlap_count = sum(
            min(
                int(_finite((active_executors or {}).get(pair)) or 0.0),
                int(values.get("count", 0.0)),
            )
            for pair, values in pending.items()
        )
        working_asset_executors = {
            pair: int(_finite((active_executors or {}).get(pair)) or 0.0)
            + int(pending.get(pair, {}).get("count", 0.0))
            for pair in set(asset_exposure) | set(pending) | set(active_executors or {})
        }
        working_portfolio_executors = sum(working_asset_executors.values())
        pre_proposal_active_executors = working_portfolio_executors
        executor_cap_triggered = False
        if use_incremental_pending_exposure:
            proposed_ids_by_pair = {
                str(pair): {
                    entry.level_id
                    for raw in raw_entries
                    for entry in [
                        raw
                        if isinstance(raw, ProposedEntry)
                        else ProposedEntry.model_validate(raw)
                    ]
                }
                for pair, raw_entries in (proposed_entries or {}).items()
            }
            for pair, level_rows in existing_by_pair.items():
                for level_id, existing in level_rows.items():
                    if level_id in proposed_ids_by_pair.get(pair, set()):
                        continue
                    released = float(existing.get("notional", 0.0) or 0.0)
                    if released <= _EPSILON:
                        continue
                    side = str(existing.get("side") or "").lower()
                    beta = _finite((betas or {}).get(pair))
                    beta = 1.0 if beta is None else beta
                    before_asset = working_asset_exposure.get(pair, 0.0)
                    before_gross = working_gross
                    working_asset_exposure[pair] = max(0.0, before_asset - released)
                    working_gross = max(0.0, working_gross - released)
                    if side == "buy":
                        working_beta -= released * beta
                    elif side == "sell":
                        working_beta += released * beta
                    working_asset_executors[pair] = max(
                        0, working_asset_executors.get(pair, 0) - 1
                    )
                    working_portfolio_executors = max(0, working_portfolio_executors - 1)
                    risk_delta_audit.append(
                        {
                            "timestamp": timestamp,
                            "trading_pair": pair,
                            "level_id": level_id,
                            "side": side or None,
                            "action": "CANCEL_RELEASE",
                            "existing_notional": released,
                            "proposed_notional": 0.0,
                            "notional_delta": -released,
                            "new_executor": False,
                            "risk_reducing": True,
                            "allowed": True,
                            "blocked_reason": None,
                            "asset_exposure_before": before_asset,
                            "asset_exposure_after": working_asset_exposure[pair],
                            "gross_exposure_before": before_gross,
                            "gross_exposure_after": working_gross,
                            "beta_exposure_after": working_beta,
                        }
                    )
        for pair, raw_entries in (proposed_entries or {}).items():
            normalised: list[ProposedEntry] = []
            for raw in raw_entries:
                entry = raw if isinstance(raw, ProposedEntry) else ProposedEntry.model_validate(raw)
                normalised.append(entry)
            allowed: list[str] = []
            blocked: list[str] = []
            for entry in normalised:
                filled_position = _finite((positions or {}).get(pair)) or 0.0
                position = (
                    filled_position
                    if use_incremental_pending_exposure
                    else current_positions.get(pair, 0.0)
                )
                beta = _finite((betas or {}).get(pair))
                beta = 1.0 if beta is None else beta
                existing = existing_by_pair.get(str(pair), {}).get(entry.level_id)
                existing_notional = (
                    float(existing.get("notional", 0.0) or 0.0)
                    if existing is not None
                    else 0.0
                )
                delta = (
                    entry.quote_notional - existing_notional
                    if use_incremental_pending_exposure and existing is not None
                    else entry.quote_notional
                )
                signed_delta = delta * beta * (1.0 if entry.side == "buy" else -1.0)
                risk_reducing_entry = (
                    (filled_position > _EPSILON and entry.side == "sell")
                    or (filled_position < -_EPSILON and entry.side == "buy")
                    or delta <= _EPSILON
                )
                if risk_reducing_entry:
                    risk_reducing.setdefault(pair, []).append(entry.side)
                candidate_asset = (
                    working_asset_exposure.get(pair, abs(position))
                    + delta
                )
                candidate_asset = max(0.0, candidate_asset)
                candidate_gross = max(0.0, working_gross + delta)
                candidate_beta = working_beta + signed_delta
                candidate_long = max(0.0, candidate_beta)
                candidate_short = max(0.0, -candidate_beta)
                is_risk_increasing = not risk_reducing_entry
                block_reason = ""
                if (
                    candidate_asset > self.config.per_asset_max_position_notional
                    and is_risk_increasing
                ):
                    block_reason = "per-asset position notional limit"
                elif (
                    candidate_gross > self.config.portfolio_max_gross_notional
                    and is_risk_increasing
                ):
                    block_reason = "portfolio gross notional hard limit"
                elif (
                    candidate_long > self.config.portfolio_max_long_beta_exposure
                    and is_risk_increasing
                ):
                    block_reason = "portfolio long BTC-beta hard limit"
                elif (
                    candidate_short > self.config.portfolio_max_short_beta_exposure
                    and is_risk_increasing
                ):
                    block_reason = "portfolio short BTC-beta hard limit"
                elif (
                    max(candidate_long, candidate_short) > self.config.portfolio_soft_beta_exposure
                    and is_risk_increasing
                ):
                    block_reason = "portfolio soft BTC-beta limit"
                elif (
                    working_asset_executors.get(pair, 0)
                    >= self.config.max_active_executors_per_asset
                    and is_risk_increasing
                ):
                    block_reason = "per-asset active executor limit"
                elif (
                    working_portfolio_executors
                    >= self.config.max_active_executors_portfolio
                    and is_risk_increasing
                ):
                    block_reason = "portfolio active executor limit"
                if block_reason:
                    blocked.append(entry.level_id)
                    sides = blocked_sides.setdefault(pair, [])
                    if entry.side not in sides:
                        sides.append(entry.side)
                    reasons.append(f"{pair} {entry.side} blocked: {block_reason}")
                else:
                    allowed.append(entry.level_id)
                    working_asset_exposure[pair] = candidate_asset
                    working_gross = candidate_gross
                    working_beta = candidate_beta
                    is_new_executor = existing is None
                    if is_new_executor:
                        working_asset_executors[pair] = working_asset_executors.get(pair, 0) + 1
                        working_portfolio_executors += 1
                if block_reason:
                    is_new_executor = False
                if existing is None:
                    action = "CREATE"
                elif delta > _EPSILON:
                    action = "RESIZE_UP"
                elif delta < -_EPSILON:
                    action = "RESIZE_DOWN"
                else:
                    action = "KEEP"
                risk_delta_audit.append(
                    {
                        "timestamp": timestamp,
                        "trading_pair": pair,
                        "level_id": entry.level_id,
                        "side": entry.side,
                        "action": action,
                        "existing_notional": existing_notional if existing is not None else 0.0,
                        "proposed_notional": entry.quote_notional,
                        "notional_delta": delta,
                        "new_executor": is_new_executor,
                        "risk_reducing": risk_reducing_entry,
                        "allowed": not bool(block_reason),
                        "blocked_reason": block_reason or None,
                        "asset_exposure_before": candidate_asset - delta,
                        "asset_exposure_after": candidate_asset,
                        "gross_exposure_before": candidate_gross - delta,
                        "gross_exposure_after": candidate_gross,
                        "beta_exposure_after": candidate_beta,
                    }
                )
                if block_reason and "executor" in block_reason:
                    executor_cap_triggered = True
            if allowed:
                allowed_ids[pair] = allowed
            if blocked:
                blocked_ids[pair] = blocked
        blocked_pairs = sorted(blocked_sides)
        global_pause = hard and not risk_reducing
        if soft:
            reasons.append(
                f"portfolio soft limit active: gross {gross:.4f}, beta {beta_value:+.4f}"
            )
        if hard:
            reasons.append(
                f"portfolio hard limit active: gross {gross:.4f}, beta {beta_value:+.4f}"
            )
        risk_ratio = max(
            [
                gross / self.config.portfolio_max_gross_notional,
                abs(beta_value) / self.config.portfolio_hard_beta_exposure,
                long_beta / self.config.portfolio_max_long_beta_exposure,
                short_beta / self.config.portfolio_max_short_beta_exposure,
            ],
            default=0.0,
        )
        return PortfolioRiskDecision(
            timestamp=timestamp,
            gross_notional=gross,
            net_notional=net,
            btc_beta_equivalent_exposure=beta_value,
            long_beta_exposure=long_beta,
            short_beta_exposure=short_beta,
            per_asset_exposure=asset_exposure,
            portfolio_risk_ratio=risk_ratio,
            soft_limit_triggered=soft,
            hard_limit_triggered=hard,
            blocked_pairs=blocked_pairs,
            blocked_sides=blocked_sides,
            global_pause_new_exposure=global_pause,
            risk_reducing_sides={pair: sorted(set(sides)) for pair, sides in risk_reducing.items()},
            allowed_level_ids=allowed_ids,
            blocked_level_ids=blocked_ids,
            active_executors=working_portfolio_executors,
            per_asset_active_executors=working_asset_executors,
            active_executor_input_count=active_executor_input_count,
            pending_executor_count=pending_executor_count,
            active_pending_executor_overlap_count=active_pending_executor_overlap_count,
            pre_proposal_active_executors=pre_proposal_active_executors,
            executor_cap_triggered=executor_cap_triggered,
            risk_delta_audit=risk_delta_audit,
            reasons=list(dict.fromkeys(reasons)),
        )

    def route_plans(
        self,
        plans: Mapping[str, GridPlan],
        *,
        positions: Mapping[str, Any] | None = None,
        pending_entries: Mapping[str, Any] | None = None,
        betas: Mapping[str, Any] | None = None,
        active_executors: Mapping[str, Any] | None = None,
        existing_entries: Mapping[str, Any] | None = None,
        execution_status_by_market: Mapping[str, str] | None = None,
        use_incremental_pending_exposure: bool = False,
    ) -> tuple[PortfolioRiskDecision, dict[str, PortfolioPlanRoute]]:
        """Return pair-scoped allowed/blocked levels for later Stage 5 routing."""

        proposals: dict[str, list[ProposedEntry]] = {}
        for pair, plan in plans.items():
            proposals[pair] = [
                _proposal_from_level(pair, level)
                for level in (*plan.buy_levels, *plan.sell_levels)
            ]
        timestamp = next((plan.timestamp for plan in plans.values()), "")
        decision = self.evaluate(
            timestamp=timestamp,
            positions=positions,
            pending_entries=pending_entries,
            proposed_entries=proposals,
            betas=betas,
            active_executors=active_executors,
            existing_entries=existing_entries,
            use_incremental_pending_exposure=use_incremental_pending_exposure,
        )
        routes: dict[str, PortfolioPlanRoute] = {}
        for pair, entries in proposals.items():
            all_ids = [entry.level_id for entry in entries]
            allowed_ids = list(decision.allowed_level_ids.get(pair, []))
            blocked_ids = list(decision.blocked_level_ids.get(pair, []))
            status = str((execution_status_by_market or {}).get(pair, "EXECUTION_ENABLED"))
            if status != "EXECUTION_ENABLED":
                blocked_ids.extend(level_id for level_id in all_ids if level_id not in blocked_ids)
                allowed_ids = []
                decision.allowed_level_ids.pop(pair, None)
                decision.blocked_level_ids[pair] = list(dict.fromkeys(blocked_ids))
                decision.blocked_pairs = sorted(set([*decision.blocked_pairs, pair]))
                decision.blocked_sides[pair] = sorted(
                    set(decision.blocked_sides.get(pair, []))
                    | {entry.side for entry in entries}
                )
                decision.reasons.append(f"{pair} {status}: execution route disabled")
            allowed = tuple(dict.fromkeys(allowed_ids))
            blocked = tuple(dict.fromkeys(blocked_ids))
            routes[pair] = PortfolioPlanRoute(
                trading_pair=pair,
                allowed_level_ids=allowed,
                blocked_level_ids=blocked,
                blocked_sides=tuple(decision.blocked_sides.get(pair, [])),
                executor_namespace=f"{pair}::",
            )
            if not allowed and not blocked and all_ids and status == "EXECUTION_ENABLED":
                routes[pair] = routes[pair].model_copy(update={"allowed_level_ids": tuple(all_ids)})
        return decision, routes


@dataclass(frozen=True)
class MultiAssetCycle:
    """One complete shared-risk -> local-state -> plan -> governor tick."""

    timestamp: str
    global_risk: GlobalRiskState
    relationships: dict[str, BTCTransmissionState]
    states: dict[str, AssetMarketState]
    decisions: dict[str, AssetGridModeDecision]
    plans: dict[str, GridPlan]
    portfolio_risk: PortfolioRiskDecision
    routes: dict[str, PortfolioPlanRoute]
    enabled_markets: tuple[str, ...]
    disabled_markets: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "global_risk": self.global_risk.model_dump(mode="json"),
            "relationships": {
                pair: value.model_dump(mode="json") for pair, value in self.relationships.items()
            },
            "states": {pair: value.model_dump(mode="json") for pair, value in self.states.items()},
            "decisions": {
                pair: value.model_dump(mode="json") for pair, value in self.decisions.items()
            },
            "plans": {pair: value.to_record() for pair, value in self.plans.items()},
            "portfolio_risk": self.portfolio_risk.model_dump(mode="json"),
            "routes": {pair: value.model_dump(mode="json") for pair, value in self.routes.items()},
            "enabled_markets": list(self.enabled_markets),
            "disabled_markets": list(self.disabled_markets),
        }


class MultiAssetCoordinator:
    """Compose independent local engines and the shared portfolio governor."""

    def __init__(self, config: MultiAssetConfig | None = None) -> None:
        self.config = config or MultiAssetConfig()
        self.state_engine = MultiAssetStateEngine(self.config)
        self._selectors = {
            pair: ModeSelector(self.config.mode) for pair in self.config.enabled_markets
        }
        self._plan_engines = {
            pair: GridParameterEngine(self.config.grid) for pair in self.config.enabled_markets
        }
        self.governor = PortfolioRiskGovernor(self.config.portfolio_risk)

    @property
    def options_update_count(self) -> int:
        return self.state_engine.options_update_count

    @property
    def plan_versions(self) -> dict[str, int]:
        return {pair: engine.plan_version for pair, engine in self._plan_engines.items()}

    def update(
        self,
        snapshots: Mapping[str, Any],
        *,
        positions: Mapping[str, Any] | None = None,
        pending_entries: Mapping[str, Any] | None = None,
        global_risk_state: GlobalRiskState | None = None,
        active_executors: Mapping[str, Any] | None = None,
        existing_entries: Mapping[str, Any] | None = None,
    ) -> MultiAssetCycle:
        state_result = self.state_engine.update(
            snapshots,
            global_risk_state=global_risk_state,
        )
        decisions: dict[str, AssetGridModeDecision] = {}
        plans: dict[str, GridPlan] = {}
        for pair in self.config.enabled_markets:
            state = state_result.states[pair]
            base_decision = self._selectors[pair].update(state)
            decision_values = base_decision.model_dump(mode="python")
            decision_values.update(
                {
                    "market_environment": self.config.market_environment,
                    "global_risk_regime": state.global_risk_regime,
                    "portfolio_risk_scope": "asset",
                }
            )
            decision = AssetGridModeDecision(**decision_values)
            decisions[pair] = decision
            snapshot = snapshots.get(pair)
            if snapshot is None:
                snapshot = {
                    "timestamp": state.timestamp,
                    "trading_pair": pair,
                    "data_valid": False,
                    "market_environment": self.config.market_environment,
                }
            plan = self._plan_engines[pair].build(snapshot, state, decision)
            plan = plan.model_copy(update={"market_environment": self.config.market_environment})
            plans[pair] = plan
        beta_values = {
            pair: state.btc_beta if state.btc_beta is not None else 1.0
            for pair, state in state_result.states.items()
        }
        derived_positions = (
            positions
            if positions is not None
            else {
                pair: state.position_notional or 0.0
                for pair, state in state_result.states.items()
            }
        )
        portfolio, routes = self.governor.route_plans(
            plans,
            positions=derived_positions,
            pending_entries=pending_entries,
            betas=beta_values,
            active_executors=active_executors,
            existing_entries=existing_entries,
            execution_status_by_market=self.config.execution_status_by_market,
            use_incremental_pending_exposure=(
                self.config.use_incremental_pending_exposure_for_reconciliation
            ),
        )
        timestamp = state_result.global_risk.timestamp
        portfolio = portfolio.model_copy(update={"timestamp": timestamp})
        return MultiAssetCycle(
            timestamp=timestamp,
            global_risk=state_result.global_risk,
            relationships=state_result.relationships,
            states=state_result.states,
            decisions=decisions,
            plans=plans,
            portfolio_risk=portfolio,
            routes=routes,
            enabled_markets=state_result.enabled_markets,
            disabled_markets=state_result.disabled_markets,
        )


def validate_market_availability(
    configured_markets: Iterable[str], available_markets: Iterable[str]
) -> dict[str, Any]:
    """Validate configured symbols without disabling unrelated markets."""

    configured = tuple(dict.fromkeys(str(pair) for pair in configured_markets))
    available = set(str(pair) for pair in available_markets)
    enabled = tuple(pair for pair in configured if pair in available)
    disabled = tuple(pair for pair in configured if pair not in available)
    return {
        "configured_markets": list(configured),
        "available_markets": sorted(available),
        "enabled_markets": list(enabled),
        "disabled_markets": list(disabled),
        "global_options_source_available": BTC_TRADING_PAIR in available,
        "reasons": [
            f"{pair} unavailable; disabled independently" for pair in disabled
        ],
    }


__all__ = [
    "AssetGridModeDecision",
    "AssetMarketState",
    "BTCTransmissionState",
    "BTC_TRADING_PAIR",
    "GlobalRiskEngine",
    "GlobalRiskRegime",
    "GlobalRiskSettings",
    "GlobalRiskState",
    "MultiAssetConfig",
    "MultiAssetCoordinator",
    "MultiAssetCycle",
    "MultiAssetStateEngine",
    "MultiAssetStateResult",
    "PortfolioPlanRoute",
    "PortfolioRiskDecision",
    "PortfolioRiskGovernor",
    "PortfolioRiskSettings",
    "ProposedEntry",
    "RelationshipSettings",
    "RollingBTCRelationshipEngine",
    "SUPPORTED_TRADING_PAIRS",
    "pair_level_id",
    "validate_market_availability",
]
