"""Configuration helpers for the configurable Derive execution basket."""

from __future__ import annotations

import re
from collections.abc import Iterable

DEFAULT_TRADING_PAIRS = ("BTC-USDC", "ETH-USDC")
# Kept as a compatibility alias for older imports.  The controller no longer
# restricts execution to this particular pair set.
BTC_HYPE_TRADING_PAIRS = ("BTC-USDC", "HYPE-USDC")
_MAX_CONFIGURED_PAIRS = 8
_DERIVE_PERPETUAL_PAIR = re.compile(r"^[A-Z0-9]+-USDC$")


def validate_trading_pairs(pairs: Iterable[str]) -> tuple[str, ...]:
    """Normalize and validate configurable Derive perpetual pairs.

    Hummingbot uses ``BASE-USDC`` names for Derive perpetuals.  The validator
    deliberately checks the shape and uniqueness of the configured universe,
    rather than maintaining a brittle hard-coded asset allow-list.  Exchange
    availability and pair-specific order/risk settings are still validated by
    the live connector and the controller configuration.
    """

    configured = tuple(str(pair).strip().upper() for pair in pairs)
    if not configured:
        raise ValueError("derive_adaptive_grid_portfolio requires at least one trading pair")
    if len(configured) > _MAX_CONFIGURED_PAIRS:
        raise ValueError(
            "derive_adaptive_grid_portfolio supports at most "
            f"{_MAX_CONFIGURED_PAIRS} configured pairs"
        )
    if len(set(configured)) != len(configured):
        raise ValueError("derive_adaptive_grid_portfolio trading_pairs must be unique")
    invalid = tuple(pair for pair in configured if not _DERIVE_PERPETUAL_PAIR.fullmatch(pair))
    if invalid:
        raise ValueError(
            "derive_adaptive_grid_portfolio pairs must use Derive BASE-USDC format; "
            f"invalid: {', '.join(invalid)}"
        )
    return configured


def validate_btc_hype_pairs(pairs: Iterable[str]) -> tuple[str, ...]:
    """Backward-compatible alias for :func:`validate_trading_pairs`."""

    return validate_trading_pairs(pairs)


__all__ = [
    "DEFAULT_TRADING_PAIRS",
    "BTC_HYPE_TRADING_PAIRS",
    "validate_trading_pairs",
    "validate_btc_hype_pairs",
]
