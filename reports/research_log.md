# Research log

## P1-001 — Data and infrastructure readiness

**Hypothesis**

The current Derive and Condor stack exposes enough historical and live information to run a small, non-options BTC perpetual market-making baseline before building adaptive options features.

**Data**

Derive public production/testnet REST and WebSocket APIs; local Condor and Hummingbot API capability inventory; installed Hummingbot 20260729 backtesting source.

**Feature**

None. This is a capability and provenance gate.

**Target**

Historical coverage, live field availability, connector health, and fill-model realism.

**Training period / testing period**

Not applicable.

**Method**

Read-only endpoint probes, field/schema inspection, pagination-edge checks, live WebSocket snapshots, and installed-source inspection.

**Result**

Derive production history and direct live feeds are sufficient to start a conservative BTC baseline. Condor's generic Derive connector provides live perpetual execution state but no native Derive candles or options-specific ticker fields. The installed backtester is candle-based and not queue-aware.

**Statistical/economic significance**

Not applicable. No alpha or profitability test was performed.

**Trading implication**

Use Derive's public production history for research, add a dedicated read-only live collector later, and treat Condor's candle backtest as a smoke test rather than market-making evidence.

**Decision: MODIFY**

Proceed only to a small BTC inventory-aware baseline with conservative custom fill assumptions and fee reconciliation. Do not add options features yet.

## P2-001 — Condor Derive market snapshot

**Hypothesis**

The installed Condor and Hummingbot Derive connector can provide a fresh,
read-only BTC perpetual order book sufficient to calculate top-of-book and
depth imbalance without accessing trading APIs.

**Data**

Live `derive_perpetual_testnet/BTC-USDC` order-book diagnostics and top-five
book levels through the local Hummingbot API.

**Feature**

Best bid, best ask, mid, absolute spread, spread basis points, bid depth, ask
depth, and normalized book imbalance.

**Target**

Transport readiness, timestamp freshness, deterministic calculations, and a
provable no-trading API boundary.

**Method**

One-shot Condor routine, fail-closed validation, focused unit tests, and two
successive live testnet runs.

**Result**

On 2026-08-22, Condor discovered the routine and two successive live testnet
runs returned fresh timestamps, populated top-five books, and all requested
metrics. The focused test suite and Ruff passed.

**Statistical/economic significance**

Not applicable. This milestone performs no alpha or profitability test.

**Trading implication**

None. The routine cannot create, cancel, or modify orders.

**Decision: STOP**

Do not add states, persistence, options, simulation, or execution without a
separate approval.

## ST1-001 — Normalized read-only data layer

**Hypothesis**

The installed Hummingbot/Condor stack can support a small, continuously
persisted, read-only Derive data layer without accessing trading APIs.

**Data**

`derive_perpetual_testnet` / `BTC-USDC` order-book diagnostics and five levels;
the installed Hummingbot API's official market-data trade WebSocket; and
authenticated read-only position and portfolio routes.

**Feature**

Normalized timestamp, BBO, mid, absolute/bps spread, BBO sizes, top-five depth,
top-level/depth imbalance, tracker age, optional OFI, optional position/notional,
and available collateral. ATM IV remains an explicit unavailable field.

**Method**

Pure Decimal feature functions, fail-closed validation, a continuous five-second
Condor routine, append-only JSONL with bounded rotation, focused unit tests, and
one bounded live smoke run. The routine contains no mutating trading call.

**Result**

The installed API contract was verified from local source. A live smoke run on
2026-08-23 produced one valid persisted snapshot and confirmed the market-data,
account-data, and official trade-subscription paths. The bounded trade probe
accepted the subscription but did not observe a trade event during its short
window. The installed Hummingbot Derive stack exposes no options ticker/IV route.

**Statistical/economic significance**

Not applicable. This milestone makes no alpha, fill, or profitability claim.

**Trading implication**

