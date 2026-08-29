"""Mainnet shadow execution and paper-account validation.

This module is the explicit adapter boundary for the validation stage.  The
strategy still produces the same Stage 2--4 states and Stage 4 ``GridPlan``;
only the final order adapter is replaced by virtual orders.  The module has no
private Derive client and does not expose an order API.

The public-data source at the bottom of the file is intentionally separate
from :class:`ShadowExecutionEngine`.  This makes the zero-mutation guarantee
testable with a fully synthetic frame and keeps credentials out of shadow
sessions.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .derive_public import DerivePublicClient
from .environment import MAINNET_OPTIONS_API_BASE_URL, environment_profile
from .mode_selector import ModeSelectorConfig
from .multi_asset import SUPPORTED_TRADING_PAIRS, MultiAssetCycle
from .options_iv import DeriveOptionsProvider, OptionsVolatilitySnapshot
from .stage12c import (
    CANCEL_TAXONOMY,
    LIFECYCLE_EVENTS,
    RiskEpisodeTracker,
    classify_cancel_reason,
    normalize_risk_reason,
)
from .stage12e import canonical_trade_rows, normalize_timestamp
from .stage13 import Stage13StabilityConfig, effective_asset_status

try:  # The integration package is available from the repository root.
    from integrations.hummingbot.derive_adaptive_grid.execution_logic import (
        ActiveLevel,
        ExecutionPolicy,
        ExecutionSide,
        RuntimeHealth,
        TradingRuleView,
        parse_grid_plan,
        reconcile_grid_plan,
    )
except ImportError as exc:  # pragma: no cover - protects installed-package misuse
    raise ImportError(
        "shadow validation must run from the project root so the Stage 5 integration is visible"
    ) from exc


SHADOW_MODE_EXCHANGE_MUTATION_BLOCKED = "SHADOW_MODE_EXCHANGE_MUTATION_BLOCKED"
SHADOW_ENVIRONMENT_CONSISTENCY_PASS = "SHADOW ENVIRONMENT CONSISTENCY: PASS"
SHADOW_BANNER = "DERIVE MAINNET DATA / SHADOW ORDERS / NO REAL FUNDS AT RISK"
_EPSILON = Decimal("1e-12")
_MUTATING_METHODS = frozenset(
    {
        "buy",
        "sell",
        "create_order",
        "place_order",
        "submit_order",
        "cancel",
        "cancel_order",
        "cancel_all",
        "cancel_all_orders",
        "edit_order",
        "modify_order",
        "market_order",
        "withdraw",
        "transfer",
        "set_leverage",
        "set_position_mode",
        "update_account",
    }
)
_READ_ONLY_METHODS = frozenset(
    {
        "ping",
        "get_order_book",
        "get_order_book_snapshot",
        "get_ticker",
        "get_trading_rules",
        "get_balances",
        "get_balance",
        "get_positions",
        "get_position",
        "get_status",
    }
)


class ShadowModeExchangeMutationBlocked(RuntimeError):
    """Raised before any private exchange mutation can be made."""

    def __init__(self, method: str) -> None:
        super().__init__(SHADOW_MODE_EXCHANGE_MUTATION_BLOCKED)
        self.method = method


class ShadowEnvironmentError(ValueError):
    """Raised when a shadow session contains a non-mainnet or mixed stream."""


class ShadowOrderStatus(StrEnum):
    PENDING = "PENDING"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    CLOSE_RESTING = "CLOSE_RESTING"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"


class ShadowLifecycleState(StrEnum):
    """Observable order lifecycle states, independent of Hummingbot status."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    RESTING = "RESTING"
    NEVER_RESTED_REJECTED = "NEVER_RESTED_REJECTED"
    CANCELLED_AFTER_RESTING = "CANCELLED_AFTER_RESTING"
    FILLED_AFTER_RESTING = "FILLED_AFTER_RESTING"


class ShadowFillModel(StrEnum):
    CONSERVATIVE_TRADE_THROUGH = "conservative_trade_through"
    CONSERVATIVE_CROSS_THROUGH = "conservative_trade_through"
    TRADE_PRINT = "trade_print"
    TRADE_BASED = "trade_print"
    ESTIMATED_QUEUE = "estimated_queue"
    QUEUE_ESTIMATE = "estimated_queue"
    TOUCH_OPTIMISTIC = "touch_optimistic"


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _epoch(value: Any) -> float | None:
    normalized, _ = normalize_timestamp(value)
    return normalized


