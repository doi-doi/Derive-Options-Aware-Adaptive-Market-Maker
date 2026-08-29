"""Tests for the small public paper-grid demo."""

from __future__ import annotations

from decimal import Decimal

import pytest

from derive_options_mm.public_demo import (
    PublicMarket,
    fetch_market,
    market_mode,
    normalize_asset,
    paper_grid,
)


class FakePublicClient:
    def post(self, method: str, params: dict[str, object]) -> object:
        if method == "public/get_instruments":
            return [
                {
                    "instrument_name": "BTC-PERP",
                    "is_active": True,
                    "minimum_amount": "0.01",
                    "tick_size": "0.1",
                }
            ]
        if method == "public/get_tickers":
            return {
                "tickers": {
                    "BTC-PERP": {
                        "instrument_ticker": {
                            "b": "100.0",
                            "a": "101.0",
                            "stats": {"p": "0.01"},
                        }
                    }
                }
            }
        raise AssertionError(f"unexpected method: {method}")


def test_normalize_asset_accepts_common_names() -> None:
    assert normalize_asset("btc") == "BTC"
    assert normalize_asset("ETH-PERP") == "ETH"
    assert normalize_asset("SOL-USDC") == "SOL"
    with pytest.raises(ValueError, match="Choose one of"):
        normalize_asset("DOGE")


def test_fetch_market_reads_public_fields() -> None:
    market = fetch_market("BTC", FakePublicClient())
    assert market.instrument == "BTC-PERP"
    assert market.mid == pytest.approx(100.5)
    assert market.spread_bps == pytest.approx(99.5024876)
    assert market.minimum_amount == Decimal("0.01")
    assert market.change_pct == pytest.approx(1.0)


def test_paper_grid_stays_away_from_midpoint() -> None:
    market = PublicMarket(
        asset="BTC",
        instrument="BTC-PERP",
        bid=100.0,
        ask=101.0,
        change_pct=0.0,
        minimum_amount=Decimal("0.01"),
        tick_size=Decimal("0.1"),
    )
    grid = paper_grid(market, levels=2)
    assert len(grid) == 4
    assert [level.side for level in grid] == ["BUY", "SELL", "BUY", "SELL"]
    assert all(level.price < market.mid for level in grid[::2])
    assert all(level.price > market.mid for level in grid[1::2])


def test_market_mode_is_explainable() -> None:
    assert market_mode(1.0) == "NORMAL"
    assert market_mode(-5.0) == "CAUTION"
    assert market_mode(10.0) == "DEFENSIVE"
