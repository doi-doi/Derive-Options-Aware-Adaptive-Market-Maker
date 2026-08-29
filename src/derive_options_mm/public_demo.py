"""Simple, read-only Derive paper-grid demo.

Only public perpetual market data is requested. This file contains no wallet,
credential, private API, or order-placement code.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any

DEFAULT_BASE_URL = "https://api.lyra.finance"
SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "HYPE")
PUBLIC_METHODS = frozenset({"public/get_instruments", "public/get_tickers"})


class PublicAPIError(RuntimeError):
    """A friendly error from the public Derive data service."""


@dataclass(frozen=True)
class PublicAPIClient:
    """Tiny public-only JSON-RPC client."""

    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 10.0
    max_attempts: int = 2

    def post(self, method: str, params: dict[str, Any]) -> Any:
        if method not in PUBLIC_METHODS:
            raise ValueError(f"Only public market-data methods are allowed: {method}")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "derive-adaptive-grid-public-demo/1.0",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or "result" not in payload:
                    raise PublicAPIError("Derive returned an unexpected response")
                if payload.get("error"):
                    raise PublicAPIError("Derive returned a public-data error")
                return payload["result"]
            except (OSError, TimeoutError, PublicAPIError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(0.5)
        raise PublicAPIError(f"Derive public data request failed: {last_error}")


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _decimal(value: Any, fallback: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return fallback
    return parsed if parsed.is_finite() and parsed > 0 else fallback


def normalize_asset(value: str) -> str:
    """Accept BTC, BTC-PERP, or BTC-USDC."""

    asset = str(value).strip().upper().replace("_", "-")
    for suffix in ("-PERP", "-USDC", "-USD"):
        if asset.endswith(suffix):
            asset = asset[: -len(suffix)]
            break
    if asset not in SUPPORTED_ASSETS:
        raise ValueError(f"Choose one of: {', '.join(SUPPORTED_ASSETS)}")
    return asset


def _rows(result: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        values = result.get(key, result.get("data", []))
        if isinstance(values, list):
            return [row for row in values if isinstance(row, dict)]
    return []


def _ticker_row(result: Any, instrument: str) -> dict[str, Any]:
    tickers = result.get("tickers", {}) if isinstance(result, dict) else {}
    if not isinstance(tickers, dict):
        return {}
    for key, value in tickers.items():
        if str(key).upper() == instrument.upper() or instrument.upper() in str(key).upper():
            row = value.get("instrument_ticker", value) if isinstance(value, dict) else {}
            return row if isinstance(row, dict) else {}
    return {}


@dataclass(frozen=True)
class PublicMarket:
    asset: str
    instrument: str
    bid: float
    ask: float
    change_pct: float | None
    minimum_amount: Decimal
    tick_size: Decimal

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000.0


@dataclass(frozen=True)
class PaperLevel:
    side: str
    level: int
    price: float
    amount: float
    notional: float


def fetch_market(asset: str, client: PublicAPIClient | None = None) -> PublicMarket:
    """Read one perpetual market from Derive's two public endpoints."""

    normalized = normalize_asset(asset)
    api = client or PublicAPIClient()
    instruments = _rows(
        api.post(
            "public/get_instruments",
            {"currency": normalized, "instrument_type": "perp", "expired": False},
        ),
        "instruments",
    )
    active = [row for row in instruments if row.get("is_active", True)]
    expected = f"{normalized}-PERP"
    instrument = next(
        (row for row in active if str(row.get("instrument_name", "")).upper() == expected),
        active[0] if active else None,
    )
    if instrument is None:
        raise PublicAPIError(f"No active {expected} market was returned")
    instrument_name = str(instrument.get("instrument_name", expected))

    ticker = _ticker_row(
        api.post("public/get_tickers", {"instrument_type": "perp", "currency": normalized}),
        instrument_name,
    )
    bid = _number(ticker.get("best_bid_price", ticker.get("b")))
    ask = _number(ticker.get("best_ask_price", ticker.get("a")))
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        raise PublicAPIError(f"No usable bid/ask was returned for {instrument_name}")

    stats = ticker.get("stats", {})
    change_ratio = _number(stats.get("p")) if isinstance(stats, dict) else None
    change_pct = (
        None
        if change_ratio is None
        else change_ratio * 100.0
        if abs(change_ratio) <= 1
        else change_ratio
    )
    return PublicMarket(
        asset=normalized,
        instrument=instrument_name,
        bid=bid,
        ask=ask,
        change_pct=change_pct,
        minimum_amount=_decimal(instrument.get("minimum_amount"), Decimal("0.01")),
        tick_size=_decimal(instrument.get("tick_size"), Decimal("0.1")),
    )


