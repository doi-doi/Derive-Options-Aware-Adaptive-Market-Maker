"""Fail-closed contracts for the Derive mainnet canary.

This module has no Hummingbot or network dependency.  It is shared by the
controller configuration, the read-only readiness tool, and tests so the
mainnet boundary is explicit and deterministic.  A mainnet canary is never
authorized by a default value: the environment, risk budgets, execution
switches, and exact acknowledgement must all be supplied independently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

MAINNET_CONNECTOR_NAME = "derive_perpetual"
TESTNET_CONNECTOR_NAME = "derive_perpetual_testnet"
MAINNET_DOMAIN = "derive_perpetual"
TESTNET_DOMAIN = "derive_perpetual_testnet"
MAINNET_CHAIN_ID = 957
MAINNET_REST_URL = "https://api.lyra.finance"
MAINNET_WS_URL = "wss://api.lyra.finance/ws"
MAINNET_CANARY_ACK = "MAINNET_CANARY_ACK=I_UNDERSTAND_REAL_FUNDS_ARE_AT_RISK"

ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def normalize_environment(value: Any) -> str:
    """Normalize the environment labels used by the four data boundaries."""

    normalized = str(value or "").strip().lower()
    if normalized in {"mainnet", "production", "prod", "live"}:
        return "mainnet"
    if normalized in {"testnet", "demo", "sandbox"}:
        return "testnet"
    return "unknown"


def environment_for_connector(connector_name: Any, domain: Any = None) -> str:
    """Return the environment implied by a connector name/domain pair."""

    connector = str(connector_name or "").strip().lower()
    connector_domain = str(domain or "").strip().lower()
    if connector == MAINNET_CONNECTOR_NAME and connector_domain in {"", MAINNET_DOMAIN}:
        return "mainnet"
    if connector == TESTNET_CONNECTOR_NAME and connector_domain in {"", TESTNET_DOMAIN}:
        return "testnet"
    return "unknown"


@dataclass(frozen=True)
class EnvironmentConsistency:
    """Evidence that market, options, account, and execution use one network."""

    required_environment: str
    market_environment: str
    options_environment: str
    account_environment: str
    execution_environment: str
    reasons: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.reasons and all(
            value == self.required_environment
            for value in (
                self.market_environment,
                self.options_environment,
                self.account_environment,
                self.execution_environment,
            )
        )


def check_environment_consistency(
    *,
    required_environment: str,
    market_connector: Any,
    market_domain: Any = None,
    options_environment: Any,
    account_environment: Any,
    execution_environment: Any,
) -> EnvironmentConsistency:
    """Check all four boundaries and return every mismatch, fail-closed."""

    required = normalize_environment(required_environment)
    market = environment_for_connector(market_connector, market_domain)
    options = normalize_environment(options_environment)
    account = normalize_environment(account_environment)
    execution = normalize_environment(execution_environment)
    reasons: list[str] = []
    for label, value in (
        ("required", required),
        ("market", market),
        ("options", options),
        ("account", account),
        ("execution", execution),
    ):
        if value == "unknown":
            reasons.append(f"{label}_environment_unknown")
    if required == "unknown":
        reasons.append("required_environment_unknown")
    for label, value in (
        ("market", market),
        ("options", options),
        ("account", account),
        ("execution", execution),
    ):
        if required != "unknown" and value != required:
            reasons.append(f"{label}_environment_mismatch")
    return EnvironmentConsistency(
        required_environment=required,
        market_environment=market,
        options_environment=options,
        account_environment=account,
        execution_environment=execution,
        reasons=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True)
class CanaryRiskLimits:
    """Explicit budgets required before a real mainnet order is possible."""

    max_order_notional: Decimal | None = None
    max_total_position_notional: Decimal | None = None
    max_loss_quote: Decimal | None = None

    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        fields = (
            ("mainnet_canary_max_order_notional", self.max_order_notional),
            (
                "mainnet_canary_max_total_position_notional",
                self.max_total_position_notional,
            ),
            ("mainnet_canary_max_loss_quote", self.max_loss_quote),
        )
        for name, value in fields:
            if value is None:
                blockers.append(f"{name}_not_configured")
            elif value <= ZERO:
                blockers.append(f"{name}_must_be_positive")
        if (
            self.max_order_notional is not None
            and self.max_total_position_notional is not None
            and self.max_order_notional > self.max_total_position_notional
        ):
            blockers.append("max_order_notional_exceeds_total_position_notional")
        return tuple(blockers)


@dataclass(frozen=True)
class CanaryOrderSize:
    """Smallest exchange-valid amount derived from one Stage 4 allocation."""

    theoretical_quote: Decimal
    reference_price: Decimal
    minimum_order_size: Decimal
    minimum_notional_size: Decimal
    amount_increment: Decimal
    amount: Decimal
    notional: Decimal
    required_scale: Decimal


def ceil_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Round upward so a minimum is not lost to exchange quantization."""

    if value <= ZERO:
        raise ValueError("value must be positive")
    if increment <= ZERO:
        return value
    units = (value / increment).to_integral_value(rounding=ROUND_CEILING)
    return units * increment


