# Judge walkthrough

This script is designed for a five-minute live explanation and a ten-minute code
follow-up.

## 1. What problem does this solve?

Perpetual market makers usually treat the grid as static. This project makes the grid
state-aware: realized volatility, Derive ATM IV, direction, and inventory can change
width, center, density, side allocation, and size. The strategy stays interpretable
and leaves execution to Hummingbot V2.

## 2. What is the options-aware part?

ATM IV enters the volatility score through an IV ratio. It changes risk geometry and
the defensive governor; it is not treated as a directional forecast. On 4,017 audited
frames, IV changed candidate mode 175 times and changed width by more than 5% 1,306
times. RV still dominated combined-score variance in this sample.

## 3. Show the architecture

Open the [README architecture](../README.md#architecture). Explain the persisted
contract: `MarketSnapshot -> MarketState -> GridModeDecision -> GridPlan`. The
Hummingbot controller consumes the plan, rather than reimplementing strategy logic.

## 4. Show the mode governor

Open the [safe demo](DEMO.md) and run `./scripts/demo.sh`. Point to the current mode,
reasons, width, center, level counts, and allocations. Explain that `DEFENSIVE` is
wider, less dense, and smaller; `PAUSE` stops new entries but does not force-liquidate
filled executors.

## 5. How does execution avoid duplicate orders?

The controller keys eligible entries by logical grid level and reconciles existing
executors before creating anything. A plan change cancels/stops a stale unfilled
entry, waits for the lifecycle state, and replaces it only when safe. Filled
executors remain managed and are not mistaken for missing entries.

## 6. What was actually proven live?

Stage 5F authenticated with Hummingbot against Derive testnet and submitted exactly
one passive post-only maker entry per side. The run had real Derive order IDs and
verified KEEP, duplicate prevention, cancel/replace, DEFENSIVE, PAUSE, recovery, and
cleanup. See [LIVE_EVIDENCE.md](LIVE_EVIDENCE.md).

## 7. What was not proven live?

No natural BTC-PERP maker execution occurred during the authorized observation. A
second account/counterparty was not authorized. Therefore there is no live fill,
live take-profit, live realized PnL, live position feedback, queue-quality, or
profitability claim.

## 8. How is the fill-dependent path covered?

The separate deterministic simulation uses a real Hummingbot `PositionExecutor`
configuration and an injected fill event. It verifies the adjacent-grid exit
direction and lifecycle state. It is labeled OFFLINE SIMULATION everywhere.

## 9. What does the evaluation say?

The latest conservative replay has static total PnL **+2.06225**, RV-only **-25.28121**,
and IV-aware **-23.48627**. IV-aware modestly improves RV-only and changes geometry,
but static is better on this sample. That is the honest result, not a forced win.

## 10. What are the risk controls?

The example configuration keeps `allow_mainnet_trading=false`, leverage 1,
`post_only=true`, `execution_max_levels_per_side=1`, `max_active_executors=2`, and
`execution_enabled=false`. The demo never contacts an exchange. A live run is
testnet-only and requires separate credentials and authorization.

## 11. What makes the evidence auditable?

The JSONL lineage is retained, Stage 6.5 keeps raw and canonical counts, the audit
checks no lookahead and position accounting, and the claim ledger distinguishes LIVE
TESTNET, RECORDED, OFFLINE REPLAY, and NOT OBSERVED LIVE.

## 12. What would you do next?

Run an explicitly authorized one-level testnet fill experiment, then reconcile the
Derive position through Stage 1--4 and the executor lifecycle. Only after that proof
would additional live depth be considered. Mainnet remains out of scope.
