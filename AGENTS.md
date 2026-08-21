# Project Working Agreement

## Scope gates

- Follow the sequence: data validation, research, feature engineering, hypothesis testing, baseline backtest, adaptive strategy, robustness, testnet, deployment.
- Do not implement a later phase until the prior phase has a written result and explicit approval.
- Never place an order from a research or audit command. Trading code must live behind a separate explicit deployment boundary.

## Evidence rules

- Do not invent Derive, Condor, Hummingbot, or Deribit capabilities. Link a primary source or record a reproducible local observation.
- Record timestamps in UTC and distinguish event time, receipt time, and processing time.
- Preserve raw inputs and machine-readable experiment outputs. Derived datasets must identify their source and transformation.
- Do not forward-fill predictors. If an upstream API backfills values, retain and flag that provenance.
- Deduplicate public Derive trade rows by `trade_id` before treating them as market trades.
- Treat candle-touch fills as optimistic unless a queue-aware fill model supports them.

## Strategy rules

- Direction may skew market-making quotes but must not become the primary strategy.
- Hard risk and inventory controls override model outputs.
- Prefer small, interpretable feature sets and chronological out-of-sample validation.
- Compare every adaptive model with the simplest inventory-aware baseline.

## Definition of done

- State what changed and why.
- Run focused tests and inspect the diff.
- Record limitations and unverified risks explicitly.
- Never claim deployment readiness from a candle-only backtest.