def calculate_minimum_canary_size(
    *,
    theoretical_quote: Decimal,
    reference_price: Decimal,
    minimum_order_size: Decimal,
    amount_increment: Decimal,
    minimum_notional_size: Decimal = ZERO,
    max_order_notional: Decimal | None = None,
) -> CanaryOrderSize:
    """Calculate the smallest amount and Stage 4 scale that satisfy live rules."""

    values = {
        "theoretical_quote": theoretical_quote,
        "reference_price": reference_price,
        "minimum_order_size": minimum_order_size,
        "amount_increment": amount_increment,
        "minimum_notional_size": minimum_notional_size,
    }
    for name, value in values.items():
        if value < ZERO:
            raise ValueError(f"{name} must be non-negative")
    if theoretical_quote <= ZERO or reference_price <= ZERO:
        raise ValueError("theoretical_quote and reference_price must be positive")
    if minimum_order_size <= ZERO and minimum_notional_size <= ZERO:
        raise ValueError("at least one exchange minimum must be positive")

    amount_floor = max(minimum_order_size, minimum_notional_size / reference_price)
    amount = ceil_to_increment(amount_floor, amount_increment)
    notional = amount * reference_price
    if max_order_notional is not None and notional > max_order_notional:
        raise ValueError(
            "minimum valid order exceeds mainnet_canary_max_order_notional "
            f"({notional} > {max_order_notional})"
        )
    return CanaryOrderSize(
        theoretical_quote=theoretical_quote,
        reference_price=reference_price,
        minimum_order_size=minimum_order_size,
        minimum_notional_size=minimum_notional_size,
        amount_increment=amount_increment,
        amount=amount,
        notional=notional,
        required_scale=notional / theoretical_quote,
    )


def maker_price_is_passive(
    side: str, wire_price: Decimal, best_bid: Decimal, best_ask: Decimal
) -> bool:
    """Require strict non-crossing relative to the observed public book."""

    normalized = str(side).lower()
    if wire_price <= ZERO or best_bid <= ZERO or best_ask <= best_bid:
        return False
    if normalized == "buy":
        return wire_price < best_ask
    if normalized == "sell":
        return wire_price > best_bid
    return False


def mainnet_canary_authorized(
    *,
    mainnet_environment_verified: bool,
    environment_consistent: bool,
    allow_mainnet_trading: bool,
    execution_enabled: bool,
    acknowledgement: str | None,
    risk_limits: CanaryRiskLimits,
    order_scale: Decimal | None,
    account_state_verified: bool = False,
    stop_loss_pct: Decimal | None = None,
) -> bool:
    """Return true only when every independent mainnet canary gate is present."""

    return not mainnet_canary_blockers(
        mainnet_environment_verified=mainnet_environment_verified,
        environment_consistent=environment_consistent,
        allow_mainnet_trading=allow_mainnet_trading,
        execution_enabled=execution_enabled,
        acknowledgement=acknowledgement,
        risk_limits=risk_limits,
        order_scale=order_scale,
        account_state_verified=account_state_verified,
        stop_loss_pct=stop_loss_pct,
    )


