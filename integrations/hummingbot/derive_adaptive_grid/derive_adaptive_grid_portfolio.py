"""Hummingbot controller for the BTC-USDC/HYPE-USDC Derive basket.

This controller is deliberately separate from the proven single-pair
``derive_adaptive_grid`` adapter.  It consumes the same Stage 4 JSONL stream,
keeps lifecycle state per pair, and applies one shared portfolio gate before
creating any new PositionExecutor.  It is testnet-only and dry-run by default.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
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

from .derive_adaptive_grid import JsonlPlanTailer, _decimal, _wire
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
from .portfolio_config import BTC_HYPE_TRADING_PAIRS, validate_btc_hype_pairs
from .portfolio_execution import (
    PortfolioExecutionPolicy,
    PortfolioRiskDecision,
    apply_portfolio_risk,
    evaluate_portfolio_risk,
)

logger = logging.getLogger(__name__)


def _as_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


class DeriveAdaptiveGridPortfolioConfig(ControllerConfigBase):
    """Fail-closed configuration for exactly BTC-USDC and HYPE-USDC."""

    controller_type: str = "market_making"
    controller_name: str = "derive_adaptive_grid_portfolio"
    total_amount_quote: Decimal = Field(default=Decimal("700"), ge=0)

    connector_name: str = "derive_perpetual_testnet"
    # Retained for compatibility with tooling that expects a single-pair field.
    # The portfolio controller uses ``trading_pairs`` everywhere internally.
    trading_pair: str = "BTC-USDC"
    trading_pairs: tuple[str, ...] = BTC_HYPE_TRADING_PAIRS
    leverage: int = Field(default=1, ge=1, le=20)
    position_mode: PositionMode = PositionMode.ONEWAY
    environment: str = "testnet"
    market_environment: str = "testnet"
    options_environment: str = "testnet"
    account_environment: str = "testnet"
    execution_environment: str = "testnet"
    allow_mainnet_trading: bool = False

    grid_plan_path: str = "/home/hummingbot/data/derive_grid_plans.jsonl"
    execution_journal_path: str = "/home/hummingbot/data/derive_execution_events.jsonl"
    replay_existing_plan: bool = True

    execution_enabled: bool = Field(default=False, json_schema_extra={"is_updatable": True})
    execution_max_levels_per_side: int = Field(
        default=1, ge=1, le=1, json_schema_extra={"is_updatable": True}
    )
    post_only: bool = True
    testnet_order_scales_by_pair: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "BTC-USDC": Decimal("10"),
            "HYPE-USDC": Decimal("10"),
        },
        json_schema_extra={"is_updatable": True},
    )

    pair_max_total_position_notional: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "BTC-USDC": Decimal("350"),
            "HYPE-USDC": Decimal("350"),
        },
        json_schema_extra={"is_updatable": True},
    )
    pair_max_side_position_notional: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "BTC-USDC": Decimal("250"),
            "HYPE-USDC": Decimal("250"),
        },
        json_schema_extra={"is_updatable": True},
    )
    pair_max_active_executors: int = Field(default=2, ge=1, le=2)
    pair_max_active_grid_levels: int = Field(default=2, ge=1, le=2)

    portfolio_max_gross_notional: Decimal = Field(
        default=Decimal("700"), gt=0, json_schema_extra={"is_updatable": True}
    )
    portfolio_soft_beta_exposure: Decimal = Field(
        default=Decimal("450"), gt=0, json_schema_extra={"is_updatable": True}
    )
    portfolio_hard_beta_exposure: Decimal = Field(
        default=Decimal("650"), gt=0, json_schema_extra={"is_updatable": True}
    )
    portfolio_max_long_beta_exposure: Decimal = Field(
        default=Decimal("650"), gt=0, json_schema_extra={"is_updatable": True}
    )
    portfolio_max_short_beta_exposure: Decimal = Field(
        default=Decimal("650"), gt=0, json_schema_extra={"is_updatable": True}
    )
    portfolio_per_asset_max_position_notional: Decimal = Field(
        default=Decimal("400"), gt=0, json_schema_extra={"is_updatable": True}
    )
    portfolio_max_active_executors: int = Field(default=4, ge=1, le=4)
    portfolio_collateral_safety_buffer_pct: Decimal = Field(
        default=Decimal("0.20"), ge=0, lt=1, json_schema_extra={"is_updatable": True}
    )
    portfolio_betas: dict[str, Decimal] = Field(
        default_factory=lambda: {"BTC-USDC": Decimal("1"), "HYPE-USDC": Decimal("1")},
        json_schema_extra={"is_updatable": True},
    )

    minimum_order_lifetime_seconds: float = Field(default=30.0, ge=0)
    minimum_replace_interval_seconds: float = Field(default=0.0, ge=0)
    maximum_order_lifetime_seconds: float = Field(default=600.0, ge=0)
    refresh_price_tolerance_bps: Decimal = Field(default=Decimal("5"), ge=0)
    refresh_amount_tolerance_pct: Decimal = Field(default=Decimal("0.05"), ge=0)
    max_consecutive_order_errors: int = Field(default=3, ge=1)
    order_error_pause_seconds: float = Field(default=60.0, ge=0)
    stale_plan_timeout_seconds: float = Field(default=30.0, gt=0)
    collateral_safety_buffer_pct: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1)
    cancel_orders_on_pause: bool = True
    cancel_orders_on_shutdown: bool = True
    manual_kill_switch: bool = False
    emergency_close_positions_on_pause: bool = False

    take_profit_mode: str = "adjacent_grid"
    take_profit_pct: Decimal = Field(default=Decimal("0.001"), ge=0)
    take_profit_step_multiplier: Decimal = Field(default=Decimal("1"), gt=0)
    stop_loss_pct: Decimal | None = Field(default=None, ge=0)
    time_limit_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_portfolio_contract(self) -> DeriveAdaptiveGridPortfolioConfig:
        validate_btc_hype_pairs(self.trading_pairs)
        if self.trading_pair != "BTC-USDC":
            raise ValueError("trading_pair compatibility field must remain BTC-USDC")
        if self.connector_name != "derive_perpetual_testnet":
            raise ValueError("BTC/HYPE portfolio execution is testnet-only")
        if self.environment != "testnet":
            raise ValueError("BTC/HYPE portfolio execution requires environment=testnet")
        if any(
            value != "testnet"
            for value in (
                self.market_environment,
                self.options_environment,
                self.account_environment,
                self.execution_environment,
            )
        ):
            raise ValueError("all BTC/HYPE portfolio environments must be testnet")
        if self.allow_mainnet_trading:
            raise ValueError("BTC/HYPE portfolio execution cannot allow mainnet trading")
        if self.leverage != 1:
            raise ValueError("BTC/HYPE portfolio rollout requires leverage=1")
        if not self.post_only:
            raise ValueError("BTC/HYPE portfolio execution requires post_only=true")
        if self.maximum_order_lifetime_seconds < self.minimum_order_lifetime_seconds:
            raise ValueError("maximum order lifetime must not be below minimum lifetime")
        if self.portfolio_hard_beta_exposure <= self.portfolio_soft_beta_exposure:
            raise ValueError("portfolio hard beta limit must exceed the soft limit")
        for field_name, values in (
            ("testnet_order_scales_by_pair", self.testnet_order_scales_by_pair),
            ("pair_max_total_position_notional", self.pair_max_total_position_notional),
            ("pair_max_side_position_notional", self.pair_max_side_position_notional),
            ("portfolio_betas", self.portfolio_betas),
        ):
            if set(values) != set(self.trading_pairs):
                raise ValueError(f"{field_name} must contain exactly the configured pairs")
            if field_name != "portfolio_betas" and any(value <= 0 for value in values.values()):
                raise ValueError(f"{field_name} values must be positive")
        if any(value == 0 for value in self.portfolio_betas.values()):
            raise ValueError("portfolio_betas values must be non-zero")
        return self

    def update_markets(self, markets: MarketDict) -> MarketDict:
        for pair in self.trading_pairs:
            markets = markets.add_or_update(self.connector_name, pair)
        return markets


class DeriveAdaptiveGridPortfolio(ControllerBase):
    """Pair-scoped grid lifecycle with one shared BTC/HYPE risk boundary."""

    def __init__(self, config: DeriveAdaptiveGridPortfolioConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._plan_tailer = JsonlPlanTailer(config.grid_plan_path)
        self._latest_plans: dict[str, GridPlanView] = {}
        self._last_health: dict[str, RuntimeHealth] = {}
        self._last_reconciliation: dict[str, ReconciliationResult] = {}
        self._last_portfolio_decision = PortfolioRiskDecision()
        self._journal = JsonlExecutionJournal(config.execution_journal_path)
        self._journal_keys: set[tuple[Any, ...]] = set()
        self._executor_plan_modes: dict[str, str] = {}
        self._pending_stop_ids: set[str] = set()
        self._seen_terminal_ids: set[str] = set()
        self._filled_ids: set[str] = set()
        self._created_success_ids: set[str] = set()
        self._previous_modes: dict[str, str] = {}
        self._previous_pause_reasons: dict[str, str] = {}
        self._consecutive_order_errors = 0
        self._orders_created = 0
        self._orders_cancelled = 0
        self._fills = 0
        self._maker_buy_fills = 0
        self._maker_sell_fills = 0
        self._filled_quote_volume = Decimal("0")
        self._order_error_pause_until = 0.0

    def _connector(self):
        return self.market_data_provider.get_connector(self.config.connector_name)

    def _policy(self, pair: str, now: float = 0.0) -> ExecutionPolicy:
        error_pause_active = (
            self._consecutive_order_errors >= self.config.max_consecutive_order_errors
            and now < self._order_error_pause_until
        )
        return ExecutionPolicy(
            execution_max_levels_per_side=self.config.execution_max_levels_per_side,
            testnet_order_scale=self.config.testnet_order_scales_by_pair[pair],
            max_total_position_notional=self.config.pair_max_total_position_notional[pair],
            max_side_position_notional=self.config.pair_max_side_position_notional[pair],
            max_active_grid_levels=self.config.pair_max_active_grid_levels,
            max_active_executors=self.config.pair_max_active_executors,
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
        )

    def _read_position_notional(
        self, connector: Any, pair: str, reference_price: Decimal
    ) -> Decimal:
        positions = connector.account_positions
        if positions is None:
            raise RuntimeError("position_unavailable")
        signed_amount = Decimal("0")
        for position in positions.values():
            if getattr(position, "trading_pair", None) != pair:
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

    def _failed_health(self, reason: str) -> RuntimeHealth:
        return RuntimeHealth(
            testnet_verified=False,
            connector_ready=False,
            market_data_ready=False,
            trading_rules_available=False,
            balance_verified=False,
            position_verified=False,
            best_bid=None,
            best_ask=None,
            reason=reason,
        )

    def _read_health(self, pair: str) -> RuntimeHealth:
        provider_ready = bool(getattr(self.market_data_provider, "ready", False))
        connector = None
        rules = None
        available = None
        best_bid = None
        best_ask = None
        position_notional = Decimal("0")
        reason = ""
        testnet_verified = False
        connector_ready = False
        position_verified = False
        try:
            connector = self._connector()
            connector_name = str(getattr(connector, "name", self.config.connector_name))
            domain = str(getattr(connector, "domain", ""))
            testnet_verified = connector_name == self.config.connector_name and (
                "testnet" in connector_name.lower() or "testnet" in domain.lower()
            )
            if not testnet_verified:
                reason = "Execution blocked: Derive testnet could not be verified."
            connector_ready = bool(getattr(connector, "ready", False))
            if not connector_ready and not reason:
                reason = "connector_not_ready"
            exchange_mode = getattr(connector, "position_mode", None)
            if exchange_mode != self.config.position_mode and not reason:
                reason = "position_mode_unverified"
            best_bid = _decimal(
                self.market_data_provider.get_price_by_type(
                    self.config.connector_name, pair, PriceType.BestBid
                ),
            )
            best_ask = _decimal(
                self.market_data_provider.get_price_by_type(
                    self.config.connector_name, pair, PriceType.BestAsk
                ),
            )
            if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= best_bid:
                best_bid = best_ask = None
                if not reason:
                    reason = "market_data_not_ready"
            mid = ((best_bid + best_ask) / Decimal("2")) if best_bid and best_ask else Decimal("0")
            position_notional = self._read_position_notional(connector, pair, mid)
            position_verified = connector.account_positions is not None
            if not position_verified and not reason:
                reason = "position_unavailable"
            available = _decimal(
                self.market_data_provider.get_available_balance(
                    self.config.connector_name, pair.split("-")[1]
                ),
            )
            rules = self.market_data_provider.get_trading_rules(self.config.connector_name, pair)
            if available is None and not reason:
                reason = "balance_unavailable"
        except Exception as exc:  # noqa: BLE001 - this boundary must fail closed per pair
            reason = reason or f"runtime_health_error:{type(exc).__name__}"
            logger.warning("Derive portfolio health failed for %s: %s", pair, reason)

        trading_rules = None
        if rules is not None:
            trading_rules = TradingRuleView(
                min_order_size=max(Decimal("0"), _as_decimal(getattr(rules, "min_order_size", 0))),
                min_notional_size=max(
                    Decimal("0"), _as_decimal(getattr(rules, "min_notional_size", 0))
                ),
                min_price_increment=max(
                    Decimal("0"), _as_decimal(getattr(rules, "min_price_increment", 0))
                ),
                min_base_amount_increment=max(
                    Decimal("0"), _as_decimal(getattr(rules, "min_base_amount_increment", 0))
                ),
            )
        trading_rules_available = trading_rules is not None
        balance_verified = available is not None and available >= 0
        market_data_ready = (
            provider_ready
            and best_bid is not None
            and best_ask is not None
            and best_bid > 0
            and best_ask > best_bid
        )
        if not market_data_ready and not reason:
            reason = "market_data_not_ready"
        if not trading_rules_available and not reason:
            reason = "trading_rules_unavailable"
        if not position_verified and not reason:
            reason = "position_unavailable"
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
        )

    def _executor_plan_mode(self, executor_id: str) -> str | None:
        mapped = self._executor_plan_modes.get(executor_id)
        if mapped is not None:
            return mapped
        if "__mode_" not in executor_id:
            return None
        return executor_id.split("__mode_", 1)[1].split("__", 1)[0] or None

    def _active_levels(self, pair: str) -> list[ActiveLevel]:
        active: list[ActiveLevel] = []
        for executor in self.executors_info:
            if not executor.is_active or getattr(executor.config, "trading_pair", None) != pair:
                continue
            config = executor.config
            level_id = (executor.custom_info or {}).get("level_id") or getattr(
                config, "level_id", None
            )
            level_id = str(level_id or f"unknown_{executor.id}")
            side = ExecutionSide.BUY if config.side == TradeType.BUY else ExecutionSide.SELL
            price = _as_decimal(
                getattr(config, "entry_price", None) or getattr(config, "price", None)
            )
            amount = _as_decimal(getattr(config, "amount", None))
            filled_quote = _as_decimal(getattr(executor, "filled_amount_quote", 0))
            custom = executor.custom_info or {}
            is_filled = bool(getattr(executor, "is_trading", False)) or filled_quote > 0
            if custom.get("held_position_orders"):
                is_filled = True
            active.append(
                ActiveLevel(
                    executor_id=executor.id,
                    level_id=level_id,
                    side=side,
                    price=price,
                    amount=amount,
                    quote_notional=price * amount,
                    created_at=float(getattr(executor, "timestamp", 0) or 0),
                    is_filled=is_filled,
                    plan_mode=self._executor_plan_mode(executor.id),
                    last_replace_at=_as_decimal(custom.get("last_replace_at"))
                    if custom.get("last_replace_at") is not None
                    else None,
                )
            )
        return active

    def _plan_records(self) -> list[dict[str, Any]]:
        if not self.config.replay_existing_plan and not self._plan_tailer._bootstrapped:
            self._plan_tailer.bootstrap()
            return []
        return self._plan_tailer.poll()

    def _update_latest_plans(self) -> None:
        for record in self._plan_records():
            pair = str(record.get("trading_pair", ""))
            if pair not in self.config.trading_pairs:
                continue
            try:
                self._latest_plans[pair] = parse_grid_plan(record, pair)
            except ValueError as exc:
                logger.warning("Skipping invalid Stage 4 GridPlan for %s: %s", pair, exc)

    def _runtime_reconciliation(
        self, now: float
    ) -> tuple[dict[str, RuntimeHealth], dict[str, ReconciliationResult]]:
        self._update_latest_plans()
        health_by_pair: dict[str, RuntimeHealth] = {}
        results: dict[str, ReconciliationResult] = {}
        for pair in self.config.trading_pairs:
            try:
                health = self._read_health(pair)
                policy = self._policy(pair, now)
                active = self._active_levels(pair)

                def quantize_price(price: Decimal, selected_pair: str = pair) -> Decimal:
                    return self.market_data_provider.quantize_order_price(
                        self.config.connector_name, selected_pair, price
                    )

                def quantize_amount(amount: Decimal, selected_pair: str = pair) -> Decimal:
                    return self.market_data_provider.quantize_order_amount(
                        self.config.connector_name, selected_pair, amount
                    )

                result = reconcile_grid_plan(
                    self._latest_plans.get(pair),
                    active=active,
                    health=health,
                    policy=policy,
                    now_epoch=now,
                    quantize_price=quantize_price,
                    quantize_amount=quantize_amount,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed for only this pair
                logger.exception("Derive portfolio reconciliation failed for %s", pair)
                health = self._failed_health(f"reconciliation_error:{type(exc).__name__}")
                result = ReconciliationResult(
                    pause_reason=health.reason,
                    testnet_verified=False,
                )
            health_by_pair[pair] = health
            results[pair] = result

        positions = {pair: health.position_notional for pair, health in health_by_pair.items()}
        pending: dict[str, dict[str, Any]] = {}
        active_counts: dict[str, int] = {}
        proposed = {}
        for pair, result in results.items():
            active = self._active_levels(pair)
            pending[pair] = {
                "buy": sum(
                    (
                        item.quote_notional
                        for item in active
                        if not item.is_filled and item.side is ExecutionSide.BUY
                    ),
                    Decimal("0"),
                ),
                "sell": sum(
                    (
                        item.quote_notional
                        for item in active
                        if not item.is_filled and item.side is ExecutionSide.SELL
                    ),
                    Decimal("0"),
                ),
                "count": len(active),
            }
            active_counts[pair] = len(active)
            proposed[pair] = [] if result.pause_reason else list(result.creates)
        available = min(
            (
                health.available_collateral
                for health in health_by_pair.values()
                if health.available_collateral > 0
            ),
            default=Decimal("0"),
        )
        decision = evaluate_portfolio_risk(
            proposed,
            positions=positions,
            pending=pending,
            active_executors=active_counts,
            available_collateral=available,
            policy=PortfolioExecutionPolicy(
                portfolio_max_gross_notional=self.config.portfolio_max_gross_notional,
                portfolio_soft_beta_exposure=self.config.portfolio_soft_beta_exposure,
                portfolio_hard_beta_exposure=self.config.portfolio_hard_beta_exposure,
                portfolio_max_long_beta_exposure=self.config.portfolio_max_long_beta_exposure,
                portfolio_max_short_beta_exposure=self.config.portfolio_max_short_beta_exposure,
                per_asset_max_position_notional=self.config.portfolio_per_asset_max_position_notional,
                max_active_executors_per_asset=self.config.pair_max_active_executors,
                max_active_executors_portfolio=self.config.portfolio_max_active_executors,
                collateral_safety_buffer_pct=self.config.portfolio_collateral_safety_buffer_pct,
                leverage=Decimal(self.config.leverage),
                betas=self.config.portfolio_betas,
            ),
        )
        apply_portfolio_risk(results, decision)
        self._last_portfolio_decision = decision
        return health_by_pair, results

    def _journal_once(self, key: tuple[Any, ...], event: str, **fields: Any) -> None:
        if key in self._journal_keys:
            return
        self._journal_keys.add(key)
        try:
            self._journal.append(event, **fields)
        except OSError as exc:
            logger.warning("Unable to append portfolio execution journal event: %s", exc)

    def _record_state_transition(self, pair: str, result: ReconciliationResult) -> None:
        plan = self._latest_plans.get(pair)
        mode = plan.mode if plan else "pause"
        previous_mode = self._previous_modes.get(pair)
        if previous_mode is not None and previous_mode != mode:
            self._journal_once(
                ("mode", pair, previous_mode, mode, plan.plan_version if plan else None),
                "MODE_CHANGE",
                trading_pair=pair,
                previous_mode=previous_mode,
                mode=mode,
                plan_version=plan.plan_version if plan else None,
            )
        previous_pause = self._previous_pause_reasons.get(pair, "")
        if result.pause_reason and not previous_pause:
            self._journal_once(
                ("pause", pair, plan.plan_version if plan else None, result.pause_reason),
                "PAUSE",
                trading_pair=pair,
                plan_version=plan.plan_version if plan else None,
                mode=mode,
                reason=result.pause_reason,
            )
        if not result.pause_reason and previous_pause:
            self._journal_once(
                ("resume", pair, plan.plan_version if plan else None),
                "RESUME",
                trading_pair=pair,
                plan_version=plan.plan_version if plan else None,
                mode=mode,
            )
        self._previous_modes[pair] = mode
        self._previous_pause_reasons[pair] = result.pause_reason

    def _record_blocked_levels(self, pair: str, result: ReconciliationResult) -> None:
        plan = self._latest_plans.get(pair)
        for blocked in result.blocked:
            event = "MIN_NOTIONAL_BLOCK"
            if (
                "portfolio" in blocked.reason
                or "position" in blocked.reason
                or "exposure" in blocked.reason
            ):
                event = "INVENTORY_BLOCK"
            elif "collateral" in blocked.reason:
                event = "BALANCE_BLOCK"
            self._journal_once(
                (
                    event,
                    pair,
                    blocked.level_id,
                    plan.plan_version if plan else None,
                    blocked.reason,
                ),
                event,
                trading_pair=pair,
                level_id=blocked.level_id,
                plan_version=plan.plan_version if plan else None,
                mode=plan.mode if plan else "pause",
                quote_amount=blocked.quote_amount,
                reason=blocked.reason,
            )

    def _record_executor_events(self, active_by_pair: Mapping[str, list[ActiveLevel]]) -> None:
        active = [item for values in active_by_pair.values() for item in values]
        active_ids = {item.executor_id for item in active}
        pair_by_executor = {
            item.executor_id: pair for pair, values in active_by_pair.items() for item in values
        }
        for item in active:
            pair = pair_by_executor[item.executor_id]
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
                    trading_pair=pair,
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
                    trading_pair=pair,
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
            pair = str(getattr(executor.config, "trading_pair", "unknown"))
            raw_close_type = getattr(executor, "close_type", "")
            close_type = getattr(raw_close_type, "name", None) or getattr(
                raw_close_type, "value", raw_close_type
            )
            close_type = str(close_type).upper()
            if close_type in {"TAKE_PROFIT", "COMPLETED"}:
                event = "POSITION_EXITED"
                self._consecutive_order_errors = 0
            elif close_type in {"FAILED", "INSUFFICIENT_BALANCE"}:
                event = "CREATE_FAILED"
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
                trading_pair=pair,
                executor_id=executor.id,
                level_id=(executor.custom_info or {}).get("level_id"),
                close_type=close_type,
                net_pnl_quote=getattr(executor, "net_pnl_quote", Decimal("0")),
            )

    async def update_processed_data(self):
        """Read both plans, reconcile independently, then apply shared risk."""

        now = float(self.market_data_provider.time())
        health_by_pair, results = self._runtime_reconciliation(now)
        self._last_health = health_by_pair
        self._last_reconciliation = results
        active_by_pair = {pair: self._active_levels(pair) for pair in self.config.trading_pairs}
        for pair in self.config.trading_pairs:
            self._record_state_transition(pair, results[pair])
            self._record_blocked_levels(pair, results[pair])
        self._record_executor_events(active_by_pair)

        pair_states: dict[str, Any] = {}
        for pair in self.config.trading_pairs:
            plan = self._latest_plans.get(pair)
            result = results[pair]
            health = health_by_pair[pair]
            active = active_by_pair[pair]
            pair_states[pair] = {
                "mode": plan.mode if plan else "pause",
                "plan_version": plan.plan_version if plan else None,
                "grid_valid": bool(plan and plan.valid),
                "grid_enabled": bool(plan and plan.enabled),
                "active_entry_count": len(active),
                "filled_executor_count": sum(1 for item in active if item.is_filled),
                "levels_to_create": [item.level_id for item in result.creates],
                "levels_to_stop": [item.level_id for item in result.stops],
                "blocked_levels": [item.level_id for item in result.blocked],
                "blocked_level_reasons": [item.reason for item in result.blocked],
                "keep_count": len(result.keeps),
                "pause_reason": result.pause_reason,
                "inventory_notional": health.position_notional,
                "available_collateral": health.available_collateral,
                "testnet_verified": health.testnet_verified,
            }
        self.processed_data = {
            "timestamp": now,
            "trading_pairs": list(self.config.trading_pairs),
            "pair_states": _wire(pair_states),
            "portfolio": _wire(vars(self._last_portfolio_decision)),
            "execution_enabled": self.config.execution_enabled,
            "orders_created": self._orders_created,
            "orders_cancelled": self._orders_cancelled,
            "fills": self._fills,
            "filled_quote_volume": self._filled_quote_volume,
            "consecutive_errors": self._consecutive_order_errors,
        }

    def _executor_config(self, pair: str, desired: Any) -> PositionExecutorConfig:
        now = float(self.market_data_provider.time())
        executor_id = (
            f"{self.config.id}__{pair}__{desired.level_id}"
            f"__v{desired.plan_version}__mode_{desired.mode}__{int(now * 1000)}"
        )
        side = TradeType.BUY if desired.side is ExecutionSide.BUY else TradeType.SELL
        config = PositionExecutorConfig(
            id=executor_id,
            timestamp=now,
            controller_id=self.config.id,
            trading_pair=pair,
            connector_name=self.config.connector_name,
            side=side,
            entry_price=desired.price,
            amount=desired.amount,
            triple_barrier_config=TripleBarrierConfig(
                stop_loss=self.config.stop_loss_pct,
                take_profit=desired.take_profit_pct,
                time_limit=self.config.time_limit_seconds,
                open_order_type=OrderType.LIMIT_MAKER,
                take_profit_order_type=OrderType.LIMIT_MAKER,
                stop_loss_order_type=OrderType.MARKET,
                time_limit_order_type=OrderType.MARKET,
            ),
            leverage=self.config.leverage,
            activation_bounds=None,
            level_id=desired.level_id,
        )
        self._executor_plan_modes[executor_id] = desired.mode
        return config

    def determine_executor_actions(self) -> list[ExecutorAction]:
        """Stop stale entries first; never replace two markets in one tick."""

        if not self._last_reconciliation:
            return []
        stops: list[ExecutorAction] = []
        has_stops = any(result.stops for result in self._last_reconciliation.values())
        if has_stops:
            for pair, result in self._last_reconciliation.items():
                plan = self._latest_plans.get(pair)
                for stop in result.stops:
                    self._journal_once(
                        ("stop", pair, stop.executor_id, stop.reason),
                        "STOP_REQUEST",
                        trading_pair=pair,
                        level_id=stop.level_id,
                        executor_id=stop.executor_id,
                        plan_version=plan.plan_version if plan else None,
                        reason=stop.reason,
                        execution_enabled=self.config.execution_enabled,
                    )
                    if (
                        not self.config.execution_enabled
                        or stop.executor_id in self._pending_stop_ids
                    ):
                        continue
                    self._pending_stop_ids.add(stop.executor_id)
                    self._orders_cancelled += 1
                    stops.append(
                        StopExecutorAction(
                            controller_id=self.config.id,
                            executor_id=stop.executor_id,
                            keep_position=stop.keep_position,
                        )
                    )
            return stops

        creates: list[CreateExecutorAction] = []
        for pair, result in self._last_reconciliation.items():
            plan = self._latest_plans.get(pair)
            if result.pause_reason:
                continue
            for desired in result.creates:
                self._journal_once(
                    (
                        "create",
                        pair,
                        desired.level_id,
                        desired.plan_version,
                        desired.price,
                        desired.amount,
                    ),
                    "CREATE_REQUEST",
                    trading_pair=pair,
                    level_id=desired.level_id,
                    plan_version=desired.plan_version,
                    mode=desired.mode,
                    price=desired.price,
                    amount=desired.amount,
                    quote_amount=desired.quote_amount,
                    execution_enabled=self.config.execution_enabled,
                    reason="dry_run" if not self.config.execution_enabled else "approved",
                )
                if not self.config.execution_enabled:
                    continue
                creates.append(
                    CreateExecutorAction(
                        controller_id=self.config.id,
                        executor_config=self._executor_config(pair, desired),
                    )
                )
                self._orders_created += 1
        return creates

    def get_custom_info(self) -> dict[str, Any]:
        realized = Decimal("0")
        unrealized = Decimal("0")
        if self.performance_report is not None:
            realized = _as_decimal(getattr(self.performance_report, "realized_pnl_quote", 0))
            unrealized = _as_decimal(getattr(self.performance_report, "unrealized_pnl_quote", 0))
        return {
            "current": _wire(self.processed_data),
            "metrics": {
                "orders_created": self._orders_created,
                "orders_cancelled": self._orders_cancelled,
                "fills": self._fills,
                "maker_buy_fills": self._maker_buy_fills,
                "maker_sell_fills": self._maker_sell_fills,
                "filled_quote_volume": str(self._filled_quote_volume),
                "realized_pnl": str(realized),
                "unrealized_pnl": str(unrealized),
                "portfolio_risk_reasons": list(self._last_portfolio_decision.reasons),
            },
            "trading_pairs": list(self.config.trading_pairs),
            "testnet_only": True,
            "execution_enabled": self.config.execution_enabled,
        }

    def to_format_status(self) -> list[str]:
        data = self.processed_data
        if not data:
            return ["Derive Adaptive Grid Portfolio: waiting for Stage 4 plans"]
        pair_states = data.get("pair_states", {})
        lines = [
            "╔ DERIVE ADAPTIVE GRID PORTFOLIO ═══════╗",
            "Pairs: BTC-USDC, HYPE-USDC  Environment: TESTNET",
        ]
        for pair in self.config.trading_pairs:
            state = pair_states.get(pair, {})
            lines.append(
                f"{pair}: mode={state.get('mode')} valid={state.get('grid_valid')} "
                f"active={state.get('active_entry_count')} "
                f"filled={state.get('filled_executor_count')} "
                f"pause={state.get('pause_reason') or 'none'}"
            )
        portfolio = data.get("portfolio", {})
        lines.extend(
            [
                f"Portfolio gross={portfolio.get('gross_notional')} "
                f"beta={portfolio.get('beta_exposure')}",
                f"Created={data.get('orders_created')} cancelled={data.get('orders_cancelled')} "
                f"fills={data.get('fills')} execution={data.get('execution_enabled')}",
                "╚════════════════════════════════════════╝",
            ]
        )
        return lines


__all__ = ["DeriveAdaptiveGridPortfolio", "DeriveAdaptiveGridPortfolioConfig"]
