"""Hummingbot V2 controller for controlled Derive grid execution.

This adapter consumes the append-only Stage 4 ``GridPlan`` JSONL boundary and
turns validated levels into one native ``PositionExecutor`` per level.  It
does not calculate market signals or grid parameters.  The default is a
testnet-gated dry run: reconciliation, quantization, and risk checks run, but
no executor action is sent until ``execution_enabled`` is explicitly true.
Mainnet additionally requires the separate canary gates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hummingbot.core.data_type.common import (
    MarketDict,
    OrderType,
    PositionMode,
    PriceType,
    TradeType,
)
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TripleBarrierConfig,
)
from hummingbot.strategy_v2.models.executor_actions import (
    CreateExecutorAction,
    ExecutorAction,
    StopExecutorAction,
)
from pydantic import Field, model_validator

from .execution_logic import (
    ActiveLevel,
    ExecutionPolicy,
    ExecutionSide,
    GridPlanView,
    JsonlExecutionJournal,
    ReconciliationResult,
    RuntimeHealth,
    TradingRuleView,
    parse_grid_plan,
    reconcile_grid_plan,
)
from .mainnet_canary import (
    CanaryRiskLimits,
    check_environment_consistency,
    environment_for_connector,
    environment_profile,
    mainnet_canary_blockers,
    normalize_environment,
)

logger = logging.getLogger(__name__)

SUPPORTED_TRADING_PAIRS = ("BTC-USDC", "ETH-USDC", "SOL-USDC", "HYPE-USDC")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _wire(item) for key, item in value.items()}
    if hasattr(value, "value"):
        return value.value
    return value


class JsonlPlanTailer:
    """Read complete Stage 4 JSONL records while tolerating partial writes."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._offset = 0
        self._inode: int | None = None
        self._pending = b""
        self._bootstrapped = False

    @staticmethod
    def _decode(lines: Iterable[bytes]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Skipping malformed Stage 4 GridPlan JSONL record")
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def bootstrap(self) -> list[dict[str, Any]]:
        """Use only the latest complete existing record for controller warm-up."""

        self._bootstrapped = True
        if not self.path.exists():
            return []
        try:
            stat = self.path.stat()
            raw = self.path.read_bytes()
        except OSError as exc:
            logger.warning("Unable to read GridPlan path %s: %s", self.path, exc)
            return []
        self._inode = getattr(stat, "st_ino", None)
        self._offset = len(raw)
        if not raw.endswith(b"\n"):
            last_newline = raw.rfind(b"\n")
            self._pending = raw[last_newline + 1 :] if last_newline >= 0 else raw
            complete = raw[:last_newline] if last_newline >= 0 else b""
        else:
            complete = raw[:-1]
        records = self._decode(complete.splitlines())
        return records[-1:] if records else []

    def poll(self) -> list[dict[str, Any]]:
        if not self._bootstrapped:
            return self.bootstrap()
        if not self.path.exists():
            return []
        try:
            stat = self.path.stat()
            inode = getattr(stat, "st_ino", None)
            if (self._inode is not None and inode != self._inode) or stat.st_size < self._offset:
                self._offset = 0
                self._pending = b""
            self._inode = inode
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                raw = handle.read()
        except OSError as exc:
            logger.warning("Unable to poll GridPlan path %s: %s", self.path, exc)
            return []
        if not raw:
            return []
        combined = self._pending + raw
        last_newline = combined.rfind(b"\n")
        if last_newline < 0:
            self._pending = combined
            return []
        complete = combined[:last_newline]
        self._pending = combined[last_newline + 1 :]
        self._offset = stat.st_size - len(self._pending)
        return self._decode(complete.splitlines())


class DeriveAdaptiveGridConfig(ControllerConfigBase):
    """Execution-only configuration for one supported Derive perpetual pair.

    The default remains the existing testnet dry run.  Mainnet is accepted
    only as a separate, explicitly populated canary configuration and remains
    fail-closed until the read-only audit and acknowledgement gates agree.
    """

    controller_type: str = "market_making"
    controller_name: str = "derive_adaptive_grid"
    total_amount_quote: Decimal = Field(default=Decimal("1000"), ge=0)

    connector_name: str = "derive_perpetual_testnet"
    trading_pair: str = "BTC-USDC"
    leverage: int = Field(default=1, ge=1, le=20)
    position_mode: PositionMode = PositionMode.ONEWAY
    environment: str = "testnet"
    market_environment: str | None = None
    options_environment: str | None = None
    account_environment: str | None = None
    execution_environment: str | None = None
    allow_mainnet_trading: bool = False

    # Stage 4 boundary.  The bot container receives this through its data
    # volume; Stage 4 itself remains a separate Condor process.
    grid_plan_path: str = "/home/hummingbot/data/derive_grid_plans.jsonl"
    execution_journal_path: str = "/home/hummingbot/data/derive_execution_events.jsonl"
    replay_existing_plan: bool = True

    # Explicit rollout gates.
    execution_enabled: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    execution_max_levels_per_side: int = Field(
        default=1, ge=1, le=100, json_schema_extra={"is_updatable": True}
    )
    testnet_order_scale: Decimal | None = Field(
        default=Decimal("0.05"), gt=0, json_schema_extra={"is_updatable": True}
    )
    mainnet_canary_order_scale: Decimal | None = Field(
        default=None, gt=0, json_schema_extra={"is_updatable": True}
    )
    post_only: bool = True

    # Hard inventory and executor limits.
    max_total_position_notional: Decimal | None = Field(
        default=Decimal("1000"), gt=0, json_schema_extra={"is_updatable": True}
    )
    max_side_position_notional: Decimal | None = Field(
        default=Decimal("1000"), gt=0, json_schema_extra={"is_updatable": True}
    )
    mainnet_canary_max_order_notional: Decimal | None = Field(
        default=None, gt=0, json_schema_extra={"is_updatable": True}
    )
    mainnet_canary_max_total_position_notional: Decimal | None = Field(
        default=None, gt=0, json_schema_extra={"is_updatable": True}
    )
    mainnet_canary_max_loss_quote: Decimal | None = Field(
        default=None, gt=0, json_schema_extra={"is_updatable": True}
    )
    mainnet_environment_verified: bool = False
    mainnet_account_state_verified: bool = False
    mainnet_canary_ack: str | None = None
    max_active_grid_levels: int = Field(
        default=2, ge=1, le=200, json_schema_extra={"is_updatable": True}
    )
    max_active_executors: int = Field(
        default=2, ge=1, le=200, json_schema_extra={"is_updatable": True}
    )

    # Queue protection and refresh controls.
    minimum_order_lifetime_seconds: float = Field(
        default=60.0, ge=0, json_schema_extra={"is_updatable": True}
    )
    minimum_replace_interval_seconds: float = Field(
        default=30.0, ge=0, json_schema_extra={"is_updatable": True}
    )
    maximum_order_lifetime_seconds: float = Field(
        default=600.0, ge=0, json_schema_extra={"is_updatable": True}
    )
    refresh_price_tolerance_bps: Decimal = Field(
        default=Decimal("15"), ge=0, json_schema_extra={"is_updatable": True}
    )
    refresh_amount_tolerance_pct: Decimal = Field(
        default=Decimal("0.15"), ge=0, json_schema_extra={"is_updatable": True}
    )
    max_consecutive_order_errors: int = Field(
        default=3, ge=1, json_schema_extra={"is_updatable": True}
    )
    order_error_pause_seconds: float = Field(
        default=60.0, ge=0, json_schema_extra={"is_updatable": True}
    )
    stale_plan_timeout_seconds: float = Field(
        default=30.0, gt=0, json_schema_extra={"is_updatable": True}
    )
    collateral_safety_buffer_pct: Decimal = Field(
        default=Decimal("0.10"), ge=0, lt=1, json_schema_extra={"is_updatable": True}
    )
    cancel_orders_on_pause: bool = True
    cancel_orders_on_shutdown: bool = True
    manual_kill_switch: bool = False
    emergency_close_positions_on_pause: bool = False

    # Native PositionExecutor exit lifecycle.
    take_profit_mode: str = "adjacent_grid"
    take_profit_pct: Decimal = Field(default=Decimal("0.001"), ge=0)
    take_profit_step_multiplier: Decimal = Field(default=Decimal("1"), gt=0)
    stop_loss_pct: Decimal | None = Field(default=None, ge=0)
    time_limit_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_environment_contract(self) -> DeriveAdaptiveGridConfig:
        if self.trading_pair not in SUPPORTED_TRADING_PAIRS:
            raise ValueError("trading_pair must be one of " + ", ".join(SUPPORTED_TRADING_PAIRS))
        if not self.post_only:
            raise ValueError("Adaptive Grid requires post_only=true")
        if self.maximum_order_lifetime_seconds < self.minimum_order_lifetime_seconds:
            raise ValueError("maximum_order_lifetime_seconds must not be below minimum lifetime")
        if self.take_profit_mode not in {"adjacent_grid", "fixed"}:
            raise ValueError("take_profit_mode must be adjacent_grid or fixed")
        configured_profile = environment_profile(self.environment)
        if self.connector_name != configured_profile.connector_name:
            raise ValueError(
                "Execution blocked: connector_name does not match the selected "
                f"{configured_profile.name} environment"
            )
        if not configured_profile.is_mainnet:
            if self.allow_mainnet_trading:
                raise ValueError("testnet configuration cannot allow mainnet trading")
            if self.testnet_order_scale is None:
                raise ValueError("testnet_order_scale must be configured for testnet")
            return self
        if self.leverage != 1:
            raise ValueError("mainnet canary requires leverage=1")
        if self.execution_max_levels_per_side != 1:
            raise ValueError("mainnet canary requires exactly one execution level per side")
        if self.max_active_grid_levels > 2 or self.max_active_executors > 2:
            raise ValueError("mainnet canary caps must not exceed two active levels/executors")
        if self.emergency_close_positions_on_pause:
            raise ValueError("mainnet canary must not force-close positions on pause")
        if self.testnet_order_scale is not None:
            raise ValueError("mainnet configuration must not reuse testnet_order_scale")
        environments = (
            self.market_environment,
            self.options_environment,
            self.account_environment,
            self.execution_environment,
        )
        if any(
            normalize_environment(value) != configured_profile.name for value in environments
        ):
            raise ValueError(
                "mainnet requires all market/options/account/execution environments to be mainnet"
            )
        risk_limits = CanaryRiskLimits(
            max_order_notional=self.mainnet_canary_max_order_notional,
            max_total_position_notional=self.mainnet_canary_max_total_position_notional,
            max_loss_quote=self.mainnet_canary_max_loss_quote,
        )
        if self.execution_enabled or self.allow_mainnet_trading:
            blockers = mainnet_canary_blockers(
                mainnet_environment_verified=self.mainnet_environment_verified,
                environment_consistent=check_environment_consistency(
                    required_environment=configured_profile.name,
                    market_connector=self.connector_name,
                    market_domain=self.connector_name,
                    options_environment=self.options_environment,
                    account_environment=self.account_environment,
                    execution_environment=self.execution_environment,
                ).consistent,
                allow_mainnet_trading=self.allow_mainnet_trading,
                execution_enabled=self.execution_enabled,
                acknowledgement=self.mainnet_canary_ack,
                risk_limits=risk_limits,
                order_scale=self.mainnet_canary_order_scale,
                account_state_verified=self.mainnet_account_state_verified,
                stop_loss_pct=self.stop_loss_pct,
            )
            if blockers:
                raise ValueError("mainnet canary gates are incomplete: " + ", ".join(blockers))
        return self

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class DeriveAdaptiveGrid(ControllerBase):
    """Deterministic GridPlan-to-PositionExecutor controller."""

    def __init__(self, config: DeriveAdaptiveGridConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._plan_tailer = JsonlPlanTailer(config.grid_plan_path)
        self._latest_plan: GridPlanView | None = None
        self._last_reconciliation: ReconciliationResult | None = None
        self._last_health: RuntimeHealth | None = None
        self._journal = JsonlExecutionJournal(config.execution_journal_path)
        self._journal_keys: set[tuple[Any, ...]] = set()
        self._executor_plan_modes: dict[str, str] = {}
        self._pending_stop_ids: set[str] = set()
        self._seen_terminal_ids: set[str] = set()
        self._filled_ids: set[str] = set()
        self._previous_mode: str | None = None
        self._previous_pause_reason = ""
        self._consecutive_order_errors = 0
        self._orders_created = 0
        self._orders_cancelled = 0
        self._fills = 0
        self._maker_buy_fills = 0
        self._maker_sell_fills = 0
        self._filled_quote_volume = Decimal("0")
        self._order_error_pause_until = 0.0
        self._created_success_ids: set[str] = set()
        self._mainnet_initial_account_checked = False
        self._mainnet_initial_account_blocker = ""

    def _policy(self, now: float = 0.0) -> ExecutionPolicy:
        error_pause_active = (
            self._consecutive_order_errors >= self.config.max_consecutive_order_errors
            and now < self._order_error_pause_until
        )
        environment = environment_profile(self.config.environment)
        order_scale = (
            self.config.mainnet_canary_order_scale
            if environment.is_mainnet
            else self.config.testnet_order_scale
        )
        total_limit = (
            self.config.mainnet_canary_max_total_position_notional
            if environment.is_mainnet
            else self.config.max_total_position_notional
        )
        side_limit = (
            total_limit
            if environment.is_mainnet
            else self.config.max_side_position_notional
        )
        return ExecutionPolicy(
            execution_max_levels_per_side=self.config.execution_max_levels_per_side,
            testnet_order_scale=order_scale,
            max_total_position_notional=total_limit,
            max_side_position_notional=side_limit,
            max_active_grid_levels=self.config.max_active_grid_levels,
            max_active_executors=self.config.max_active_executors,
            minimum_order_lifetime_seconds=self.config.minimum_order_lifetime_seconds,
            minimum_replace_interval_seconds=self.config.minimum_replace_interval_seconds,
            maximum_order_lifetime_seconds=self.config.maximum_order_lifetime_seconds,
            refresh_price_tolerance_bps=self.config.refresh_price_tolerance_bps,
            refresh_amount_tolerance_pct=self.config.refresh_amount_tolerance_pct,
            collateral_safety_buffer_pct=self.config.collateral_safety_buffer_pct,
            leverage=Decimal(self.config.leverage),
            stale_plan_timeout_seconds=self.config.stale_plan_timeout_seconds,
            cancel_orders_on_pause=self.config.cancel_orders_on_pause,
            manual_kill_switch=self.config.manual_kill_switch,
            emergency_close_positions_on_pause=self.config.emergency_close_positions_on_pause,
            post_only=self.config.post_only,
            take_profit_mode=self.config.take_profit_mode,
            take_profit_pct=self.config.take_profit_pct,
            take_profit_step_multiplier=self.config.take_profit_step_multiplier,
            stop_loss_pct=self.config.stop_loss_pct,
            time_limit_seconds=self.config.time_limit_seconds,
            forced_pause_reason="order_error_pause" if error_pause_active else "",
            environment=environment.name,
            mainnet_canary_authorized=(
                self.config.mainnet_environment_verified
                and self.config.allow_mainnet_trading
                and self.config.execution_enabled
                and self.config.mainnet_canary_ack is not None
            ),
            mainnet_canary_max_order_notional=self.config.mainnet_canary_max_order_notional,
            mainnet_canary_max_loss_quote=self.config.mainnet_canary_max_loss_quote,
        )

    def _connector(self):
        return self.market_data_provider.get_connector(self.config.connector_name)

    def _executor_plan_mode(self, executor_id: str) -> str | None:
        mapped_mode = self._executor_plan_modes.get(executor_id)
        if mapped_mode is not None:
            return mapped_mode
        marker = "__mode_"
        if marker not in executor_id:
            return None
        return executor_id.split(marker, 1)[1].split("__", 1)[0] or None

    def _read_position_notional(self, connector, reference_price: Decimal) -> Decimal:
        positions = connector.account_positions
        if positions is None:
            raise RuntimeError("position_unavailable")
        signed_amount = Decimal("0")
        for position in positions.values():
            if getattr(position, "trading_pair", None) != self.config.trading_pair:
                continue
            amount = _decimal(getattr(position, "amount", None))
            if amount is None:
                raise RuntimeError("position_amount_unavailable")
            position_side = str(
                getattr(getattr(position, "position_side", None), "name", "")
            ).upper()
            if position_side == "SHORT" and amount > 0:
                amount = -amount
            signed_amount += amount
        return signed_amount * reference_price

    def _read_health(self) -> RuntimeHealth:
        provider_ready = bool(getattr(self.market_data_provider, "ready", False))
        connector = None
        testnet_verified = False
        configured_profile = environment_profile(self.config.environment)
        configured_environment = configured_profile.name
        environment = configured_environment
        environment_verified = False
        environment_consistent = not configured_profile.is_mainnet
        canary_authorized = False
        connector_ready = False
        rules = None
        available = None
        position_notional = Decimal("0")
        best_bid = None
        best_ask = None
        reason = ""
        try:
            connector = self._connector()
            connector_name = str(getattr(connector, "name", self.config.connector_name))
            domain = str(getattr(connector, "domain", ""))
            runtime_environment = environment_for_connector(connector_name, domain)
            environment_verified = runtime_environment == configured_environment
            testnet_verified = not configured_profile.is_mainnet and environment_verified
            if configured_profile.is_mainnet:
                consistency = check_environment_consistency(
                    required_environment=configured_profile.name,
                    market_connector=connector_name,
                    market_domain=domain,
                    options_environment=self.config.options_environment,
                    account_environment=self.config.account_environment,
                    execution_environment=self.config.execution_environment,
                )
                environment_consistent = consistency.consistent
                canary_authorized = not mainnet_canary_blockers(
                    mainnet_environment_verified=environment_verified,
                    environment_consistent=environment_consistent,
                    allow_mainnet_trading=self.config.allow_mainnet_trading,
                    execution_enabled=self.config.execution_enabled,
                    acknowledgement=self.config.mainnet_canary_ack,
                    risk_limits=CanaryRiskLimits(
                        max_order_notional=self.config.mainnet_canary_max_order_notional,
                        max_total_position_notional=(
                            self.config.mainnet_canary_max_total_position_notional
                        ),
                        max_loss_quote=self.config.mainnet_canary_max_loss_quote,
                    ),
                    order_scale=self.config.mainnet_canary_order_scale,
                    account_state_verified=self.config.mainnet_account_state_verified,
                    stop_loss_pct=self.config.stop_loss_pct,
                )
                if not environment_verified:
                    reason = (
                        "Execution blocked: Derive mainnet connector/domain could not be verified."
                    )
                elif not environment_consistent:
                    reason = "Execution blocked: mainnet environments are inconsistent."
                elif not canary_authorized:
                    reason = "Execution blocked: mainnet canary approval gates are not satisfied."
            elif not testnet_verified:
                reason = "Execution blocked: Derive testnet could not be verified."
            connector_ready = bool(getattr(connector, "ready", False))
            if not connector_ready and not reason:
                reason = "connector_not_ready"
            configured_mode = self.config.position_mode
            exchange_mode = getattr(connector, "position_mode", None)
            if exchange_mode != configured_mode and not reason:
                reason = "position_mode_unverified"
            best_bid = _decimal(
                self.market_data_provider.get_price_by_type(
                    self.config.connector_name, self.config.trading_pair, PriceType.BestBid
                )
            )
            best_ask = _decimal(
                self.market_data_provider.get_price_by_type(
                    self.config.connector_name, self.config.trading_pair, PriceType.BestAsk
                )
            )
            mid = ((best_bid + best_ask) / Decimal("2")) if best_bid and best_ask else Decimal("0")
            position_notional = self._read_position_notional(connector, mid)
            if configured_profile.is_mainnet and not self._mainnet_initial_account_checked:
                self._mainnet_initial_account_checked = True
                open_orders = getattr(connector, "in_flight_orders", None)
                if open_orders is None:
                    self._mainnet_initial_account_blocker = "open_order_state_unavailable"
                elif open_orders:
                    self._mainnet_initial_account_blocker = (
                        "existing BTC orders require explicit acknowledgement"
                    )
                elif position_notional != Decimal("0"):
                    self._mainnet_initial_account_blocker = (
                        "existing BTC position requires explicit acknowledgement"
                    )
            if self._mainnet_initial_account_blocker and not reason:
                reason = f"Execution blocked: {self._mainnet_initial_account_blocker}."
            available = _decimal(
                self.market_data_provider.get_available_balance(
                    self.config.connector_name, self.config.trading_pair.split("-")[1]
                )
            )
            rules = self.market_data_provider.get_trading_rules(
                self.config.connector_name, self.config.trading_pair
            )
            if available is None and not reason:
                reason = "balance_unavailable"
            if not connector_ready and not reason:
                reason = "connector_not_ready"
        except Exception as exc:
            if not reason:
                reason = f"runtime_health_error:{type(exc).__name__}"
            logger.warning("Derive adaptive grid health is not ready: %s", reason)

        trading_rules = None
        if rules is not None:
            trading_rules = TradingRuleView(
                min_order_size=max(
                    Decimal("0"), _decimal(getattr(rules, "min_order_size", 0)) or Decimal("0")
                ),
                min_notional_size=max(
                    Decimal("0"), _decimal(getattr(rules, "min_notional_size", 0)) or Decimal("0")
                ),
                min_price_increment=max(
                    Decimal("0"), _decimal(getattr(rules, "min_price_increment", 0)) or Decimal("0")
                ),
                min_base_amount_increment=max(
                    Decimal("0"),
                    _decimal(getattr(rules, "min_base_amount_increment", 0)) or Decimal("0"),
                ),
            )
        trading_rules_available = trading_rules is not None
        balance_verified = available is not None and available >= Decimal("0")
        position_verified = connector is not None
        if position_verified and connector is not None:
            try:
                position_verified = connector.account_positions is not None
            except Exception:
                position_verified = False
        market_data_ready = (
            provider_ready
            and best_bid is not None
            and best_ask is not None
            and best_bid > Decimal("0")
            and best_ask > best_bid
        )
        if not market_data_ready and not reason:
            reason = "market_data_not_ready"
        if not trading_rules_available and not reason:
            reason = "trading_rules_unavailable"
        if not position_verified and not reason:
            reason = "position_unavailable"
        if available is not None and available <= Decimal("0") and not reason:
            reason = "balance_unavailable"
        return RuntimeHealth(
            testnet_verified=testnet_verified,
            connector_ready=connector_ready,
            market_data_ready=market_data_ready,
            trading_rules_available=trading_rules_available,
            balance_verified=balance_verified,
            position_verified=position_verified,
            best_bid=best_bid,
            best_ask=best_ask,
            position_notional=position_notional,
            available_collateral=available or Decimal("0"),
            trading_rules=trading_rules,
            reason=reason,
            environment=environment,
            environment_verified=environment_verified,
            environment_consistent=environment_consistent,
            mainnet_canary_authorized=canary_authorized,
        )

    def _active_levels(self) -> list[ActiveLevel]:
        active: list[ActiveLevel] = []
        for executor in self.executors_info:
            if not executor.is_active:
                continue
            config = executor.config
            level_id = (executor.custom_info or {}).get("level_id") or getattr(
                config, "level_id", None
            )
            if not level_id:
                level_id = f"unknown_{executor.id}"
            side = ExecutionSide.BUY if config.side == TradeType.BUY else ExecutionSide.SELL
            price = _decimal(
                getattr(config, "entry_price", None) or getattr(config, "price", None)
            ) or Decimal("0")
            amount = _decimal(getattr(config, "amount", None) or Decimal("0")) or Decimal("0")
            filled_quote = _decimal(getattr(executor, "filled_amount_quote", 0)) or Decimal("0")
            custom = executor.custom_info or {}
            is_filled = bool(getattr(executor, "is_trading", False)) or filled_quote > Decimal("0")
            if custom.get("held_position_orders"):
                is_filled = True
            active.append(
                ActiveLevel(
                    executor_id=executor.id,
                    level_id=str(level_id),
                    side=side,
                    price=price,
                    amount=amount,
                    quote_notional=price * amount,
                    created_at=float(getattr(executor, "timestamp", 0) or 0),
                    is_filled=is_filled,
                    plan_mode=self._executor_plan_mode(executor.id),
                    last_replace_at=_decimal(custom.get("last_replace_at"))
                    if custom.get("last_replace_at") is not None
                    else None,
                )
            )
        self._pending_stop_ids.intersection_update({item.executor_id for item in active})
        return active

    def _plan_records(self) -> list[dict[str, Any]]:
        if not self.config.replay_existing_plan and not self._plan_tailer._bootstrapped:
            self._plan_tailer.bootstrap()
            return []
        return self._plan_tailer.poll()

    def _update_latest_plan(self) -> None:
        for record in self._plan_records():
            try:
                self._latest_plan = parse_grid_plan(record, self.config.trading_pair)
            except ValueError as exc:
                logger.warning("Skipping invalid Stage 4 GridPlan: %s", exc)

    def _runtime_reconciliation(self, now: float) -> tuple[RuntimeHealth, ReconciliationResult]:
        self._update_latest_plan()
        health = self._read_health()
        if self._order_error_pause_until and now >= self._order_error_pause_until:
            if health.ready_for_new_entries:
                self._consecutive_order_errors = 0
                self._order_error_pause_until = 0.0
        policy = self._policy(now)

        def quantize_price(price: Decimal) -> Decimal:
            return self.market_data_provider.quantize_order_price(
                self.config.connector_name, self.config.trading_pair, price
            )

        def quantize_amount(amount: Decimal) -> Decimal:
            return self.market_data_provider.quantize_order_amount(
                self.config.connector_name, self.config.trading_pair, amount
            )

        result = reconcile_grid_plan(
            self._latest_plan,
            active=self._active_levels(),
            health=health,
            policy=policy,
            now_epoch=now,
            quantize_price=quantize_price,
            quantize_amount=quantize_amount,
        )
        return health, result

    async def update_processed_data(self):
        """Read the latest plan and expose the current deterministic execution state."""

        now = float(self.market_data_provider.time())
        try:
            health, result = self._runtime_reconciliation(now)
        except Exception as exc:
            logger.exception("Derive adaptive grid reconciliation failed closed")
            health = RuntimeHealth(
                testnet_verified=False,
                connector_ready=False,
                market_data_ready=False,
                trading_rules_available=False,
                balance_verified=False,
                position_verified=False,
                best_bid=None,
                best_ask=None,
                reason=f"reconciliation_error:{type(exc).__name__}",
                environment=normalize_environment(self.config.environment),
                environment_verified=False,
                environment_consistent=False,
                mainnet_canary_authorized=False,
            )
            result = ReconciliationResult(
                pause_reason=health.reason,
                testnet_verified=False,
            )
        self._last_health = health
        self._last_reconciliation = result
        self._record_state_transitions(result)
        active = self._active_levels()
        self._record_executor_events(active)
        self._record_blocked_levels(result)
        plan = self._latest_plan
        desired_ids = [item.level_id for item in plan.levels] if plan else []
        active_ids = [item.level_id for item in active]
        filled_ids = [item.level_id for item in active if item.is_filled]
        inventory_blocks = [
            item.level_id
            for item in result.blocked
            if "position notional" in item.reason or "exposure" in item.reason
        ]
        balance_blocks = [item.level_id for item in result.blocked if "collateral" in item.reason]
        minimum_blocks = [
            item.level_id
            for item in result.blocked
            if "minimum" in item.reason or "quantized" in item.reason
        ]
        self.processed_data = {
            "timestamp": now,
            "trading_pair": self.config.trading_pair,
            "mode": plan.mode if plan else "PAUSE",
            "plan_version": plan.plan_version if plan else None,
            "grid_valid": bool(plan and plan.valid),
            "grid_enabled": bool(plan and plan.enabled),
            "center_price": plan.center_price if plan else None,
            "grid_width_pct": plan.total_grid_width_pct if plan else Decimal("0"),
            "desired_buy_levels": (
                min(len(plan.buy_levels), self.config.execution_max_levels_per_side) if plan else 0
            ),
            "desired_sell_levels": (
                min(len(plan.sell_levels), self.config.execution_max_levels_per_side) if plan else 0
            ),
            "active_entry_count": len(active),
            "filled_executor_count": sum(1 for item in active if item.is_filled),
            "desired_levels": desired_ids,
            "active_entry_levels": active_ids,
            "filled_position_levels": filled_ids,
            "levels_to_create": [item.level_id for item in result.creates],
            "levels_to_stop": [item.level_id for item in result.stops],
            "keep_count": len(result.keeps),
            "refresh_count": sum(result.replacement_reason_counts.values()),
            "replacement_reason_counts": result.replacement_reason_counts,
            "keep_reasons": result.keep_reasons,
            "levels_blocked_by_inventory": inventory_blocks,
            "levels_blocked_by_balance": balance_blocks,
            "levels_blocked_by_minimum": minimum_blocks,
            "execution_cap_per_side": self.config.execution_max_levels_per_side,
            "inventory_notional": health.position_notional,
            "potential_long_exposure": result.potential_long_exposure,
            "potential_short_exposure": result.potential_short_exposure,
            "blocked_levels": [blocked.level_id for blocked in result.blocked],
            "blocked_level_reasons": [blocked.reason for blocked in result.blocked],
            "consecutive_errors": self._consecutive_order_errors,
            "testnet_verified": health.testnet_verified,
            "environment": health.environment,
            "environment_verified": health.environment_verified,
            "environment_consistent": health.environment_consistent,
            "mainnet_canary_authorized": health.mainnet_canary_authorized,
            "pause_reason": result.pause_reason,
            "execution_enabled": self.config.execution_enabled,
            "orders_created": self._orders_created,
            "orders_cancelled": self._orders_cancelled,
            "fills": self._fills,
        }

    def _journal_once(self, key: tuple[Any, ...], event: str, **fields: Any) -> None:
        if key in self._journal_keys:
            return
        self._journal_keys.add(key)
        try:
            self._journal.append(event, **fields)
        except OSError as exc:
            logger.warning("Unable to append execution journal event: %s", exc)

    def _record_state_transitions(self, result: ReconciliationResult) -> None:
        plan = self._latest_plan
        mode = plan.mode if plan else "pause"
        if self._previous_mode is not None and mode != self._previous_mode:
            self._journal_once(
                ("mode", self._previous_mode, mode, plan.plan_version if plan else None),
                "MODE_CHANGE",
                previous_mode=self._previous_mode,
                mode=mode,
                plan_version=plan.plan_version if plan else None,
            )
        if result.pause_reason and not self._previous_pause_reason:
            self._journal_once(
                ("pause", plan.plan_version if plan else None, result.pause_reason),
                "PAUSE",
                level_id=None,
                plan_version=plan.plan_version if plan else None,
                mode=mode,
                reason=result.pause_reason,
            )
        if not result.pause_reason and self._previous_pause_reason:
            self._journal_once(
                ("resume", plan.plan_version if plan else None),
                "RESUME",
                plan_version=plan.plan_version if plan else None,
                mode=mode,
            )
        self._previous_mode = mode
        self._previous_pause_reason = result.pause_reason

    def _record_blocked_levels(self, result: ReconciliationResult) -> None:
        plan = self._latest_plan
        for blocked in result.blocked:
            event = "MIN_NOTIONAL_BLOCK"
            if "inventory" in blocked.reason or "position" in blocked.reason:
                event = "INVENTORY_BLOCK"
            elif "collateral" in blocked.reason:
                event = "BALANCE_BLOCK"
            self._journal_once(
                (event, blocked.level_id, plan.plan_version if plan else None, blocked.reason),
                event,
                level_id=blocked.level_id,
                plan_version=plan.plan_version if plan else None,
                mode=plan.mode if plan else "pause",
                quote_amount=blocked.quote_amount,
                reason=blocked.reason,
            )

    def _record_executor_events(self, active: list[ActiveLevel]) -> None:
        active_ids = {item.executor_id for item in active}
        for item in active:
            if item.is_filled and item.executor_id not in self._filled_ids:
                self._filled_ids.add(item.executor_id)
                self._fills += 1
                if item.side is ExecutionSide.BUY:
                    self._maker_buy_fills += 1
                else:
                    self._maker_sell_fills += 1
                self._filled_quote_volume += item.quote_notional
                self._journal_once(
                    ("fill", item.executor_id),
                    "ENTRY_FILLED",
                    level_id=item.level_id,
                    executor_id=item.executor_id,
                    price=item.price,
                    amount=item.amount,
                    quote_amount=item.quote_notional,
                )
            if (
                item.executor_id in self._executor_plan_modes
                and item.executor_id not in self._created_success_ids
            ):
                self._created_success_ids.add(item.executor_id)
                self._journal_once(
                    ("create_success", item.executor_id),
                    "CREATE_SUCCESS",
                    level_id=item.level_id,
                    executor_id=item.executor_id,
                    price=item.price,
                    amount=item.amount,
                    quote_amount=item.quote_notional,
                )
        for executor in self.executors_info:
            if executor.id in active_ids or executor.id in self._seen_terminal_ids:
                continue
            self._seen_terminal_ids.add(executor.id)
            raw_close_type = getattr(executor, "close_type", "")
            close_type = getattr(raw_close_type, "name", None) or getattr(
                raw_close_type, "value", raw_close_type
            )
            close_type = str(close_type).upper()
            if close_type in {"TAKE_PROFIT", "COMPLETED"}:
                event = "POSITION_EXITED"
                self._consecutive_order_errors = 0
            elif close_type in {"FAILED", "INSUFFICIENT_BALANCE"}:
                event = "STOP_FAILED" if executor.id in self._pending_stop_ids else "CREATE_FAILED"
                self._consecutive_order_errors += 1
                if self._consecutive_order_errors >= self.config.max_consecutive_order_errors:
                    try:
                        now = float(self.market_data_provider.time())
                    except Exception:
                        now = 0.0
                    self._order_error_pause_until = max(
                        self._order_error_pause_until,
                        now + self.config.order_error_pause_seconds,
                    )
            else:
                event = "STOP_SUCCESS"
                self._pending_stop_ids.discard(executor.id)
            self._journal_once(
                ("terminal", executor.id),
                event,
                executor_id=executor.id,
                level_id=(executor.custom_info or {}).get("level_id"),
                close_type=close_type,
                net_pnl_quote=getattr(executor, "net_pnl_quote", Decimal("0")),
            )

    def _executor_config(self, desired) -> PositionExecutorConfig:
        now = float(self.market_data_provider.time())
        executor_id = (
            f"{self.config.id}__{self.config.trading_pair}__{desired.level_id}"
            f"__v{desired.plan_version}"
            f"__mode_{desired.mode}__{int(now * 1000)}"
        )
        side = TradeType.BUY if desired.side is ExecutionSide.BUY else TradeType.SELL
        triple_barrier = TripleBarrierConfig(
            stop_loss=self.config.stop_loss_pct,
            take_profit=desired.take_profit_pct,
            time_limit=self.config.time_limit_seconds,
            open_order_type=OrderType.LIMIT_MAKER,
            take_profit_order_type=OrderType.LIMIT_MAKER,
            stop_loss_order_type=OrderType.MARKET,
            time_limit_order_type=OrderType.MARKET,
        )
        config = PositionExecutorConfig(
            id=executor_id,
            timestamp=now,
            controller_id=self.config.id,
            trading_pair=self.config.trading_pair,
            connector_name=self.config.connector_name,
            side=side,
            entry_price=desired.price,
            amount=desired.amount,
            triple_barrier_config=triple_barrier,
            leverage=self.config.leverage,
            activation_bounds=None,
            level_id=desired.level_id,
        )
        self._executor_plan_modes[executor_id] = desired.mode
        return config

    def determine_executor_actions(self) -> list[ExecutorAction]:
        """Apply stops first; create actions are deferred until replacements clear."""

        result = self._last_reconciliation
        plan = self._latest_plan
        if result is None:
            return []
        if result.stops:
            for stop in result.stops:
                self._journal_once(
                    ("stop", stop.executor_id, stop.reason),
                    "STOP_REQUEST",
                    level_id=stop.level_id,
                    executor_id=stop.executor_id,
                    plan_version=plan.plan_version if plan else None,
                    mode=plan.mode if plan else "pause",
                    reason=stop.reason,
                    execution_enabled=self.config.execution_enabled,
                )
            if not self.config.execution_enabled:
                logger.info("WOULD STOP %s unfilled grid entries", len(result.stops))
                return []
            actions: list[ExecutorAction] = []
            for stop in result.stops:
                if stop.executor_id in self._pending_stop_ids:
                    continue
                self._pending_stop_ids.add(stop.executor_id)
                self._orders_cancelled += 1
                actions.append(
                    StopExecutorAction(
                        controller_id=self.config.id,
                        executor_id=stop.executor_id,
                        keep_position=stop.keep_position,
                    )
                )
            return actions
        if result.pause_reason or not result.creates:
            return []
        for desired in result.creates:
            self._journal_once(
                ("create", desired.level_id, desired.plan_version, desired.price, desired.amount),
                "CREATE_REQUEST",
                level_id=desired.level_id,
                plan_version=desired.plan_version,
                mode=desired.mode,
                theoretical_price=desired.theoretical_price,
                price=desired.price,
                amount=desired.amount,
                quote_amount=desired.quote_amount,
                execution_enabled=self.config.execution_enabled,
                reason="dry_run" if not self.config.execution_enabled else "approved",
            )
        if not self.config.execution_enabled:
            logger.info("WOULD CREATE %s maker grid entries", len(result.creates))
            return []
        actions = []
        for desired in result.creates:
            actions.append(
                CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=self._executor_config(desired),
                )
            )
            self._orders_created += 1
        return actions

    def get_custom_info(self) -> dict:
        current = {key: _wire(value) for key, value in self.processed_data.items()}
        realized = Decimal("0")
        unrealized = Decimal("0")
        if self.performance_report is not None:
            realized = _decimal(
                getattr(self.performance_report, "realized_pnl_quote", 0)
            ) or Decimal("0")
            unrealized = _decimal(
                getattr(self.performance_report, "unrealized_pnl_quote", 0)
            ) or Decimal("0")
        return {
            "current": current,
            "metrics": {
                "orders_created": self._orders_created,
                "orders_cancelled": self._orders_cancelled,
                "keep_count": len((self._last_reconciliation or ReconciliationResult()).keeps),
                "refresh_count": sum(
                    (
                        self._last_reconciliation or ReconciliationResult()
                    ).replacement_reason_counts.values()
                ),
                "replacement_reason_counts": (
                    self._last_reconciliation or ReconciliationResult()
                ).replacement_reason_counts,
                "order_errors": self._consecutive_order_errors,
                "fills": self._fills,
                "maker_buy_fills": self._maker_buy_fills,
                "maker_sell_fills": self._maker_sell_fills,
                "filled_quote_volume": str(self._filled_quote_volume),
                "realized_pnl": str(realized),
                "unrealized_pnl": str(unrealized),
            },
            "testnet_verified": bool(self._last_health and self._last_health.testnet_verified),
            "execution_enabled": self.config.execution_enabled,
        }

    def to_format_status(self) -> list[str]:
        data = self.processed_data
        if not data:
            return ["Derive Adaptive Grid: waiting for Stage 4 GridPlan"]
        status = [
            "╔ DERIVE ADAPTIVE GRID ═════════════════╗",
            (
                f"Pair: {data.get('trading_pair')}  Mode: {data.get('mode')}  "
                f"Plan: v{data.get('plan_version')}"
            ),
            (
                f"Grid valid: {'YES' if data.get('grid_valid') else 'NO'}  "
                f"Enabled: {'YES' if data.get('grid_enabled') else 'NO'}"
            ),
            f"Reference: {data.get('center_price')}  Width: {data.get('grid_width_pct')}",
            (
                f"Desired: BUY {data.get('desired_buy_levels')} / "
                f"SELL {data.get('desired_sell_levels')}"
            ),
            (
                f"Active entries: {data.get('active_entry_count')}  "
                f"Filled: {data.get('filled_executor_count')}"
            ),
            (
                f"Inventory: {data.get('inventory_notional')}  "
                f"Potential long: {data.get('potential_long_exposure')}"
            ),
            (
                f"Potential short: {data.get('potential_short_exposure')}  "
                f"Blocked: {len(data.get('blocked_levels', []))}"
            ),
            (
                f"Errors: {data.get('consecutive_errors')}  "
                f"Environment: {data.get('environment', 'unknown')}  "
                f"Verified: {'YES' if data.get('environment_verified') else 'NO'}"
            ),
            (
                f"Execution enabled: {data.get('execution_enabled')}  "
                f"Canary authorized: {data.get('mainnet_canary_authorized')}  "
                f"Pause: {data.get('pause_reason') or 'none'}"
            ),
            "╚════════════════════════════════════════╝",
        ]
        return status