def mainnet_canary_blockers(
    *,
    mainnet_environment_verified: bool,
    environment_consistent: bool,
    allow_mainnet_trading: bool,
    execution_enabled: bool,
    acknowledgement: str | None,
    risk_limits: CanaryRiskLimits,
    order_scale: Decimal | None,
    account_state_verified: bool = False,
    stop_loss_pct: Decimal | None = None,
) -> tuple[str, ...]:
    """Explain why a mainnet canary cannot create an order."""

    blockers: list[str] = []
    if not mainnet_environment_verified:
        blockers.append("mainnet_environment_not_verified")
    if not environment_consistent:
        blockers.append("environment_inconsistent")
    if not allow_mainnet_trading:
        blockers.append("allow_mainnet_trading_false")
    if not execution_enabled:
        blockers.append("execution_enabled_false")
    if acknowledgement != MAINNET_CANARY_ACK:
        blockers.append("mainnet_canary_acknowledgement_missing_or_invalid")
    if not account_state_verified:
        blockers.append("authenticated_account_state_not_verified")
    blockers.extend(risk_limits.blockers())
    if stop_loss_pct is None:
        blockers.append("mainnet_loss_control_not_configured")
    elif stop_loss_pct <= ZERO:
        blockers.append("mainnet_stop_loss_pct_must_be_positive")
    elif (
        risk_limits.max_loss_quote is not None
        and risk_limits.max_total_position_notional is not None
        and risk_limits.max_total_position_notional * stop_loss_pct
        > risk_limits.max_loss_quote
    ):
        blockers.append("configured_stop_loss_exceeds_mainnet_loss_budget")
    if order_scale is None:
        blockers.append("mainnet_canary_order_scale_not_configured")
    elif order_scale <= ZERO:
        blockers.append("mainnet_canary_order_scale_must_be_positive")
    return tuple(dict.fromkeys(blockers))


def existing_account_blockers(
    *,
    account_read_available: bool,
    position_notional: Decimal | None,
    open_order_count: int | None,
    other_bot_active: bool = False,
) -> tuple[str, ...]:
    """Block the canary until existing BTC exposure and orders are known clear."""

    blockers: list[str] = []
    if not account_read_available:
        blockers.append("authenticated_account_state_unavailable")
    if position_notional is None:
        blockers.append("position_state_unavailable")
    elif abs(position_notional) > ZERO:
        blockers.append("existing_btc_position_requires_explicit_acknowledgement")
    if open_order_count is None:
        blockers.append("open_order_state_unavailable")
    elif open_order_count > 0:
        blockers.append("existing_btc_orders_require_explicit_acknowledgement")
    if other_bot_active:
        blockers.append("other_bot_or_strategy_is_active")
    return tuple(dict.fromkeys(blockers))


def config_canary_risk_limits(values: Mapping[str, Any]) -> CanaryRiskLimits:
    """Build limits from YAML-like values without inventing a fallback."""

    return CanaryRiskLimits(
        max_order_notional=_decimal(values.get("mainnet_canary_max_order_notional")),
        max_total_position_notional=_decimal(
            values.get("mainnet_canary_max_total_position_notional")
        ),
        max_loss_quote=_decimal(values.get("mainnet_canary_max_loss_quote")),
    )


__all__ = [
    "CanaryOrderSize",
    "CanaryRiskLimits",
    "EnvironmentConsistency",
    "MAINNET_CANARY_ACK",
    "MAINNET_CHAIN_ID",
    "MAINNET_CONNECTOR_NAME",
    "MAINNET_DOMAIN",
    "MAINNET_REST_URL",
    "MAINNET_WS_URL",
    "TESTNET_CONNECTOR_NAME",
    "TESTNET_DOMAIN",
    "calculate_minimum_canary_size",
    "ceil_to_increment",
    "check_environment_consistency",
    "config_canary_risk_limits",
    "environment_for_connector",
    "existing_account_blockers",
    "mainnet_canary_authorized",
    "mainnet_canary_blockers",
    "maker_price_is_passive",
    "normalize_environment",
]
