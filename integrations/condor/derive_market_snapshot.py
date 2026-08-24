"""Read-only Derive perpetual data collection routine for Condor.

This module is deliberately limited to FETCH -> NORMALIZE -> LOG -> EXPOSE DATA.
It never calls an order, cancellation, leverage, position-mode, or executor API.
The underlying Hummingbot order-book tracker may update faster than this routine;
``snapshot_interval_seconds`` only controls persisted/reportable snapshots.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

_PROJECT_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from derive_options_mm.options_iv import (  # noqa: E402
    DeriveOptionsProvider,
    OptionsVolatilitySnapshot,
    unavailable_options_snapshot,
)

logger = logging.getLogger(__name__)

CATEGORY = "Market Data"
CONTINUOUS = True


class Config(BaseModel):
    """Configuration for one read-only Derive perpetual data stream."""

    connector_name: str = Field(
        default="derive_perpetual_testnet",
        description="Installed Hummingbot Derive perpetual connector",
    )
    trading_pair: str = Field(
        default="BTC-USDC",
        description="Hummingbot pair mapped by the connector from Derive BTC-PERP",
    )
    book_depth_levels: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Number of order-book levels to aggregate on each side",
    )
    max_data_age_seconds: float = Field(
        default=15.0,
        gt=0,
        le=300,
        description="Maximum accepted tracker/order-book age",
    )
    snapshot_interval_seconds: float = Field(
        default=5.0,
        gt=0,
        le=3600,
        description="Interval between stored normalized snapshots",
    )
    request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description="Timeout for each read-only Hummingbot API request",
    )
    output_path: str = Field(
        default="data/derive_market_snapshots.jsonl",
        description="Append-only JSONL path, relative to the Condor process directory",
    )
    max_output_file_bytes: int = Field(
        default=50_000_000,
        ge=1024,
        le=1_000_000_000,
        description="Rotate the active JSONL file after it reaches this size",
    )
    max_rotated_files: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of bounded .1, .2, ... JSONL backups to retain",
    )
    enable_recent_trades: bool = Field(
        default=True,
        description="Use the installed Hummingbot API market-data WebSocket when available",
    )
    trade_update_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        le=60,
        description="Official Hummingbot API WebSocket trade-batch interval",
    )
    account_name: str | None = Field(
        default="master_account",
        description="Optional authenticated account to read without changing state",
    )
    options_enabled: bool = Field(
        default=True,
        description="Read the public Derive BTC options surface for ATM IV",
    )
    options_environment: str = Field(
        default="production",
        description="Environment label for the public Derive options source",
    )
    options_api_base_url: str = Field(
        default="https://api.lyra.finance",
        description="Official Derive public API base URL for option data",
    )
    options_currency: str = Field(
        default="BTC",
        description="Underlying currency used for Derive option discovery",
    )
    options_min_days_to_expiry: float = Field(default=2.0, ge=0, le=365)
    options_target_days_to_expiry: float = Field(default=7.0, ge=0, le=365)
    options_max_days_to_expiry: float = Field(default=14.0, ge=0, le=365)
    options_max_atm_distance_pct: float = Field(
        default=0.05,
        gt=0,
        le=1,
        description="Maximum relative distance from perpetual reference to ATM strike",
    )
    max_option_data_age_seconds: float = Field(
        default=15.0,
        gt=0,
        le=300,
        description="Maximum accepted age of selected Derive option ticker data",
    )
    options_metadata_refresh_interval_seconds: float = Field(
        default=900.0,
        gt=0,
        le=86_400,
        description="How often active option metadata and expiries are refreshed",
    )
    options_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description="Timeout for each public Derive options request",
    )
    options_max_iv: float = Field(
        default=10.0,
        gt=0,
        le=100,
        description="Broad upper sanity bound for decimal IV values",
    )


class MarketDataError(RuntimeError):
    """Raised when an input cannot be normalized safely."""


class MarketSnapshot(BaseModel):
    """One normalized, JSON-serializable perpetual market snapshot."""

    timestamp: str
    connector: str
    trading_pair: str
    book_depth_levels: int
    order_book_timestamp: str | None = None

    best_bid: float | None = None
    best_ask: float | None = None
    mid_price: float | None = None
    spread_abs: float | None = None
    spread_bps: float | None = None
    best_bid_size: float | None = None
    best_ask_size: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    top_level_imbalance: float | None = None
    depth_imbalance: float | None = None
    data_age_seconds: float | None = None

    recent_buy_volume: float | None = None
    recent_sell_volume: float | None = None
    order_flow_imbalance: float | None = None
    trade_data_available: bool = False

    atm_iv: float | None = None
    atm_call_iv: float | None = None
    atm_put_iv: float | None = None
    atm_strike: float | None = None
    atm_distance_pct: float | None = None
    option_instrument: str | None = None
    option_call_instrument: str | None = None
    option_put_instrument: str | None = None
    option_expiry: str | None = None
    option_expiry_dte: float | None = None
    option_data_timestamp: str | None = None
    option_data_age_seconds: float | None = None
    option_data_source: str | None = None
    option_environment: str | None = None
    iv_confidence: float = Field(default=0.0, ge=0, le=1)
    option_data_errors: list[str] = Field(default_factory=list)
    iv_data_available: bool = False

    current_position: float | None = None
    position_notional: float | None = None
    available_balance: float | None = None
    account_data_available: bool = False
    account_data_errors: list[str] = Field(default_factory=list)

    data_valid: bool = False
    validation_errors: list[str] = Field(default_factory=list)


class BookFeatures(NamedTuple):
    """Decimal calculations derived from one order-book response."""

    order_book_timestamp: str
    data_age_seconds: float
    best_bid: Decimal
    best_ask: Decimal
    mid_price: Decimal
    spread_abs: Decimal
    spread_bps: Decimal
    best_bid_size: Decimal
    best_ask_size: Decimal
    bid_depth: Decimal
    ask_depth: Decimal
    top_level_imbalance: Decimal | None
    depth_imbalance: Decimal | None


class AccountData(NamedTuple):
    """Read-only account values; failures do not invalidate market data."""

    current_position: float | None = None
    position_notional: float | None = None
    available_balance: float | None = None
    account_data_available: bool = False
    errors: tuple[str, ...] = ()


class TradeWindow(NamedTuple):
    """Trade volumes drained from the official Hummingbot API stream."""

    buy_volume: Decimal | None
    sell_volume: Decimal | None
    available: bool
    error: str | None = None


def _decimal(value: Any, field_name: str) -> Decimal:
    """Convert a value to a finite Decimal or raise a normalized data error."""

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketDataError(f"invalid {field_name}") from exc
    if not parsed.is_finite():
        raise MarketDataError(f"invalid {field_name}")
    return parsed


def _as_optional_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _unix_seconds(value: Any, field_name: str) -> float:
    parsed = _decimal(value, field_name)
    if parsed <= 0:
        raise MarketDataError(f"missing or invalid {field_name}")
    seconds = float(parsed)
    if seconds > 10_000_000_000:
        seconds /= 1_000
    return seconds


def _iso_utc(seconds: float | None = None) -> str:
    value = time.time() if seconds is None else seconds
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _float_or_none(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def calculate_mid_price(best_bid: Decimal, best_ask: Decimal) -> Decimal:
    """Calculate the midpoint without changing the source precision."""

    return (best_bid + best_ask) / Decimal(2)


def calculate_spread_abs(best_bid: Decimal, best_ask: Decimal) -> Decimal:
    """Calculate the absolute best-quote spread."""

    return best_ask - best_bid


def calculate_spread_bps(spread_abs: Decimal, mid_price: Decimal) -> Decimal:
    """Calculate spread in basis points, rejecting an unusable midpoint."""

    if mid_price <= 0:
        raise MarketDataError("mid price is invalid")
    return spread_abs / mid_price * Decimal(10_000)


def safe_imbalance(first: Decimal, second: Decimal) -> Decimal | None:
    """Return a normalized difference or None when the denominator is zero."""

    denominator = first + second
    if denominator == 0:
        return None
    return (first - second) / denominator


def aggregate_depth(levels: Sequence[tuple[Decimal, Decimal]], depth: int) -> Decimal:
    """Sum the amount from at most ``depth`` parsed levels."""

    if depth < 1:
        raise ValueError("depth must be at least one")
    return sum((amount for _, amount in levels[:depth]), start=Decimal(0))


def calculate_order_flow_imbalance(
    recent_buy_volume: Decimal, recent_sell_volume: Decimal
) -> Decimal | None:
    """Calculate OFI safely for a drained recent-trade window."""

    return safe_imbalance(recent_buy_volume, recent_sell_volume)


def _parse_level(level: Any, side: str) -> tuple[Decimal, Decimal]:
    if isinstance(level, dict):
        price_raw = level.get("price", level.get("Price"))
        amount_raw = level.get(
            "amount",
            level.get("quantity", level.get("size", level.get("Quantity"))),
        )
    else:
        try:
            price_raw, amount_raw = level[0], level[1]
        except (IndexError, KeyError, TypeError) as exc:
            raise MarketDataError(f"invalid {side} order-book level") from exc

    price = _decimal(price_raw, f"{side} price")
    amount = _decimal(amount_raw, f"{side} amount")
    if price <= 0 or amount < 0:
        raise MarketDataError(f"invalid {side} order-book level")
    return price, amount


def _tracker_age_seconds(diagnostics: Any, trading_pair: str) -> float | None:
    """Read tracker monotonic metrics, never the API collection timestamp."""

    if not isinstance(diagnostics, dict):
        return None
    metrics = diagnostics.get("metrics")
    if not isinstance(metrics, dict):
        return None
    pair_metrics_all = metrics.get("per_pair_metrics")
    pair_metrics = (
        pair_metrics_all.get(trading_pair) if isinstance(pair_metrics_all, dict) else None
    )
    if not isinstance(pair_metrics, dict):
        return None

    tracker_start = _as_optional_decimal(metrics.get("tracker_start_time"))
    uptime = _as_optional_decimal(metrics.get("uptime_seconds"))
    last_snapshot = _as_optional_decimal(pair_metrics.get("last_snapshot_timestamp"))
    if tracker_start is None or uptime is None or last_snapshot is None:
        return None
    if tracker_start < 0 or uptime < 0 or last_snapshot <= 0:
        return None
    age = tracker_start + uptime - last_snapshot
    return max(float(age), 0.0)


def _diagnostic_errors(
    diagnostics: Any, config: Config
) -> tuple[float | None, list[str]]:
    errors: list[str] = []
    if not isinstance(diagnostics, dict):
        return None, ["order-book diagnostics are not an object"]
    if diagnostics.get("error"):
        errors.append("order-book tracker returned an error")
    if diagnostics.get("tracker_ready") is not True:
        errors.append("order-book tracker is not ready")
    if diagnostics.get("websocket_status") != "connected":
        errors.append("order-book WebSocket is not connected")

    trading_pairs = diagnostics.get("trading_pairs")
    order_books = diagnostics.get("order_books")
    pair_is_tracked = isinstance(trading_pairs, list) and config.trading_pair in trading_pairs
    if not pair_is_tracked and isinstance(order_books, dict):
        pair_is_tracked = config.trading_pair in order_books
    if not pair_is_tracked:
        errors.append(f"{config.trading_pair} is not tracked")

    age = _tracker_age_seconds(diagnostics, config.trading_pair)
    if age is None:
        errors.append("order-book tracker age is unavailable")
    elif age > config.max_data_age_seconds:
        errors.append(
            f"stale order-book tracker snapshot ({age:.1f}s old; "
            f"limit {config.max_data_age_seconds:.1f}s)"
        )
    return age, errors


def calculate_book_features(
    order_book: Any,
    *,
    depth: int,
    data_age_seconds: float | None = None,
    now: float | None = None,
) -> BookFeatures:
    """Validate and normalize one Hummingbot order-book response."""

    if not isinstance(order_book, dict):
        raise MarketDataError("order-book response is not an object")
    if order_book.get("error"):
        raise MarketDataError("Hummingbot returned an order-book error")

    timestamp_seconds = _unix_seconds(order_book.get("timestamp"), "order-book timestamp")
    api_age = (time.time() if now is None else now) - timestamp_seconds
    if api_age < -5:
        raise MarketDataError("order-book timestamp is too far in the future")
    if data_age_seconds is None:
        data_age_seconds = max(api_age, 0.0)

    bids_raw = order_book.get("bids")
    asks_raw = order_book.get("asks")
    if not isinstance(bids_raw, list) or not bids_raw:
        raise MarketDataError("order book has no bids")
    if not isinstance(asks_raw, list) or not asks_raw:
        raise MarketDataError("order book has no asks")

    bids = [_parse_level(row, "bid") for row in bids_raw[:depth]]
    asks = [_parse_level(row, "ask") for row in asks_raw[:depth]]
    if not bids or not asks:
        raise MarketDataError("order book has no populated top levels")

    best_bid, best_bid_size = bids[0]
    best_ask, best_ask_size = asks[0]
    if best_bid <= 0 or best_ask <= 0:
        raise MarketDataError("best bid or ask is not positive")
    if best_bid >= best_ask:
        raise MarketDataError("order book is locked or crossed")

    mid_price = calculate_mid_price(best_bid, best_ask)
    spread_abs = calculate_spread_abs(best_bid, best_ask)
    bid_depth = aggregate_depth(bids, depth)
    ask_depth = aggregate_depth(asks, depth)
    return BookFeatures(
        order_book_timestamp=_iso_utc(timestamp_seconds),
        data_age_seconds=float(data_age_seconds),
        best_bid=best_bid,
        best_ask=best_ask,
        mid_price=mid_price,
        spread_abs=spread_abs,
        spread_bps=calculate_spread_bps(spread_abs, mid_price),
        best_bid_size=best_bid_size,
        best_ask_size=best_ask_size,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        top_level_imbalance=safe_imbalance(best_bid_size, best_ask_size),
        depth_imbalance=safe_imbalance(bid_depth, ask_depth),
    )


def validate_snapshot(snapshot: MarketSnapshot, max_data_age_seconds: float) -> tuple[str, ...]:
    """Return validation errors without mutating the normalized snapshot."""

    errors: list[str] = []
    if not snapshot.timestamp:
        errors.append("timestamp is missing")
    if snapshot.best_bid is None:
        errors.append("best bid is missing")
    elif snapshot.best_bid <= 0:
        errors.append("best bid is not positive")
    if snapshot.best_ask is None:
        errors.append("best ask is missing")
    elif snapshot.best_ask <= 0:
        errors.append("best ask is not positive")
    if snapshot.best_bid is not None and snapshot.best_ask is not None:
        if snapshot.best_bid >= snapshot.best_ask:
            errors.append("best bid is greater than or equal to best ask")
    if snapshot.mid_price is None or snapshot.mid_price <= 0:
        errors.append("mid price is invalid")
    if snapshot.spread_abs is None or snapshot.spread_abs < 0:
        errors.append("absolute spread is invalid")
    if snapshot.spread_bps is None or snapshot.spread_bps < 0:
        errors.append("spread bps is invalid")
    if snapshot.data_age_seconds is not None:
        if snapshot.data_age_seconds < -5:
            errors.append("data age is too far in the future")
        elif snapshot.data_age_seconds > max_data_age_seconds:
            errors.append(
                f"data is stale ({snapshot.data_age_seconds:.1f}s old; "
                f"limit {max_data_age_seconds:.1f}s)"
            )
    return tuple(dict.fromkeys(errors))


def _trade_fields(trades: TradeWindow) -> dict[str, Any]:
    if not trades.available or trades.buy_volume is None or trades.sell_volume is None:
        return {
            "recent_buy_volume": None,
            "recent_sell_volume": None,
            "order_flow_imbalance": None,
            "trade_data_available": False,
        }
    return {
        "recent_buy_volume": float(trades.buy_volume),
        "recent_sell_volume": float(trades.sell_volume),
        "order_flow_imbalance": _float_or_none(
            calculate_order_flow_imbalance(trades.buy_volume, trades.sell_volume)
        ),
        "trade_data_available": True,
    }


def _snapshot_from_inputs(
    config: Config,
    *,
    now: float,
    features: BookFeatures | None,
    errors: Iterable[str] = (),
    trades: TradeWindow | None = None,
    account: AccountData | None = None,
    options: OptionsVolatilitySnapshot | None = None,
) -> MarketSnapshot:
    trade_values = _trade_fields(
        trades or TradeWindow(buy_volume=None, sell_volume=None, available=False)
    )
    account = account or AccountData()
    option_values = options or unavailable_options_snapshot(
        now=now,
        reference_price=_float_or_none(features.mid_price) if features else None,
        errors=("options data not collected",),
        environment=config.options_environment,
    )
    snapshot = MarketSnapshot(
        timestamp=_iso_utc(now),
        connector=config.connector_name,
        trading_pair=config.trading_pair,
        book_depth_levels=config.book_depth_levels,
        order_book_timestamp=features.order_book_timestamp if features else None,
        best_bid=_float_or_none(features.best_bid) if features else None,
        best_ask=_float_or_none(features.best_ask) if features else None,
        mid_price=_float_or_none(features.mid_price) if features else None,
        spread_abs=_float_or_none(features.spread_abs) if features else None,
        spread_bps=_float_or_none(features.spread_bps) if features else None,
        best_bid_size=_float_or_none(features.best_bid_size) if features else None,
        best_ask_size=_float_or_none(features.best_ask_size) if features else None,
        bid_depth=_float_or_none(features.bid_depth) if features else None,
        ask_depth=_float_or_none(features.ask_depth) if features else None,
        top_level_imbalance=(
            _float_or_none(features.top_level_imbalance) if features else None
        ),
        depth_imbalance=_float_or_none(features.depth_imbalance) if features else None,
        data_age_seconds=features.data_age_seconds if features else None,
        **trade_values,
        atm_iv=option_values.atm_iv,
        atm_call_iv=option_values.atm_call_iv,
        atm_put_iv=option_values.atm_put_iv,
        atm_strike=option_values.atm_strike,
        atm_distance_pct=option_values.atm_distance_pct,
        option_instrument=option_values.call_instrument or option_values.put_instrument,
        option_call_instrument=option_values.call_instrument,
        option_put_instrument=option_values.put_instrument,
        option_expiry=option_values.expiry,
        option_expiry_dte=option_values.days_to_expiry,
        option_data_timestamp=option_values.option_data_timestamp,
        option_data_age_seconds=option_values.option_data_age_seconds,
        option_data_source=option_values.source,
        option_environment=option_values.environment,
        iv_confidence=option_values.confidence,
        option_data_errors=list(option_values.errors),
        iv_data_available=option_values.data_available,
        current_position=account.current_position,
        position_notional=account.position_notional,
        available_balance=account.available_balance,
        account_data_available=account.account_data_available,
        account_data_errors=list(account.errors),
    )
    validation_errors = list(errors)
    validation_errors.extend(validate_snapshot(snapshot, config.max_data_age_seconds))
    validation_errors = list(dict.fromkeys(validation_errors))
    return snapshot.model_copy(
        update={
            "data_valid": not validation_errors,
            "validation_errors": validation_errors,
        }
    )


def _json_line(snapshot: MarketSnapshot) -> str:
    """Serialize a snapshot with finite JSON values and one trailing newline."""

    return json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, allow_nan=False) + "\n"


def append_snapshot(
    snapshot: MarketSnapshot,
    output_path: str | Path,
    *,
    max_file_bytes: int = 50_000_000,
    max_rotated_files: int = 3,
) -> Path:
    """Append one JSONL record and rotate bounded backups when necessary."""

    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= max_file_bytes:
        for index in range(max_rotated_files - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}")
            newer = path.with_name(f"{path.name}.{index + 1}")
            if older.exists():
                older.replace(newer)
        if max_rotated_files > 0:
            path.replace(path.with_name(f"{path.name}.1"))
        else:
            path.unlink()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_json_line(snapshot))
        handle.flush()
    return path


def format_console_summary(snapshot: MarketSnapshot) -> str:
    """Format the concise per-snapshot console/log summary."""

    def value(item: float | None, fmt: str = ".8g") -> str:
        return "unavailable" if item is None else format(item, fmt)

    ofi = value(snapshot.order_flow_imbalance)
    iv = "unavailable" if snapshot.atm_iv is None else f"{snapshot.atm_iv:.2%}"
    call_iv = "unavailable" if snapshot.atm_call_iv is None else f"{snapshot.atm_call_iv:.2%}"
    put_iv = "unavailable" if snapshot.atm_put_iv is None else f"{snapshot.atm_put_iv:.2%}"
    position = value(snapshot.current_position)
    return "\n".join(
        [
            "[DERIVE DATA]",
            f"Pair: {snapshot.trading_pair}",
            f"Bid: {value(snapshot.best_bid)} / Ask: {value(snapshot.best_ask)}",
            f"Mid: {value(snapshot.mid_price)}",
            f"Spread: {value(snapshot.spread_bps)} bps",
            f"Bid depth({snapshot.book_depth_levels}): {value(snapshot.bid_depth)}",
            f"Ask depth({snapshot.book_depth_levels}): {value(snapshot.ask_depth)}",
            f"Book imbalance: {value(snapshot.depth_imbalance)}",
            f"OFI: {ofi} / {'available' if snapshot.trade_data_available else 'unavailable'}",
            f"ATM IV: {iv} / {'available' if snapshot.iv_data_available else 'unavailable'}",
            f"ATM IV sides: call {call_iv} / put {put_iv}",
            f"IV expiry: {snapshot.option_expiry or 'unavailable'} / "
            f"strike {value(snapshot.atm_strike)}",
            f"IV age: {value(snapshot.option_data_age_seconds)}s / "
            f"confidence {snapshot.iv_confidence:.2f}",
            f"Position: {position} / "
            f"{'available' if snapshot.account_data_available else 'unavailable'}",
            f"Data valid: {str(snapshot.data_valid).lower()}",
        ]
    )


def _extract_list(result: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        for key in keys:
            value = result.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _signed_position_amount(position: dict[str, Any]) -> Decimal | None:
    amount = _as_optional_decimal(
        position.get("amount", position.get("position_amt", position.get("size")))
    )
    if amount is None:
        return None
    side = str(position.get("side", position.get("position_side", ""))).upper()
    signed = abs(amount)
    return -signed if "SHORT" in side else signed


def _available_collateral(
    portfolio: Any, account_name: str, trading_pair: str, connector_name: str
) -> Decimal | None:
    if not isinstance(portfolio, dict):
        return None
    account = portfolio.get(account_name)
    if not isinstance(account, dict):
        return None
    connector = account.get(connector_name)
    if connector is None:
        connector = next(iter(account.values()), None)
    if not isinstance(connector, list):
        return None
    quote = trading_pair.split("-", 1)[1] if "-" in trading_pair else "USDC"
    rows = [row for row in connector if isinstance(row, dict) and row.get("token") == quote]
    if not rows:
        return None
    value = rows[0].get("available_units", rows[0].get("units"))
    return _as_optional_decimal(value)


async def _collect_account_data(
    client: Any, config: Config, mid_price: Decimal | None
) -> AccountData:
    if not config.account_name:
        return AccountData(errors=("account reads disabled by configuration",))

    account_name = config.account_name
    errors: list[str] = []

    async def read_positions() -> Any:
        return await asyncio.wait_for(
            client.trading.get_open_positions(account_name, config.connector_name),
            timeout=config.request_timeout_seconds,
        )

    async def read_portfolio() -> Any:
        return await asyncio.wait_for(
            client.portfolio.get_state(
                account_names=[account_name],
                connector_names=[config.connector_name],
                skip_gateway=True,
                refresh=True,
            ),
            timeout=config.request_timeout_seconds,
        )

    position_result, portfolio_result = await asyncio.gather(
        read_positions(), read_portfolio(), return_exceptions=True
    )
    if isinstance(position_result, BaseException):
        if isinstance(position_result, asyncio.CancelledError):
            raise position_result
        errors.append(f"position read failed: {type(position_result).__name__}")
        position_result = None
    if isinstance(portfolio_result, BaseException):
        if isinstance(portfolio_result, asyncio.CancelledError):
            raise portfolio_result
        errors.append(f"balance read failed: {type(portfolio_result).__name__}")
        portfolio_result = None

    position_amounts: list[Decimal] = []
    if position_result is not None:
        for position in _extract_list(position_result, "data", "positions"):
            if position.get("trading_pair") != config.trading_pair:
                continue
            amount = _signed_position_amount(position)
            if amount is not None:
                position_amounts.append(amount)

    current_position = (
        sum(position_amounts, start=Decimal(0)) if position_result is not None else None
    )
    position_notional = (
        sum((abs(amount) for amount in position_amounts), start=Decimal(0)) * mid_price
        if position_amounts and mid_price is not None
        else (Decimal(0) if position_result is not None and mid_price is not None else None)
    )
    available_balance = (
        _available_collateral(
            portfolio_result, account_name, config.trading_pair, config.connector_name
        )
        if portfolio_result is not None
        else None
    )
    if portfolio_result is not None and available_balance is None:
        errors.append("configured collateral token was not present in portfolio state")

    return AccountData(
        current_position=_float_or_none(current_position),
        position_notional=_float_or_none(position_notional),
        available_balance=_float_or_none(available_balance),
        account_data_available=position_result is not None or portfolio_result is not None,
        errors=tuple(errors),
    )


class RecentTradeCollector:
    """Read recent trades through Hummingbot API v1.5.7's official WebSocket."""

    def __init__(self, config: Config):
        self._config = config
        self._context: Any = None
        self._websocket: Any = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()
        self._buy_volume = Decimal(0)
        self._sell_volume = Decimal(0)
        self.available = False
        self.error: str | None = None
        self.started = False

    async def start(self, client: Any) -> None:
        if self.started:
            return
        self.started = True
        if not self._config.enable_recent_trades:
            self.error = "recent trade collection disabled"
            return
        try:
            websocket_router = getattr(client, "ws", None)
            if websocket_router is None or not hasattr(websocket_router, "market_data"):
                raise RuntimeError("Hummingbot API client has no market-data WebSocket")
            self._context = websocket_router.market_data()
            self._websocket = await self._context.__aenter__()
            await self._websocket.subscribe_trades(
                self._config.connector_name,
                self._config.trading_pair,
                update_interval=self._config.trade_update_interval_seconds,
            )
            self.available = True
            self._reader_task = asyncio.create_task(self._reader())
            logger.info(
                "Recent trades enabled through the official Hummingbot API WebSocket for %s/%s",
                self._config.connector_name,
                self._config.trading_pair,
            )
        except Exception as exc:
            self.error = f"recent trades unavailable: {type(exc).__name__}"
            self.available = False
            logger.warning("%s", self.error)
            await self.stop()

    async def _reader(self) -> None:
        try:
            while True:
                message = await self._websocket.receive()
                if not isinstance(message, dict) or message.get("type") != "trades":
                    continue
                rows = message.get("data")
                if not isinstance(rows, list):
                    continue
                async with self._lock:
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        amount = _as_optional_decimal(row.get("amount"))
                        side = str(row.get("side", "")).lower()
                        if amount is None or amount < 0:
                            continue
                        if side == "buy":
                            self._buy_volume += amount
                        elif side == "sell":
                            self._sell_volume += amount
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.available = False
            self.error = f"recent trade stream stopped: {type(exc).__name__}"
            logger.warning("%s", self.error)

    async def drain(self) -> TradeWindow:
        if not self.available:
            return TradeWindow(None, None, False, self.error)
        async with self._lock:
            buy_volume = self._buy_volume
            sell_volume = self._sell_volume
            self._buy_volume = Decimal(0)
            self._sell_volume = Decimal(0)
        return TradeWindow(buy_volume, sell_volume, True, self.error)

    async def stop(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._reader_task = None
        if self._context is not None:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception:
                logger.debug("Error closing recent-trade WebSocket", exc_info=True)
        self._websocket = None
        self._context = None


async def collect_snapshot_once(
    config: Config,
    client: Any,
    *,
    trade_collector: RecentTradeCollector | None = None,
    options_provider: DeriveOptionsProvider | None = None,
    now: float | None = None,
) -> MarketSnapshot:
    """Read, normalize, and validate one snapshot without any trading route."""

    snapshot_now = time.time() if now is None else now
    errors: list[str] = []
    diagnostics: Any = None
    order_book: Any = None
    tracker_age: float | None = None

    try:
        diagnostics = await asyncio.wait_for(
            client.market_data.get_order_book_diagnostics(config.connector_name),
            timeout=config.request_timeout_seconds,
        )
        tracker_age, diagnostic_errors = _diagnostic_errors(diagnostics, config)
        errors.extend(diagnostic_errors)
    except Exception as exc:
        errors.append(f"order-book diagnostics failed: {type(exc).__name__}")

    try:
        order_book = await asyncio.wait_for(
            client.market_data.get_order_book(
                connector_name=config.connector_name,
                trading_pair=config.trading_pair,
                depth=config.book_depth_levels,
            ),
            timeout=config.request_timeout_seconds,
        )
    except Exception as exc:
        errors.append(f"order-book read failed: {type(exc).__name__}")

    features: BookFeatures | None = None
    if order_book is not None:
        try:
            features = calculate_book_features(
                order_book,
                depth=config.book_depth_levels,
                data_age_seconds=tracker_age,
                now=snapshot_now,
            )
        except MarketDataError as exc:
            errors.append(str(exc))

    trade_window = (
        await trade_collector.drain()
        if trade_collector is not None
        else TradeWindow(None, None, False, "recent trade collector not started")
    )
    mid_price = features.mid_price if features else None
    options = (
        await options_provider.snapshot(
            _float_or_none(mid_price),
            now=snapshot_now,
        )
        if options_provider is not None
        else unavailable_options_snapshot(
            now=snapshot_now,
            reference_price=_float_or_none(mid_price),
            errors=("options disabled by configuration",),
            environment=config.options_environment,
        )
    )
    account = await _collect_account_data(client, config, mid_price)
    if account.errors:
        logger.warning("Read-only account data limitations: %s", "; ".join(account.errors))

    snapshot = _snapshot_from_inputs(
        config,
        now=snapshot_now,
        features=features,
        errors=errors,
        trades=trade_window,
        account=account,
        options=options,
    )
    if options.errors and not options.data_available:
        logger.warning("Read-only Derive options data unavailable: %s", "; ".join(options.errors))
    return snapshot


async def run(config: Config, context: Any) -> str:
    """Poll and persist read-only normalized snapshots until Condor stops the routine."""

    from config_manager import get_client

    chat_id = getattr(context, "_chat_id", None)
    trade_collector = RecentTradeCollector(config)
    options_provider = (
        DeriveOptionsProvider(
            base_url=config.options_api_base_url,
            currency=config.options_currency,
            environment=config.options_environment,
            min_days_to_expiry=config.options_min_days_to_expiry,
            target_days_to_expiry=config.options_target_days_to_expiry,
            max_days_to_expiry=config.options_max_days_to_expiry,
            max_atm_distance_pct=config.options_max_atm_distance_pct,
            max_option_data_age_seconds=config.max_option_data_age_seconds,
            metadata_refresh_interval_seconds=config.options_metadata_refresh_interval_seconds,
            request_timeout_seconds=config.options_request_timeout_seconds,
            max_iv=config.options_max_iv,
        )
        if config.options_enabled
        else None
    )
    snapshot_count = 0
    last_path: Path | None = None

    try:
        while True:
            tick_started = time.monotonic()
            try:
                client = await asyncio.wait_for(
                    get_client(chat_id, context=context), timeout=config.request_timeout_seconds
                )
                if client is None:
                    raise MarketDataError("no Hummingbot server is configured")
                if not trade_collector.started:
                    await trade_collector.start(client)
                snapshot = await collect_snapshot_once(
                    config,
                    client,
                    trade_collector=trade_collector,
                    options_provider=options_provider,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Snapshot tick failed closed: %s", exc)
                snapshot = _snapshot_from_inputs(
                    config,
                    now=time.time(),
                    features=None,
                    errors=(f"snapshot tick failed: {type(exc).__name__}",),
                    trades=await trade_collector.drain(),
                    options=unavailable_options_snapshot(
                        now=time.time(),
                        reference_price=None,
                        errors=(f"options tick failed: {type(exc).__name__}",),
                        environment=config.options_environment,
                    ),
                )

            try:
                last_path = append_snapshot(
                    snapshot,
                    config.output_path,
                    max_file_bytes=config.max_output_file_bytes,
                    max_rotated_files=config.max_rotated_files,
                )
            except Exception as exc:
                logger.warning("Could not persist snapshot: %s", type(exc).__name__)

            snapshot_count += 1
            logger.info("%s", format_console_summary(snapshot))
            elapsed = time.monotonic() - tick_started
            await asyncio.sleep(max(0.0, config.snapshot_interval_seconds - elapsed))
    except asyncio.CancelledError:
        return (
            f"Stopped after {snapshot_count} snapshots"
            + (f"; JSONL: {last_path}" if last_path else "; no snapshot was persisted")
        )
    finally:
        await trade_collector.stop()
