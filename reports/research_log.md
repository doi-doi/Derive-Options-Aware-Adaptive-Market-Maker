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