def _iso(seconds: float) -> str:
    return (
        datetime.fromtimestamp(seconds, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _quantize_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


class ShadowConfig(BaseModel):
    """Explicit, fail-closed configuration for one paper session."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    execution_mode: str = "SHADOW"
    execution_backend: str = "SHADOW"
    market_environment: str = "mainnet"
    execution_enabled: bool = False
    allow_mainnet_trading: bool = False
    starting_equity_usdc: float = Field(default=800.0, gt=0)
    markets: tuple[str, ...] = SUPPORTED_TRADING_PAIRS
    enabled_markets: tuple[str, ...] = ("ETH-USDC", "SOL-USDC", "HYPE-USDC")
    fill_model: ShadowFillModel = ShadowFillModel.CONSERVATIVE_TRADE_THROUGH
    maker_fee_bps: float | None = Field(default=None, ge=0)
    fee_model: str = "unknown"
    persistence: bool = True
    dashboard_refresh_seconds: float = Field(default=3.0, gt=0)
    session_duration_seconds: float = Field(default=48 * 3600.0, gt=0)
    order_scale: float = Field(default=1.0, gt=0)
    min_order_size: float = Field(default=0.0, ge=0)
    amount_increment: float = Field(default=0.0, ge=0)
    price_increment: float = Field(default=0.0, ge=0)
    min_notional_size: float = Field(default=0.0, ge=0)
    max_total_position_notional: float = Field(default=1100.0, gt=0)
    max_side_position_notional: float = Field(default=1100.0, gt=0)
    max_active_grid_levels: int = Field(default=6, ge=1)
    max_active_executors: int = Field(default=6, ge=1)
    execution_max_levels_per_side: int = Field(default=1, ge=1)
    minimum_order_lifetime_seconds: float = Field(default=120.0, ge=0)
    minimum_replace_interval_seconds: float = Field(default=60.0, ge=0)
    maximum_order_lifetime_seconds: float = Field(default=900.0, gt=0)
    refresh_price_tolerance_bps: float = Field(default=12.0, ge=0)
    refresh_amount_tolerance_pct: float = Field(default=0.15, ge=0)
    collateral_safety_buffer_pct: float = Field(default=0.20, ge=0, lt=1)
    leverage: float = Field(default=1.0, gt=0)
    post_only: bool = True
    event_path: str = "data/shadow_execution_events.jsonl"
    sqlite_path: str = "data/shadow_execution.sqlite3"
    report_root: str = "reports/shadow_sessions"
    strategy_profile: str = "configs/competition_800_usdc.yml"
    baseline_config_version: str = "stage12-mainnet-shadow-baseline-v1"
    self_tuning_mode: str = "SUGGEST_ONLY"
    checkpoint_interval_seconds: float = Field(default=300.0, gt=0)
    market_data_stale_seconds: float = Field(default=15.0, gt=0)
    pnl_reconciliation_tolerance: float = Field(default=1e-6, gt=0)
    minimum_fill_samples: int = Field(default=5, ge=1)
    minimum_markout_samples: int = Field(default=5, ge=1)
    minimum_cycle_samples: int = Field(default=3, ge=1)
    minimum_data_coverage_pct: float = Field(default=95.0, ge=0, le=100)
    high_cancel_churn_min_creates: int = Field(default=5, ge=1)
    high_cancel_churn_ratio: float = Field(default=2.0, ge=0)
    high_cancel_churn_lifetime_seconds: float = Field(default=30.0, ge=0)
    touch_sensitivity_medium_pct: float = Field(default=25.0, ge=0)
    touch_sensitivity_high_pct: float = Field(default=100.0, ge=0)
    inventory_soft_threshold_ratio: float = Field(default=0.50, ge=0)
    inventory_defensive_threshold_ratio: float = Field(default=0.75, ge=0)
    inventory_hard_threshold_ratio: float = Field(default=1.00, ge=0)
    stage13: Stage13StabilityConfig = Field(default_factory=Stage13StabilityConfig)

    @model_validator(mode="after")
    def validate_shadow_boundary(self) -> ShadowConfig:
        if self.execution_mode.upper() != "SHADOW":
            raise ValueError("shadow config requires execution_mode=SHADOW")
        if self.execution_backend.upper() != "SHADOW":
            raise ValueError("shadow config requires execution_backend=SHADOW")
        if self.market_environment.lower() != "mainnet":
            raise ValueError("shadow config requires market_environment=mainnet")
        if self.execution_enabled or self.allow_mainnet_trading:
            raise ValueError("shadow mode never enables real execution or mainnet trading")
        if not self.post_only:
            raise ValueError("shadow validation requires post_only=true")
        if not set(self.markets).issubset(set(SUPPORTED_TRADING_PAIRS)):
            raise ValueError("shadow markets must use the supported Derive perpetual pairs")
        if "BTC-USDC" not in self.markets:
            raise ValueError("BTC-USDC must remain the shared options signal market")
        if not set(self.enabled_markets).issubset(set(self.markets)):
            raise ValueError("enabled_markets must be a subset of markets")
        if self.fee_model == "explicit" and self.maker_fee_bps is None:
            raise ValueError("explicit fee_model requires maker_fee_bps")
        if self.fee_model not in {"unknown", "explicit"}:
            raise ValueError("fee_model must be unknown or explicit")
        if self.self_tuning_mode.upper() not in {"SUGGEST_ONLY", "OFF"}:
            raise ValueError("self_tuning_mode must be SUGGEST_ONLY or OFF")
        if not (
            self.inventory_soft_threshold_ratio
            <= self.inventory_defensive_threshold_ratio
            <= self.inventory_hard_threshold_ratio
        ):
            raise ValueError("inventory thresholds must be ordered soft <= defensive <= hard")
        if self.maximum_order_lifetime_seconds < self.minimum_order_lifetime_seconds:
            raise ValueError("maximum_order_lifetime_seconds must exceed minimum lifetime")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> ShadowConfig:
        raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"shadow config must be a mapping: {path}")
        shadow = raw.get("shadow", raw)
        if not isinstance(shadow, dict):
            raise ValueError("shadow config section must be a mapping")
        return cls.model_validate(shadow)

    @property
    def config_hash(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the effective shadow configuration."""

        return _json_value(self.model_dump(mode="python"))

    @property
    def strategy_config_hash(self) -> str | None:
        """Hash the referenced strategy profile separately from shadow controls."""

        path = Path(self.strategy_profile).expanduser()
        if not path.is_absolute() and not path.is_file():
            path = Path(__file__).resolve().parents[2] / path
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    @property
    def fees_known(self) -> bool:
        return self.fee_model == "explicit" and self.maker_fee_bps is not None

    def execution_policy(self) -> ExecutionPolicy:
        return ExecutionPolicy(
            execution_max_levels_per_side=self.execution_max_levels_per_side,
            testnet_order_scale=Decimal(str(self.order_scale)),
            max_total_position_notional=Decimal(str(self.max_total_position_notional)),
            max_side_position_notional=Decimal(str(self.max_side_position_notional)),
            max_active_grid_levels=self.max_active_grid_levels,
            max_active_executors=self.max_active_executors,
            minimum_order_lifetime_seconds=self.minimum_order_lifetime_seconds,
            minimum_replace_interval_seconds=self.minimum_replace_interval_seconds,
            maximum_order_lifetime_seconds=self.maximum_order_lifetime_seconds,
            refresh_price_tolerance_bps=Decimal(str(self.refresh_price_tolerance_bps)),
            refresh_amount_tolerance_pct=Decimal(str(self.refresh_amount_tolerance_pct)),
            collateral_safety_buffer_pct=Decimal(str(self.collateral_safety_buffer_pct)),
            leverage=Decimal(str(self.leverage)),
            post_only=self.post_only,
            environment="mainnet",
            execution_mode="SHADOW",
            preserve_existing_quotes_during_soft_pause_confirmation=(
                self.stage13.enabled
                and self.stage13.preserve_existing_quotes_during_soft_pause_confirmation
            ),
            suppress_new_entries_during_soft_pause_confirmation=(
                self.stage13.enabled
                and self.stage13.suppress_new_entries_during_soft_pause_confirmation
            ),
        )


@dataclass(frozen=True)
class ShadowEnvironmentStatus:
    """Result of the all-stream mainnet consistency guard."""

    consistent: bool
    environments: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "environments": list(self.environments),
            "reasons": list(self.reasons),
            "message": SHADOW_ENVIRONMENT_CONSISTENCY_PASS if self.consistent else "FAIL",
        }


def check_shadow_environment(streams: Iterable[Any]) -> ShadowEnvironmentStatus:
    """Require every supplied stream, including options, to be Derive mainnet."""

    values: list[str] = []
    reasons: list[str] = []
    for stream in streams:
        if isinstance(stream, Mapping):
            environment = stream.get("market_environment", stream.get("environment"))
            option_environment = stream.get("option_environment")
            option_snapshot = stream.get("option_snapshot")
        else:
            environment = getattr(
                stream, "market_environment", getattr(stream, "environment", None)
            )
            option_environment = getattr(stream, "option_environment", None)
            option_snapshot = getattr(stream, "option_snapshot", None)
        option_snapshot_environment = (
            option_snapshot.get("environment")
            if isinstance(option_snapshot, Mapping)
            else getattr(option_snapshot, "environment", None)
        )
        for label, value in (
            ("market", environment),
            ("options", option_environment),
            ("options snapshot", option_snapshot_environment),
        ):
            if value is None:
                continue
            normalized = str(value).strip().lower()
            values.append(normalized)
            if normalized != "mainnet":
                reasons.append(f"{label} stream is {normalized or 'unknown'}")
    if not values:
        reasons.append("no environment-tagged market data streams supplied")
    if any(value != "mainnet" for value in values):
        return ShadowEnvironmentStatus(False, tuple(sorted(set(values))), tuple(reasons))
    return ShadowEnvironmentStatus(True, tuple(sorted(set(values))), ())


def require_shadow_environment(streams: Iterable[Any]) -> ShadowEnvironmentStatus:
    status = check_shadow_environment(streams)
    if not status.consistent:
        raise ShadowEnvironmentError("; ".join(status.reasons) or "shadow environment mismatch")
    return status


class ShadowExchangeMutationGuard:
    """Wrap a client so accidental private mutations fail before invocation."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.blocked_attempts = 0

    def __getattr__(self, name: str) -> Any:
        if name in _MUTATING_METHODS or name not in _READ_ONLY_METHODS:

            def blocked(*args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                self.blocked_attempts += 1
                raise ShadowModeExchangeMutationBlocked(name)

            return blocked
        return getattr(self._client, name)


@dataclass(frozen=True)
class ShadowTrade:
    """Public trade evidence used only by the paper fill model."""

    timestamp: float
    price: float
    amount: float
    aggressor_side: str | None = None
    trade_id: str | None = None

    def to_record(self) -> dict[str, Any]:
        return _json_value(self.__dict__)


@dataclass(frozen=True)
class ShadowMarketFrame:
    """One timestamped, public-only Derive market frame."""

    timestamp: float
    trading_pair: str
    environment: str
    best_bid: float
    best_ask: float
    best_bid_size: float = 0.0
    best_ask_size: float = 0.0
    bid_depth: float | None = None
    ask_depth: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    trades: tuple[ShadowTrade, ...] = ()
    rule: TradingRuleView = field(default_factory=TradingRuleView)
    maker_fee_bps: float | None = None
    option_environment: str = "mainnet"
    option_snapshot: OptionsVolatilitySnapshot | None = None
    # Stage 12E public-trade provenance.  These fields are metadata only and
    # do not alter the Stage 1--4 snapshot or grid calculations.
    trade_source: str = "unknown"
    trade_collection_status: str = "UNKNOWN"
    trade_endpoint: str | None = None
    trade_channel: str | None = None
    trade_request_window_start_epoch: float | None = None
    trade_request_window_end_epoch: float | None = None
    trade_collection_start_epoch: float | None = None
    trade_collection_end_epoch: float | None = None
    trade_sample_interval_seconds: float | None = None
    trade_raw_count: int = 0
    trade_canonical_count: int = 0
    trade_duplicate_count: int = 0
    trade_rejected_count: int = 0
    trade_page_count: int = 0
    trade_page_size: int = 0
    trade_pagination_count: int = 0
    trade_timestamp_unit: str | None = None
    trade_sort_order: str | None = None
    trade_dedup_key: str | None = None
    trade_connection_status: str | None = None
    trade_reconnect_count: int = 0
    trade_rate_limit_status: str | None = None
    trade_collection_error: str | None = None
    trade_crosscheck_status: str | None = None
    trade_crosscheck_collector_count: int | None = None
    trade_crosscheck_rest_count: int | None = None
    trade_crosscheck_missing_from_collector: int | None = None
    trade_crosscheck_extra_in_collector: int | None = None
    trade_crosscheck_error: str | None = None
    trade_crosscheck_window_start_epoch: float | None = None
    trade_crosscheck_window_end_epoch: float | None = None
    # Stage 12F repair provenance.  Raw cross-check counts are retained even
    # when REST is used to repair the frame that reaches the shadow engine.
    trade_crosscheck_raw_status: str | None = None
    trade_crosscheck_raw_collector_count: int | None = None
    trade_crosscheck_raw_rest_count: int | None = None
    trade_crosscheck_raw_missing_from_collector: int | None = None
    trade_crosscheck_raw_extra_in_collector: int | None = None
    trade_crosscheck_matched_count: int | None = None
    trade_crosscheck_missing_ids: tuple[str, ...] = ()
    trade_crosscheck_extra_ids: tuple[str, ...] = ()
    trade_crosscheck_attribute_mismatch_count: int | None = None
    trade_crosscheck_attribute_mismatch_ids: tuple[str, ...] = ()
    trade_recovery_status: str | None = None
    trade_backfill_attempted: bool = False
    trade_backfill_trades_found: int = 0
    trade_backfill_complete: bool | None = None
    trade_backfill_error: str | None = None
    trade_previous_request_end_epoch: float | None = None
    trade_request_overlap_seconds: float | None = None
    trade_poll_gap_seconds: float | None = None
    # The ticker timestamp is an exchange/event timestamp and can remain
    # unchanged while a polling controller receives the same healthy BBO.
    # ``timestamp`` remains the controller receipt time for sequencing; this
    # field preserves the source timestamp for freshness/audit checks.
    source_timestamp_epoch: float | None = None
    source_timestamp_age_seconds: float | None = None
    source_timestamp_stale: bool = False

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        return (self.best_ask - self.best_bid) / mid * 10_000 if mid > 0 else 0.0

    @property
    def microprice(self) -> float:
        total = self.best_bid_size + self.best_ask_size
        if total <= 0:
            return self.mid_price
        return (self.best_ask * self.best_bid_size + self.best_bid * self.best_ask_size) / total

    def to_strategy_snapshot(self, *, current_position: float = 0.0) -> dict[str, Any]:
        option = self.option_snapshot
        position_notional = abs(current_position * self.mid_price)
        validation_errors = (
            [
                "ticker source timestamp is stale beyond the configured market-data limit",
            ]
            if self.source_timestamp_stale
            else []
        )
        return {
            "timestamp": _iso(self.timestamp),
            "connector": "derive_perpetual",
            "trading_pair": self.trading_pair,
            "market_environment": self.environment,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "best_bid_size": self.best_bid_size,
            "best_ask_size": self.best_ask_size,
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "mid_price": self.mid_price,
            "microprice": self.microprice,
            "spread_bps": self.spread_bps,
            "spread_abs": self.best_ask - self.best_bid,
            "top_level_imbalance": (
                (self.best_bid_size - self.best_ask_size)
                / (self.best_bid_size + self.best_ask_size)
                if self.best_bid_size + self.best_ask_size > 0
                else None
            ),
            "depth_imbalance": None,
            "order_flow_imbalance": None,
            "trade_data_available": bool(self.trades),
            "trade_collection_available": self.trade_collection_status
            in {"OK", "CONNECTED", "CONNECTED_NO_TRADES", "REST_FALLBACK", "WEBSOCKET"},
            "trade_source": self.trade_source,
            "trade_collection_status": self.trade_collection_status,
            "recent_buy_volume": sum(
                trade.amount for trade in self.trades if trade.aggressor_side == "buy"
            ),
            "recent_sell_volume": sum(
                trade.amount for trade in self.trades if trade.aggressor_side == "sell"
            ),
            "atm_iv": option.atm_iv if option else None,
            "atm_call_iv": option.atm_call_iv if option else None,
            "atm_put_iv": option.atm_put_iv if option else None,
            "atm_strike": option.atm_strike if option else None,
            "atm_distance_pct": option.atm_distance_pct if option else None,
            "option_call_instrument": option.call_instrument if option else None,
            "option_put_instrument": option.put_instrument if option else None,
            "option_expiry": option.expiry if option else None,
            "option_expiry_dte": option.days_to_expiry if option else None,
            "option_data_timestamp": option.option_data_timestamp if option else None,
            "option_data_age_seconds": option.option_data_age_seconds if option else None,
            "option_data_source": option.source if option else None,
            "option_environment": option.environment if option else self.option_environment,
            "iv_confidence": option.confidence if option else 0.0,
            "iv_data_available": bool(option and option.data_available),
            "option_data_errors": list(option.errors) if option else ["options unavailable"],
            "current_position": current_position,
            "position_notional": position_notional,
            "available_balance": None,
            "account_data_available": True,
            "data_valid": (
                self.best_bid > 0
                and self.best_ask > self.best_bid
                and not self.source_timestamp_stale
            ),
            "validation_errors": validation_errors,
            "source_timestamp_epoch": self.source_timestamp_epoch,
            "source_timestamp_age_seconds": self.source_timestamp_age_seconds,
            "source_timestamp_stale": self.source_timestamp_stale,
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "timestamp": _iso(self.timestamp),
            "trading_pair": self.trading_pair,
            "market_environment": self.environment,
            "option_environment": self.option_environment,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid_price": self.mid_price,
            "spread_bps": self.spread_bps,
            "best_bid_size": self.best_bid_size,
            "best_ask_size": self.best_ask_size,
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "trades": [trade.to_record() for trade in self.trades],
            "trade_source": self.trade_source,
            "trade_collection_status": self.trade_collection_status,
            "trade_endpoint": self.trade_endpoint,
            "trade_channel": self.trade_channel,
            "trade_request_window_start_epoch": self.trade_request_window_start_epoch,
            "trade_request_window_end_epoch": self.trade_request_window_end_epoch,
            "trade_collection_start_epoch": self.trade_collection_start_epoch,
            "trade_collection_end_epoch": self.trade_collection_end_epoch,
            "trade_sample_interval_seconds": self.trade_sample_interval_seconds,
            "trade_raw_count": self.trade_raw_count,
            "trade_canonical_count": self.trade_canonical_count,
            "trade_duplicate_count": self.trade_duplicate_count,
            "trade_rejected_count": self.trade_rejected_count,
            "trade_page_count": self.trade_page_count,
            "trade_page_size": self.trade_page_size,
            "trade_pagination_count": self.trade_pagination_count,
            "trade_timestamp_unit": self.trade_timestamp_unit,
            "trade_sort_order": self.trade_sort_order,
            "trade_dedup_key": self.trade_dedup_key,
            "trade_connection_status": self.trade_connection_status,
            "trade_reconnect_count": self.trade_reconnect_count,
            "trade_rate_limit_status": self.trade_rate_limit_status,
            "trade_collection_error": self.trade_collection_error,
            "trade_crosscheck_status": self.trade_crosscheck_status,
            "trade_crosscheck_collector_count": self.trade_crosscheck_collector_count,
            "trade_crosscheck_rest_count": self.trade_crosscheck_rest_count,
            "trade_crosscheck_missing_from_collector": self.trade_crosscheck_missing_from_collector,
            "trade_crosscheck_extra_in_collector": self.trade_crosscheck_extra_in_collector,
            "trade_crosscheck_error": self.trade_crosscheck_error,
            "trade_crosscheck_window_start_epoch": self.trade_crosscheck_window_start_epoch,
            "trade_crosscheck_window_end_epoch": self.trade_crosscheck_window_end_epoch,
            "trade_crosscheck_raw_status": self.trade_crosscheck_raw_status,
            "trade_crosscheck_raw_collector_count": self.trade_crosscheck_raw_collector_count,
            "trade_crosscheck_raw_rest_count": self.trade_crosscheck_raw_rest_count,
            "trade_crosscheck_raw_missing_from_collector": (
                self.trade_crosscheck_raw_missing_from_collector
            ),
            "trade_crosscheck_raw_extra_in_collector": self.trade_crosscheck_raw_extra_in_collector,
            "trade_crosscheck_matched_count": self.trade_crosscheck_matched_count,
            "trade_crosscheck_missing_ids": list(self.trade_crosscheck_missing_ids),
            "trade_crosscheck_extra_ids": list(self.trade_crosscheck_extra_ids),
            "trade_crosscheck_attribute_mismatch_count": (
                self.trade_crosscheck_attribute_mismatch_count
            ),
            "trade_crosscheck_attribute_mismatch_ids": list(
                self.trade_crosscheck_attribute_mismatch_ids
            ),
            "trade_recovery_status": self.trade_recovery_status,
            "trade_backfill_attempted": self.trade_backfill_attempted,
            "trade_backfill_trades_found": self.trade_backfill_trades_found,
            "trade_backfill_complete": self.trade_backfill_complete,
            "trade_backfill_error": self.trade_backfill_error,
            "trade_previous_request_end_epoch": self.trade_previous_request_end_epoch,
            "trade_request_overlap_seconds": self.trade_request_overlap_seconds,
            "trade_poll_gap_seconds": self.trade_poll_gap_seconds,
            "source_timestamp_epoch": self.source_timestamp_epoch,
            "source_timestamp_age_seconds": self.source_timestamp_age_seconds,
            "source_timestamp_stale": self.source_timestamp_stale,
        }


@dataclass
class ShadowPosition:
    trading_pair: str
    amount: Decimal = Decimal("0")
    average_entry_price: Decimal | None = None

    def to_record(self) -> dict[str, Any]:
        return _json_value(self.__dict__)


@dataclass(frozen=True)
class ShadowFill:
    fill_id: str
    shadow_order_id: str
    trading_pair: str
    side: str
    price: Decimal
    amount: Decimal
    notional: Decimal
    timestamp: str
    timestamp_epoch: float
    fill_model: str
    entry_exit: str
    time_to_fill: float | None
    fees: Decimal
    realized_pnl: Decimal
    cycle_id: str | None = None
    markouts_bps: dict[str, float | None] = field(default_factory=dict)
    quote_distance_bps: float | None = None
    quote_distance_before_fill_bps: float | None = None
    mode: str | None = None
    state: str | None = None
    inventory_before: float | None = None
    inventory_after: float | None = None
    evidence: str | None = None
    global_iv_regime: str | None = None
    conservative_eligibility_status: str | None = None
    conservative_rejection_reason: str | None = None
    evidence_trade_id: str | None = None
    evidence_trade_timestamp: float | None = None

    def to_record(self) -> dict[str, Any]:
        return _json_value(self.__dict__)


class PositionLedger:
    """Virtual positions and reconciled paper PnL; never reads an account."""

    def __init__(
        self, starting_equity: Decimal, *, fees_known: bool, maker_fee_bps: Decimal | None
    ):
        self.starting_equity = starting_equity
        self.fees_known = fees_known
        self.maker_fee_bps = maker_fee_bps
        self.positions: dict[str, ShadowPosition] = {}
        self.realized_grid_capture = Decimal("0")
        self.realized_other_pnl = Decimal("0")
        self.realized_pnl = Decimal("0")
        self.fees = Decimal("0")
        self.unrealized_inventory_pnl = Decimal("0")
        self.mark_prices: dict[str, Decimal] = {}
        self.high_water_mark = starting_equity

    def position(self, pair: str) -> ShadowPosition:
        return self.positions.setdefault(pair, ShadowPosition(pair))

    def signed_notional(self, pair: str, mid_price: float | Decimal) -> Decimal:
        price = _decimal(mid_price, Decimal("0")) or Decimal("0")
        return self.position(pair).amount * price

    def _fee(self, notional: Decimal) -> Decimal:
        if not self.fees_known or self.maker_fee_bps is None:
            return Decimal("0")
        return notional * self.maker_fee_bps / Decimal("10000")

    def apply_fill(
        self, pair: str, side: str, price: Decimal, amount: Decimal
    ) -> tuple[Decimal, Decimal]:
        if amount <= 0 or price <= 0:
            raise ValueError("fill price and amount must be positive")
        position = self.position(pair)
        delta = amount if side.lower() == "buy" else -amount
        old = position.amount
        realized = Decimal("0")
        if old == 0 or old * delta > 0:
            total = abs(old) + abs(delta)
            if total > 0:
                old_price = position.average_entry_price or price
                position.average_entry_price = (old_price * abs(old) + price * abs(delta)) / total
            position.amount += delta
        else:
            closing = min(abs(old), abs(delta))
            average = position.average_entry_price or price
            realized = (price - average) * closing if old > 0 else (average - price) * closing
            position.amount += delta
            remainder = abs(position.amount)
            position.average_entry_price = price if remainder > 0 else None
        fee = self._fee(price * amount)
        self.realized_pnl += realized
        self.realized_grid_capture += realized
        self.fees += fee
        return realized, fee

    def mark(self, marks: Mapping[str, float | Decimal]) -> None:
        self.unrealized_inventory_pnl = Decimal("0")
        for pair, value in marks.items():
            price = _decimal(value)
            if price is None or price <= 0:
                continue
            self.mark_prices[pair] = price
            position = self.position(pair)
            if position.amount == 0 or position.average_entry_price is None:
                continue
            if position.amount > 0:
                self.unrealized_inventory_pnl += (
                    price - position.average_entry_price
                ) * position.amount
            else:
                self.unrealized_inventory_pnl += (position.average_entry_price - price) * abs(
                    position.amount
                )
        self.high_water_mark = max(self.high_water_mark, self.current_equity)

    @property
    def gross_total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_inventory_pnl

    @property
    def total_pnl(self) -> Decimal | None:
        return self.gross_total_pnl - self.fees if self.fees_known else None

    @property
    def current_equity(self) -> Decimal:
        return self.starting_equity + self.gross_total_pnl - (self.fees if self.fees_known else 0)

    @property
    def drawdown_quote(self) -> Decimal:
        return max(Decimal("0"), self.high_water_mark - self.current_equity)

    @property
    def drawdown_pct(self) -> Decimal:
        return (
            self.drawdown_quote / self.high_water_mark if self.high_water_mark > 0 else Decimal("0")
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "starting_equity": self.starting_equity,
            "realized_grid_capture": self.realized_grid_capture,
            "realized_other_pnl": self.realized_other_pnl,
            "realized_pnl": self.realized_pnl,
            "unrealized_inventory_pnl": self.unrealized_inventory_pnl,
            "fees": self.fees,
            "fees_known": self.fees_known,
            "fees_status": "KNOWN" if self.fees_known else "UNKNOWN",
            "gross_total_pnl": self.gross_total_pnl,
            "total_pnl": self.total_pnl,
            "current_equity": self.current_equity,
            "high_water_mark": self.high_water_mark,
            "drawdown_quote": self.drawdown_quote,
            "drawdown_pct": self.drawdown_pct,
            "positions": {pair: position.to_record() for pair, position in self.positions.items()},
        }


class ShadowStore:
    """Small SQLite/JSONL persistence layer for dashboard refresh and restart safety."""

    def __init__(self, sqlite_path: str | Path, event_path: str | Path) -> None:
        self.sqlite_path = Path(sqlite_path).expanduser()
        self.event_path = Path(event_path).expanduser()
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.sqlite_path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_sessions (
                session_id TEXT PRIMARY KEY, started_at TEXT, stopped_at TEXT,
                config_hash TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                event TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_order_lifecycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                event TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_orders (
                shadow_order_id TEXT PRIMARY KEY, session_id TEXT, status TEXT,
                timestamp TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_fills (
                fill_id TEXT PRIMARY KEY, session_id TEXT, timestamp TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_baseline_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, model TEXT,
                kind TEXT, timestamp TEXT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,
                payload TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def append_event(self, session_id: str, event: str, timestamp: str, **fields: Any) -> None:
        payload = _json_value(
            {"session_id": session_id, "timestamp": timestamp, "event": event, **fields}
        )
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._db.execute(
            "INSERT INTO shadow_events(session_id,timestamp,event,payload) VALUES (?,?,?,?)",
            (session_id, timestamp, event, json.dumps(payload, sort_keys=True)),
        )
        if event in {"RISK_BLOCK", "PORTFOLIO_RISK_BLOCK"}:
            table = "shadow_risk_events"
            self._db.execute(
                f"INSERT INTO {table}(session_id,timestamp,payload) VALUES (?,?,?)",
                (session_id, timestamp, json.dumps(payload, sort_keys=True)),
            )
        self._db.commit()

    def append_lifecycle(self, session_id: str, event: str, timestamp: str, **fields: Any) -> None:
        """Persist a bounded-safe order lifecycle event separately from legacy events."""

        payload = _json_value(
            {"session_id": session_id, "timestamp": timestamp, "event": event, **fields}
        )
        self._db.execute(
            "INSERT INTO shadow_order_lifecycle(session_id,timestamp,event,payload) "
            "VALUES (?,?,?,?)",
            (session_id, timestamp, event, json.dumps(payload, sort_keys=True)),
        )
        self._db.commit()

    def _insert_payload(self, table: str, session_id: str, timestamp: str, payload: Any) -> None:
        self._db.execute(
            f"INSERT INTO {table}(session_id,timestamp,payload) VALUES (?,?,?)",
            (session_id, timestamp, json.dumps(_json_value(payload), sort_keys=True)),
        )
        self._db.commit()

    def save_session(
        self, session_id: str, config_hash: str, payload: Any, stopped_at: str | None = None
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO shadow_sessions("
            "session_id,started_at,stopped_at,config_hash,payload) "
            "VALUES (?,?,?,?,?)",
            (
                session_id,
                _json_value(payload).get("start_timestamp"),
                stopped_at,
                config_hash,
                json.dumps(_json_value(payload), sort_keys=True),
            ),
        )
        self._db.commit()

    def save_order(self, session_id: str, order: ShadowOrder) -> None:
        payload = order.to_record()
        self._db.execute(
            "INSERT OR REPLACE INTO shadow_orders("
            "shadow_order_id,session_id,status,timestamp,payload) "
            "VALUES (?,?,?,?,?)",
            (
                order.shadow_order_id,
                session_id,
                order.status.value,
                order.updated_timestamp,
                json.dumps(payload),
            ),
        )
        self._db.commit()

    def save_fill(self, session_id: str, fill: ShadowFill) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO shadow_fills("
            "fill_id,session_id,timestamp,payload) VALUES (?,?,?,?)",
            (fill.fill_id, session_id, fill.timestamp, json.dumps(fill.to_record())),
        )
        self._db.commit()

    def save_position(self, session_id: str, timestamp: str, payload: Any) -> None:
        self._insert_payload("shadow_positions", session_id, timestamp, payload)

    def save_cycle(self, session_id: str, timestamp: str, payload: Any) -> None:
        self._insert_payload("shadow_cycles", session_id, timestamp, payload)

    def save_equity(self, session_id: str, timestamp: str, payload: Any) -> None:
        self._insert_payload("shadow_equity", session_id, timestamp, payload)

    def save_metrics(self, session_id: str, timestamp: str, payload: Any) -> None:
        self._insert_payload("shadow_metrics", session_id, timestamp, payload)

    def save_baseline_record(
        self,
        session_id: str,
        model: str,
        kind: str,
        timestamp: str,
        payload: Any,
    ) -> None:
        """Persist one Stage 12 report row for dashboard/restart inspection."""

        self._db.execute(
            "INSERT INTO shadow_baseline_records(session_id,model,kind,timestamp,payload) "
            "VALUES (?,?,?,?,?)",
            (
                session_id,
                model,
                kind,
                timestamp,
                json.dumps(_json_value(payload), sort_keys=True),
            ),
        )
        self._db.commit()

    def save_checkpoint(self, session_id: str, timestamp: str, payload: Any) -> None:
        """Persist a lightweight restart checkpoint without replacing history."""

        self._db.execute(
            "INSERT INTO shadow_checkpoints(session_id,timestamp,payload) VALUES (?,?,?)",
            (session_id, timestamp, json.dumps(_json_value(payload), sort_keys=True)),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


@dataclass
class ShadowOrder:
    shadow_order_id: str
    trading_pair: str
    level_id: str
    side: str
    order_type: str
    price: Decimal
    amount: Decimal
    notional: Decimal
    created_timestamp: str
    created_epoch: float
    updated_timestamp: str
    updated_epoch: float
    status: ShadowOrderStatus
    grid_plan_version: int
    mode_at_creation: str
    market_mid_at_creation: float | None
    spread_at_creation: float | None
    quote_distance_bps: float | None
    queue_model: str
    filled_amount: Decimal = Decimal("0")
    remaining_amount: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    cancel_timestamp: str | None = None
    cancel_reason: str | None = None
    cancel_market_mid: float | None = None
    cancel_price_deviation_bps: float | None = None
    fill_timestamp: str | None = None
    time_to_fill: float | None = None
    take_profit_price: Decimal | None = None
    take_profit_order_id: str | None = None
    parent_order_id: str | None = None
    cycle_id: str | None = None
    cancel_cycle_id: str | None = None
    controller_created_timestamp: str | None = None
    controller_created_epoch: float | None = None
    controller_terminal_timestamp: str | None = None
    controller_terminal_epoch: float | None = None
    is_exit: bool = False
    lifecycle_state: str = ShadowLifecycleState.CREATED.value
    validated_timestamp: str | None = None
    validated_epoch: float | None = None
    resting_start_timestamp: str | None = None
    resting_start_epoch: float | None = None
    terminal_timestamp: str | None = None
    terminal_epoch: float | None = None
    cancel_requested_timestamp: str | None = None
    cancel_requested_epoch: float | None = None
    cancel_reason_raw: str | None = None
    cancel_reason_category: str | None = None
    cancel_reason_detail: str | None = None
    cancel_market_best_bid: float | None = None
    cancel_market_best_ask: float | None = None
    old_price: Decimal | None = None
    new_desired_price: Decimal | None = None
    price_deviation_bps: float | None = None
    old_amount: Decimal | None = None
    new_desired_amount: Decimal | None = None
    amount_deviation_pct: float | None = None
    old_mode: str | None = None
    new_mode: str | None = None
    old_plan_version: int | None = None
    new_plan_version: int | None = None
    old_level_present: bool | None = None
    new_level_present: bool | None = None
    risk_state: str | None = None
    inventory_ratio: float | None = None
    portfolio_gross_exposure: float | None = None
    portfolio_beta_exposure: float | None = None
    minimum_order_lifetime_seconds: float | None = None
    replacement_cooldown_seconds: float | None = None
    time_since_last_replace_seconds: float | None = None
    cooldown_remaining_seconds: float | None = None
    safety_override: bool = False
    safety_override_reason: str | None = None
    replace_deferred: bool = False
    last_replace_epoch: float | None = None
    fill_eligibility_status: str | None = None
    fill_eligibility_reason: str | None = None
    # Stage 12G lifecycle and eligibility provenance.  These fields describe
    # the existing shadow decision; they do not participate in pricing,
    # sizing, risk limits, or fill selection.
    lifecycle_state_sequence: list[str] = field(default_factory=list)
    theoretical_price: Decimal | None = None
    desired_price: Decimal | None = None
    desired_amount: Decimal | None = None
    desired_notional: Decimal | None = None
    quantized_price: Decimal | None = None
    quantized_amount: Decimal | None = None
    bbo_best_bid_at_create: float | None = None
    bbo_best_ask_at_create: float | None = None
    post_only_valid: bool | None = None
    maker_valid: bool | None = None
    eligible_to_rest: bool | None = None
    reached_execution_engine: bool = False
    plan_valid_at_create: bool | None = None
    plan_valid_at_terminal: bool | None = None
    plan_valid_next_frame: bool | None = None
    next_frame_timestamp: str | None = None
    next_frame_cycle_id: str | None = None
    next_frame_controller_timestamp: str | None = None
    risk_allowed_at_create: bool | None = None
    minimum_exchange_size_valid: bool | None = None
    portfolio_risk_valid: bool | None = None
    asset_risk_valid: bool | None = None
    market_data_valid: bool | None = None
    btc_iv_valid: bool | None = None
    relationship_data_valid: bool | None = None
    state_confidence_valid: bool | None = None
    terminal_reason: str | None = None
    same_cycle_create_cancel: bool = False
    create_validation_latency_ms: float | None = None
    validation_resting_latency_ms: float | None = None
    create_terminal_latency_ms: float | None = None
    resting_definition: str = (
        "virtual order passed maker/risk gates and is eligible for future shadow fill evaluation"
    )

    def to_record(self) -> dict[str, Any]:
        return _json_value(self.__dict__)


class ShadowExecutionEngine:
    """Virtual Stage 5 adapter with a hard no-private-mutation boundary."""

    def __init__(
        self,
        config: ShadowConfig,
        *,
        session_id: str | None = None,
        store: ShadowStore | None = None,
        exchange_client: Any | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError(
                "shadow session is disabled; invoke the explicit shadow runner to enable it"
            )
        if config.execution_mode.upper() != "SHADOW":
            raise ValueError("ShadowExecutionEngine requires execution_mode=SHADOW")
        if config.market_environment.lower() != "mainnet":
            raise ValueError("ShadowExecutionEngine requires mainnet public data")
        self.config = config
        self.session_id = (
            session_id
            or f"shadow-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        )
        self.store = store
        self.exchange = (
            ShadowExchangeMutationGuard(exchange_client) if exchange_client is not None else None
        )
        self.orders: dict[str, ShadowOrder] = {}
        self.fills: list[ShadowFill] = []
        self.events: list[dict[str, Any]] = []
        self.lifecycle_events: deque[dict[str, Any]] = deque(maxlen=20_000)
        self.lifecycle_path = Path(config.event_path).expanduser().with_name(
            "shadow_order_lifecycle.jsonl"
        )
        self.equity_history: list[dict[str, Any]] = []
        self.market_history: dict[str, deque[ShadowMarketFrame]] = defaultdict(
            lambda: deque(maxlen=10_000)
        )
        self.ledger = PositionLedger(
            Decimal(str(config.starting_equity_usdc)),
            fees_known=config.fees_known,
            maker_fee_bps=Decimal(str(config.maker_fee_bps))
            if config.maker_fee_bps is not None
            else None,
        )
        self._counter = 0
        self.real_exchange_mutation_calls = 0
        self.blocked_mutation_attempts = 0
        self.completed_cycles = 0
        self.latest_cycle: MultiAssetCycle | None = None
        self.latest_frames: dict[str, ShadowMarketFrame] = {}
        self.latest_plans: dict[str, dict[str, Any]] = {}
        self.latest_states: dict[str, dict[str, Any]] = {}
        self.latest_risk: dict[str, Any] = {}
        self.risk_episodes = RiskEpisodeTracker()
        self.reconciliation_audit: list[dict[str, Any]] = []
        self.order_eligibility_audit: list[dict[str, Any]] = []
        self.risk_reservation_audit: list[dict[str, Any]] = []
        self.risk_delta_audit: list[dict[str, Any]] = []

    def execution_status(self, trading_pair: str) -> str:
        """Return the explicit Stage 13 route status for one pair."""

        if not self.config.stage13.enabled:
            return "EXECUTION_ENABLED"
        return effective_asset_status(
            self.config.stage13,
            self.config.markets,
            self.config.enabled_markets,
        ).get(trading_pair, "DISABLED")

    def _emit(self, event: str, timestamp: float, **fields: Any) -> dict[str, Any]:
        record = _json_value(
            {
                "session_id": self.session_id,
                "timestamp": _iso(timestamp),
                "timestamp_epoch": timestamp,
                "event": event,
                "real_exchange_mutation_calls": self.real_exchange_mutation_calls,
                **fields,
            }
        )
        self.events.append(record)
        if self.store is not None:
            fields = {
                key: value
                for key, value in record.items()
                if key not in {"event", "timestamp", "session_id"}
            }
            self.store.append_event(self.session_id, event, record["timestamp"], **fields)
        return record

    def _emit_lifecycle(self, event: str, timestamp: float, **fields: Any) -> dict[str, Any]:
        """Append one bounded, secret-free lifecycle record.

        Lifecycle events intentionally use a separate file/table.  This keeps
        the legacy event stream backward-compatible while making the new
        CREATED/VALIDATED/RESTING transitions directly queryable.
        """

        if event not in LIFECYCLE_EVENTS:
            raise ValueError(f"unsupported lifecycle event: {event}")
        record = _json_value(
            {
                "session_id": self.session_id,
                "timestamp": _iso(timestamp),
                "timestamp_epoch": timestamp,
                "event": event,
                **fields,
            }
        )
        self.lifecycle_events.append(record)
        self.lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lifecycle_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self.lifecycle_path.stat().st_size > 10_000_000:
            lines = self.lifecycle_path.read_text(encoding="utf-8").splitlines()[-20_000:]
            self.lifecycle_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if self.store is not None:
            self.store.append_lifecycle(
                self.session_id,
                event,
                record["timestamp"],
                **{
                    key: value
                    for key, value in record.items()
                    if key not in {"event", "session_id", "timestamp"}
                },
            )
        return record

    def _record_risk_block(
        self,
        timestamp: float,
        *,
        event: str = "RISK_BLOCK",
        trading_pair: str | None = None,
        level_id: str | None = None,
        side: str | None = None,
        reason: Any,
        assets: Sequence[str] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Persist a raw risk check and its stable episode identity."""

        self.risk_episodes.record_check()
        episode = self.risk_episodes.record(
            timestamp,
            reason=reason,
            trading_pair=trading_pair,
            level_id=level_id,
            side=side,
            assets=list(assets) if assets is not None else ([trading_pair] if trading_pair else []),
            context=fields,
        )
        return self._emit(
            event,
            timestamp,
            trading_pair=trading_pair,
            level_id=level_id,
            side=side,
            reason=str(reason),
            risk_category=normalize_risk_reason(reason),
            risk_episode_id=episode["episode_id"],
            risk_episode_key=episode["episode_key"],
            **fields,
        )

    def record_reconciliation_audit(self, row: Mapping[str, Any]) -> None:
        """Keep one auditable desired/active decision row per pair and cycle."""

        self.reconciliation_audit.append(_json_value(dict(row)))

    def record_order_eligibility_audit(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Keep one pre-create eligibility row per candidate grid level."""

        normalized = _json_value(dict(row))
        self.order_eligibility_audit.append(normalized)
        return normalized

    def record_risk_reservation_audit(self, row: Mapping[str, Any]) -> None:
        """Keep one filled/pending/worst-case reservation snapshot per cycle."""

        self.risk_reservation_audit.append(_json_value(dict(row)))

    @staticmethod
    def _append_lifecycle_state(order: ShadowOrder, state: str) -> None:
        if not order.lifecycle_state_sequence or order.lifecycle_state_sequence[-1] != state:
            order.lifecycle_state_sequence.append(state)
        order.lifecycle_state = state

    def annotate_order_next_frame(
        self,
        *,
        frame: ShadowMarketFrame,
        plan_valid: bool,
        plan_version: int,
        mode: str,
        cycle_id: str | None = None,
        controller_timestamp: float | None = None,
    ) -> None:
        """Attach the first later controller-frame validity observation to an order."""

        for order in self.orders.values():
            if (
                order.trading_pair != frame.trading_pair
                or order.is_exit
                or (
                    order.created_epoch >= frame.timestamp
                    and not (
                        order.cycle_id is not None
                        and cycle_id is not None
                        and order.cycle_id != cycle_id
                    )
                )
                or order.next_frame_timestamp is not None
            ):
                continue
            order.plan_valid_next_frame = bool(plan_valid)
            order.next_frame_timestamp = _iso(frame.timestamp)
            order.next_frame_cycle_id = cycle_id
            if controller_timestamp is not None:
                order.next_frame_controller_timestamp = _iso(controller_timestamp)
            if self.store is not None:
                self.store.save_order(self.session_id, order)
            self._emit(
                "ORDER_NEXT_FRAME_PLAN_VALIDITY",
                frame.timestamp,
                shadow_order_id=order.shadow_order_id,
                trading_pair=order.trading_pair,
                level_id=order.level_id,
                side=order.side,
                plan_valid_next_frame=bool(plan_valid),
                next_frame_timestamp=order.next_frame_timestamp,
                next_frame_cycle_id=order.next_frame_cycle_id,
                next_frame_controller_timestamp=order.next_frame_controller_timestamp,
                plan_version=plan_version,
                mode=mode,
            )

    def record_risk_reservation_snapshot(
        self,
        *,
        timestamp: float,
        frames: Mapping[str, ShadowMarketFrame],
        portfolio_risk: Mapping[str, Any],
        active_executor_inputs: Mapping[str, Any],
        pending_entries_before: Mapping[str, Any],
    ) -> None:
        """Persist filled, pending, and worst-case exposure separately."""

        filled_by_asset: dict[str, float] = {}
        pending_by_asset: dict[str, dict[str, float]] = {}
        for pair, frame in frames.items():
            filled_by_asset[pair] = abs(float(self.ledger.signed_notional(pair, frame.mid_price)))
            pending_by_asset[pair] = {"buy": 0.0, "sell": 0.0, "count": 0.0}
        for order in self.orders.values():
            if (
                order.is_exit
                or order.status
                not in {ShadowOrderStatus.RESTING, ShadowOrderStatus.PARTIALLY_FILLED}
                or order.trading_pair not in pending_by_asset
            ):
                continue
            pending_by_asset[order.trading_pair][order.side] += float(order.notional)
            pending_by_asset[order.trading_pair]["count"] += 1.0
        pending_gross = sum(
            values["buy"] + values["sell"] for values in pending_by_asset.values()
        )
        filled_gross = sum(filled_by_asset.values())
        worst_case = filled_gross + pending_gross
        pending_before_count = sum(
            float(values.get("count", 0.0) or 0.0)
            for values in pending_entries_before.values()
            if isinstance(values, Mapping)
        )
        pending_before_gross = sum(
            float(values.get("buy", 0.0) or 0.0)
            + float(values.get("sell", 0.0) or 0.0)
            for values in pending_entries_before.values()
            if isinstance(values, Mapping)
        )
        risk_gross = _finite(portfolio_risk.get("gross_notional"))
        expected_before_reconcile = filled_gross + pending_before_gross
        new_pending_reservation = pending_gross - pending_before_gross
        self.record_risk_reservation_audit(
            {
                "timestamp": _iso(timestamp),
                "timestamp_epoch": timestamp,
                "scope": "PORTFOLIO",
                "filled_gross_exposure": filled_gross,
                "pending_reserved_gross": pending_gross,
                "pending_reserved_gross_before_reconcile": pending_before_gross,
                "pending_reserved_gross_after_reconcile": pending_gross,
                "new_pending_reserved_gross": new_pending_reservation,
                "worst_case_gross": worst_case,
                "portfolio_gross_exposure": risk_gross,
                "portfolio_gross_exposure_before_reconcile": risk_gross,
                "portfolio_gross_exposure_after_reconcile": worst_case,
                "portfolio_gross_difference": (
                    risk_gross - expected_before_reconcile
                    if risk_gross is not None
                    else None
                ),
                "reservation_gross_difference_after_reconcile": (
                    risk_gross + new_pending_reservation - worst_case
                    if risk_gross is not None
                    else None
                ),
                "pending_buy_notional": sum(
                    values["buy"] for values in pending_by_asset.values()
                ),
                "pending_sell_notional": sum(
                    values["sell"] for values in pending_by_asset.values()
                ),
                "pending_order_count": sum(
                    values["count"] for values in pending_by_asset.values()
                ),
                "pending_order_count_before_reconcile": pending_before_count,
                "active_executor_inputs": dict(active_executor_inputs),
                "pending_potential_inventory": pending_gross,
                "filled_inventory_notional": filled_gross,
                "portfolio_beta_exposure": portfolio_risk.get(
                    "btc_beta_equivalent_exposure"
                ),
                "active_executor_input_count": portfolio_risk.get(
                    "active_executor_input_count"
                ),
                "pending_executor_count": portfolio_risk.get("pending_executor_count"),
                "active_pending_executor_overlap_count": portfolio_risk.get(
                    "active_pending_executor_overlap_count"
                ),
                "pre_proposal_active_executors": portfolio_risk.get(
                    "pre_proposal_active_executors"
                ),
                "governor_active_executors": portfolio_risk.get("active_executors"),
                "keep_double_count_invariant": True,
                "ledger_pending_does_not_change_filled_inventory": True,
                "pending_reservation_oscillation": False,
            }
        )
        for pair, values in pending_by_asset.items():
            self.record_risk_reservation_audit(
                {
                    "timestamp": _iso(timestamp),
                    "timestamp_epoch": timestamp,
                    "scope": "ASSET",
                    "trading_pair": pair,
                    "filled_gross_exposure": filled_by_asset[pair],
                    "pending_reserved_gross": values["buy"] + values["sell"],
                    "pending_reserved_gross_before_reconcile": (
                        float(
                            (pending_entries_before.get(pair) or {}).get("buy", 0.0)
                            or 0.0
                        )
                        + float(
                            (pending_entries_before.get(pair) or {}).get("sell", 0.0)
                            or 0.0
                        )
                    ),
                    "pending_reserved_gross_after_reconcile": values["buy"] + values["sell"],
                    "worst_case_gross": filled_by_asset[pair] + values["buy"] + values["sell"],
                    "pending_buy_notional": values["buy"],
                    "pending_sell_notional": values["sell"],
                    "pending_order_count": values["count"],
                    "pending_potential_inventory": values["buy"] + values["sell"],
                    "filled_inventory_notional": filled_by_asset[pair],
                    "keep_double_count_invariant": True,
                    "ledger_pending_does_not_change_filled_inventory": True,
                }
            )

    def _new_order_id(self, pair: str, level_id: str) -> str:
        self._counter += 1
        return f"shadow::{self.session_id}::{pair}::{level_id}::{self._counter}"

    @staticmethod
    def _crosses(side: str, price: Decimal, best_bid: float | None, best_ask: float | None) -> bool:
        bid = _decimal(best_bid)
        ask = _decimal(best_ask)
        return (side == "buy" and ask is not None and price >= ask) or (
            side == "sell" and bid is not None and price <= bid
        )

    def create_order(
        self,
        *,
        trading_pair: str,
        level_id: str,
        side: str,
        price: Decimal | float,
        amount: Decimal | float,
        timestamp: float,
        grid_plan_version: int = 0,
        mode: str = "normal",
        mid_price: float | None = None,
        spread_bps: float | None = None,
        best_bid: float | None = None,
        best_ask: float | None = None,
        take_profit_price: Decimal | float | None = None,
        parent_order_id: str | None = None,
        is_exit: bool = False,
        cycle_id: str | None = None,
        controller_timestamp: float | None = None,
        diagnostic_context: Mapping[str, Any] | None = None,
    ) -> ShadowOrder:
        normalized_side = str(side).lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("shadow order side must be buy or sell")
        order_price = _decimal(price)
        order_amount = _decimal(amount)
        if order_price is None or order_price <= 0 or order_amount is None or order_amount <= 0:
            raise ValueError("shadow order price and amount must be positive")
        order_id = self._new_order_id(trading_pair, level_id)
        now_text = _iso(timestamp)
        crossed = self.config.post_only and self._crosses(
            normalized_side, order_price, best_bid, best_ask
        )
        status = ShadowOrderStatus.REJECTED if crossed else ShadowOrderStatus.PENDING
        diagnostics = dict(diagnostic_context or {})
        controller_created_epoch = (
            controller_timestamp if controller_timestamp is not None else timestamp
        )
        quote_distance = (
            abs(order_price - Decimal(str(mid_price))) / Decimal(str(mid_price)) * Decimal("10000")
            if mid_price and mid_price > 0
            else None
        )
        order = ShadowOrder(
            shadow_order_id=order_id,
            trading_pair=trading_pair,
            level_id=level_id,
            side=normalized_side,
            order_type="LIMIT_MAKER",
            price=order_price,
            amount=order_amount,
            notional=order_price * order_amount,
            created_timestamp=now_text,
            created_epoch=timestamp,
            updated_timestamp=now_text,
            updated_epoch=timestamp,
            status=status,
            grid_plan_version=grid_plan_version,
            mode_at_creation=mode,
            market_mid_at_creation=mid_price,
            spread_at_creation=spread_bps,
            quote_distance_bps=float(quote_distance) if quote_distance is not None else None,
            queue_model=self.config.fill_model.value,
            remaining_amount=order_amount,
            take_profit_price=_decimal(take_profit_price),
            parent_order_id=parent_order_id,
            cycle_id=cycle_id,
            controller_created_timestamp=_iso(controller_created_epoch),
            controller_created_epoch=controller_created_epoch,
            is_exit=is_exit,
            lifecycle_state=ShadowLifecycleState.CREATED.value,
            lifecycle_state_sequence=[ShadowLifecycleState.CREATED.value],
            theoretical_price=_decimal(diagnostics.get("theoretical_price"), order_price),
            desired_price=_decimal(diagnostics.get("desired_price"), order_price),
            desired_amount=_decimal(diagnostics.get("desired_amount"), order_amount),
            desired_notional=_decimal(
                diagnostics.get("desired_notional"), order_price * order_amount
            ),
            quantized_price=_decimal(diagnostics.get("quantized_price"), order_price),
            quantized_amount=_decimal(diagnostics.get("quantized_amount"), order_amount),
            bbo_best_bid_at_create=_finite(best_bid),
            bbo_best_ask_at_create=_finite(best_ask),
            post_only_valid=diagnostics.get("post_only_valid", not crossed),
            maker_valid=diagnostics.get("maker_valid", not crossed),
            eligible_to_rest=diagnostics.get("eligible_to_rest", not crossed),
            reached_execution_engine=True,
            plan_valid_at_create=diagnostics.get("plan_valid_at_create"),
            risk_allowed_at_create=diagnostics.get("risk_allowed_at_create", not crossed),
            minimum_exchange_size_valid=diagnostics.get("minimum_exchange_size_valid", True),
            portfolio_risk_valid=diagnostics.get("portfolio_risk_valid", True),
            asset_risk_valid=diagnostics.get("asset_risk_valid", True),
            market_data_valid=diagnostics.get(
                "market_data_valid",
                best_bid is not None and best_ask is not None and best_ask > best_bid,
            ),
            btc_iv_valid=diagnostics.get("btc_iv_valid"),
            relationship_data_valid=diagnostics.get("relationship_data_valid"),
            state_confidence_valid=diagnostics.get("state_confidence_valid"),
        )
        self.orders[order_id] = order
        self._emit_lifecycle(
            "ORDER_CREATED",
            timestamp,
            shadow_order_id=order_id,
            trading_pair=trading_pair,
            level_id=level_id,
            side=normalized_side,
            order_type="LIMIT_MAKER",
            price=order_price,
            amount=order_amount,
            notional=order.notional,
            status=status.value,
            lifecycle_state=ShadowLifecycleState.CREATED.value,
            cycle_id=order.cycle_id,
            controller_created_timestamp=order.controller_created_timestamp,
            controller_created_epoch=order.controller_created_epoch,
            grid_plan_version=grid_plan_version,
            mode=mode,
            is_exit=is_exit,
            reached_execution_engine=order.reached_execution_engine,
            post_only_valid=order.post_only_valid,
            maker_valid=order.maker_valid,
            eligible_to_rest=order.eligible_to_rest,
            plan_valid_at_create=order.plan_valid_at_create,
            risk_allowed_at_create=order.risk_allowed_at_create,
            minimum_exchange_size_valid=order.minimum_exchange_size_valid,
            portfolio_risk_valid=order.portfolio_risk_valid,
            asset_risk_valid=order.asset_risk_valid,
            market_data_valid=order.market_data_valid,
        )
        if crossed:
            order.post_only_valid = False
            order.maker_valid = False
            order.eligible_to_rest = False
            self._append_lifecycle_state(
                order, ShadowLifecycleState.NEVER_RESTED_REJECTED.value
            )
            order.terminal_timestamp = now_text
            order.terminal_epoch = timestamp
            order.terminal_reason = "WOULD_CROSS_MARKET"
            order.controller_terminal_epoch = controller_created_epoch
            order.controller_terminal_timestamp = _iso(controller_created_epoch)
            order.create_terminal_latency_ms = 0.0
            self._emit(
                "ORDER_REJECT",
                timestamp,
                shadow_order_id=order_id,
                trading_pair=trading_pair,
                level_id=level_id,
                reason="WOULD_VIOLATE_POST_ONLY",
                order_type="LIMIT_MAKER",
                cancel_reason_category="WOULD_CROSS_MARKET",
                lifecycle_state=order.lifecycle_state,
                lifecycle_state_sequence=order.lifecycle_state_sequence,
                cycle_id=order.cycle_id,
            )
        else:
            order.status = ShadowOrderStatus.CLOSE_RESTING if is_exit else ShadowOrderStatus.RESTING
            order.validated_timestamp = now_text
            order.validated_epoch = timestamp
            order.create_validation_latency_ms = 0.0
            self._append_lifecycle_state(order, ShadowLifecycleState.VALIDATED.value)
            order.resting_start_timestamp = now_text
            order.resting_start_epoch = timestamp
            order.validation_resting_latency_ms = 0.0
            self._append_lifecycle_state(order, ShadowLifecycleState.RESTING.value)
            self._emit_lifecycle(
                "ORDER_RESTING",
                timestamp,
                shadow_order_id=order_id,
                trading_pair=trading_pair,
                level_id=level_id,
                side=normalized_side,
                previous_state=ShadowLifecycleState.VALIDATED.value,
                lifecycle_state=ShadowLifecycleState.RESTING.value,
                validated_timestamp=now_text,
                resting_start_timestamp=now_text,
                status=order.status.value,
                lifecycle_state_sequence=order.lifecycle_state_sequence,
                eligible_to_rest=order.eligible_to_rest,
                cycle_id=order.cycle_id,
                controller_created_timestamp=order.controller_created_timestamp,
                controller_created_epoch=order.controller_created_epoch,
            )
            self._emit(
                "TP_CREATE" if is_exit else "ORDER_CREATE",
                timestamp,
                shadow_order_id=order_id,
                trading_pair=trading_pair,
                level_id=level_id,
                side=normalized_side,
                price=order_price,
                amount=order_amount,
                notional=order.notional,
                status=status,
                order_type="LIMIT_MAKER",
                grid_plan_version=grid_plan_version,
                mode=mode,
                cycle_id=order.cycle_id,
                controller_created_timestamp=order.controller_created_timestamp,
                controller_created_epoch=order.controller_created_epoch,
            )
        if self.store is not None:
            self.store.save_order(self.session_id, order)
        return order

    def cancel_order(
        self,
        order_id: str,
        *,
        timestamp: float,
        reason: str,
        market_mid: float | None = None,
        market_best_bid: float | None = None,
        market_best_ask: float | None = None,
        reason_code: str | None = None,
        controller_timestamp: float | None = None,
        decision_context: Mapping[str, Any] | None = None,
        **decision_fields: Any,
    ) -> ShadowOrder:
        order = self.orders[order_id]
        if order.status not in {
            ShadowOrderStatus.RESTING,
            ShadowOrderStatus.PARTIALLY_FILLED,
            ShadowOrderStatus.CLOSE_RESTING,
        }:
            return order
        context = dict(decision_context or {})
        context.update(decision_fields)
        if controller_timestamp is not None:
            context["controller_timestamp"] = controller_timestamp
        context.setdefault("plan_version", order.grid_plan_version)
        context.setdefault("old_plan_version", order.grid_plan_version)
        context.setdefault("old_mode", order.mode_at_creation)
        context.setdefault("old_level_present", True)
        category = classify_cancel_reason(reason, reason_code=reason_code or "", context=context)
        cancel_timestamp = _iso(timestamp)
        controller_terminal_epoch = _finite(context.get("controller_timestamp"))
        if controller_terminal_epoch is None:
            controller_terminal_epoch = timestamp
        cycle_context_present = "cycle_id" in context
        cancel_cycle_id = context.get("cycle_id")
        if cycle_context_present:
            order.cancel_cycle_id = (
                str(cancel_cycle_id) if cancel_cycle_id is not None else None
            )
            same_cycle = (
                order.cycle_id is not None
                and order.cancel_cycle_id is not None
                and order.cycle_id == order.cancel_cycle_id
            )
        else:
            # Backward-compatible fallback for direct engine callers that do
            # not provide controller-cycle provenance.
            same_cycle = timestamp <= order.created_epoch + 1e-6
        order.controller_terminal_epoch = controller_terminal_epoch
        order.controller_terminal_timestamp = _iso(controller_terminal_epoch)
        order.cancel_requested_timestamp = cancel_timestamp
        order.cancel_requested_epoch = timestamp
        self._emit_lifecycle(
            "ORDER_CANCEL_REQUESTED",
            timestamp,
            shadow_order_id=order_id,
            trading_pair=order.trading_pair,
            level_id=order.level_id,
            side=order.side,
            previous_state=order.lifecycle_state,
            lifecycle_state=order.lifecycle_state,
            reason=category,
            reason_raw=str(reason),
            reason_code=reason_code,
            decision_path=context.get("decision_path", "cancel_order"),
            cycle_id=order.cycle_id,
            cancel_cycle_id=order.cancel_cycle_id,
            same_cycle_create_cancel=same_cycle,
            controller_created_timestamp=order.controller_created_timestamp,
            controller_terminal_timestamp=order.controller_terminal_timestamp,
        )
        order.status = ShadowOrderStatus.CANCELLED
        order.updated_epoch = timestamp
        order.updated_timestamp = cancel_timestamp
        order.cancel_timestamp = cancel_timestamp
        order.terminal_timestamp = cancel_timestamp
        order.terminal_epoch = timestamp
        self._append_lifecycle_state(order, ShadowLifecycleState.CANCELLED_AFTER_RESTING.value)
        order.cancel_reason_raw = str(reason)
        order.cancel_reason_category = category
        order.cancel_reason = category
        order.terminal_reason = category
        order.same_cycle_create_cancel = same_cycle
        order.create_terminal_latency_ms = max(
            0.0,
            controller_terminal_epoch - (order.controller_created_epoch or order.created_epoch),
        ) * 1000.0
        order.plan_valid_at_terminal = (
            bool(context["plan_valid"]) if "plan_valid" in context else None
        )
        if "plan_valid_next_frame" in context:
            order.plan_valid_next_frame = (
                bool(context["plan_valid_next_frame"])
                if context["plan_valid_next_frame"] is not None
                else None
            )
        if context.get("next_frame_timestamp") is not None:
            order.next_frame_timestamp = str(context["next_frame_timestamp"])
        order.cancel_reason_detail = json.dumps(_json_value(context), sort_keys=True)
        order.old_price = _decimal(context.get("old_price"), order.price) or order.price
        order.new_desired_price = _decimal(context.get("new_desired_price"))
        order.price_deviation_bps = _finite(context.get("price_deviation_bps"))
        if order.price_deviation_bps is None and order.new_desired_price is not None:
            order.price_deviation_bps = (
                abs(float(order.price) - float(order.new_desired_price))
                / float(order.price)
                * 10_000
                if order.price > 0
                else None
            )
        order.old_amount = _decimal(context.get("old_amount"), order.amount) or order.amount
        order.new_desired_amount = _decimal(context.get("new_desired_amount"))
        order.amount_deviation_pct = _finite(context.get("amount_deviation_pct"))
        order.old_mode = str(context.get("old_mode", order.mode_at_creation))
        order.new_mode = (
            str(context["new_mode"]) if context.get("new_mode") is not None else None
        )
        order.old_plan_version = int(context.get("old_plan_version", order.grid_plan_version))
        order.new_plan_version = (
            int(context["new_plan_version"])
            if context.get("new_plan_version") is not None
            else None
        )
        order.old_level_present = context.get("old_level_present", True)
        order.new_level_present = context.get("new_level_present")
        order.risk_state = (
            str(context["risk_state"]) if context.get("risk_state") is not None else None
        )
        for field_name in (
            "inventory_ratio",
            "portfolio_gross_exposure",
            "portfolio_beta_exposure",
            "minimum_order_lifetime_seconds",
            "replacement_cooldown_seconds",
            "time_since_last_replace_seconds",
            "cooldown_remaining_seconds",
        ):
            setattr(order, field_name, _finite(context.get(field_name)))
        order.safety_override = bool(context.get("safety_override", False))
        order.safety_override_reason = (
            str(context["safety_override_reason"])
            if context.get("safety_override_reason") is not None
            else None
        )
        order.replace_deferred = bool(context.get("replace_deferred", False))
        if market_mid is not None and market_mid > 0:
            order.cancel_market_mid = market_mid
            order.cancel_price_deviation_bps = (
                abs(float(order.price) - market_mid) / market_mid * 10_000
            )
        order.cancel_market_best_bid = market_best_bid
        order.cancel_market_best_ask = market_best_ask
        if self.store is not None:
            self.store.save_order(self.session_id, order)
        resting_start = order.controller_created_epoch or order.resting_start_epoch
        resting_lifetime = (
            max(0.0, controller_terminal_epoch - resting_start)
            if resting_start is not None
            else None
        )
        self._emit_lifecycle(
            "ORDER_CANCELLED",
            timestamp,
            shadow_order_id=order_id,
            trading_pair=order.trading_pair,
            level_id=order.level_id,
            side=order.side,
            previous_state=ShadowLifecycleState.RESTING.value,
            lifecycle_state=order.lifecycle_state,
            lifecycle_state_sequence=order.lifecycle_state_sequence,
            reason=category,
            reason_raw=str(reason),
            reason_code=reason_code,
            resting_start_timestamp=order.resting_start_timestamp,
            terminal_timestamp=cancel_timestamp,
            resting_lifetime_seconds=resting_lifetime,
            age_seconds=resting_lifetime,
            created_timestamp=order.created_timestamp,
            cancel_requested_timestamp=order.cancel_requested_timestamp,
            old_price=order.old_price,
            new_desired_price=order.new_desired_price,
            price_deviation_bps=order.price_deviation_bps,
            old_amount=order.old_amount,
            new_desired_amount=order.new_desired_amount,
            amount_deviation_pct=order.amount_deviation_pct,
            old_mode=order.old_mode,
            new_mode=order.new_mode,
            old_plan_version=order.old_plan_version,
            new_plan_version=order.new_plan_version,
            old_level_present=order.old_level_present,
            new_level_present=order.new_level_present,
            risk_state=order.risk_state,
            inventory_ratio=order.inventory_ratio,
            portfolio_gross_exposure=order.portfolio_gross_exposure,
            portfolio_beta_exposure=order.portfolio_beta_exposure,
            minimum_order_lifetime_seconds=order.minimum_order_lifetime_seconds,
            replacement_cooldown_seconds=order.replacement_cooldown_seconds,
            time_since_last_replace_seconds=order.time_since_last_replace_seconds,
            cooldown_remaining_seconds=order.cooldown_remaining_seconds,
            safety_override=order.safety_override,
            safety_override_reason=order.safety_override_reason,
            cancel_market_mid=order.cancel_market_mid,
            cancel_market_best_bid=order.cancel_market_best_bid,
            cancel_market_best_ask=order.cancel_market_best_ask,
            cancel_price_deviation_bps=order.cancel_price_deviation_bps,
            cancel_reason_detail=order.cancel_reason_detail,
            same_cycle_create_cancel=order.same_cycle_create_cancel,
            plan_valid_at_terminal=order.plan_valid_at_terminal,
            plan_valid_next_frame=order.plan_valid_next_frame,
            cycle_id=order.cycle_id,
            cancel_cycle_id=order.cancel_cycle_id,
            controller_created_timestamp=order.controller_created_timestamp,
            controller_terminal_timestamp=order.controller_terminal_timestamp,
            controller_resting_lifetime_seconds=resting_lifetime,
        )
        self._emit(
            "ORDER_CANCEL",
            timestamp,
            shadow_order_id=order_id,
            trading_pair=order.trading_pair,
            level_id=order.level_id,
            side=order.side,
            reason=category,
            reason_raw=str(reason),
            reason_code=reason_code,
            category=category,
            detail=order.cancel_reason_detail,
            age_seconds=resting_lifetime,
            lifetime_seconds=resting_lifetime,
            resting_lifetime_seconds=resting_lifetime,
            created_timestamp=order.created_timestamp,
            resting_start_timestamp=order.resting_start_timestamp,
            cancel_requested_timestamp=order.cancel_requested_timestamp,
            cancelled_timestamp=order.cancel_timestamp,
            price=order.price,
            amount=order.amount,
            old_price=order.old_price,
            new_desired_price=order.new_desired_price,
            price_deviation_bps=order.price_deviation_bps,
            old_amount=order.old_amount,
            new_desired_amount=order.new_desired_amount,
            amount_deviation_pct=order.amount_deviation_pct,
            old_mode=order.old_mode,
            new_mode=order.new_mode,
            old_plan_version=order.old_plan_version,
            new_plan_version=order.new_plan_version,
            old_level_present=order.old_level_present,
            new_level_present=order.new_level_present,
            risk_state=order.risk_state,
            inventory_ratio=order.inventory_ratio,
            portfolio_gross_exposure=order.portfolio_gross_exposure,
            portfolio_beta_exposure=order.portfolio_beta_exposure,
            minimum_order_lifetime_seconds=order.minimum_order_lifetime_seconds,
            replacement_cooldown_seconds=order.replacement_cooldown_seconds,
            time_since_last_replace_seconds=order.time_since_last_replace_seconds,
            cooldown_remaining_seconds=order.cooldown_remaining_seconds,
            safety_override=order.safety_override,
            safety_override_reason=order.safety_override_reason,
            lifecycle_state=order.lifecycle_state,
            cancel_market_mid=order.cancel_market_mid,
            cancel_market_best_bid=order.cancel_market_best_bid,
            cancel_market_best_ask=order.cancel_market_best_ask,
            cancel_price_deviation_bps=order.cancel_price_deviation_bps,
            same_cycle_create_cancel=order.same_cycle_create_cancel,
            lifecycle_state_sequence=order.lifecycle_state_sequence,
            terminal_reason=order.terminal_reason,
            cycle_id=order.cycle_id,
            cancel_cycle_id=order.cancel_cycle_id,
            controller_created_timestamp=order.controller_created_timestamp,
            controller_terminal_timestamp=order.controller_terminal_timestamp,
            controller_resting_lifetime_seconds=resting_lifetime,
        )
        return order

    def replace_order(
        self, order_id: str, *, timestamp: float, reason: str, **new_order: Any
    ) -> ShadowOrder:
        decision_context = new_order.pop("decision_context", None)
        reason_code = new_order.pop("reason_code", None)
        old = self.cancel_order(
            order_id,
            timestamp=timestamp,
            reason=reason,
            reason_code=reason_code,
            decision_context=decision_context,
        )
        replacement = self.create_order(timestamp=timestamp, **new_order)
        replacement.last_replace_epoch = timestamp
        self._emit(
            "ORDER_REPLACE",
            timestamp,
            old_shadow_order_id=old.shadow_order_id,
            new_shadow_order_id=replacement.shadow_order_id,
            trading_pair=old.trading_pair,
            level_id=old.level_id,
            reason=reason,
            reason_code=reason_code,
            old_lifecycle_state=old.lifecycle_state,
            new_lifecycle_state=replacement.lifecycle_state,
        )
        return replacement

    def shutdown(
        self,
        *,
        timestamp: float | None = None,
        controller_timestamp: float | None = None,
    ) -> None:
        now = time.time() if timestamp is None else timestamp
        controller_now = (
            controller_timestamp if controller_timestamp is not None else now
        )
        for order_id in list(self.orders):
            if self.orders[order_id].status in {
                ShadowOrderStatus.RESTING,
                ShadowOrderStatus.PARTIALLY_FILLED,
                ShadowOrderStatus.CLOSE_RESTING,
            }:
                self.cancel_order(
                    order_id,
                    timestamp=now,
                    reason="SESSION_SHUTDOWN",
                    reason_code="SESSION_SHUTDOWN",
                    controller_timestamp=controller_now,
                    decision_context={
                        "decision_path": "session_shutdown",
                        "safety_override": True,
                        "safety_override_reason": "session shutdown",
                    },
                )
        self._emit("SESSION_STOP", now, reason="SHUTDOWN")

    def _trade_evidence(
        self, order: ShadowOrder, frame: ShadowMarketFrame
    ) -> tuple[bool, str, float | None]:
        resting_start = order.resting_start_epoch or order.created_epoch
        if frame.timestamp <= resting_start:
            return False, "same-timestamp evidence rejected", None
        side = order.side
        selected = self.config.fill_model
        if selected in {ShadowFillModel.TRADE_PRINT, ShadowFillModel.CONSERVATIVE_TRADE_THROUGH}:
            usable_trade = False
            for trade in frame.trades:
                if trade.timestamp <= resting_start or trade.timestamp > frame.timestamp:
                    continue
                if trade.aggressor_side not in {"buy", "sell"}:
                    continue
                usable_trade = True
                threshold = (
                    (
                        trade.price < float(order.price)
                        if selected is ShadowFillModel.CONSERVATIVE_TRADE_THROUGH
                        else trade.price <= float(order.price)
                    )
                    if side == "buy" and trade.aggressor_side == "sell"
                    else (
                        trade.price > float(order.price)
                        if selected is ShadowFillModel.CONSERVATIVE_TRADE_THROUGH
                        else trade.price >= float(order.price)
                    )
                    if side == "sell" and trade.aggressor_side == "buy"
                    else False
                )
                if threshold:
                    return True, "qualifying public trade passed maker price", trade.price
            if usable_trade:
                return False, "public trades did not pass maker price", None
            return False, "public trade evidence unavailable", None
        if selected is ShadowFillModel.ESTIMATED_QUEUE:
            return False, "estimated queue model requires defensible depletion data", None
        if side == "buy":
            threshold = (
                frame.best_ask <= float(order.price)
                if selected is ShadowFillModel.TOUCH_OPTIMISTIC
                else frame.best_ask < float(order.price)
            )
        else:
            threshold = (
                frame.best_bid >= float(order.price)
                if selected is ShadowFillModel.TOUCH_OPTIMISTIC
                else frame.best_bid > float(order.price)
            )
        return (
            threshold,
            "future BBO qualified" if threshold else "future BBO did not qualify",
            (frame.best_ask if side == "buy" else frame.best_bid) if threshold else None,
        )

    def _markouts(self, fill: ShadowFill, frame: ShadowMarketFrame) -> None:
        if frame.trading_pair != fill.trading_pair or frame.timestamp <= fill.timestamp_epoch:
            return
        if frame.mid_price <= 0 or fill.price <= 0:
            return
        signed = (frame.mid_price - float(fill.price)) / float(fill.price) * 10_000
        if fill.side == "sell":
            signed = -signed
        for horizon in (5, 30, 60):
            key = f"{horizon}s"
            if key not in fill.markouts_bps and frame.timestamp >= fill.timestamp_epoch + horizon:
                fill.markouts_bps[key] = signed
                if self.store is not None:
                    self.store.save_fill(self.session_id, fill)

    def _fill(
        self,
        order: ShadowOrder,
        frame: ShadowMarketFrame,
        evidence: str,
        evidence_price: float | None,
        eligibility_status: str | None = None,
        evidence_trade_id: str | None = None,
        evidence_trade_timestamp: float | None = None,
        controller_timestamp: float | None = None,
        cycle_id: str | None = None,
    ) -> ShadowFill:
        amount = order.remaining_amount
        price = order.price
        inventory_before = self.ledger.position(order.trading_pair).amount
        realized, fee = self.ledger.apply_fill(order.trading_pair, order.side, price, amount)
        inventory_after = self.ledger.position(order.trading_pair).amount
        order.filled_amount += amount
        order.remaining_amount = Decimal("0")
        order.average_fill_price = price
        order.fill_timestamp = _iso(frame.timestamp)
        controller_terminal_epoch = (
            controller_timestamp
            if controller_timestamp is not None
            else frame.timestamp
        )
        controller_created_epoch = order.controller_created_epoch or order.created_epoch
        order.time_to_fill = max(
            0.0,
            controller_terminal_epoch - controller_created_epoch,
        )
        order.updated_timestamp = order.fill_timestamp
        order.updated_epoch = frame.timestamp
        order.status = ShadowOrderStatus.FILLED
        order.terminal_timestamp = order.fill_timestamp
        order.terminal_epoch = frame.timestamp
        order.terminal_reason = "FILLED"
        order.controller_terminal_epoch = controller_terminal_epoch
        order.controller_terminal_timestamp = _iso(controller_terminal_epoch)
        order.create_terminal_latency_ms = max(
            0.0,
            controller_terminal_epoch
            - (order.controller_created_epoch or order.created_epoch),
        ) * 1000.0
        self._append_lifecycle_state(order, ShadowLifecycleState.FILLED_AFTER_RESTING.value)
        fill = ShadowFill(
            fill_id=f"{order.shadow_order_id}:fill:1",
            shadow_order_id=order.shadow_order_id,
            trading_pair=order.trading_pair,
            side=order.side,
            price=price,
            amount=amount,
            notional=price * amount,
            timestamp=order.fill_timestamp,
            timestamp_epoch=frame.timestamp,
            fill_model=self.config.fill_model.value,
            entry_exit="exit" if order.is_exit else "entry",
            time_to_fill=order.time_to_fill,
            fees=fee,
            realized_pnl=realized,
            cycle_id=order.cycle_id,
            quote_distance_bps=order.quote_distance_bps,
            quote_distance_before_fill_bps=order.quote_distance_bps,
            mode=order.mode_at_creation,
            state="FILLED",
            inventory_before=float(inventory_before),
            inventory_after=float(inventory_after),
            evidence=evidence,
            conservative_eligibility_status=eligibility_status,
            evidence_trade_id=evidence_trade_id,
            evidence_trade_timestamp=evidence_trade_timestamp,
        )
        self.fills.append(fill)
        if self.store is not None:
            self.store.save_order(self.session_id, order)
            self.store.save_fill(self.session_id, fill)
            self.store.save_position(self.session_id, fill.timestamp, self.ledger.snapshot())
        self._emit(
            "TP_FILL" if order.is_exit else "ORDER_FILL",
            frame.timestamp,
            shadow_order_id=order.shadow_order_id,
            trading_pair=order.trading_pair,
            level_id=order.level_id,
            side=order.side,
            price=price,
            amount=amount,
            notional=price * amount,
            fill_model=self.config.fill_model.value,
            evidence=evidence,
            evidence_price=evidence_price,
            time_to_fill=order.time_to_fill,
            fees=fee,
            realized_pnl=realized,
            cycle_id=cycle_id,
            controller_created_timestamp=order.controller_created_timestamp,
            controller_terminal_timestamp=order.controller_terminal_timestamp,
        )
        self._emit_lifecycle(
            "ORDER_FILLED",
            frame.timestamp,
            shadow_order_id=order.shadow_order_id,
            trading_pair=order.trading_pair,
            level_id=order.level_id,
            side=order.side,
            price=price,
            amount=amount,
            fill_model=self.config.fill_model.value,
            evidence=evidence,
            evidence_price=evidence_price,
            resting_start_timestamp=order.resting_start_timestamp,
            terminal_timestamp=order.terminal_timestamp,
            resting_lifetime_seconds=order.time_to_fill,
            lifecycle_state=order.lifecycle_state,
            lifecycle_state_sequence=order.lifecycle_state_sequence,
            cycle_id=order.cycle_id,
            terminal_cycle_id=cycle_id,
            controller_created_timestamp=order.controller_created_timestamp,
            controller_terminal_timestamp=order.controller_terminal_timestamp,
        )
        self._emit(
            "POSITION_CHANGE",
            frame.timestamp,
            trading_pair=order.trading_pair,
            position=self.ledger.position(order.trading_pair).amount,
            position_notional=self.ledger.signed_notional(order.trading_pair, frame.mid_price),
        )
        if order.is_exit:
            parent = self.orders.get(order.parent_order_id or "")
            if parent is not None:
                parent.status = ShadowOrderStatus.COMPLETE
                parent.updated_timestamp = order.updated_timestamp
                parent.updated_epoch = order.updated_epoch
                self._append_lifecycle_state(parent, "COMPLETE")
                parent.terminal_timestamp = order.terminal_timestamp
                parent.terminal_epoch = order.terminal_epoch
                parent.controller_terminal_timestamp = order.controller_terminal_timestamp
                parent.controller_terminal_epoch = order.controller_terminal_epoch
                if self.store is not None:
                    self.store.save_order(self.session_id, parent)
                self._emit_lifecycle(
                    "ORDER_COMPLETE",
                    frame.timestamp,
                    shadow_order_id=parent.shadow_order_id,
                    trading_pair=parent.trading_pair,
                    level_id=parent.level_id,
                    side=parent.side,
                    lifecycle_state=parent.lifecycle_state,
                    completed_by_order_id=order.shadow_order_id,
                )
            self.completed_cycles += 1
        elif order.take_profit_price is not None:
            tp = self.create_order(
                trading_pair=order.trading_pair,
                level_id=f"{order.level_id}::tp",
                side="sell" if order.side == "buy" else "buy",
                price=order.take_profit_price,
                amount=amount,
                timestamp=frame.timestamp,
                grid_plan_version=order.grid_plan_version,
                mode=order.mode_at_creation,
                mid_price=frame.mid_price,
                spread_bps=frame.spread_bps,
                best_bid=frame.best_bid,
                best_ask=frame.best_ask,
                parent_order_id=order.shadow_order_id,
                is_exit=True,
                cycle_id=order.cycle_id,
                controller_timestamp=controller_timestamp,
            )
            order.take_profit_order_id = tp.shadow_order_id
            if self.store is not None:
                self.store.save_order(self.session_id, order)
            self._emit_lifecycle(
                "ORDER_TP_CREATED",
                frame.timestamp,
                shadow_order_id=tp.shadow_order_id,
                parent_order_id=order.shadow_order_id,
                trading_pair=order.trading_pair,
                level_id=order.level_id,
                side=tp.side,
                price=tp.price,
                amount=tp.amount,
                lifecycle_state=tp.lifecycle_state,
                cycle_id=tp.cycle_id,
                controller_created_timestamp=tp.controller_created_timestamp,
            )
        return fill

    def process_frame(
        self,
        frame: ShadowMarketFrame,
        *,
        controller_timestamp: float | None = None,
        cycle_id: str | None = None,
    ) -> list[ShadowFill]:
        require_shadow_environment([frame])
        self.latest_frames[frame.trading_pair] = frame
        self.market_history[frame.trading_pair].append(frame)
        filled: list[ShadowFill] = []
        active = [
            order
            for order in self.orders.values()
            if order.trading_pair == frame.trading_pair
            and order.status
            in {
                ShadowOrderStatus.RESTING,
                ShadowOrderStatus.PARTIALLY_FILLED,
                ShadowOrderStatus.CLOSE_RESTING,
            }
        ]
        for order in active:
            qualifies, reason, evidence_price = self._trade_evidence(order, frame)
            order.fill_eligibility_reason = reason
            if qualifies:
                order.fill_eligibility_status = (
                    "TRADED_THROUGH_FILLED"
                    if self.config.fill_model is ShadowFillModel.CONSERVATIVE_TRADE_THROUGH
                    else "TOUCHED_FILLED"
                )
            elif self.config.fill_model in {
                ShadowFillModel.TRADE_PRINT,
                ShadowFillModel.CONSERVATIVE_TRADE_THROUGH,
            }:
                usable_trade = any(
                    trade.timestamp > (order.resting_start_epoch or order.created_epoch)
                    and trade.aggressor_side in {"buy", "sell"}
                    for trade in frame.trades
                )
                touched = (
                    frame.best_ask <= float(order.price)
                    if order.side == "buy"
                    else frame.best_bid >= float(order.price)
                )
                order.fill_eligibility_status = (
                    "INSUFFICIENT_TRADE_EVIDENCE"
                    if not usable_trade
                    else "TOUCHED_NOT_TRADED_THROUGH"
                    if touched
                    else "NEVER_REACHED_PRICE"
                )
            else:
                order.fill_eligibility_status = (
                    "TOUCHED_NOT_TRADED_THROUGH"
                    if not qualifies
                    else "TOUCHED_FILLED"
                )
            if qualifies:
                evidence_trade = next(
                    (
                        trade
                        for trade in frame.trades
                        if trade.timestamp > (order.resting_start_epoch or order.created_epoch)
                        and trade.timestamp <= frame.timestamp
                        and trade.aggressor_side in {"buy", "sell"}
                        and (
                            (
                                order.side == "buy"
                                and trade.aggressor_side == "sell"
                                and (
                                    trade.price < float(order.price)
                                    if self.config.fill_model
                                    is ShadowFillModel.CONSERVATIVE_TRADE_THROUGH
                                    else trade.price <= float(order.price)
                                )
                            )
                            or (
                                order.side == "sell"
                                and trade.aggressor_side == "buy"
                                and (
                                    trade.price > float(order.price)
                                    if self.config.fill_model
                                    is ShadowFillModel.CONSERVATIVE_TRADE_THROUGH
                                    else trade.price >= float(order.price)
                                )
                            )
                        )
                    ),
                    None,
                )
                filled.append(
                    self._fill(
                        order,
                        frame,
                        reason,
                        evidence_price,
                        eligibility_status=order.fill_eligibility_status,
                        evidence_trade_id=evidence_trade.trade_id if evidence_trade else None,
                        evidence_trade_timestamp=(
                            evidence_trade.timestamp if evidence_trade else None
                        ),
                        controller_timestamp=controller_timestamp,
                        cycle_id=cycle_id,
                    )
                )
        self.ledger.mark({pair: current.mid_price for pair, current in self.latest_frames.items()})
        for fill in self.fills:
            self._markouts(fill, frame)
        equity_record = {"timestamp": _iso(frame.timestamp), **self.ledger.snapshot()}
        self.equity_history.append(equity_record)
        if self.store is not None:
            self.store.save_equity(self.session_id, _iso(frame.timestamp), equity_record)
            self.store.save_metrics(
                self.session_id, _iso(frame.timestamp), self.metrics(frame.timestamp)
            )
        return filled

    def _active_levels(self, pair: str) -> list[ActiveLevel]:
        active: list[ActiveLevel] = []
        for order in self.orders.values():
            if order.trading_pair != pair or order.is_exit:
                continue
            if order.status not in {
                ShadowOrderStatus.RESTING,
                ShadowOrderStatus.PARTIALLY_FILLED,
                ShadowOrderStatus.FILLED,
            }:
                continue
            active.append(
                ActiveLevel(
                    executor_id=order.shadow_order_id,
                    level_id=order.level_id,
                    side=ExecutionSide.BUY if order.side == "buy" else ExecutionSide.SELL,
                    price=order.price,
                    amount=order.amount,
                    quote_notional=order.notional,
                    created_at=order.resting_start_epoch or order.created_epoch,
                    is_filled=order.status is ShadowOrderStatus.FILLED,
                    is_active=True,
                    plan_mode=order.mode_at_creation,
                    last_replace_at=order.last_replace_epoch,
                )
            )
        return active

    @staticmethod
    def _adjacent_tp(plan: Any, level_id: str, entry_price: Decimal) -> Decimal:
        side, _, raw_index = level_id.rpartition("_")
        index = int(raw_index) if raw_index.isdigit() else 0
        source = plan.buy_levels if side == "buy" else plan.sell_levels
        target = plan.center_price
        if index > 0:
            previous = next((level for level in source if level.level_index == index - 1), None)
            target = previous.theoretical_price if previous is not None else target
        if (
            target is None
            or target <= 0
            or (side == "buy" and target <= entry_price)
            or (side == "sell" and target >= entry_price)
        ):
            target = entry_price * (Decimal("1.001") if side == "buy" else Decimal("0.999"))
        return target

    def _order_diagnostic_context(
        self,
        *,
        frame: ShadowMarketFrame,
        plan: Any,
        desired: Any,
        portfolio_route_allowed: bool,
    ) -> dict[str, Any]:
        """Build secret-free gate provenance for one already-approved create."""

        state = self.latest_states.get(frame.trading_pair, {})
        relationship = state.get("btc_transmission")
        if isinstance(relationship, Mapping):
            relationship_valid = relationship.get("relationship_valid")
        elif frame.trading_pair == "BTC-USDC":
            relationship_valid = True
        else:
            relationship_valid = None
        global_iv_valid = self.latest_risk.get("btc_iv_available")
        if global_iv_valid is None and frame.option_snapshot is not None:
            global_iv_valid = bool(frame.option_snapshot.data_available)
        state_valid = state.get("state_valid")
        state_confidence = _finite(state.get("confidence"))
        return {
            "theoretical_price": desired.theoretical_price,
            "desired_price": desired.price,
            "desired_amount": desired.amount,
            "desired_notional": desired.quote_notional,
            "quantized_price": desired.price,
            "quantized_amount": desired.amount,
            "post_only_valid": True,
            "maker_valid": True,
            "eligible_to_rest": bool(portfolio_route_allowed),
            "plan_valid_at_create": plan.valid,
            "risk_allowed_at_create": bool(portfolio_route_allowed),
            "minimum_exchange_size_valid": True,
            "portfolio_risk_valid": bool(portfolio_route_allowed),
            "asset_risk_valid": bool(portfolio_route_allowed),
            "market_data_valid": frame.best_bid > 0 and frame.best_ask > frame.best_bid,
            "btc_iv_valid": global_iv_valid,
            "relationship_data_valid": relationship_valid,
            "state_confidence_valid": (
                bool(state_valid) and (state_confidence is None or state_confidence > 0)
                if state_valid is not None
                else None
            ),
            "asset_execution_status": self.execution_status(frame.trading_pair),
            "portfolio_gross_exposure": self.latest_risk.get("gross_notional"),
            "portfolio_beta_exposure": self.latest_risk.get("btc_beta_equivalent_exposure"),
            "risk_state": self.latest_risk.get("global_risk_regime"),
            "decision_path": "reconcile_grid_plan",
        }

    def _record_candidate_eligibility(
        self,
        *,
        plan: Any,
        result: Any,
        frame: ShadowMarketFrame,
        active_levels: Sequence[ActiveLevel],
        portfolio_allowed_level_ids: Sequence[str] | None,
        cycle_id: str | None,
    ) -> dict[str, dict[str, Any]]:
        """Persist the candidate/create/keep/block split before any order is made."""

        desired_by_level = {level.level_id: level for level in result.desired_levels}
        blocked_by_level = {level.level_id: level for level in result.blocked}
        active_by_level = {level.level_id: level for level in active_levels if level.is_active}
        creates = {level.level_id for level in result.creates}
        keeps = set(result.keeps)
        stops = {stop.level_id for stop in result.stops}
        route_is_authoritative = portfolio_allowed_level_ids is not None
        allowed = set(portfolio_allowed_level_ids or ())
        execution_status = self.execution_status(frame.trading_pair)
        rows: dict[str, dict[str, Any]] = {}
        for level in plan.levels:
            selected = level.level_index < self.config.execution_max_levels_per_side
            desired = desired_by_level.get(level.level_id)
            blocked = blocked_by_level.get(level.level_id)
            active = active_by_level.get(level.level_id)
            active_order = self.orders.get(active.executor_id) if active is not None else None
            scoped = f"{frame.trading_pair}::{level.level_id}"
            route_allowed = not route_is_authoritative or scoped in allowed
            if not selected:
                action = "NOT_SELECTED_EXECUTION_LEVEL_CAP"
            elif result.pause_reason:
                action = "PAUSE_SUPPRESSED"
            elif blocked is not None:
                action = "BLOCKED"
            elif level.level_id in creates:
                action = "CREATE_DECISION"
            elif level.level_id in keeps:
                action = "KEEP"
            elif level.level_id in stops:
                action = "STOP"
            elif active is not None:
                action = "ACTIVE_UNCLASSIFIED"
            else:
                action = "NOT_DESIRED"
            raw_action = action
            if action == "CREATE_DECISION" and execution_status != "EXECUTION_ENABLED":
                action = execution_status
            elif action == "CREATE_DECISION" and not route_allowed:
                action = "ROUTE_BLOCKED"
            min_valid = (
                False
                if execution_status == "SIGNAL_ONLY_MIN_SIZE"
                else None
                if blocked is None
                else not any(
                    token in blocked.reason.lower()
                    for token in ("minimum", "amount below", "notional below")
                )
            )
            maker_valid = (
                None
                if blocked is None
                else not any(token in blocked.reason.lower() for token in ("cross", "maker"))
            )
            risk_allowed = bool(
                selected
                and not result.pause_reason
                and blocked is None
                and route_allowed
                and execution_status == "EXECUTION_ENABLED"
                and level.level_id in creates | keeps
            )
            row = {
                "timestamp": _iso(frame.timestamp),
                "timestamp_epoch": frame.timestamp,
                "cycle_id": cycle_id,
                "trading_pair": frame.trading_pair,
                "level_id": level.level_id,
                "side": level.side.value,
                "level_index": level.level_index,
                "candidate_grid_level": True,
                "execution_level_selected": selected,
                "planned_action": action,
                "raw_planned_action": raw_action,
                "final_eligibility": (
                    "ELIGIBLE" if risk_allowed and desired is not None else "BLOCKED"
                ),
                "pre_create_block_category": (
                    blocked.reason_code
                    if blocked is not None and blocked.reason_code
                    else execution_status
                    if action == execution_status and execution_status != "EXECUTION_ENABLED"
                    else "PORTFOLIO_RISK_BLOCK"
                    if action == "ROUTE_BLOCKED"
                    else None
                ),
                "asset_execution_status": execution_status,
                "why_created": (
                    "Stage 4 GridPlan level selected by reconcile_grid_plan"
                    if action == "CREATE_DECISION"
                    else None
                ),
                "theoretical_price": level.theoretical_price,
                "desired_notional": (
                    desired.quote_notional if desired is not None else level.quote_amount
                ),
                "desired_price": desired.price if desired is not None else None,
                "desired_amount": desired.amount if desired is not None else None,
                "quantized_price": desired.price if desired is not None else None,
                "quantized_amount": desired.amount if desired is not None else None,
                "bbo_best_bid_at_create": frame.best_bid,
                "bbo_best_ask_at_create": frame.best_ask,
                "post_only_valid": maker_valid if desired is None else True,
                "maker_valid": maker_valid if desired is None else True,
                "eligible_to_rest": risk_allowed and desired is not None,
                "plan_valid": plan.valid,
                "plan_version": plan.plan_version,
                "mode": plan.mode,
                "risk_allowed": risk_allowed,
                "minimum_exchange_size_valid": min_valid,
                "portfolio_risk_valid": route_allowed and execution_status == "EXECUTION_ENABLED",
                "asset_risk_valid": execution_status == "EXECUTION_ENABLED",
                "market_data_valid": frame.best_bid > 0 and frame.best_ask > frame.best_bid,
                "btc_iv_valid": self.latest_risk.get("btc_iv_available"),
                "relationship_data_valid": (
                    self.latest_states.get(frame.trading_pair, {})
                    .get("btc_transmission", {})
                    .get("relationship_valid")
                    if isinstance(
                        self.latest_states.get(frame.trading_pair, {}).get(
                            "btc_transmission", {}
                        ),
                        Mapping,
                    )
                    else True
                    if frame.trading_pair == "BTC-USDC"
                    else None
                ),
                "state_confidence_valid": (
                    self.latest_states.get(frame.trading_pair, {}).get("state_valid")
                ),
                "blocked_reason": (
                    blocked.reason
                    if blocked is not None
                    else "asset execution route is signal-only"
                    if execution_status != "EXECUTION_ENABLED" and raw_action == "CREATE_DECISION"
                    else "portfolio route blocked level"
                    if raw_action == "CREATE_DECISION" and not route_allowed
                    else None
                ),
                "portfolio_route_allowed": route_allowed,
                "execution_engine_reached": False,
                "shadow_order_id": active.executor_id if active is not None else None,
                "order_instantiated": False,
                "validated": False,
                "entered_resting": False,
                "rejected_before_resting": False,
                "cancelled_before_resting": False,
                "terminal_reason": result.pause_reason
                or "asset execution route is signal-only"
                if execution_status != "EXECUTION_ENABLED" and raw_action == "CREATE_DECISION"
                else result.pause_reason or None,
                "resting_start_timestamp": (
                    active_order.resting_start_timestamp if active_order is not None else None
                ),
                "resting_start_timestamp_before": (
                    active_order.resting_start_timestamp if active_order is not None else None
                ),
                "resting_start_timestamp_after": (
                    active_order.resting_start_timestamp if active_order is not None else None
                ),
            }
            rows[level.level_id] = self.record_order_eligibility_audit(row)
        return rows

    def reconcile_pair(
        self,
        plan_record: Mapping[str, Any],
        *,
        frame: ShadowMarketFrame,
        current_position: float = 0.0,
        portfolio_allowed_level_ids: Sequence[str] | None = None,
        cycle_id: str | None = None,
        controller_timestamp: float | None = None,
    ) -> Any:
        """Run the existing Stage 5 reconciliation and translate actions to virtual orders."""

        plan = parse_grid_plan(plan_record, expected_pair=frame.trading_pair)
        health = RuntimeHealth(
            testnet_verified=False,
            connector_ready=True,
            market_data_ready=True,
            trading_rules_available=True,
            balance_verified=True,
            position_verified=True,
            best_bid=Decimal(str(frame.best_bid)),
            best_ask=Decimal(str(frame.best_ask)),
            position_notional=Decimal(str(abs(current_position * frame.mid_price))),
            available_collateral=self.ledger.current_equity,
            trading_rules=frame.rule,
            environment="mainnet",
            execution_mode="SHADOW",
            environment_verified=True,
            environment_consistent=True,
        )
        policy = self.config.execution_policy()
        active_levels = self._active_levels(frame.trading_pair)
        allowed = set(portfolio_allowed_level_ids or ())
        route_is_authoritative = portfolio_allowed_level_ids is not None
        execution_status = self.execution_status(frame.trading_pair)

        def final_create_gate(desired: Any) -> tuple[bool, str, str]:
            """Re-check every mutable routing input immediately before create."""

            if not plan.valid or not plan.enabled or plan.mode == "pause":
                return False, "PLAN_INVALID_BEFORE_CREATE", "plan is invalid or disabled"
            if execution_status == "SIGNAL_ONLY_MIN_SIZE":
                return (
                    False,
                    "SIGNAL_ONLY_MIN_SIZE",
                    "asset is signal-only pending minimum-size evidence",
                )
            if execution_status == "SIGNAL_ONLY":
                return False, "SIGNAL_ONLY", "asset execution status is signal-only"
            if execution_status == "DISABLED":
                return False, "DATA_CRITICAL", "asset execution route is disabled"
            scoped = f"{frame.trading_pair}::{desired.level_id}"
            if route_is_authoritative and scoped not in allowed:
                return False, "PORTFOLIO_RISK_BLOCK", "portfolio route blocked level"
            if frame.best_bid <= 0 or frame.best_ask <= frame.best_bid:
                return False, "DATA_CRITICAL", "best bid/ask is not valid"
            if self._crosses(desired.side.value, desired.price, frame.best_bid, frame.best_ask):
                return False, "MAKER_SAFETY", "post-only level would cross the book"
            if any(
                item.is_active and not item.is_filled and item.level_id == desired.level_id
                for item in active_levels
            ):
                return False, "DUPLICATE_LEVEL", "active level already exists"
            return True, "", ""

        result = reconcile_grid_plan(
            plan,
            active=active_levels,
            health=health,
            policy=policy,
            now_epoch=frame.timestamp,
            quantize_price=lambda value: _quantize_down(
                value, Decimal(str(self.config.price_increment or frame.rule.min_price_increment))
            ),
            quantize_amount=lambda value: _quantize_down(
                value,
                Decimal(str(self.config.amount_increment or frame.rule.min_base_amount_increment)),
            ),
            final_create_gate=final_create_gate,
        )
        self.latest_plans[frame.trading_pair] = dict(plan_record)
        desired_by_level = {level.level_id: level for level in result.desired_levels}
        active_by_executor = {item.executor_id: item for item in active_levels}
        self.annotate_order_next_frame(
            frame=frame,
            plan_valid=plan.valid,
            plan_version=plan.plan_version,
            mode=plan.mode,
            cycle_id=cycle_id,
            controller_timestamp=controller_timestamp,
        )
        eligibility_rows = self._record_candidate_eligibility(
            plan=plan,
            result=result,
            frame=frame,
            active_levels=active_levels,
            portfolio_allowed_level_ids=portfolio_allowed_level_ids,
            cycle_id=cycle_id,
        )
        plan_context = {
            "decision_path": "reconcile_grid_plan",
            "plan_version": plan.plan_version,
            "new_plan_version": plan.plan_version,
            "plan_valid": plan.valid,
            "plan_enabled": plan.enabled and plan.mode != "pause",
            "plan_mode": plan.mode,
            "plan_age_seconds": result.plan_age_seconds,
            "plan_stale": bool(result.pause_reason and "STALE" in result.pause_reason.upper()),
            "account_state_invalid": not plan.valid,
            "risk_state": self.latest_risk.get("global_risk_regime"),
            "portfolio_gross_exposure": self.latest_risk.get("gross_notional"),
            "portfolio_beta_exposure": self.latest_risk.get(
                "btc_beta_equivalent_exposure"
            ),
            "minimum_order_lifetime_seconds": self.config.minimum_order_lifetime_seconds,
            "replacement_cooldown_seconds": self.config.minimum_replace_interval_seconds,
        }
        for level_id in result.keeps:
            active = next(
                (
                    item
                    for item in active_levels
                    if item.level_id == level_id and not item.is_filled
                ),
                None,
            )
            keep_reason = result.keep_reasons.get(level_id, "KEEP")
            self._emit(
                "ORDER_KEEP",
                frame.timestamp,
                trading_pair=frame.trading_pair,
                level_id=level_id,
                reason=keep_reason,
                cycle_id=cycle_id,
            )
            self._emit_lifecycle(
                "ORDER_KEEP",
                frame.timestamp,
                shadow_order_id=active.executor_id if active else None,
                trading_pair=frame.trading_pair,
                level_id=level_id,
                side=active.side.value if active else None,
                reason=keep_reason,
                decision_path="reconcile_grid_plan",
                plan_version=plan.plan_version,
                mode=plan.mode,
                cycle_id=cycle_id,
            )
            if keep_reason in {"MINIMUM_ORDER_LIFETIME", "REPLACEMENT_COOLDOWN"}:
                elapsed = (
                    max(0.0, frame.timestamp - (active.last_replace_at or active.created_at))
                    if active
                    else None
                )
                self._emit_lifecycle(
                    "ORDER_REPLACE_DEFERRED",
                    frame.timestamp,
                    shadow_order_id=active.executor_id if active else None,
                    trading_pair=frame.trading_pair,
                    level_id=level_id,
                    reason=keep_reason,
                    replace_deferred=True,
                    minimum_order_lifetime_seconds=self.config.minimum_order_lifetime_seconds,
                    replacement_cooldown_seconds=self.config.minimum_replace_interval_seconds,
                    time_since_last_replace_seconds=elapsed,
                )
        for stop in result.stops:
            if stop.executor_id in self.orders:
                old_active = active_by_executor.get(stop.executor_id)
                desired = desired_by_level.get(stop.level_id)
                stop_context = {
                    **plan_context,
                    "new_level_present": desired is not None,
                    "plan_level_present": desired is not None,
                    "old_level_present": old_active is not None,
                    "old_plan_version": (
                        old_active.executor_id
                        and self.orders[old_active.executor_id].grid_plan_version
                        if old_active is not None
                        else None
                    ),
                    "old_mode": (
                        self.orders[old_active.executor_id].mode_at_creation
                        if old_active is not None
                        else None
                    ),
                    "new_mode": plan.mode,
                    "new_plan_version": plan.plan_version,
                    "would_cross_market": stop.reason_code == "MAKER_SAFETY",
                    "safety_override": stop.reason_code == "MAKER_SAFETY",
                    "safety_override_reason": (
                        "minimum lifetime overridden by post-only safety"
                        if stop.reason_code == "MAKER_SAFETY"
                        and old_active is not None
                        and frame.timestamp - old_active.created_at
                        < self.config.minimum_order_lifetime_seconds
                        else "post-only safety"
                        if stop.reason_code == "MAKER_SAFETY"
                        else None
                    ),
                    "price_deviation_bps": (
                        abs(float(self.orders[stop.executor_id].price) - float(desired.price))
                        / float(self.orders[stop.executor_id].price)
                        * 10_000
                        if desired is not None and self.orders[stop.executor_id].price > 0
                        else None
                    ),
                    "amount_deviation_pct": (
                        abs(
                            float(self.orders[stop.executor_id].notional)
                            - float(desired.quote_notional)
                        )
                        / float(desired.quote_notional)
                        if desired is not None and desired.quote_notional > 0
                        else None
                    ),
                    "new_desired_price": desired.price if desired is not None else None,
                    "new_desired_amount": desired.amount if desired is not None else None,
                    "minimum_order_lifetime_seconds": self.config.minimum_order_lifetime_seconds,
                    "replacement_cooldown_seconds": self.config.minimum_replace_interval_seconds,
                    "next_frame_timestamp": (
                        self.orders[stop.executor_id].next_frame_timestamp
                        if stop.executor_id in self.orders
                        else None
                    ),
                    "plan_valid_next_frame": (
                        self.orders[stop.executor_id].plan_valid_next_frame
                        if stop.executor_id in self.orders
                        else None
                    ),
                    "cycle_id": cycle_id,
                    "controller_timestamp": controller_timestamp,
                }
                self.cancel_order(
                    stop.executor_id,
                    timestamp=frame.timestamp,
                    reason=stop.reason_code or stop.reason,
                    market_mid=frame.mid_price,
                    market_best_bid=frame.best_bid,
                    market_best_ask=frame.best_ask,
                    reason_code=stop.reason_code or None,
                    controller_timestamp=controller_timestamp,
                    decision_context=stop_context,
                )
        for blocked in result.blocked:
            self._record_risk_block(
                frame.timestamp,
                trading_pair=frame.trading_pair,
                level_id=blocked.level_id,
                reason=blocked.reason,
                side=blocked.side.value if blocked.side is not None else None,
                candidate_notional=blocked.quote_amount,
                exposure_before=float(plan_context.get("portfolio_gross_exposure") or 0.0),
                exposure_after_candidate=(
                    float(plan_context.get("portfolio_gross_exposure") or 0.0)
                    + float(blocked.quote_amount)
                ),
                decision_path="quantize_or_risk_gate",
                plan_version=plan.plan_version,
                mode=plan.mode,
            )
        if result.pause_reason:
            self._record_risk_block(
                frame.timestamp,
                trading_pair=frame.trading_pair,
                reason=result.pause_reason,
                decision_path="plan_or_health_pause",
                plan_version=plan.plan_version,
                mode=plan.mode,
                plan_valid=plan.valid,
                plan_enabled=plan.enabled,
                plan_age_seconds=result.plan_age_seconds,
            )
        self.risk_episodes.record_check(len(result.creates))
        audit = {
            "timestamp": _iso(frame.timestamp),
            "timestamp_epoch": frame.timestamp,
            "cycle_id": cycle_id,
            "trading_pair": frame.trading_pair,
            "plan_version": plan.plan_version,
            "mode": plan.mode,
            "plan_valid": plan.valid,
            "plan_enabled": plan.enabled,
            "desired_level_ids": [level.level_id for level in plan.levels],
            "active_level_ids": [item.level_id for item in active_levels if item.is_active],
            "create_count": len(result.creates),
            "keep_count": len(result.keeps),
            "stop_count": len(result.stops),
            "skip_count": len(result.blocked),
            "defer_count": result.deferred_create_count
            + sum(
                result.keep_reasons.get(level_id)
                in {"MINIMUM_ORDER_LIFETIME", "REPLACEMENT_COOLDOWN"}
                for level_id in result.keeps
            ),
            "risk_block_count": len(result.blocked) + bool(result.pause_reason),
            "filled_managed_count": sum(item.is_filled for item in active_levels),
            "tp_managed_count": sum(
                order.is_exit
                and order.trading_pair == frame.trading_pair
                and order.status
                in {
                    ShadowOrderStatus.CLOSE_RESTING,
                    ShadowOrderStatus.PARTIALLY_FILLED,
                    ShadowOrderStatus.FILLED,
                }
                for order in self.orders.values()
            ),
            "pause_reason": result.pause_reason or None,
            "pending_buy_notional": result.pending_buy_notional,
            "pending_sell_notional": result.pending_sell_notional,
            "potential_long_exposure": result.potential_long_exposure,
            "potential_short_exposure": result.potential_short_exposure,
            "candidate_level_count": len(plan.levels),
            "execution_candidate_count": sum(
                level.level_index < self.config.execution_max_levels_per_side
                for level in plan.levels
            ),
            "portfolio_route_authoritative": portfolio_allowed_level_ids is not None,
        }
        self.record_reconciliation_audit(audit)
        if result.stops:
            return result
        allowed = set(portfolio_allowed_level_ids or ())
        for desired in result.creates:
            scoped = f"{frame.trading_pair}::{desired.level_id}"
            route_allowed = portfolio_allowed_level_ids is None or scoped in allowed
            candidate_row = eligibility_rows.get(desired.level_id)
            if not route_allowed:
                if candidate_row is not None:
                    candidate_row.update(
                        {
                            "planned_action": "ROUTE_BLOCKED",
                            "blocked_reason": "portfolio route blocked level",
                            "portfolio_route_allowed": False,
                            "risk_allowed": False,
                            "portfolio_risk_valid": False,
                            "asset_risk_valid": False,
                            "eligible_to_rest": False,
                            "terminal_reason": "PORTFOLIO_RISK_BLOCK",
                        }
                    )
                self._record_risk_block(
                    frame.timestamp,
                    trading_pair=frame.trading_pair,
                    level_id=desired.level_id,
                    reason="portfolio route blocked level",
                    side=desired.side.value,
                    candidate_notional=desired.quote_notional,
                    decision_path="portfolio_route",
                    plan_version=plan.plan_version,
                    mode=plan.mode,
                )
                continue
            tp_price = self._adjacent_tp(plan, desired.level_id, desired.price)
            order = self.create_order(
                trading_pair=frame.trading_pair,
                level_id=desired.level_id,
                side=desired.side.value,
                price=desired.price,
                amount=desired.amount,
                timestamp=frame.timestamp,
                grid_plan_version=desired.plan_version,
                mode=desired.mode,
                mid_price=frame.mid_price,
                spread_bps=frame.spread_bps,
                best_bid=frame.best_bid,
                best_ask=frame.best_ask,
                take_profit_price=tp_price,
                cycle_id=cycle_id,
                controller_timestamp=controller_timestamp,
                diagnostic_context=self._order_diagnostic_context(
                    frame=frame,
                    plan=plan,
                    desired=desired,
                    portfolio_route_allowed=route_allowed,
                ),
            )
            if candidate_row is not None:
                candidate_row.update(
                    {
                        "planned_action": "ORDER_INSTANTIATED",
                        "shadow_order_id": order.shadow_order_id,
                        "execution_engine_reached": order.reached_execution_engine,
                        "order_instantiated": True,
                        "validated": order.validated_timestamp is not None,
                        "entered_resting": order.resting_start_timestamp is not None,
                        "rejected_before_resting": order.status is ShadowOrderStatus.REJECTED,
                        "cancelled_before_resting": False,
                        "terminal_reason": order.terminal_reason,
                    }
                )
        return result

    def metrics(self, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else now
        counts = Counter(str(event.get("event", "")) for event in self.events)
        created = counts["ORDER_CREATE"]
        cancelled = counts["ORDER_CANCEL"]
        fills = counts["ORDER_FILL"] + counts["TP_FILL"]
        keep = counts["ORDER_KEEP"]
        order_lifetimes: list[float] = []
        never_rested = 0
        for order in self.orders.values():
            if order.resting_start_epoch is None:
                never_rested += 1
                continue
            resting_start = order.controller_created_epoch or order.resting_start_epoch
            terminal = order.controller_terminal_epoch or current
            order_lifetimes.append(max(0.0, terminal - resting_start))
        cancel_orders = [order for order in self.orders.values() if order.cancel_timestamp]
        operational_cancel_orders = [
            order
            for order in cancel_orders
            if order.cancel_reason_category not in {"SESSION_SHUTDOWN", "MANUAL_STOP"}
        ]
        shutdown_cancel_orders = [
            order
            for order in cancel_orders
            if order.cancel_reason_category in {"SESSION_SHUTDOWN", "MANUAL_STOP"}
        ]
        cancel_reason_counts = Counter(
            order.cancel_reason_category or "UNKNOWN_INTERNAL" for order in cancel_orders
        )
        risk_episode_rows = self.risk_episodes.rows(current)
        deviation_buckets: Counter[str] = Counter()
        for order in cancel_orders:
            value = order.price_deviation_bps
            bucket = (
                "UNKNOWN"
                if value is None
                else "0-5bps"
                if value < 5
                else "5-12bps"
                if value < 12
                else "12-20bps"
                if value <= 20
                else ">20bps"
            )
            deviation_buckets[bucket] += 1
        entry_fills = [fill for fill in self.fills if fill.entry_exit == "entry"]
        volume_buy = sum(fill.notional for fill in self.fills if fill.side == "buy")
        volume_sell = sum(fill.notional for fill in self.fills if fill.side == "sell")
        active = sum(
            order.status
            in {
                ShadowOrderStatus.RESTING,
                ShadowOrderStatus.PARTIALLY_FILLED,
                ShadowOrderStatus.CLOSE_RESTING,
            }
            for order in self.orders.values()
        )
        per_asset_volume: dict[str, float] = defaultdict(float)
        for fill in self.fills:
            per_asset_volume[fill.trading_pair] += float(fill.notional)
        return {
            "timestamp": _iso(current),
            "paper_equity": self.ledger.current_equity,
            "session_pnl": self.ledger.total_pnl,
            "gross_total_pnl": self.ledger.gross_total_pnl,
            "realized_grid_capture": self.ledger.realized_grid_capture,
            "realized_pnl": self.ledger.realized_pnl,
            "unrealized_inventory_pnl": self.ledger.unrealized_inventory_pnl,
            "fees": self.ledger.fees,
            "fees_status": "KNOWN" if self.ledger.fees_known else "UNKNOWN",
            "starting_equity": self.ledger.starting_equity,
            "high_water_mark": self.ledger.high_water_mark,
            "drawdown_quote": self.ledger.drawdown_quote,
            "drawdown_pct": self.ledger.drawdown_pct,
            "orders_created": created,
            "orders_kept": keep,
            "orders_cancelled": cancelled,
            "orders_replaced": counts["ORDER_REPLACE"],
            "orders_expired": sum(
                order.status is ShadowOrderStatus.EXPIRED for order in self.orders.values()
            ),
            "orders_rejected": counts["ORDER_REJECT"],
            "orders_filled": len(entry_fills),
            "orders_partially_filled": counts["ORDER_PARTIAL_FILL"],
            "tp_orders_created": counts["TP_CREATE"],
            "tp_orders_filled": counts["TP_FILL"],
            "active_orders": active,
            "active_positions": sum(
                position.amount != 0 for position in self.ledger.positions.values()
            ),
            "completed_cycles": self.completed_cycles,
            "fills": fills,
            "buy_volume_quote": volume_buy,
            "sell_volume_quote": volume_sell,
            "total_trade_volume_quote": volume_buy + volume_sell,
            "per_asset_volume": dict(per_asset_volume),
            "fill_create_ratio": fills / created if created else None,
            "cancel_create_ratio": cancelled / created if created else None,
            "keep_rate": keep / (keep + created) if keep + created else None,
            "cancels_per_hour": cancelled * 3600.0 / max(1.0, current - self._session_start_epoch)
            if hasattr(self, "_session_start_epoch")
            else None,
            "median_quote_lifetime": (
                sorted(order_lifetimes)[len(order_lifetimes) // 2] if order_lifetimes else None
            ),
            "average_quote_lifetime": (
                sum(order_lifetimes) / len(order_lifetimes) if order_lifetimes else None
            ),
            "resting_lifetime_sample_count": len(order_lifetimes),
            "resting_lifetime_excluded_never_rested": never_rested,
            "resting_lifetime_p25": (
                sorted(order_lifetimes)[int((len(order_lifetimes) - 1) * 0.25)]
                if order_lifetimes
                else None
            ),
            "resting_lifetime_p75": (
                sorted(order_lifetimes)[int((len(order_lifetimes) - 1) * 0.75)]
                if order_lifetimes
                else None
            ),
            "resting_lifetime_p90": (
                sorted(order_lifetimes)[int((len(order_lifetimes) - 1) * 0.90)]
                if order_lifetimes
                else None
            ),
            "lifecycle_state_counts": dict(
                Counter(order.lifecycle_state for order in self.orders.values())
            ),
            "cancel_reason_counts": {
                category: cancel_reason_counts.get(category, 0) for category in CANCEL_TAXONOMY
            },
            "unknown_internal_cancel_count": cancel_reason_counts.get("UNKNOWN_INTERNAL", 0),
            "operational_cancels": len(operational_cancel_orders),
            "shutdown_cancels": len(shutdown_cancel_orders),
            "operational_cancel_create_ratio": (
                len(operational_cancel_orders) / created if created else None
            ),
            "operational_replacements": sum(
                event.get("event") == "ORDER_REPLACE"
                and event.get("reason") not in {"SESSION_SHUTDOWN", "MANUAL_STOP"}
                for event in self.events
            ),
            "cancellation_deviation_buckets": dict(deviation_buckets),
            "risk_checks_total": self.risk_episodes.risk_checks_total,
            "risk_blocks_raw": self.risk_episodes.raw_blocks_total,
            "risk_block_rate": (
                self.risk_episodes.raw_blocks_total / self.risk_episodes.risk_checks_total
                if self.risk_episodes.risk_checks_total
                else 0.0
            ),
            "unique_risk_episodes": len(risk_episode_rows),
            "unique_episode_rate": (
                len(risk_episode_rows) / self.risk_episodes.risk_checks_total
                if self.risk_episodes.risk_checks_total
                else 0.0
            ),
            "duration_blocked_seconds": sum(
                float(row.get("blocked_seconds", 0.0) or 0.0) for row in risk_episode_rows
            ),
            "risk_episode_duration_median_seconds": (
                sorted(float(row.get("blocked_seconds", 0.0) or 0.0) for row in risk_episode_rows)[
                    len(risk_episode_rows) // 2
                ]
                if risk_episode_rows
                else None
            ),
            "risk_episode_duration_p90_seconds": (
                sorted(float(row.get("blocked_seconds", 0.0) or 0.0) for row in risk_episode_rows)[
                    min(len(risk_episode_rows) - 1, int(len(risk_episode_rows) * 0.90))
                ]
                if risk_episode_rows
                else None
            ),
            "risk_episode_rows": risk_episode_rows,
            "risk_episode_summary": self.risk_episodes.summary(current),
            "reconciliation_decisions": list(self.reconciliation_audit),
            "gross_pnl": self.ledger.gross_total_pnl,
            "verified_net_pnl": self.ledger.total_pnl if self.ledger.fees_known else None,
            "fee_model": self.config.fee_model,
            "fee_model_status": "KNOWN" if self.ledger.fees_known else "UNKNOWN",
            "real_exchange_mutation_calls": self.real_exchange_mutation_calls,
            "blocked_mutation_attempts": self.blocked_mutation_attempts
            + (self.exchange.blocked_attempts if self.exchange is not None else 0),
            "fill_model": self.config.fill_model.value,
            "environment": "mainnet",
            "orders_are_simulated": True,
        }

    def attempt_exchange_mutation(self, method: str) -> None:
        """Test hook proving that a mutation would fail before a call."""

        self.blocked_mutation_attempts += 1
        raise ShadowModeExchangeMutationBlocked(method)


class ShadowSession:
    """Closed-loop coordinator plus lifecycle persistence for a shadow session."""

    def __init__(
        self,
        config: ShadowConfig,
        *,
        coordinator: Any | None = None,
        engine: ShadowExecutionEngine | None = None,
        store: ShadowStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.config = config
        self.session_id = (
            session_id
            or f"shadow-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        )
        self.store = store or (
            ShadowStore(config.sqlite_path, config.event_path) if config.persistence else None
        )
        self.engine = engine or ShadowExecutionEngine(
            config, session_id=self.session_id, store=self.store
        )
        from .multi_asset import MultiAssetConfig, MultiAssetCoordinator

        mode_config = ModeSelectorConfig()
        if config.stage13.enabled:
            mode_config = mode_config.model_copy(
                update={
                    "strategy_pause_entry_confirm_seconds": (
                        config.stage13.regime_pause_entry_confirm_seconds
                    ),
                    "strategy_pause_exit_confirm_seconds": (
                        config.stage13.regime_pause_exit_confirm_seconds
                    ),
                }
            )
        status_by_market = (
            effective_asset_status(
                config.stage13,
                config.markets,
                config.enabled_markets,
            )
            if config.stage13.enabled
            else {}
        )
        self.coordinator = coordinator or MultiAssetCoordinator(
            MultiAssetConfig(
                market_environment="mainnet",
                execution_mode="SHADOW",
                enabled_markets=config.enabled_markets,
                supported_markets=config.markets,
                execution_enabled=False,
                allow_mainnet_trading=False,
                mode=mode_config,
                execution_status_by_market=status_by_market,
                use_incremental_pending_exposure_for_reconciliation=(
                    config.stage13.enabled
                    and config.stage13.use_incremental_pending_exposure_for_reconciliation
                ),
            )
        )
        self.start_timestamp: str | None = None
        self.stop_timestamp: str | None = None
        self._session_start_epoch = 0.0
        self.cycles: list[dict[str, Any]] = []

    def start(self, *, timestamp: float | None = None) -> None:
        now = time.time() if timestamp is None else timestamp
        self._session_start_epoch = now
        self.engine._session_start_epoch = now
        self.start_timestamp = _iso(now)
        self.engine._emit(
            "SESSION_START",
            now,
            mode="MAINNET SHADOW",
            data="REAL DERIVE MAINNET",
            orders="SIMULATED",
            funds="PAPER ONLY",
            real_exchange_actions=0,
            shadow_config_hash=self.config.config_hash,
            strategy_config_hash=self.config.strategy_config_hash,
            strategy_profile=self.config.strategy_profile,
            markets=list(self.config.markets),
            fill_model=self.config.fill_model.value,
        )
        if self.store is not None:
            self.store.save_session(
                self.session_id,
                self.config.config_hash,
                {
                    "session_id": self.session_id,
                    "start_timestamp": self.start_timestamp,
                    "config": self.config.to_record(),
                },
            )

    def run_cycle(
        self,
        frames: Mapping[str, ShadowMarketFrame],
        *,
        global_risk_state: Any | None = None,
        timestamp: float | None = None,
        controller_timestamp: float | None = None,
    ) -> MultiAssetCycle:
        if self.start_timestamp is None:
            self.start(timestamp=timestamp or max(frame.timestamp for frame in frames.values()))
        require_shadow_environment(frames.values())
        controller_epoch = (
            controller_timestamp
            if controller_timestamp is not None
            else timestamp
            if timestamp is not None
            else time.time()
        )
        cycle_id = f"cycle::{self.session_id}::{len(self.cycles) + 1}"
        options = next(
            (frame.option_snapshot for frame in frames.values() if frame.option_snapshot), None
        )
        if options is not None:
            require_shadow_environment([{"environment": options.environment}])
        for frame in frames.values():
            self.engine.process_frame(
                frame,
                controller_timestamp=controller_epoch,
                cycle_id=cycle_id,
            )
        snapshots = {
            pair: frame.to_strategy_snapshot(
                current_position=float(self.engine.ledger.position(pair).amount)
            )
            for pair, frame in frames.items()
        }
        positions = {
            pair: float(self.engine.ledger.signed_notional(pair, frame.mid_price))
            for pair, frame in frames.items()
        }
        # Pending entries are supplied separately below.  Count only filled
        # entry executors here so the governor does not add the same pending
        # executor twice when enforcing capacity.
        active_counts = {
            pair: sum(
                order.trading_pair == pair
                and not order.is_exit
                and order.status
                is ShadowOrderStatus.FILLED
                for order in self.engine.orders.values()
            )
            for pair in frames
        }
        pending_entries: dict[str, dict[str, float]] = {
            pair: {"buy": 0.0, "sell": 0.0, "count": 0.0} for pair in frames
        }
        existing_entries: dict[str, dict[str, dict[str, Any]]] = {
            pair: {} for pair in frames
        }
        for order in self.engine.orders.values():
            if (
                order.is_exit
                or order.status
                not in {ShadowOrderStatus.RESTING, ShadowOrderStatus.PARTIALLY_FILLED}
                or order.trading_pair not in pending_entries
            ):
                continue
            pending_entries[order.trading_pair][order.side] += float(order.notional)
            pending_entries[order.trading_pair]["count"] += 1.0
            existing_entries[order.trading_pair][
                f"{order.trading_pair}::{order.level_id}"
            ] = {
                "notional": float(order.notional),
                "side": order.side,
                "shadow_order_id": order.shadow_order_id,
            }
        cycle = self.coordinator.update(
            snapshots,
            positions=positions,
            pending_entries=pending_entries,
            active_executors=active_counts,
            global_risk_state=global_risk_state,
            existing_entries=existing_entries,
        )
        self.engine.latest_cycle = cycle
        self.engine.latest_states = {
            pair: state.model_dump(mode="json") for pair, state in cycle.states.items()
        }
        self.engine.latest_risk = cycle.portfolio_risk.model_dump(mode="json")
        self.engine.risk_delta_audit.extend(
            dict(row) for row in cycle.portfolio_risk.risk_delta_audit
        )
        blocked_level_ids = {
            level_id
            for level_ids in cycle.portfolio_risk.blocked_level_ids.values()
            for level_id in level_ids
        }
        if blocked_level_ids:
            for pair, plan in cycle.plans.items():
                for level in (*plan.buy_levels, *plan.sell_levels):
                    level_id = f"{level.side.value}_{level.level_index}"
                    scoped_id = f"{pair}::{level_id}"
                    if scoped_id not in blocked_level_ids:
                        continue
                    self.engine._record_risk_block(
                        _epoch(cycle.timestamp) or time.time(),
                        trading_pair=pair,
                        level_id=level_id,
                        event="PORTFOLIO_RISK_BLOCK",
                        candidate_notional=level.quote_amount,
                        exposure_before=cycle.portfolio_risk.per_asset_exposure.get(pair, 0.0),
                        exposure_after_candidate=(
                            cycle.portfolio_risk.per_asset_exposure.get(pair, 0.0)
                            + float(level.quote_amount)
                        ),
                        reason="; ".join(cycle.portfolio_risk.reasons)
                        or "portfolio risk governor block",
                    )
        for pair, plan in cycle.plans.items():
            frame = frames.get(pair)
            if frame is None:
                continue
            route = cycle.routes.get(pair)
            self.engine.reconcile_pair(
                plan.to_record(),
                frame=frame,
                current_position=float(self.engine.ledger.position(pair).amount),
                portfolio_allowed_level_ids=(route.allowed_level_ids if route else None),
                cycle_id=cycle_id,
                controller_timestamp=controller_epoch,
            )
        self.engine.record_risk_reservation_snapshot(
            timestamp=_epoch(cycle.timestamp) or time.time(),
            frames=frames,
            portfolio_risk=cycle.portfolio_risk.model_dump(mode="json"),
            active_executor_inputs=active_counts,
            pending_entries_before=pending_entries,
        )
        record = {
            "cycle_id": cycle_id,
            "controller_timestamp": _iso(controller_epoch),
            "timestamp": cycle.timestamp,
            "global_risk": cycle.global_risk.model_dump(mode="json"),
            "relationships": {
                pair: value.model_dump(mode="json")
                for pair, value in cycle.relationships.items()
            },
            "plans": {pair: plan.to_record() for pair, plan in cycle.plans.items()},
            "decisions": {
                pair: decision.model_dump(mode="json") for pair, decision in cycle.decisions.items()
            },
            "states": {pair: state.model_dump(mode="json") for pair, state in cycle.states.items()},
            "routes": {pair: route.model_dump(mode="json") for pair, route in cycle.routes.items()},
            "portfolio_risk": cycle.portfolio_risk.model_dump(mode="json"),
            "pending_entries": pending_entries,
            "active_executor_inputs": active_counts,
            "risk_reservation": next(
                (
                    row
                    for row in reversed(self.engine.risk_reservation_audit)
                    if row.get("scope") == "PORTFOLIO"
                ),
                {},
            ),
            "metrics": self.engine.metrics(_epoch(cycle.timestamp) or time.time()),
        }
        self.cycles.append(record)
        if self.store is not None:
            self.store.save_cycle(self.session_id, cycle.timestamp, record)
        return cycle

    def stop(self, *, timestamp: float | None = None, reason: str = "MANUAL") -> Path:
        now = time.time() if timestamp is None else timestamp
        if self.start_timestamp is None:
            self.start(timestamp=now)
        self.engine.shutdown(timestamp=now, controller_timestamp=now)
        self.stop_timestamp = _iso(now)
        summary = self.summary(now=now, reason=reason)
        report_path = self.write_report(summary)
        if self.store is not None:
            self.store.save_session(
                self.session_id,
                self.config.config_hash,
                summary,
                stopped_at=self.stop_timestamp,
            )
            self.store.save_metrics(self.session_id, self.stop_timestamp, summary["metrics"])
            self.store.close()
        return report_path

    def summary(self, *, now: float | None = None, reason: str = "MANUAL") -> dict[str, Any]:
        current = time.time() if now is None else now
        metrics = self.engine.metrics(current)
        duration = (
            max(0.0, current - self._session_start_epoch) if self._session_start_epoch else 0.0
        )
        try:
            git_commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            git_commit = None
        return {
            "session_id": self.session_id,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.stop_timestamp or _iso(current),
            "duration_seconds": duration,
            "reason": reason,
            "shadow_config_hash": self.config.config_hash,
            "strategy_config_hash": self.config.strategy_config_hash,
            "strategy_profile": self.config.strategy_profile,
            "git_commit": git_commit,
            "markets": list(self.config.markets),
            "enabled_markets": list(self.config.enabled_markets),
            "market_environment": "mainnet",
            "execution_mode": "SHADOW",
            "fill_model": self.config.fill_model.value,
            "starting_equity": self.engine.ledger.starting_equity,
            "metrics": metrics,
            "environment_consistency": SHADOW_ENVIRONMENT_CONSISTENCY_PASS,
            "real_exchange_mutation_calls": self.engine.real_exchange_mutation_calls,
            "orders": len(self.engine.orders),
            "fills": len(self.engine.fills),
            "limitations": [
                "shadow orders never enter Derive matching-engine queue",
                "queue priority and latency are estimated or unknown",
                "simulated fills and paper PnL are not live fills or live PnL",
                "partial fills are not fabricated when evidence is insufficient",
                "paper margin and liquidation are conservative approximations",
            ],
        }

    def write_report(self, summary: Mapping[str, Any]) -> Path:
        root = Path(self.config.report_root).expanduser() / self.session_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "summary.json").write_text(
            json.dumps(_json_value(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._write_csv(
            root / "orders.csv", [order.to_record() for order in self.engine.orders.values()]
        )
        self._write_csv(root / "fills.csv", [fill.to_record() for fill in self.engine.fills])
        self._write_csv(
            root / "cancels.csv",
            [event for event in self.engine.events if event.get("event") == "ORDER_CANCEL"],
        )
        self._write_csv(
            root / "equity.csv",
            self.engine.equity_history,
        )
        self._write_csv(root / "metrics.csv", [self.engine.metrics()])
        self._write_csv(
            root / "risk_events.csv",
            [event for event in self.engine.events if event.get("event") == "RISK_BLOCK"],
        )
        metrics = summary.get("metrics", {}) if isinstance(summary, Mapping) else {}
        quality = self._quality(metrics if isinstance(metrics, Mapping) else {})
        lines = [
            "# Derive Mainnet Shadow Session",
            "",
            SHADOW_BANNER,
            "",
            f"- Session: `{self.session_id}`",
            f"- Execution quality: **{quality}**",
            f"- Fill model: `{self.config.fill_model.value}`",
            f"- Real exchange mutation calls: **{self.engine.real_exchange_mutation_calls}**",
            f"- Paper equity: `{metrics.get('paper_equity')}`",
            f"- Shadow PnL: `{metrics.get('session_pnl')}`",
            f"- Executed shadow volume: `{metrics.get('total_trade_volume_quote')}`",
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in summary.get("limitations", [])],
            "",
            "No profitability or live-PnL claim is made by this report.",
        ]
        md_path = root / "summary.md"
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return md_path

    @staticmethod
    def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
        keys = sorted({key for row in rows for key in row})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys or ["timestamp"])
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: json.dumps(_json_value(value), sort_keys=True)
                        if isinstance(value, (dict, list, tuple))
                        else _json_value(value)
                        for key, value in row.items()
                    }
                )

    @staticmethod
    def _quality(metrics: Mapping[str, Any]) -> str:
        if metrics.get("real_exchange_mutation_calls", 0) != 0:
            return "POOR"
        if not metrics.get("orders_created"):
            return "INSUFFICIENT DATA"
        if metrics.get("fill_create_ratio") is None:
            return "MIXED"
        return "GOOD" if metrics.get("cancel_create_ratio", 0) <= 2 else "MIXED"


class DerivePublicTradeWebSocket:
    """Bounded, public-only Derive trade stream used by shadow diagnostics."""

    def __init__(self, *, url: str, reconnect_delay_seconds: float = 1.0) -> None:
        self.url = url
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self._currencies: set[str] = set()
        self._task: asyncio.Task[Any] | None = None
        self._trades: dict[str, list[ShadowTrade]] = defaultdict(list)
        self._seen_trade_ids: dict[str, set[str]] = defaultdict(set)
        self._lock = threading.Lock()
        self.connection_status = "DISCONNECTED"
        self.reconnect_count = 0
        self.last_error: str | None = None
        self.connected_since_epoch: float | None = None

    async def ensure(self, currencies: Sequence[str]) -> None:
        self._currencies.update(str(currency).upper() for currency in currencies)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        deadline = asyncio.get_running_loop().time() + 0.75
        while (
            self.connection_status == "DISCONNECTED"
            and not self._task.done()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        self.connection_status = "CLOSED"

    def snapshot(self, currency: str, *, start: float, end: float) -> tuple[ShadowTrade, ...]:
        with self._lock:
            values = list(self._trades.get(currency.upper(), ()))
        return tuple(
            trade
            for trade in values
            if start <= trade.timestamp <= end
        )

    async def _run(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - depends on environment packaging
            self.last_error = f"websockets unavailable: {exc}"
            self.connection_status = "UNAVAILABLE"
            return
        while True:
            channels = [f"trades.perp.{currency}" for currency in sorted(self._currencies)]
            if not channels:
                await asyncio.sleep(0.1)
                continue
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    close_timeout=5,
                ) as websocket:
                    self.connection_status = "CONNECTED"
                    self.connected_since_epoch = time.time()
                    await websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "subscribe",
                                "params": {"channels": channels},
                            }
                        )
                    )
                    async for raw in websocket:
                        message = json.loads(raw)
                        params = message.get("params") if isinstance(message, dict) else None
                        if not isinstance(params, Mapping):
                            continue
                        channel = str(params.get("channel", ""))
                        if not channel.startswith("trades.perp."):
                            continue
                        currency = channel.rsplit(".", 1)[-1].upper()
                        data = params.get("data", [])
                        rows = data if isinstance(data, list) else [data]
                        parsed = canonical_trade_rows(
                            rows,
                            instrument_name=f"{currency}-PERP",
                            timestamp_unit="milliseconds",
                        )
                        with self._lock:
                            for row in parsed["rows"]:
                                trade_id = str(row["trade_id"]) if row.get("trade_id") else None
                                if trade_id and trade_id in self._seen_trade_ids[currency]:
                                    continue
                                if trade_id:
                                    self._seen_trade_ids[currency].add(trade_id)
                                self._trades[currency].append(
                                    ShadowTrade(
                                        timestamp=float(row["timestamp"]),
                                        price=float(row["price"]),
                                        amount=float(row["amount"]),
                                        aggressor_side=row.get("aggressor_side"),
                                        trade_id=trade_id,
                                    )
                                )
                            cutoff = time.time() - 3_600.0
                            self._trades[currency] = [
                                trade
                                for trade in self._trades[currency]
                                if trade.timestamp >= cutoff
                            ]
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - exercised by live reconnects
                self.connection_status = "DISCONNECTED"
                self.connected_since_epoch = None
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.reconnect_count += 1
                await asyncio.sleep(self.reconnect_delay_seconds)


class MainnetPublicDataSource:
    """Read-only Derive mainnet ticker/instrument/trade adapter for shadow runs."""

    def __init__(
        self,
        *,
        client: DerivePublicClient | None = None,
        options_provider: DeriveOptionsProvider | None = None,
        base_url: str = MAINNET_OPTIONS_API_BASE_URL,
        request_timeout_seconds: float = 10.0,
        trade_history_enabled: bool = True,
        trade_transport: str = "websocket",
        trade_window_seconds: float = 60.0,
        trade_sample_interval_seconds: float = 5.0,
        trade_page_size: int = 1000,
        trade_websocket_url: str = "wss://api.lyra.finance/ws",
        trade_crosscheck_interval_seconds: float = 60.0,
        trade_safety_overlap_seconds: float = 2.0,
        market_data_stale_seconds: float = 15.0,
    ) -> None:
        profile = environment_profile("mainnet")
        if base_url.rstrip("/") != profile.options_api_base_url.rstrip("/"):
            raise ShadowEnvironmentError(
                "mainnet shadow data source cannot use the testnet API URL"
            )
        self.client = client or DerivePublicClient(
            base_url=profile.options_api_base_url,
            timeout_seconds=request_timeout_seconds,
        )
        self.options_provider = options_provider or DeriveOptionsProvider(
            base_url=profile.options_api_base_url,
            environment="mainnet",
            request_timeout_seconds=request_timeout_seconds,
        )
        self.trade_history_enabled = trade_history_enabled
        normalized_transport = str(trade_transport).strip().lower()
        if normalized_transport not in {"websocket", "rest"}:
            raise ValueError("trade_transport must be websocket or rest")
        if trade_window_seconds <= 0:
            raise ValueError("trade_window_seconds must be positive")
        if trade_sample_interval_seconds <= 0:
            raise ValueError("trade_sample_interval_seconds must be positive")
        if trade_page_size < 1 or trade_page_size > 1000:
            raise ValueError("trade_page_size must be between 1 and 1000")
        if trade_crosscheck_interval_seconds <= 0:
            raise ValueError("trade_crosscheck_interval_seconds must be positive")
        if trade_safety_overlap_seconds < 0:
            raise ValueError("trade_safety_overlap_seconds must be non-negative")
        if market_data_stale_seconds <= 0:
            raise ValueError("market_data_stale_seconds must be positive")
        self.trade_transport = normalized_transport
        self.trade_window_seconds = float(trade_window_seconds)
        self.trade_sample_interval_seconds = float(trade_sample_interval_seconds)
        self.trade_page_size = int(trade_page_size)
        self.trade_crosscheck_interval_seconds = float(trade_crosscheck_interval_seconds)
        self.trade_safety_overlap_seconds = float(trade_safety_overlap_seconds)
        self.market_data_stale_seconds = float(market_data_stale_seconds)
        self.trade_stream = (
            DerivePublicTradeWebSocket(url=trade_websocket_url)
            if normalized_transport == "websocket" and trade_history_enabled
            else None
        )
        self._last_crosscheck_epoch: dict[str, float] = {}
        self._last_request_end_epoch: dict[str, float] = {}
        self._last_reconnect_count: dict[str, int] = {}
        self._trade_pipeline_audit: list[dict[str, Any]] = []
        self._last_trade_metadata: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _rows(result: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        if isinstance(result, dict):
            values = result.get(key, result.get("data", []))
            if isinstance(values, list):
                return [row for row in values if isinstance(row, dict)]
        return []

    @staticmethod
    def _find_ticker(result: Any, instrument_name: str) -> dict[str, Any]:
        tickers = result.get("tickers", {}) if isinstance(result, dict) else {}
        if not isinstance(tickers, dict):
            return {}
        for key, value in tickers.items():
            if instrument_name in str(key):
                row = value.get("instrument_ticker", value) if isinstance(value, dict) else {}
                if isinstance(row, dict):
                    return row
        return {}

    def _instrument(self, currency: str) -> dict[str, Any]:
        result = self.client.post(
            "public/get_instruments",
            {"currency": currency, "instrument_type": "perp", "expired": False},
        )
        rows = self._rows(result, "instruments")
        active = [row for row in rows if row.get("is_active") is not False]
        if not active:
            raise ShadowEnvironmentError(f"no active mainnet perpetual instrument for {currency}")
        return sorted(active, key=lambda row: str(row.get("instrument_name", "")))[0]

    def _rest_trades(
        self,
        currency: str,
        instrument_name: str,
        now: float,
        window_start_epoch: float | None = None,
    ) -> tuple[tuple[ShadowTrade, ...], dict[str, Any]]:
        """Fetch a complete bounded REST window with explicit pagination."""

        start = max(
            0.0,
            window_start_epoch
            if window_start_epoch is not None
            else now - self.trade_window_seconds,
        )
        rows: list[dict[str, Any]] = []
        page = 1
        page_count = 0
        pagination_count = 0
        try:
            while page <= 20:
                result = self.client.post(
                    "public/get_trade_history",
                    {
                        "currency": currency,
                        "instrument_name": instrument_name,
                        "instrument_type": "perp",
                        "from_timestamp": int(start * 1000),
                        "to_timestamp": int(now * 1000),
                        "page": page,
                        "page_size": self.trade_page_size,
                    },
                )
                page_rows = self._rows(result, "trades")
                rows.extend(page_rows)
                page_count = page
                pagination = result.get("pagination", {}) if isinstance(result, dict) else {}
                num_pages = int(pagination.get("num_pages", page) or page)
                pagination_count = int(pagination.get("count", len(rows)) or len(rows))
                if not page_rows or page >= num_pages:
                    break
                page += 1
            canonical = canonical_trade_rows(
                rows,
                instrument_name=instrument_name,
                timestamp_unit="milliseconds",
            )
            raw_trades = tuple(
                ShadowTrade(
                    timestamp=float(row["timestamp"]),
                    price=float(row["price"]),
                    amount=float(row["amount"]),
                    aggressor_side=row.get("aggressor_side"),
                    trade_id=str(row["trade_id"]) if row.get("trade_id") else None,
                )
                for row in canonical["rows"]
                if start <= float(row["timestamp"]) <= now
            )
            trades = self._merge_trades((), raw_trades)
            return trades, {
                "source": "rest_fallback",
                "status": "OK",
                "endpoint": "public/get_trade_history",
                "channel": None,
                "request_window_start_epoch": start,
                "request_window_end_epoch": now,
                "raw_count": canonical["raw_count"],
                "canonical_count": len(trades),
                "duplicate_count": canonical["duplicate_count"]
                + max(0, len(raw_trades) - len(trades)),
                "rejected_count": canonical["rejected_count"],
                "page_count": page_count,
                "page_size": self.trade_page_size,
                "pagination_count": pagination_count,
                "timestamp_unit": next(
                    iter(canonical["timestamp_units"]), "milliseconds"
                ),
                "sort_order": canonical["sort_order"],
                "dedup_key": canonical["dedup_key"],
                "connection_status": "REST_OK",
                "reconnect_count": 0,
                "rate_limit_status": "NOT_OBSERVED",
                "error": None,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return (), {
                "source": "rest_fallback",
                "status": "ERROR",
                "endpoint": "public/get_trade_history",
                "channel": None,
                "request_window_start_epoch": start,
                "request_window_end_epoch": now,
                "raw_count": len(rows),
                "canonical_count": 0,
                "duplicate_count": 0,
                "rejected_count": 0,
                "page_count": page_count,
                "page_size": self.trade_page_size,
                "pagination_count": pagination_count,
                "timestamp_unit": "milliseconds",
                "sort_order": "unknown",
                "dedup_key": "trade_id; no-id timestamp/price/amount/aggressor_side",
                "connection_status": "REST_ERROR",
                "reconnect_count": 0,
                "rate_limit_status": "RATE_LIMITED" if "429" in error else "NOT_OBSERVED",
                "error": error,
            }

    @staticmethod
    def _merge_trades(
        primary: Sequence[ShadowTrade], additional: Sequence[ShadowTrade]
    ) -> tuple[ShadowTrade, ...]:
        """Merge public rows by stable ID, retaining safe no-ID signatures."""

        values: dict[tuple[Any, ...], ShadowTrade] = {}
        for trade in (*primary, *additional):
            key = (
                ("id", str(trade.trade_id))
                if trade.trade_id
                else (
                    "row",
                    float(trade.timestamp),
                    float(trade.price),
                    float(trade.amount),
                    trade.aggressor_side,
                )
            )
            # Additional rows are REST recovery/reference rows and therefore
            # win when the same stable ID is present in both sources.
            values[key] = trade
        return tuple(
            sorted(values.values(), key=lambda row: (row.timestamp, str(row.trade_id or "")))
        )

    @staticmethod
    def _trade_key(trade: ShadowTrade) -> tuple[Any, ...]:
        if trade.trade_id:
            return ("id", str(trade.trade_id))
        return (
            "row",
            float(trade.timestamp),
            float(trade.price),
            float(trade.amount),
            trade.aggressor_side,
        )

    @classmethod
    def _trade_keys(cls, trades: Sequence[ShadowTrade]) -> set[tuple[Any, ...]]:
        return {cls._trade_key(trade) for trade in trades}

    @staticmethod
    def _trade_key_label(key: tuple[Any, ...]) -> str:
        if key and key[0] == "id":
            return str(key[1])
        return "row:" + ":".join(
            "" if value is None else str(value) for value in key[1:]
        )

    @staticmethod
    def _trade_attributes_match(left: ShadowTrade, right: ShadowTrade) -> bool:
        return (
            abs(left.timestamp - right.timestamp) <= 0.001
            and abs(left.price - right.price) <= 1e-9
            and abs(left.amount - right.amount) <= 1e-9
            and left.aggressor_side == right.aggressor_side
        )

    def _trades(
        self,
        currency: str,
        instrument_name: str,
        now: float,
    ) -> tuple[tuple[ShadowTrade, ...], dict[str, Any]]:
        if not self.trade_history_enabled:
            return (), {
                "source": "disabled",
                "status": "DISABLED",
                "endpoint": None,
                "channel": None,
                "request_window_start_epoch": None,
                "request_window_end_epoch": None,
                "raw_count": 0,
                "canonical_count": 0,
                "duplicate_count": 0,
                "rejected_count": 0,
                "page_count": 0,
                "page_size": self.trade_page_size,
                "pagination_count": 0,
                "timestamp_unit": None,
                "sort_order": None,
                "dedup_key": None,
                "connection_status": "DISABLED",
                "reconnect_count": 0,
                "rate_limit_status": None,
                "error": None,
                "recovery_status": "DISABLED",
            }

        start = max(0.0, now - self.trade_window_seconds)
        previous_end = self._last_request_end_epoch.get(currency)
        overlap_seconds = (
            max(0.0, min(self.trade_safety_overlap_seconds, previous_end - start))
            if previous_end is not None
            else 0.0
        )
        poll_gap_seconds = (
            max(0.0, start - previous_end) if previous_end is not None else 0.0
        )
        self._last_request_end_epoch[currency] = now

        if self.trade_stream is not None and self.trade_stream.connection_status == "CONNECTED":
            # Use the same inclusive end bound for the WebSocket snapshot and
            # REST request.  A +1s WS tail made otherwise valid trades look
            # like REST mismatches at every cross-check.
            raw_trades = self.trade_stream.snapshot(currency, start=start, end=now)
            trades = self._merge_trades((), raw_trades)
            metadata: dict[str, Any] = {
                "source": "websocket",
                "status": "CONNECTED" if trades else "CONNECTED_NO_TRADES",
                "endpoint": None,
                "channel": f"trades.perp.{currency}",
                "request_window_start_epoch": start,
                "request_window_end_epoch": now,
                "raw_count": len(raw_trades),
                "canonical_count": len(trades),
                "duplicate_count": max(0, len(raw_trades) - len(trades)),
                "rejected_count": 0,
                "page_count": 0,
                "page_size": 0,
                "pagination_count": 0,
                "timestamp_unit": "milliseconds",
                "sort_order": "timestamp_ascending_then_trade_id",
                "dedup_key": "trade_id; no-id timestamp/price/amount/aggressor_side",
                "connection_status": self.trade_stream.connection_status,
                "reconnect_count": self.trade_stream.reconnect_count,
                "rate_limit_status": "NOT_OBSERVED",
                "error": self.trade_stream.last_error,
                "previous_request_end_epoch": previous_end,
                "request_overlap_seconds": overlap_seconds,
                "poll_gap_seconds": poll_gap_seconds,
                "recovery_status": "NOT_REQUIRED",
                "backfill_attempted": False,
                "backfill_trades_found": 0,
                "backfill_complete": None,
                "backfill_error": None,
            }
            reconnect_count = self.trade_stream.reconnect_count
            reconnect_changed = (
                currency in self._last_reconnect_count
                and reconnect_count > self._last_reconnect_count[currency]
            )
            if reconnect_changed:
                recovery_start = max(
                    start,
                    (previous_end or start) - self.trade_safety_overlap_seconds,
                )
                backfill, backfill_meta = self._rest_trades(
                    currency,
                    instrument_name,
                    now,
                    window_start_epoch=recovery_start,
                )
                metadata.update(
                    {
                        "recovery_status": (
                            "BACKFILL_COMPLETE"
                            if not backfill_meta.get("error")
                            else "BACKFILL_FAILED"
                        ),
                        "backfill_attempted": True,
                        "backfill_trades_found": len(backfill),
                        "backfill_complete": not bool(backfill_meta.get("error")),
                        "backfill_error": backfill_meta.get("error"),
                        "backfill_window_start_epoch": recovery_start,
                    }
                )
                if not backfill_meta.get("error"):
                    trades = self._merge_trades(trades, backfill)
                    metadata["raw_count"] = len(trades)
                    metadata["canonical_count"] = len(trades)
            self._last_reconnect_count[currency] = reconnect_count

            last_crosscheck = self._last_crosscheck_epoch.get(currency, 0.0)
            if now - last_crosscheck >= self.trade_crosscheck_interval_seconds:
                connected_since = self.trade_stream.connected_since_epoch
                crosscheck_start = max(
                    start,
                    connected_since if connected_since is not None else start,
                )
                if now - crosscheck_start < max(1.0, self.trade_sample_interval_seconds):
                    metadata.update(
                        {
                            "crosscheck_status": "WARMUP",
                            "crosscheck_collector_count": len(self._trade_keys(trades)),
                            "crosscheck_rest_count": None,
                            "crosscheck_missing_from_collector": None,
                            "crosscheck_extra_in_collector": None,
                            "crosscheck_timestamp_epoch": now,
                            "crosscheck_rest_error": None,
                        }
                    )
                    self._last_crosscheck_epoch[currency] = now
                    return trades, metadata
                rest_trades, rest_meta = self._rest_trades(
                    currency,
                    instrument_name,
                    now,
                    window_start_epoch=crosscheck_start,
                )
                collector_window = tuple(
                    trade for trade in trades if crosscheck_start <= trade.timestamp <= now
                )
                collector_by_key = {
                    self._trade_key(trade): trade for trade in collector_window
                }
                rest_by_key = {self._trade_key(trade): trade for trade in rest_trades}
                collector_keys = set(collector_by_key)
                rest_keys = set(rest_by_key)
                missing = rest_keys - collector_keys
                extra = collector_keys - rest_keys
                common_ids = {
                    key
                    for key in collector_keys & rest_keys
                    if key and key[0] == "id"
                }
                attribute_mismatch = {
                    key
                    for key in common_ids
                    if not self._trade_attributes_match(
                        collector_by_key[key], rest_by_key[key]
                    )
                }
                raw_status = (
                    "ERROR"
                    if rest_meta.get("error")
                    else "PASS"
                    if not missing and not extra and not attribute_mismatch
                    else "MISMATCH"
                )
                if not rest_meta.get("error") and (missing or extra or attribute_mismatch):
                    # The REST history is authoritative for the checked
                    # interval. Keep the older WS buffer for the surrounding
                    # window, but replace the checked slice with canonical
                    # REST rows. Raw mismatch counts/IDs remain in metadata
                    # for Stage 12F diagnosis.
                    older = tuple(trade for trade in trades if trade.timestamp < crosscheck_start)
                    trades = self._merge_trades(older, rest_trades)
                    repaired_window = tuple(
                        trade for trade in trades if crosscheck_start <= trade.timestamp <= now
                    )
                    repaired_by_key = {
                        self._trade_key(trade): trade for trade in repaired_window
                    }
                    repaired_keys = set(repaired_by_key)
                    missing_after = rest_keys - repaired_keys
                    extra_after = repaired_keys - rest_keys
                    attribute_mismatch_after = {
                        key
                        for key in attribute_mismatch
                        if key in repaired_by_key
                        and not self._trade_attributes_match(
                            repaired_by_key[key], rest_by_key[key]
                        )
                    }
                    repaired = (
                        not missing_after
                        and not extra_after
                        and not attribute_mismatch_after
                    )
                    metadata["recovery_status"] = (
                        "REST_AUTHORITATIVE_REPAIR" if repaired else "REST_REPAIR_INCOMPLETE"
                    )
                    crosscheck_status = "REPAIRED" if repaired else "MISMATCH"
                else:
                    crosscheck_status = raw_status
                metadata.update(
                    {
                        "crosscheck_status": crosscheck_status,
                        "crosscheck_raw_status": raw_status,
                        "crosscheck_collector_count": len(collector_keys),
                        "crosscheck_rest_count": len(rest_keys),
                        "crosscheck_missing_from_collector": len(missing),
                        "crosscheck_extra_in_collector": len(extra),
                        "crosscheck_raw_collector_count": len(collector_keys),
                        "crosscheck_raw_rest_count": len(rest_keys),
                        "crosscheck_raw_missing_from_collector": len(missing),
                        "crosscheck_raw_extra_in_collector": len(extra),
                        "crosscheck_matched_count": len(collector_keys & rest_keys),
                        "crosscheck_missing_ids": sorted(
                            self._trade_key_label(key) for key in missing
                        ),
                        "crosscheck_extra_ids": sorted(
                            self._trade_key_label(key) for key in extra
                        ),
                        "crosscheck_attribute_mismatch_count": len(attribute_mismatch),
                        "crosscheck_attribute_mismatch_ids": sorted(
                            self._trade_key_label(key) for key in attribute_mismatch
                        ),
                        "crosscheck_timestamp_epoch": now,
                        "crosscheck_window_start_epoch": crosscheck_start,
                        "crosscheck_window_end_epoch": now,
                        "crosscheck_rest_error": rest_meta.get("error"),
                    }
                )
                self._last_crosscheck_epoch[currency] = now
            return trades, metadata

        trades, metadata = self._rest_trades(currency, instrument_name, now)
        metadata.update(
            {
                "previous_request_end_epoch": previous_end,
                "request_overlap_seconds": overlap_seconds,
                "poll_gap_seconds": poll_gap_seconds,
                "recovery_status": "REST_AUTHORITATIVE",
                "backfill_attempted": False,
                "backfill_trades_found": 0,
                "backfill_complete": None,
                "backfill_error": None,
            }
        )
        if self.trade_stream is not None:
            metadata["websocket_status"] = self.trade_stream.connection_status
            metadata["websocket_error"] = self.trade_stream.last_error
            metadata["websocket_reconnect_count"] = self.trade_stream.reconnect_count
        return trades, metadata

    def fetch_frame(self, trading_pair: str, *, now: float | None = None) -> ShadowMarketFrame:
        current = time.time() if now is None else now
        currency = trading_pair.split("-", 1)[0].upper()
        instrument = self._instrument(currency)
        name = str(instrument.get("instrument_name", f"{currency}-PERP"))
        result = self.client.post(
            "public/get_tickers", {"instrument_type": "perp", "currency": currency}
        )
        ticker = self._find_ticker(result, name)
        bid = _finite(ticker.get("best_bid_price", ticker.get("b")))
        ask = _finite(ticker.get("best_ask_price", ticker.get("a")))
        if bid is None or ask is None or bid <= 0 or ask <= bid:
            raise ShadowEnvironmentError(f"invalid mainnet BBO for {trading_pair}")
        rule = TradingRuleView(
            min_order_size=Decimal(str(instrument.get("minimum_amount", 0))),
            min_notional_size=Decimal("0"),
            min_price_increment=Decimal(str(instrument.get("tick_size", 0))),
            min_base_amount_increment=Decimal(str(instrument.get("amount_step", 0))),
        )
        trades, trade_metadata = self._trades(currency, name, current)
        collection_interval = min(
            self.trade_sample_interval_seconds, self.trade_window_seconds
        )
        trade_metadata.setdefault(
            "collection_start_epoch", max(0.0, current - collection_interval)
        )
        trade_metadata.setdefault("collection_end_epoch", current)
        trade_metadata.setdefault("sample_interval_seconds", collection_interval)
        source_timestamp = _epoch(ticker.get("timestamp", ticker.get("t")))
        source_age = (
            max(0.0, current - source_timestamp) if source_timestamp is not None else 0.0
        )
        return ShadowMarketFrame(
            # The ticker timestamp is an exchange/event timestamp and can
            # remain unchanged while this healthy BBO is polled again.  The
            # controller needs receipt time for sequencing; source time is
            # retained below and still enforces the stale-data boundary.
            timestamp=current,
            trading_pair=trading_pair,
            environment="mainnet",
            best_bid=bid,
            best_ask=ask,
            best_bid_size=_finite(ticker.get("best_bid_amount", ticker.get("B"))) or 0.0,
            best_ask_size=_finite(ticker.get("best_ask_amount", ticker.get("A"))) or 0.0,
            bid_depth=_finite(ticker.get("five_percent_bid_depth")),
            ask_depth=_finite(ticker.get("five_percent_ask_depth")),
            mark_price=_finite(ticker.get("mark_price", ticker.get("M"))),
            index_price=_finite(ticker.get("index_price", ticker.get("I"))),
            trades=trades,
            rule=rule,
            maker_fee_bps=(
                _finite(instrument.get("maker_fee_rate")) * 10_000
                if _finite(instrument.get("maker_fee_rate")) is not None
                else None
            ),
            option_environment="mainnet",
            trade_source=trade_metadata.get("source", "unknown"),
            trade_collection_status=trade_metadata.get("status", "UNKNOWN"),
            trade_endpoint=trade_metadata.get("endpoint"),
            trade_channel=trade_metadata.get("channel"),
            trade_request_window_start_epoch=trade_metadata.get(
                "request_window_start_epoch"
            ),
            trade_request_window_end_epoch=trade_metadata.get("request_window_end_epoch"),
            trade_collection_start_epoch=trade_metadata.get("collection_start_epoch"),
            trade_collection_end_epoch=trade_metadata.get("collection_end_epoch"),
            trade_sample_interval_seconds=trade_metadata.get("sample_interval_seconds"),
            trade_raw_count=trade_metadata.get("raw_count", len(trades)),
            trade_canonical_count=trade_metadata.get("canonical_count", len(trades)),
            trade_duplicate_count=trade_metadata.get("duplicate_count", 0),
            trade_rejected_count=trade_metadata.get("rejected_count", 0),
            trade_page_count=trade_metadata.get("page_count", 0),
            trade_page_size=trade_metadata.get("page_size", 0),
            trade_pagination_count=trade_metadata.get("pagination_count", 0),
            trade_timestamp_unit=trade_metadata.get("timestamp_unit"),
            trade_sort_order=trade_metadata.get("sort_order"),
            trade_dedup_key=trade_metadata.get("dedup_key"),
            trade_connection_status=trade_metadata.get("connection_status"),
            trade_reconnect_count=trade_metadata.get("reconnect_count", 0),
            trade_rate_limit_status=trade_metadata.get("rate_limit_status"),
            trade_collection_error=trade_metadata.get("error"),
            trade_crosscheck_status=trade_metadata.get("crosscheck_status"),
            trade_crosscheck_collector_count=trade_metadata.get(
                "crosscheck_collector_count"
            ),
            trade_crosscheck_rest_count=trade_metadata.get("crosscheck_rest_count"),
            trade_crosscheck_missing_from_collector=trade_metadata.get(
                "crosscheck_missing_from_collector"
            ),
            trade_crosscheck_extra_in_collector=trade_metadata.get(
                "crosscheck_extra_in_collector"
            ),
            trade_crosscheck_error=trade_metadata.get("crosscheck_rest_error"),
            trade_crosscheck_window_start_epoch=trade_metadata.get(
                "crosscheck_window_start_epoch"
            ),
            trade_crosscheck_window_end_epoch=trade_metadata.get(
                "crosscheck_window_end_epoch"
            ),
            trade_crosscheck_raw_status=trade_metadata.get("crosscheck_raw_status"),
            trade_crosscheck_raw_collector_count=trade_metadata.get(
                "crosscheck_raw_collector_count"
            ),
            trade_crosscheck_raw_rest_count=trade_metadata.get("crosscheck_raw_rest_count"),
            trade_crosscheck_raw_missing_from_collector=trade_metadata.get(
                "crosscheck_raw_missing_from_collector"
            ),
            trade_crosscheck_raw_extra_in_collector=trade_metadata.get(
                "crosscheck_raw_extra_in_collector"
            ),
            trade_crosscheck_matched_count=trade_metadata.get("crosscheck_matched_count"),
            trade_crosscheck_missing_ids=tuple(
                trade_metadata.get("crosscheck_missing_ids", ()) or ()
            ),
            trade_crosscheck_extra_ids=tuple(
                trade_metadata.get("crosscheck_extra_ids", ()) or ()
            ),
            trade_crosscheck_attribute_mismatch_count=trade_metadata.get(
                "crosscheck_attribute_mismatch_count"
            ),
            trade_crosscheck_attribute_mismatch_ids=tuple(
                trade_metadata.get("crosscheck_attribute_mismatch_ids", ()) or ()
            ),
            trade_recovery_status=trade_metadata.get("recovery_status"),
            trade_backfill_attempted=bool(trade_metadata.get("backfill_attempted", False)),
            trade_backfill_trades_found=int(trade_metadata.get("backfill_trades_found", 0) or 0),
            trade_backfill_complete=trade_metadata.get("backfill_complete"),
            trade_backfill_error=trade_metadata.get("backfill_error"),
            trade_previous_request_end_epoch=trade_metadata.get("previous_request_end_epoch"),
            trade_request_overlap_seconds=trade_metadata.get("request_overlap_seconds"),
            trade_poll_gap_seconds=trade_metadata.get("poll_gap_seconds"),
            source_timestamp_epoch=source_timestamp,
            source_timestamp_age_seconds=source_age,
            source_timestamp_stale=source_age > self.market_data_stale_seconds,
        )

    async def fetch_bundle(
        self,
        trading_pairs: Sequence[str],
        *,
        now: float | None = None,
    ) -> tuple[dict[str, ShadowMarketFrame], OptionsVolatilitySnapshot | None]:
        current = time.time() if now is None else now
        if self.trade_stream is not None:
            await self.trade_stream.ensure(
                [pair.split("-", 1)[0].upper() for pair in trading_pairs]
            )
        frames: dict[str, ShadowMarketFrame] = {}
        for pair in trading_pairs:
            frames[pair] = await asyncio.to_thread(self.fetch_frame, pair, now=current)
        btc = frames.get("BTC-USDC")
        options: OptionsVolatilitySnapshot | None = None
        if btc is not None:
            options = await self.options_provider.snapshot(btc.mid_price, now=current)
            require_shadow_environment([{"environment": options.environment}])
            for pair, frame in list(frames.items()):
                frames[pair] = ShadowMarketFrame(
                    **{
                        **frame.__dict__,
                        "option_snapshot": options if pair == "BTC-USDC" else None,
                    }
                )
        require_shadow_environment([*frames.values(), {"environment": "mainnet"}])
        return frames, options

    async def close(self) -> None:
        """Close the optional public WebSocket without touching exchange state."""

        if self.trade_stream is not None:
            await self.trade_stream.close()


__all__ = [
    "DerivePublicTradeWebSocket",
    "MainnetPublicDataSource",
    "PositionLedger",
    "ShadowConfig",
    "ShadowEnvironmentError",
    "ShadowEnvironmentStatus",
    "ShadowExecutionEngine",
    "ShadowExchangeMutationGuard",
    "ShadowFill",
    "ShadowFillModel",
    "ShadowMarketFrame",
    "ShadowModeExchangeMutationBlocked",
    "ShadowOrder",
    "ShadowOrderStatus",
    "ShadowSession",
    "ShadowStore",
    "ShadowTrade",
    "SHADOW_BANNER",
    "SHADOW_ENVIRONMENT_CONSISTENCY_PASS",
    "SHADOW_MODE_EXCHANGE_MUTATION_BLOCKED",
    "check_shadow_environment",
    "require_shadow_environment",
]


_BASELINE_EXPORTS = frozenset(
    {
        "BaselineConfigChanged",
        "BaselineSession",
        "DataQualityTracker",
        "PnLReconciliation",
        "ShadowBaselineSession",
        "TimeWeightedExposure",
        "reconcile_paper_equity",
    }
)
__all__.extend(sorted(_BASELINE_EXPORTS))


def __getattr__(name: str) -> Any:
    """Lazily expose Stage 12 types without creating an import cycle."""

    if name not in _BASELINE_EXPORTS:
        raise AttributeError(name)
    from . import shadow_baseline

    return getattr(shadow_baseline, name)