None. The output is an input contract for a later approved stage and cannot
place, cancel, or modify orders.

**Decision: STOP**

Do not add state logic, grid generation, simulation, risk, or execution here.

## ST2-001 — Read-only Derive market state engine

**Hypothesis**

The normalized Stage 1 `MarketSnapshot` stream can be converted into
explainable volatility, direction, and inventory states without duplicating
market-data collection or accessing any mutating API.

**Data**

Stage 1 `derive_market_snapshots.jsonl` records, including receipt timestamp,
mid price, depth imbalance, optional OFI/ATM IV, signed position, position
notional, account availability, and explicit validation status.

**Feature**

Bounded-history log-return volatility ratio, optional relative ATM IV, weighted
microstructure direction score, signed inventory ratio, confidence, and
deterministic reasons.

**Method**

Pure functions and a bounded `deque` state engine; a separate Condor routine
tails new Stage 1 JSONL records and persists state JSONL. Realized volatility
uses RMS log returns without annualization. Volatility uses enter/exit
hysteresis; direction uses separate thresholds and consecutive-sample
confirmation. Missing optional data is removed or marked unknown rather than
filled with synthetic neutral values.

**Result**

Focused unit tests passed. A replay of the active Stage 1 feed produced valid
state observations with `MarketSnapshot -> MarketState` and no exchange or
execution calls. The final state remained explainable through stored reasons.

**Statistical/economic significance**

Not applicable. This milestone is an analytics state layer and makes no alpha,
fill, profitability, or deployment claim.

**Trading implication**

None. The state engine cannot place, cancel, modify, or size orders and does
not emit grid modes or grid parameters.

**Decision: STOP**

Stop at `MarketSnapshot -> MarketState`. Require a separate approval before
building mode selection, grid parameters, simulation, or execution.

## ST2.5-001 — Read-only Derive ATM implied volatility

**Hypothesis**

The missing ATM-IV field can be filled by a small official Derive public
options adapter while preserving the existing perpetual snapshot, state, and
direction contracts.

**Data**

Derive public production `public/get_instruments` and `public/get_tickers`
responses for active BTC options; the perpetual reference price remains the
Stage 1 Hummingbot order-book midpoint.

**Feature**

One selected BTC ATM IV: active expiry nearest the configured seven-day target
inside the two-to-fourteen-day range, nearest strike to the perpetual midpoint,
and the mean of valid ATM call/put mark IVs with bid/ask and one-side fallbacks.

**Method**

The installed Hummingbot source was inspected first. It has no Derive options
ticker route, so the isolated adapter uses only the two official public REST
methods. It filters `is_active=true`, caches metadata for 900 seconds, checks
the official ticker timestamp against a 15-second age limit, stores only the
selected fields, and returns explicit unavailable errors. Stage 2 keeps a
bounded prior-IV deque/window, waits for five valid prior observations by
default, uses a rolling median IV ratio, and renormalizes volatility weights
when IV is absent. Direction remains book/OFI/return only.

**Result**

On 2026-08-24 a bounded live public probe discovered active expiry
`2026-08-28`, selected strike `77000` against reference `77225`, and returned
both `BTC-20260828-77000-C` and `BTC-20260828-77000-P` with mark IV `0.47971`.
The selected strike was `0.291%` from reference, option age was `0.0s`, and
deterministic confidence was `0.985`. The focused suite passed 55 tests and
Ruff passed.

**Statistical/economic significance**

Not applicable. This is a market-data integration smoke, not an alpha,
pricing, fill, PnL, or profitability test.

**Trading implication**

None. The options adapter has no private, order, cancellation, leverage,
position-mode, or executor route. Option data affects volatility context only;
it cannot change direction or place trades.

**Decision: STOP**

ATM IV is integrated into the normalized snapshot and state engine. Stop before
mode selection, grid generation, or execution.

## ST3-001 — Read-only Derive grid mode selector

**Hypothesis**

