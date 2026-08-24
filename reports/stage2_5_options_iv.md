# Stage 2.5 — Read-only Derive ATM implied volatility

## Goal and decision

Stage 2.5 adds one optional, read-only Derive BTC ATM implied-volatility input
to the existing Stage 1 snapshot and Stage 2 state pipeline. It does not add
grid modes, order execution, position changes, hedging, or a volatility
surface. The implementation stops at `MarketSnapshot -> MarketState`.

## Why ATM IV was previously unavailable

The installed Hummingbot build observed locally (`20260729`) exposes the
Derive perpetual order book and account read routes used by Stage 1, but no
Derive options instrument or options ticker route. Stage 1 therefore had no
options client, hard-coded `atm_iv=None`, and emitted
`iv_data_available=false`. This was a missing data-source integration, not a
missing account credential or an IV-field calculation problem.

The direct options probe also showed that Derive can return future scheduled
option metadata with `is_active=false`; those rows are filtered before expiry
selection. Selecting one of those inactive expiries produces no usable ticker
map, so the adapter requires active contracts as well as a valid DTE range.

## Data source and exact fields

The perpetual leg remains the installed Hummingbot `derive_perpetual_testnet`
or `derive_perpetual` connector selected in the Stage 1 Condor configuration.
The options leg uses only Derive's official public production REST API, with no
authentication and no external exchange:

1. `POST https://api.lyra.finance/public/get_instruments` with
   `currency=BTC`, `instrument_type=option`, and `expired=false`.
2. `POST https://api.lyra.finance/public/get_tickers` with `currency=BTC`,
   `instrument_type=option`, and the selected expiry as `YYYYMMDD`.

The adapter uses actual response fields from the option metadata and ticker
payload:

- metadata: `is_active`, `option_details.expiry`,
  `option_details.strike`, and `option_details.option_type` (`C`/`P`);
- ticker timestamp/index: `t` and `I`;
- IV hierarchy: `option_pricing.i` (mark IV), then `bi`/`ai` midpoint, then
  one valid `bi` or `ai` side.

The official endpoint references are [Get Instruments](https://docs.derive.xyz/reference/public-get_instruments)
and [Get Tickers](https://docs.derive.xyz/reference/post_public-get-tickers).
The installed Hummingbot connector remains the perpetual data source; the
adapter does not create an execution client or a private API surface.

## Selection and normalization

Defaults are configurable in the Stage 1 routine:

```text
min_days_to_expiry = 2
target_days_to_expiry = 7
max_days_to_expiry = 14
max_atm_distance_pct = 0.05
max_option_data_age_seconds = 15
options_metadata_refresh_interval_seconds = 900
```

The adapter filters active future BTC calls and puts, chooses the expiry
nearest the target DTE inside the configured range, then chooses the strike
minimizing `abs(strike - perpetual_mid)`. It rejects the selection when the
relative distance exceeds the configured limit. At the selected strike it
averages valid call and put IVs. If only one side is valid, that value is
retained with deterministic lower confidence; if neither side is valid, IV is
unavailable and the perpetual snapshot continues.

Only the selected ATM fields cross the Stage 1 boundary: expiry, DTE, strike,
distance, call/put instrument names, call/put IV, ATM IV, timestamp, age,
source, environment, confidence, and diagnostic errors. The full option chain
is never persisted.

## State-engine behavior

The Stage 2 engine keeps a bounded in-memory ATM-IV history. `iv_ratio` is the
current ATM IV divided by the median of prior valid ATM IV observations inside
`iv_history_window_seconds`, and it remains `None`/initializing until
`iv_minimum_samples` is met. `iv_change` is the current decimal IV minus the
previous valid IV.

The existing realized-volatility behavior and direction path are preserved.
When IV is ready, the volatility score combines RV ratio and IV ratio using the
existing configurable RV/IV weights. Missing IV removes its weight and uses RV
alone. IV is never an input to bullish/bearish direction. Option errors are
retained in the snapshot and state reasons, and stale option data fails closed
to `iv_data_available=false`.

Stage 2 console output now exposes ATM IV as a percentage, IV ratio/warm-up
status, expiry/DTE, and ATM strike without printing the option chain.

## Files

Created or materially extended:

- `src/derive_options_mm/options_iv.py` — isolated public, read-only options
  adapter, deterministic selection, parsing, freshness, and caching;
- `tests/test_options_iv.py` — selection, IV hierarchy, fallback, stale data,
  provider cache/error behavior, and read-only surface tests.

Modified:

- `integrations/condor/derive_market_snapshot.py` — configurable options
  provider and selected fields in `MarketSnapshot`;
- `src/derive_options_mm/state_engine.py` — IV warm-up/history, ratio/change,
  confidence, reasons, and console fields;
- `tests/test_derive_market_snapshot.py` and `tests/test_state_engine.py` —
  pipeline and state integration coverage;
- `README.md`, `reports/phase2_market_snapshot.md`,
  `reports/phase2_market_state.md`, and `reports/research_log.md` — current
  Stage 2.5 contract and evidence.

## Verification

```text
pytest -q                         55 passed
uv run --with ruff ruff check .  All checks passed
```

A bounded live public-API smoke on 2026-08-24 discovered the active
`2026-08-28` expiry, selected the `77000` strike against a `77225` reference,
and returned both `BTC-20260828-77000-C` and
`BTC-20260828-77000-P` at `0.47971` decimal IV. The distance was `0.291%`,
the data age was `0.0s`, and the deterministic confidence was `0.985`.

The integrated routine is run from the existing Condor/PyCharm setup by
starting `derive_market_snapshot`; Stage 2 continues to tail its JSONL output.
The exact direct adapter smoke command is:

```bash
cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
PYTHONPATH=src python3 -c 'import asyncio,json; from derive_options_mm.options_iv import DeriveOptionsProvider; print(json.dumps(asyncio.run(DeriveOptionsProvider().snapshot(77225.0)).__dict__, sort_keys=True, default=str))'
```

## Remaining limitations

- The options leg is public production data while the perpetual leg may be
  testnet or production, so the two feeds are not the same market environment
  unless Stage 1 is configured accordingly.
- The adapter uses REST ticker refreshes at snapshot frequency and does not
  claim a documented rate-limit margin; production operation should monitor
  API errors and tune the snapshot interval if Derive's limits require it.
- ATM IV is a selected call/put estimate, not a full surface, skew, term
  structure, or executable pricing signal.
- No fill, PnL, alpha, deployment, or profitability claim follows from this
  data-layer smoke.

## Decision

Stage 2.5 is complete at ATM IV integration. Stop before mode selection, grid
generation, or Hummingbot execution.
