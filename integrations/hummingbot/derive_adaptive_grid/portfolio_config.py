"""Configuration helpers for the BTC/HYPE Derive execution basket."""

from __future__ import annotations

from collections.abc import Iterable

BTC_HYPE_TRADING_PAIRS = ("BTC-USDC", "HYPE-USDC")


def validate_btc_hype_pairs(pairs: Iterable[str]) -> tuple[str, ...]:
    """Validate the intentionally narrow two-asset execution scope."""

    configured = tuple(dict.fromkeys(str(pair).strip().upper() for pair in pairs))
    if configured != BTC_HYPE_TRADING_PAIRS:
        raise ValueError(
            "derive_adaptive_grid_portfolio supports exactly BTC-USDC and HYPE-USDC in that order"
        )
    return configured


__all__ = ["BTC_HYPE_TRADING_PAIRS", "validate_btc_hype_pairs"]
