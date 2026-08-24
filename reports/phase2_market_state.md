# Stage 2 — Read-only Derive market state engine

Stage 2.5 adds the optional ATM-IV input described in the
[Stage 2.5 report](stage2_5_options_iv.md); the state engine remains read-only
and stops at `MarketSnapshot -> MarketState`.

## Goal

Convert Stage 1 normalized `MarketSnapshot` JSONL observations into one
explainable `MarketState` observation per new snapshot:

```text
Stage 1 MarketSnapshot -> bounded StateEngine history -> MarketState
```

This stage does not build grid levels, select modes, create executors, or call
any trading API.

## Stage 1 interface reused

The state engine consumes the existing fields from
`integrations/condor/derive_market_snapshot.py`:

- `timestamp` and `trading_pair`
- `mid_price`
- `depth_imbalance`, falling back to `top_level_imbalance`
- `order_flow_imbalance` only when `trade_data_available` is true
- `atm_iv` and selected option metadata only when `iv_data_available` is true
- signed `current_position`, absolute `position_notional`, and
  `account_data_available`
- `data_valid` and `validation_errors`

The state routine does not duplicate Stage 1 market-data collection. It tails
`data/derive_market_snapshots.jsonl` and keeps only the fields needed for
state calculations in an in-memory bounded deque. Existing snapshots may be
replayed once at startup to warm the history; the entire file is never read on
each state update.

## State engine architecture

The pure engine lives in
`src/derive_options_mm/state_engine.py`. The Condor entrypoint is
`integrations/condor/derive_market_state.py`.

The Condor routine:

1. warms the engine from at most `bootstrap_max_samples` existing JSONL rows;
2. positions a file tailer at the active file's end;
3. consumes only newly completed Stage 1 JSONL lines;
4. calls `StateEngine.update(snapshot)`;
5. appends the resulting state to a separate JSONL file; and
6. logs one concise `[DERIVE STATE]` summary per new snapshot.

## Volatility formula

For consecutive positive mid prices:

```text
log_return = ln(mid_t / mid_(t-1))
realized_volatility = sqrt(mean(log_return^2))
```

The measure is not annualized. The current realized volatility is divided by
the RMS volatility from the prior baseline window. ATM IV, when reliable, is
divided by the recent median IV after the configured IV-history warm-up and
combined with the realized ratio using configured weights. Missing or
warming-up IV falls back to the realized-volatility ratio.

Default classification uses hysteresis:

```text
enter HIGH at 1.50
leave HIGH below 1.25
```

These are transparent starting defaults, not optimized parameters.

## Direction formula

Available components are clamped to `[-1, 1]` and their configured weights are
renormalized when a component is unavailable:

```text
direction_score = weighted(book_imbalance, OFI, price_signal)
price_signal = clamp(short_log_return / direction_price_scale, -1, 1)
```

ATM IV is not used for direction. Direction changes require
`direction_confirmation_samples` consecutive observations. Entry and exit
thresholds are separate to prevent order-book noise from flipping the label.

## Inventory formula

Stage 1 `current_position` is signed while `position_notional` is absolute.
Stage 2 reconstructs signed notional using the position sign, then calculates:

```text
inventory_ratio = signed_position_notional / max_position_notional
```

Unavailable account data produces `inventory_state=unknown`; it is never
silently treated as neutral.

## Fallback behavior

- **ATM IV unavailable:** use realized volatility only and add an explicit
  `ATM IV unavailable` reason.
- **OFI unavailable:** remove OFI from the direction score and renormalize the
  book/price weights.
- **Account data unavailable:** emit `inventory_state=unknown` and a null
  inventory ratio.
- **Insufficient history:** emit initializing labels, low confidence, and a
  deterministic sample-count reason; no state signal is invented.
- **Invalid or stale Stage 1 snapshot:** do not add it to history; emit an
  invalid state observation with the Stage 1 validation errors.

## Persistence and run command

Deploy the routine into Condor's global routine directory:

```bash
ln -sfn \
  /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/integrations/condor/derive_market_state.py \
  /Users/wilfred/Documents/Hummingbot/condor/routines/derive_market_state.py
```

Start Stage 1 first, then open the authenticated Condor `/routines` page,
select `derive_market_state`, and choose **Run**. The routine uses the same
pair filter as the Stage 1 default, `BTC-USDC`, and consumes whichever
connector Stage 1 has configured (testnet or production).

The state output is:

```text
/Users/wilfred/Documents/Hummingbot/condor/data/derive_market_states.jsonl
```

The state routine is continuous and stops only when Condor stops it. Its
returned stop summary reports the number of processed states and the last
persisted path.

## Example console output

```text
[DERIVE STATE]
Pair: BTC-USDC
Volatility:
  HIGH
  Score: 2.66
  RV: 2.66x baseline
  ATM IV: 47.97%
  IV Ratio: initializing
  IV Expiry: 2026-08-28
  IV DTE: 4.27
  ATM Strike: 77000
Direction:
  NEUTRAL
  Score: +0.07337
  Book: -0.08869
  OFI: unavailable
  Return: +0.012%
Inventory:
  NEUTRAL
  Ratio: +0
Confidence: 0.85
Valid: true
Reasons:
  realized volatility 2.66x baseline
  book imbalance -0.08869
  OFI unavailable
  ATM IV 47.97% (confidence 0.99)
```

## Tests and verification

The Stage 2 tests cover:

1. log-return and zero-volatility calculations;
2. volatility normalization, IV regime, combined score, entry, and hysteresis;
3. direction score with and without OFI;
4. bullish, bearish, neutral, and confirmation behavior;
5. long, short, neutral, and unavailable inventory;
6. insufficient history, invalid input, and confidence;
7. Stage 1 JSONL tailing, partial-line handling, and state persistence;
8. absence of market-data and mutating trading surfaces.

A replay of the active Stage 1 JSONL feed produced valid market states and
retained only the configured rolling history. The implementation remains
analytics-only; no orders, executors, controller configuration, leverage, or
position state are changed.

## Known limitations

- The installed Hummingbot Derive stack does not provide ATM IV directly; the
  Stage 2.5 state input comes from the separate public Derive options adapter.
  During its history warm-up, live states may show raw ATM IV while `iv_ratio`
  remains initializing.
- OFI is event-window dependent and may be unavailable or undefined when no
  recent trades arrive.
- The default `max_position_notional` is a diagnostic placeholder and must be
  configured to the account's approved exposure reference before any future
  strategy stage uses the ratio.
- The state engine is not a mode selector, grid parameter engine, trading
  strategy, or profitability result.

## Decision

Stage 2 ends at `MarketSnapshot -> MarketState`. Stop before mode selection,
grid generation, or Hummingbot execution.