The existing Stage 2 `MarketState` can be converted into a deterministic,
explainable symbolic grid mode without duplicating state calculations or
accessing Hummingbot execution surfaces.

**Data**

Stage 2 `derive_market_states.jsonl`, including volatility regime and score,
direction regime and score, inventory ratio, confidence, validity, and Stage
2.5 IV ratio when available.

**Feature**

One `GridModeDecision` per state: `NORMAL`, `DEFENSIVE`, `LONG_BIAS`,
`SHORT_BIAS`, or `PAUSE`, with prior mode, transition flag, symbolic profile,
and deterministic reasons.

**Method**

An isolated pure candidate evaluator applies the ordered hierarchy
`PAUSE -> DEFENSIVE -> inventory-gated directional bias -> NORMAL`. A stateful
selector adds candidate confirmation, minimum mode duration, defensive exit
confirmation, and delayed safe recovery from `PAUSE`. A separate Condor
routine tails Stage 2 JSONL and appends compact mode JSONL without opening a
market-data connection.

**Result**

The focused Stage 3 suite passed 28 tests and Ruff passed. Tests cover all
five modes, risk priority, confidence, inventory gates, missing optional data,
immediate pause, delayed recovery, mode stability, JSONL persistence, and the
no-trading boundary. A bounded replay smoke consumed the live Stage 2 stream
and wrote one valid `NORMAL`/`standard` record to
`data/derive_grid_modes.jsonl` with volatility score `1.088` and bearish
direction score `-0.234`. The implementation stops at `MarketState ->
GridModeDecision`.

**Statistical/economic significance**

Not applicable. This milestone is a symbolic mode contract, not a backtest,
fill, PnL, or profitability test.

**Trading implication**

None. No order, executor, controller, leverage, position-mode, or market-data
API is called. `PAUSE` is an output for a later execution boundary and does
not cancel existing orders.

**Decision: STOP**

Require a separate approval before building grid centers, widths, levels,
prices, order sizes, or execution.

## ST5-001 — Testnet-gated Hummingbot V2 execution controller

**Hypothesis**

The Stage 4 theoretical `GridPlan` can be converted into safe, independently
identifiable Derive testnet entry executors without changing the upstream
signal, state, mode, or grid-parameter stages.

**Data and runtime**

The controller consumes the append-only Stage 4 JSONL boundary and the
installed Hummingbot Docker runtime observed as distribution `20260729`. The
fixed connector is `derive_perpetual_testnet` and the fixed pair is
`BTC-USDC`.

**Method**

The implementation selects one native `PositionExecutor` per level, uses
official Hummingbot price/amount quantization, requires `LIMIT_MAKER`, and
reconciles desired levels against active unfilled and filled executors. It
fails closed on unverified testnet, stale/invalid plans, connector/account
health failures, inventory/pending exposure limits, collateral limits, and
exchange minimums. The default is `execution_enabled=false`, one level per
side, and a five-percent scale. PAUSE stops unfilled entries while preserving
filled positions by default. Native PositionExecutor take-profit manages the
opposite-side adjacent-grid exit.

**Result**

The Hummingbot image import/config smoke passed. The pure Stage 5 suite passed
26 tests; the full repository suite passed 150 tests; Ruff and compile checks
passed. No live testnet order was enabled, so no fill, exit, realized PnL, or
position-feedback observation is claimed.

**Trading implication**

This is a dry-run-ready execution boundary, not a production or mainnet
approval. The existing Stage 4 Condor output needs an explicit mirror into the
bot instance data volume. Derive's observed approximately `0.01 BTC` testnet
minimum means the conservative five-percent scale skips the current `$100`
Stage 4 levels.

**Decision: STOP**

Deploy a separate dry-run bot, inspect verified status and proposed actions,
then manually approve a one-level testnet run before increasing the scale or
execution cap. Do not proceed to mainnet, optimization, multi-asset trading,
or advanced analytics.
