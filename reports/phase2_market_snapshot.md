# Stage 1 — Read-only Derive data layer

Stage 2.5 extends this snapshot boundary with the selected ATM-IV fields; see
the [Stage 2.5 report](stage2_5_options_iv.md) for the options contract and
live evidence.

## Goal

Build only the data layer for one Derive perpetual pair:

```text
FETCH -> NORMALIZE -> LOG -> EXPOSE DATA
```

No state engine, volatility or direction classification, grid mode or
parameter generation, executor, order placement/cancellation, leverage change,
position-mode change, or risk logic is included.

## Installed contract inspected before implementation

- Hummingbot source version observed in the local API container: `20260729`.
- The runtime Hummingbot source is loaded from
  `/home/hummingbot/hummingbot` in the local `hummingbot/hummingbot:latest`
  container; the API container installs the same Hummingbot build as package
  `20260729`.
- Condor discovers files in its routine directories and requires a Pydantic
  `Config` plus `async def run(config, context)`. `CONTINUOUS = True` marks a
  routine with an internal polling loop.
- Existing local examples inspected before editing included
  `routines/arb_check.py` for order-book reads,
  `routines/derive_btc_perpetual_market_data.py` for Derive diagnostics, and
  `routines/price_monitor.py` for continuous polling.
- A routine receives the authenticated Hummingbot API client. It cannot access
  the in-process `MarketDataProvider` directly.
- Verified read-only client methods are:
  `market_data.get_order_book_diagnostics`,
  `market_data.get_order_book`,
  `connectors.get_trading_rules`,
  `trading.get_open_positions`, and `portfolio.get_state`.
- The installed Derive perpetual connector supports
  `derive_perpetual_testnet`; its testnet constants include the Derive demo
  REST/WebSocket endpoints (`https://api-demo.lyra.finance` and
  `wss://api-demo.lyra.finance/ws`, chain ID `901`). The connector maps Derive
  `BTC-PERP` to Hummingbot `BTC-USDC`.
- The Hummingbot connector source has `get_mid_price`, but the API route used
  by Condor does not expose it. The routine therefore calculates mid from the
  two best book prices; `get_order_book` rows provide the first-level bid/ask
  sizes and all top-N depth amounts.
- The Hummingbot API client exposes an official market-data WebSocket with
  `subscribe_trades`. The routine uses it when available; the REST
  `trading.get_recent_trades` route is account execution history, not public
  market trades.
- No ATM-IV/options ticker route exists in the installed Hummingbot Derive
  connector/API. The routine does not create an external options client.

## Normalized snapshot

`MarketSnapshot` is a Pydantic model and each JSONL line contains one instance.
It includes timestamp/connector/pair provenance; best bid/ask and sizes; mid,
absolute spread, spread bps; top-N depths; top-level and depth imbalance;
tracker/order-book age; optional recent buy/sell volume and OFI; optional
account position/notional/available collateral; explicit IV-unavailable fields;
and `data_valid` plus `validation_errors`.

All arithmetic is performed in small pure Decimal functions. Zero denominators
return `None`. A crossed/locked book, missing or non-positive best quote,
invalid midpoint, or stale data marks the snapshot invalid. The poller logs and
persists the invalid record, then continues.

The `timestamp` field is processing/receipt time. `order_book_timestamp` retains
the Hummingbot API collection timestamp, while `data_age_seconds` uses the
order-book tracker's monotonic freshness metrics when present. The API
collection timestamp is not treated as exchange event time.

## Persistence and console output

The continuous routine writes append-only JSONL to:

```text
/Users/wilfred/Documents/Hummingbot/condor/data/derive_market_snapshots.jsonl
```

when run from the Condor checkout. The active file rotates after 50 MB and
retains three bounded `.1`, `.2`, and `.3` backups by default. The five-second
interval controls this routine only; it does not throttle Hummingbot's tracker.

Each tick emits a concise `[DERIVE DATA]` summary to the Condor log. When the
public options provider has a fresh valid observation, it reports ATM IV,
call/put sides, expiry, strike, age, and confidence; otherwise it reports the
explicit unavailable reason. OFI is reported unavailable until the official
trade stream provides a non-zero recent window.

## Verification

The project tests cover:

1. midpoint;
2. absolute spread;
3. spread bps;
4. top-level imbalance;
5. depth aggregation;
6. depth imbalance;
7. zero-denominator handling;
8. crossed-book rejection;
9. OFI;
10. snapshot validation;
11. diagnostics staleness;
12. normalized account reads;
13. official trade-WebSocket aggregation;
14. JSONL append/rotation;
15. console output;
16. absence of mutating trading API calls.

Results:

```text
pytest -q       16 passed
ruff check .    All checks passed!
```

A bounded live smoke run through the local authenticated Hummingbot API on
2026-08-23 persisted one valid record. It confirmed live market data, account
data, the official trade subscription, and `iv_data_available: false`; no
order, cancellation, leverage, position-mode, or executor route was called.

## Known limitations

- The routine is a data collector, not a trading system or alpha result.
- The current API's `available_balance` is the configured collateral token's
  `available_units`; `position_notional` is derived as absolute position size
  times the current mid price. Full Derive portfolio-margin telemetry is not
  exposed by the installed mapping.
- Recent trade events are event-driven. An interval with no events has zero
  volumes and no defined OFI; a failed subscription records trade data as
  unavailable. The live probe accepted the subscription but observed no trade
  event during its short bounded window.
- The installed Hummingbot Derive stack still has no options ticker route. The
  separate Stage 2.5 adapter therefore reads only Derive's official public
  production options endpoints and fails closed when the selected data is
  stale or unavailable.
- Testnet liquidity and account state are not representative of production.
- The data file is local append-only research output; it is not an immutable
  exchange event archive and does not provide exchange-side queue or fill data.

## Decision

Stage 1 is complete. Stop here until the next stage is explicitly approved.
