# Stage 11 — Risk-Adjusted Volume Efficiency

Status: **Phase A — measurement only**

Automatic optimization: **not implemented**

Execution/configuration mutation: **disabled**

Evidence boundary: **live Condor streams are partial; offline replay event records are available**

## 1. Executive summary

Stage 11 starts with the required measurement gate. The existing bounded
observer now exposes `VolumeEfficiencyMetrics` for an asset or the combined
portfolio. It measures actual fill notional, time-weighted deployed exposure,
inventory exposure-time, lifecycle ratios, quote lifetime, entry-to-exit cycle
duration, maker markout, capture, fees, PnL, and drawdown.

No quote, TP, order-size, active-level, allocation, risk-limit, leverage,
environment, or execution behavior was changed. The next Stage 11 phases are
intentionally deferred until these measurements are validated against a
complete execution journal and replay state records.

## 2. Objective

The eventual objective is legitimate maker turnover per unit of deployed risk,
subject to market-integrity rules and the existing `PortfolioRiskGovernor`.
Phase A does not calculate or optimize a composite score.

## 3. Why raw volume is not enough

Create, cancel, replace, KEEP, and executor-message counts are not trading
volume. Only actual executed fill notional is counted. A high-volume result
without inventory, markout, drawdown, fee, and capital-time context is not
treated as an improvement.

## 4. Volume-per-risk methodology

`executed_total_notional` is the sum of actual entry and exit fill records.
Accepted notional fields are `executed_notional`, `quote_notional`,
`quote_amount`, or a validated `price * amount` fallback. A fill with no
reconstructable notional makes the aggregate volume `UNKNOWN` rather than
silently producing a partial total.

`volume_per_average_gross_exposure` divides executed notional by the measured
average deployed gross exposure. The denominator is time-weighted where a
timestamped state series is available.

## 5. Capital turnover

The measurement layer distinguishes deployed exposure from account equity. It
uses the left-hold convention between consecutive timestamped samples:

```text
average exposure = sum(value_i * (t_(i+1) - t_i)) / (t_last - t_first)
```

The final sample is not carried forward beyond the last observed timestamp.
`capital_time_efficiency` is executed notional divided by inventory
exposure-time in notional-hours. If the required series is absent, the result
is `UNKNOWN`.

## 6. Quote-distance analysis

Deferred. Phase A does not bucket distances or propose quote changes.

## 7. Markout

The observer reuses the existing elapsed-horizon markout contract. A fill's
5/30/60-second markout is accepted only when the fill timestamp plus that
horizon is no later than the observation end. No future mark is synthesized,
and no missing markout is forward-filled.

## 8. TP / capital recycling

Phase A measures completed entry-to-exit durations and realized capture when
the exit can be associated with its entry by position/order/executor ID, with a
level FIFO fallback for sequential same-level lifecycles. It does not change TP
distance or force an exit.

## 9. Inventory efficiency

Average and maximum absolute inventory are read from notional/base-position
state records. Portfolio views aggregate per-asset inventory at matching
timestamps. Inventory exposure-time is reported separately from gross
exposure-time.

## 10. Shallow grid policy

Unchanged. The competition profile remains one active entry level per side.
Phase A does not activate a second level or bypass Stage 4.

## 11. Second-level activation

Deferred. No active-level policy was added in this measurement phase.

## 12. Order-size adaptation

Deferred. No order-size multiplier or capacity redistribution is applied.

## 13. Asset capital allocation

Deferred. No allocation weights are learned or applied.

## 14. Portfolio-risk integration

Portfolio gross and BTC-beta measurements are read-only inputs. The
`PortfolioRiskGovernor` remains authoritative; Phase A has no path that can
raise hard limits.

## 15. Self-tuning integration

Deferred. The existing Stage 10 observer remains `SUGGEST_ONLY`, and Stage 11
does not add diagnosis, promotion, rollback, or `AUTO_BOUNDED` behavior.

## 16. Dashboard

Deferred. The read-only Stage 10 dashboard continues to show its existing
observer surface. Phase A artifacts are available locally for inspection, but
no optimization controls were added.

## 17. Walk-forward replay

Deferred. Phase A performs no parameter selection and therefore cannot create a
look-ahead-safe challenger.

## 18. Baseline comparison

Deferred. No fixed-vs-optimized comparison is claimed because no optimized
variant exists.

## 19. Results and validation

The read-only command is:

```bash
.venv/bin/python tools/run_stage11_measurement.py \
  --data-dir /Users/wilfred/Documents/Hummingbot/condor/data
```

It writes:

- `reports/stage11/asset_volume_efficiency.csv`
- `reports/stage11/portfolio_volume_efficiency.csv`
- `reports/stage11/cycle_efficiency.csv`
- `reports/stage11/phase_a_measurement.json`

The inspected Condor window has no `derive_execution_events.jsonl`. Therefore
live executed volume, fills, completed cycles, quote lifetime, markout, capture,
fees, and drawdown are `UNKNOWN`. Inventory/gross exposure and portfolio beta
are available from the current state/risk streams, but they are zero in the
observed window and do not prove trading activity.

The existing offline replay event record was also checked with the same pure
measurement contract. It reconstructs actual fill notional, lifecycle ratios,
cycle duration, markout, capture, and drawdown; it cannot reconstruct average
deployed risk because `reports/stage6/replay_events.jsonl` is an event-only
artifact without the replay tick state series. Those observations remain
offline/replay evidence and are not live results.

## 20. Limitations and stop conditions

Phase A stops here because the current live source is incomplete. The next
phase must not begin until a complete execution journal and corresponding state
records are available. In particular, do not claim risk-adjusted efficiency if
fills cannot be reconstructed, inventory exposure-time is absent, TP lifecycle
IDs are ambiguous, or markout timestamps are unreliable.

## 21. Recommended competition profile

Keep the current testnet, post-only, one-level, fail-closed profile unchanged:

- `allow_mainnet_trading=false`
- `execution_enabled=false` unless separately authorized for the existing
  one-level testnet canary
- `post_only=true`
- leverage and hard portfolio/asset limits unchanged
- Stage 11 measurement-only / Stage 10 `SUGGEST_ONLY`

No recommendation is automatically applied.

## Phase A gate

| Measurement | Status on current live Condor data |
| --- | --- |
| Executed notional | **FAIL — execution journal missing** |
| Average deployed gross exposure | **PASS — state/risk fields present** |
| Average inventory | **PASS — state fields present** |
| Completed cycles | **FAIL — execution journal missing** |
| Cycle duration | **FAIL — execution journal missing** |
| Fill/create and cancel/create | **FAIL — execution journal missing** |
| Quote lifetime | **FAIL — execution journal missing** |
| 5/30/60s markout | **FAIL — live fill records missing** |
| Realized capture and fees | **FAIL — execution journal missing** |
| Drawdown | **FAIL — live PnL series missing** |

This is a deliberate evidence stop, not a trading failure. No artificial
volume, self-trade path, order-count reward, cancellation reward, or execution
mutation was introduced.