def market_mode(change_pct: float | None) -> str:
    """Label movement for display; this is not a price prediction."""

    movement = abs(change_pct or 0.0)
    if movement >= 8:
        return "DEFENSIVE"
    if movement >= 4:
        return "CAUTION"
    return "NORMAL"


def _round_price(value: float, tick_size: Decimal, rounding: str) -> float:
    tick = tick_size if isinstance(tick_size, Decimal) else Decimal(str(tick_size))
    units = (Decimal(str(value)) / tick).to_integral_value(rounding=rounding)
    return float(units * tick)


def paper_grid(market: PublicMarket, levels: int = 3) -> tuple[PaperLevel, ...]:
    """Suggest paper prices around the midpoint without submitting them."""

    if not 1 <= levels <= 5:
        raise ValueError("levels must be between 1 and 5")
    movement_bps = abs(market.change_pct or 0.0) * 100.0
    step_bps = min(250.0, max(5.0, market.spread_bps * 1.5, movement_bps * 0.25))
    step_bps *= {"NORMAL": 1.0, "CAUTION": 1.5, "DEFENSIVE": 2.5}[market_mode(market.change_pct)]
    amount = float(market.minimum_amount)
    result: list[PaperLevel] = []
    for level in range(1, levels + 1):
        distance = step_bps * level / 10_000.0
        buy = _round_price(market.mid * (1 - distance), market.tick_size, ROUND_DOWN)
        sell = _round_price(market.mid * (1 + distance), market.tick_size, ROUND_UP)
        result.extend(
            (
                PaperLevel("BUY", level, buy, amount, buy * amount),
                PaperLevel("SELL", level, sell, amount, sell * amount),
            )
        )
    return tuple(result)


def format_snapshot(market: PublicMarket, levels: int = 3) -> str:
    change = "n/a" if market.change_pct is None else f"{market.change_pct:+.2f}%"
    lines = [
        "Derive Adaptive Grid — public paper demo",
        "Public data only | no API key | no orders placed",
        "",
        f"Market : {market.instrument}",
        f"Mid    : {market.mid:,.4f}",
        f"Bid/ask: {market.bid:,.4f} / {market.ask:,.4f}",
        f"Spread : {market.spread_bps:.2f} bps",
        f"24h move: {change}",
        f"Mode   : {market_mode(market.change_pct)} (paper label, not a prediction)",
        "",
        "Suggested paper grid:",
        "  side  level  price          amount       notional",
    ]
    for level in paper_grid(market, levels):
        lines.append(
            f"  {level.side:<5} {level.level:<6} {level.price:>12,.4f}"
            f"  {level.amount:>10.4f}  {level.notional:>12,.2f}"
        )
    lines.extend(
        (
            "",
            "These are educational paper prices. Nothing is sent to Derive.",
            "Use Ctrl-C to stop a watch session.",
        )
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read public Derive prices and print a safe paper grid."
    )
    parser.add_argument("--asset", default="BTC", help="BTC, ETH, SOL, or HYPE")
    parser.add_argument("--levels", type=int, default=3, help="paper levels per side, 1 to 5")
    parser.add_argument("--watch", action="store_true", help="refresh continuously")
    parser.add_argument("--interval", type=float, default=10.0, help="watch interval in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 1 <= args.levels <= 5:
            raise ValueError("levels must be between 1 and 5")
        if args.interval <= 0:
            raise ValueError("interval must be positive")
        while True:
            market = fetch_market(args.asset)
            if args.watch:
                print("\033[2J\033[H", end="")
            print(format_snapshot(market, args.levels), flush=True)
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except (OSError, PublicAPIError, ValueError) as exc:
        print(f"Could not read Derive public data: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_BASE_URL",
    "PaperLevel",
    "PublicAPIClient",
    "PublicAPIError",
    "PublicMarket",
    "SUPPORTED_ASSETS",
    "fetch_market",
    "format_snapshot",
    "main",
    "market_mode",
    "normalize_asset",
    "paper_grid",
]
