"""Small, read-only Derive BTC ATM implied-volatility adapter.

The installed Hummingbot Derive connector exposes perpetual market data but no
options ticker surface.  This module uses only Derive's public REST methods:

* ``public/get_instruments`` for active option metadata and expiry/strike
  selection;
* ``public/get_tickers`` for the selected expiry's option pricing fields.

It deliberately does not contain a private client, WebSocket session, order
method, account method, or option-chain persistence.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

DAY_SECONDS = 86_400.0
DEFAULT_SOURCE = "derive_public_get_tickers"
DEFAULT_BASE_URL = "https://api.lyra.finance"
_ALLOWED_METHODS = frozenset(
    {
        "public/get_instruments",
        "public/get_tickers",
    }
)


class OptionsDataError(RuntimeError):
    """Raised when Derive cannot provide a usable public options observation."""


def _finite_float(value: Any) -> float | None:
    if value in (None, "", "null", "NaN", "nan"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _timestamp_seconds(value: Any) -> float | None:
    parsed = _finite_float(value)
    if parsed is None:
        return None
    return parsed / 1_000.0 if parsed > 10_000_000_000 else parsed


def _iso_utc(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _option_type(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"CALL", "C"}:
        return "C"
    if normalized in {"PUT", "P"}:
        return "P"
    return normalized


@dataclass(frozen=True)
class OptionContract:
    """Minimal active contract metadata needed for ATM selection."""

    instrument_name: str
    underlying: str
    expiry_ts: float
    strike: float
    option_type: str

    @property
    def expiry_date(self) -> str:
        return datetime.fromtimestamp(self.expiry_ts, UTC).strftime("%Y%m%d")

    @property
    def expiry_label(self) -> str:
        return datetime.fromtimestamp(self.expiry_ts, UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class ATMSelection:
    """Deterministic selected expiry, strike, and same-strike call/put pair."""

    expiry_ts: float
    atm_strike: float
    atm_distance_pct: float
    call: OptionContract | None
    put: OptionContract | None

    @property
    def days_to_expiry(self) -> float:
        return max(0.0, (self.expiry_ts - time.time()) / DAY_SECONDS)

    @property
    def expiry_label(self) -> str:
        return datetime.fromtimestamp(self.expiry_ts, UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class OptionTicker:
    """Selected ticker fields and the normalized IV value used by Stage 2.5."""

    instrument_name: str
    option_type: str
    expiry_ts: float
    strike: float
    iv: float | None
    bid_iv: float | None
    ask_iv: float | None
    iv_source: str | None
    option_data_timestamp: float | None
    index_price: float | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionsVolatilitySnapshot:
    """Small normalized ATM-IV observation joined into MarketSnapshot."""

    timestamp: str
    underlying: str
    reference_price: float | None
    expiry: str | None
    days_to_expiry: float | None
    atm_strike: float | None
    atm_distance_pct: float | None
    call_instrument: str | None
    put_instrument: str | None
    atm_call_iv: float | None
    atm_put_iv: float | None
    atm_iv: float | None
    option_data_timestamp: str | None
    option_data_age_seconds: float | None
    source: str
    environment: str
    data_available: bool
    confidence: float
    errors: tuple[str, ...] = ()


def parse_option_contract(row: dict[str, Any], underlying: str = "BTC") -> OptionContract | None:
    """Parse one official ``public/get_instruments`` row defensively."""

    if not isinstance(row, dict):
        return None
    if str(row.get("instrument_type", "option")).lower() != "option":
        return None
    details = row.get("option_details") or row.get("optionDetails") or {}
    instrument_name = row.get("instrument_name") or row.get("instrumentName")
    expiry_ts = _timestamp_seconds(details.get("expiry"))
    strike = _finite_float(details.get("strike"))
    option_type = _option_type(details.get("option_type", details.get("optionType")))
    if (
        not instrument_name
        or expiry_ts is None
        or strike is None
        or strike <= 0
        or option_type not in {"C", "P"}
    ):
        return None
    return OptionContract(
        instrument_name=str(instrument_name),
        underlying=underlying,
        expiry_ts=expiry_ts,
        strike=strike,
        option_type=option_type,
    )


def parse_active_option_contracts(
    rows: Any,
    *,
    underlying: str = "BTC",
    now: float,
) -> list[OptionContract]:
    """Keep only active, future BTC option contracts from metadata."""

    if not isinstance(rows, list):
        return []
    contracts: list[OptionContract] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("is_active") is not True:
            continue
        contract = parse_option_contract(row, underlying=underlying)
        if contract is not None and contract.expiry_ts > now:
            contracts.append(contract)
    return sorted(
        contracts,
        key=lambda item: (
            item.expiry_ts,
            item.strike,
            item.option_type,
            item.instrument_name,
        ),
    )


def select_expiry(
    contracts: list[OptionContract],
    *,
    now: float,
    min_days_to_expiry: float,
    target_days_to_expiry: float,
    max_days_to_expiry: float,
) -> tuple[float, list[OptionContract]]:
    """Select the eligible expiry nearest the configured target DTE."""

    if min_days_to_expiry < 0 or target_days_to_expiry < min_days_to_expiry:
        raise ValueError("target_days_to_expiry must be at least min_days_to_expiry")
    if max_days_to_expiry < target_days_to_expiry:
        raise ValueError("max_days_to_expiry must be at least target_days_to_expiry")

    grouped: dict[float, list[OptionContract]] = {}
    for contract in contracts:
        dte = (contract.expiry_ts - now) / DAY_SECONDS
        if min_days_to_expiry <= dte <= max_days_to_expiry:
            grouped.setdefault(contract.expiry_ts, []).append(contract)

    eligible = [
        (expiry, rows)
        for expiry, rows in grouped.items()
        if any(row.option_type == "C" for row in rows)
        and any(row.option_type == "P" for row in rows)
    ]
    if not eligible:
        raise OptionsDataError(
            "no active BTC option expiry with both calls and puts in the configured DTE range"
        )
    return min(
        eligible,
        key=lambda item: (
            abs((item[0] - now) / DAY_SECONDS - target_days_to_expiry),
            item[0],
        ),
    )


def select_atm_strike(
    contracts: list[OptionContract],
    *,
    expiry_ts: float,
    reference_price: float,
    max_atm_distance_pct: float,
) -> ATMSelection:
    """Select the same-strike call/put nearest the current reference price."""

    reference = _finite_float(reference_price)
    if reference is None or reference <= 0:
        raise OptionsDataError("reference price is unavailable or invalid")
    if max_atm_distance_pct <= 0:
        raise ValueError("max_atm_distance_pct must be positive")

    rows = [row for row in contracts if row.expiry_ts == expiry_ts]
    if not rows:
        raise OptionsDataError("selected expiry has no option contracts")
    strikes = sorted({row.strike for row in rows})
    atm_strike = min(strikes, key=lambda strike: (abs(strike - reference), strike))
    distance_pct = abs(atm_strike - reference) / reference
    if distance_pct > max_atm_distance_pct:
        raise OptionsDataError(
            f"nearest ATM strike is {distance_pct:.2%} from reference; "
            f"limit is {max_atm_distance_pct:.2%}"
        )
    same_strike = [row for row in rows if row.strike == atm_strike]
    call = next((row for row in same_strike if row.option_type == "C"), None)
    put = next((row for row in same_strike if row.option_type == "P"), None)
    if call is None and put is None:
        raise OptionsDataError("nearest ATM strike has neither a call nor a put")
    return ATMSelection(
        expiry_ts=expiry_ts,
        atm_strike=atm_strike,
        atm_distance_pct=distance_pct,
        call=call,
        put=put,
    )


def _read_iv(pricing: dict[str, Any], *keys: str, max_iv: float) -> float | None:
    for key in keys:
        value = _finite_float(pricing.get(key))
        if value is not None and 0 < value <= max_iv:
            return value
    return None


def parse_option_ticker(
    payload: Any,
    contract: OptionContract,
    *,
    max_iv: float,
) -> OptionTicker:
    """Parse one entry from the official ``result.tickers`` map."""

    row = payload if isinstance(payload, dict) else {}
    pricing = row.get("option_pricing") or row.get("optionPricing") or {}
    if not isinstance(pricing, dict):
        pricing = {}
    mark_iv = _read_iv(pricing, "i", "iv", max_iv=max_iv)
    bid_iv = _read_iv(pricing, "bi", "bid_iv", max_iv=max_iv)
    ask_iv = _read_iv(pricing, "ai", "ask_iv", max_iv=max_iv)
    errors: list[str] = []

    if mark_iv is not None:
        iv = mark_iv
        iv_source = "mark_iv"
    elif bid_iv is not None and ask_iv is not None:
        iv = (bid_iv + ask_iv) / 2.0
        iv_source = "bid_ask_iv_midpoint"
    elif bid_iv is not None:
        iv = bid_iv
        iv_source = "bid_iv_only"
    elif ask_iv is not None:
        iv = ask_iv
        iv_source = "ask_iv_only"
    else:
        iv = None
        iv_source = None
        errors.append("no valid mark, bid, or ask IV")

    raw_timestamp = row.get("t", row.get("timestamp"))
    option_data_timestamp = _timestamp_seconds(raw_timestamp)
    if option_data_timestamp is None:
        errors.append("option ticker timestamp is unavailable")
    index_price = _finite_float(row.get("I", row.get("index_price")))
    return OptionTicker(
        instrument_name=contract.instrument_name,
        option_type=contract.option_type,
        expiry_ts=contract.expiry_ts,
        strike=contract.strike,
        iv=iv,
        bid_iv=bid_iv,
        ask_iv=ask_iv,
        iv_source=iv_source,
        option_data_timestamp=option_data_timestamp,
        index_price=index_price,
        errors=tuple(errors),
    )


def build_options_snapshot(
    selection: ATMSelection,
    ticker_map: dict[str, Any],
    *,
    reference_price: float,
    now: float,
    max_option_data_age_seconds: float,
    max_iv: float,
    max_atm_distance_pct: float = 0.05,
    source: str = DEFAULT_SOURCE,
    environment: str = "production",
) -> OptionsVolatilitySnapshot:
    """Combine current ATM call/put ticker values with freshness validation."""

    reference = _finite_float(reference_price)
    errors: list[str] = []
    tickers: list[OptionTicker] = []
    for contract in (selection.call, selection.put):
        if contract is None:
            continue
        payload = ticker_map.get(contract.instrument_name)
        if payload is None:
            errors.append(f"ticker missing for {contract.instrument_name}")
            continue
        ticker = parse_option_ticker(payload, contract, max_iv=max_iv)
        ticker_age = (
            None
            if ticker.option_data_timestamp is None
            else max(0.0, now - ticker.option_data_timestamp)
        )
        if ticker_age is None:
            errors.append(f"ticker timestamp missing for {contract.instrument_name}")
            continue
        if now - ticker.option_data_timestamp < -60.0:
            errors.append(
                "ticker timestamp is too far in the future for "
                f"{contract.instrument_name}"
            )
            continue
        if ticker_age > max_option_data_age_seconds:
            errors.append(
                f"ticker stale for {contract.instrument_name} ({ticker_age:.1f}s; "
                f"limit {max_option_data_age_seconds:.1f}s)"
            )
            continue
        errors.extend(ticker.errors)
        if ticker.iv is not None:
            tickers.append(ticker)

    call_iv = next((ticker.iv for ticker in tickers if ticker.option_type == "C"), None)
    put_iv = next((ticker.iv for ticker in tickers if ticker.option_type == "P"), None)
    iv_values = [value for value in (call_iv, put_iv) if value is not None]
    atm_iv = sum(iv_values) / len(iv_values) if iv_values else None
    timestamp_values = [ticker.option_data_timestamp for ticker in tickers]
    latest_timestamp = max(timestamp_values) if timestamp_values else None
    age = None if latest_timestamp is None else max(0.0, now - latest_timestamp)

    if atm_iv is None:
        errors.append("ATM IV unavailable after ticker validation")
        confidence = 0.0
        data_available = False
    else:
        side_factor = 1.0 if call_iv is not None and put_iv is not None else 0.75
        distance_fraction = min(
            1.0,
            selection.atm_distance_pct / max(1e-12, max_atm_distance_pct),
        )
        distance_factor = max(
            0.75,
            1.0 - 0.25 * distance_fraction,
        )
        confidence = round(side_factor * distance_factor, 3)
        data_available = True

    return OptionsVolatilitySnapshot(
        timestamp=_iso_utc(now) or "",
        underlying="BTC",
        reference_price=reference,
        expiry=selection.expiry_label,
        days_to_expiry=max(0.0, (selection.expiry_ts - now) / DAY_SECONDS),
        atm_strike=selection.atm_strike,
        atm_distance_pct=selection.atm_distance_pct,
        call_instrument=selection.call.instrument_name if selection.call else None,
        put_instrument=selection.put.instrument_name if selection.put else None,
        atm_call_iv=call_iv,
        atm_put_iv=put_iv,
        atm_iv=atm_iv,
        option_data_timestamp=_iso_utc(latest_timestamp),
        option_data_age_seconds=age,
        source=source,
        environment=environment,
        data_available=data_available,
        confidence=confidence,
        errors=tuple(dict.fromkeys(errors)),
    )


def unavailable_options_snapshot(
    *,
    now: float,
    reference_price: float | None,
    errors: tuple[str, ...] | list[str],
    source: str = DEFAULT_SOURCE,
    environment: str = "production",
) -> OptionsVolatilitySnapshot:
    """Create an explicit unavailable observation without fabricating IV."""

    return OptionsVolatilitySnapshot(
        timestamp=_iso_utc(now) or "",
        underlying="BTC",
        reference_price=_finite_float(reference_price),
        expiry=None,
        days_to_expiry=None,
        atm_strike=None,
        atm_distance_pct=None,
        call_instrument=None,
        put_instrument=None,
        atm_call_iv=None,
        atm_put_iv=None,
        atm_iv=None,
        option_data_timestamp=None,
        option_data_age_seconds=None,
        source=source,
        environment=environment,
        data_available=False,
        confidence=0.0,
        errors=tuple(dict.fromkeys(str(error) for error in errors)),
    )


@dataclass
class DeriveOptionsProvider:
    """Cached metadata plus one bounded public ticker request per snapshot."""

    base_url: str = DEFAULT_BASE_URL
    currency: str = "BTC"
    environment: str = "production"
    min_days_to_expiry: float = 2.0
    target_days_to_expiry: float = 7.0
    max_days_to_expiry: float = 14.0
    max_atm_distance_pct: float = 0.05
    max_option_data_age_seconds: float = 15.0
    metadata_refresh_interval_seconds: float = 900.0
    request_timeout_seconds: float = 10.0
    max_iv: float = 10.0
    user_agent: str = "derive-options-mm-stage2-5/0.1"
    _contracts: list[OptionContract] = field(default_factory=list, init=False, repr=False)
    _metadata_fetched_at: float | None = field(default=None, init=False, repr=False)

    def _post(self, method: str, params: dict[str, Any]) -> Any:
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"method is not allowed by the read-only options adapter: {method}")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                payload = json.load(response)
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise OptionsDataError(f"{method} request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise OptionsDataError(f"{method} response is not an object")
        if payload.get("error"):
            raise OptionsDataError(f"{method} returned an API error")
        if "result" not in payload:
            raise OptionsDataError(f"{method} response has no result")
        return payload["result"]

    def _get_contracts(self, now: float) -> list[OptionContract]:
        if (
            self._metadata_fetched_at is not None
            and now - self._metadata_fetched_at < self.metadata_refresh_interval_seconds
        ):
            return [contract for contract in self._contracts if contract.expiry_ts > now]

        result = self._post(
            "public/get_instruments",
            {
                "currency": self.currency,
                "instrument_type": "option",
                "expired": False,
            },
        )
        contracts = parse_active_option_contracts(result, underlying=self.currency, now=now)
        if not contracts:
            raise OptionsDataError("Derive returned no active future BTC option contracts")
        self._contracts = contracts
        self._metadata_fetched_at = now
        return contracts

    def _snapshot_sync(self, reference_price: float, now: float) -> OptionsVolatilitySnapshot:
        contracts = self._get_contracts(now)
        expiry_ts, expiry_contracts = select_expiry(
            contracts,
            now=now,
            min_days_to_expiry=self.min_days_to_expiry,
            target_days_to_expiry=self.target_days_to_expiry,
            max_days_to_expiry=self.max_days_to_expiry,
        )
        selection = select_atm_strike(
            expiry_contracts,
            expiry_ts=expiry_ts,
            reference_price=reference_price,
            max_atm_distance_pct=self.max_atm_distance_pct,
        )
        result = self._post(
            "public/get_tickers",
            {
                "currency": self.currency,
                "expiry_date": datetime.fromtimestamp(expiry_ts, UTC).strftime("%Y%m%d"),
                "instrument_type": "option",
            },
        )
        ticker_map = result.get("tickers") if isinstance(result, dict) else None
        if not isinstance(ticker_map, dict):
            raise OptionsDataError("Derive ticker response has no tickers map")
        snapshot = build_options_snapshot(
            selection,
            ticker_map,
            reference_price=reference_price,
            now=now,
            max_option_data_age_seconds=self.max_option_data_age_seconds,
            max_iv=self.max_iv,
            max_atm_distance_pct=self.max_atm_distance_pct,
            environment=self.environment,
        )
        if not snapshot.data_available:
            raise OptionsDataError("; ".join(snapshot.errors) or "ATM IV unavailable")
        return snapshot

    async def snapshot(
        self, reference_price: float | None, *, now: float | None = None
    ) -> OptionsVolatilitySnapshot:
        """Fetch one current ATM-IV observation without blocking the event loop."""

        current_time = time.time() if now is None else now
        reference = _finite_float(reference_price)
        if reference is None or reference <= 0:
            return unavailable_options_snapshot(
                now=current_time,
                reference_price=reference,
                errors=("perpetual reference price unavailable",),
                source=DEFAULT_SOURCE,
                environment=self.environment,
            )
        try:
            return await asyncio.to_thread(self._snapshot_sync, reference, current_time)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return unavailable_options_snapshot(
                now=current_time,
                reference_price=reference,
                errors=(str(exc) or type(exc).__name__,),
                source=DEFAULT_SOURCE,
                environment=self.environment,
            )


__all__ = [
    "ATMSelection",
    "DAY_SECONDS",
    "DEFAULT_BASE_URL",
    "DEFAULT_SOURCE",
    "DeriveOptionsProvider",
    "OptionContract",
    "OptionTicker",
    "OptionsDataError",
    "OptionsVolatilitySnapshot",
    "build_options_snapshot",
    "parse_active_option_contracts",
    "parse_option_contract",
    "parse_option_ticker",
    "select_atm_strike",
    "select_expiry",
    "unavailable_options_snapshot",
]
