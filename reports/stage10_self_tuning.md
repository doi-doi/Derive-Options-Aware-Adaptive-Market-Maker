# Stage 10 — Bounded self-tuning

Status: **Phase 1 observer-only implementation**
Default mode: `SUGGEST_ONLY`
Automatic configuration mutation: **disabled and not implemented**
Evidence date: 2026-08-25 local Condor streams, inspected from the 2026-08-26
implementation run

This report deliberately records what is supported now and what remains deferred.
It is not a claim that self-tuning improves trading performance.

## 1. Executive summary

Phase 1 adds a deterministic `PerformanceObserver` and a local CLI/dashboard
surface. It reconstructs bounded performance windows from existing JSONL records,
keeps unsupported values as `UNKNOWN`, enforces elapsed-horizon markout checks,
and labels replay evidence separately from live observations. The observed Condor
directory has no `derive_execution_events.jsonl`, so live order lifecycle, fills,
capture, PnL, fees, turnover, and markouts remain `UNKNOWN`. Inventory and
portfolio beta observations are available from the existing state/risk streams.

No Hummingbot execution path, connector, configuration file, risk limit, or
exchange endpoint was changed by Stage 10 Phase 1.

## 2. Why bounded self-tuning

The market/state/grid pipeline runs frequently, but configuration learning must
operate on slower, evidence-qualified windows. A bounded observer makes the
measurement boundary explicit before any diagnosis or recommendation can be
trusted. It also prevents a future learner from changing hard risk controls,
environment, credentials, or execution permission.

## 3. Architecture

```text
Condor JSONL streams
  -> bounded reader and source-health records
  -> PerformanceObserver (windowed, no mutation)
  -> CSV / supportability JSON / status JSON
  -> read-only Streamlit SELF-TUNING page
```

`evaluation/self_tuning_observer.py` owns the aggregation contract. It reuses
`evaluation.metrics.summarize_replay` only for offline replay adaptation and
labels that evidence `SHADOW_REPLAY`. `tools/run_stage10_observer.py` reads the
local Condor directory and writes atomic local artifacts. `dashboard/app.py`
renders the same observer contract without proposal or Apply controls.

## 4. Learnable parameters

The specification's initial future whitelist is documented but not active in
Phase 1: order-stability deadbands/lifetimes, carefully bounded geometry
multipliers, and order-size multipliers. No parameter proposal or mutation exists
in this increment. Stage 4 remains the source of truth for theoretical grid
allocations and formulas.

## 5. Locked human-only parameters

The observer status records an explicit lock list covering leverage,
`execution_enabled`, mainnet permission, connector/environment, `post_only`,
supported assets, collateral reserve, hard gross/beta/asset limits, hard
drawdown and emergency rules, credentials, and stale-data safety. These values
are not inputs to any Phase 1 write path.

## 6. Safety envelopes

Future learnable parameters require immutable operator-defined minimum, maximum,
maximum-step, and total-deviation envelopes. Phase 1 does not create or change
those envelopes, and therefore cannot drift a value toward an envelope boundary.
`AUTO_BOUNDED` is not available.

## 7. Performance metrics

`PerformanceWindow` exposes the requested lifecycle, capture, accounting,
inventory, beta, drawdown, turnover, fee, regime, confidence, sample-count, and
reason fields. A metric is `AVAILABLE` only when its source is present and a
value can be reconstructed; otherwise the value is `null` and status is
`UNKNOWN`.

The current local observation produced:

| Metric group | Current status |
| --- | --- |
| State inventory ratio | Available from `derive_market_states.jsonl` |
| Portfolio beta exposure | Available from `derive_portfolio_risk_states.jsonl` |
| Mode / global volatility label | Available from state/plan records |
| Relationship regime label | `UNKNOWN` — the stream has relationship statistics but no categorical regime field |
| Order creates/cancels/KEEP/refresh | `UNKNOWN` — no execution journal |
| Fills/cycles/lifetimes | `UNKNOWN` — no execution journal |
| Capture, realized/unrealized/total PnL, fees | `UNKNOWN` live |
| 5/30/60-second markouts | `UNKNOWN` live |

The machine-readable output records the exact window, statuses, source paths,
counts, malformed-line counts, and reasons.

## 8. Markout methodology

For a fill carrying a markout field, the observer uses it only when the fill
timestamp plus the requested horizon is no later than the observation end
timestamp. Thus a 30-second or 60-second markout cannot appear before its future
period has elapsed. Replay summaries cannot override this check. Directional
normalization is the responsibility of the upstream fill/replay event contract;
the observer does not invent a markout when the signed value is absent.

## 9. Performance score

No composite score is produced in Phase 1. This is intentional: the live
execution journal is absent, and a score would imply unsupported lifecycle and
PnL evidence. A later phase may expose a decomposed score, but raw PnL must not
be its sole objective.

## 10. Diagnosis rules

No diagnosis engine is implemented yet. High cancel churn, low fills, adverse
selection, excess inventory, drawdown, and conflicting signals are recorded as
future diagnosis requirements, not inferred from missing live data. The status
file uses `last_diagnosis: PHASE1_OBSERVER_ONLY`.

## 11. Champion/challenger

No champion/challenger experiment is started. The status file reports the fixed
`BASELINE` as the current champion for bookkeeping only; there is no challenger,
promotion, or rollback event.

## 12. Shadow replay

`PerformanceObserver.observe_replay()` adapts the existing replay metrics and
labels the result `SHADOW_REPLAY`. It keeps the existing accounting contract
(including replay fee treatment) while retaining the observer's no-lookahead
markout gate. Replay evidence is never merged into the live observed window.

## 13. Promotion rules

Deferred. No promotion rule can be validated until evidence thresholds,
walk-forward replay, a baseline comparison, and a champion/challenger ledger
exist. No automatic promotion path is present.

## 14. Rollback rules

Deferred. Stage 9 configuration history remains the operator-controlled local
versioning path. Stage 10 does not write configuration or invoke rollback.

## 15. Contextual profiles

The observer accepts an asset selector and carries mode, volatility, and
relationship labels in each window. Per-asset learning profiles are deferred
until the execution journal and sufficient multi-asset lifecycle samples exist.
No global parameter is changed as a proxy for an asset-specific observation.

## 16. Dashboard integration

The local dashboard now has a `SELF-TUNING` page. It shows `SUGGEST_ONLY`, live
source health, known/unknown counts, the observed window, metric status, reasons,
and the locked-parameter boundary. It intentionally contains no proposal,
Apply, Reject, Auto, or execution control. The page was checked at the default
desktop viewport and at 390×844; the primary content remained readable and the
dashboard browser console reported no errors.

## 17. Walk-forward replay

Deferred. The replay adapter's elapsed-horizon markout guard is implemented and
tested, but no self-tuning walk-forward promotion run is claimed in Phase 1.

## 18. Fixed vs self-tuning comparison

Deferred. The only current comparison is evidence supportability, not strategy
performance: fixed baseline operation continues unchanged, while self-tuning is
observer-only and produces no alternative configuration.

## 19. Test results

Focused Phase 1 checks completed during implementation:

- `tests/test_stage10_observer.py`: 4 passed.
- dashboard import/status regression checks: 9 passed when run with the Stage 10
  observer tests.
- `ruff check` passed for the changed observer, dashboard, reader, CLI, and test
  files.
- local Streamlit dashboard rendered the new page on desktop and mobile-sized
  viewports with no browser console errors.

The final repository-wide verification is recorded in the handoff response after
the full suite, Ruff, and diff check complete. The completed gate is:

- `.venv/bin/python -m pytest -q` — **221 passed**.
- `.venv/bin/ruff check .` — **All checks passed**.
- `git diff --check` — **clean**.

## 20. Limitations

- The current Condor data directory does not contain an execution journal.
- The existing live journal schema has request/success distinctions; the observer
  counts confirmed `STOP_SUCCESS` cancellations and uses executor IDs for
  lifetime reconstruction.
- Live markouts, fills, PnL, fees, and realized-vs-inventory accounting cannot be
  claimed without the journal and appropriate mark data.
- No diagnosis, proposal, experiment, champion/challenger, promotion, rollback,
  hot reload, or automatic mutation is part of Phase 1.
- This is not evidence of profitability, execution quality, or mainnet readiness.

## 21. Recommended competition mode

Keep the existing testnet, one-level, post-only, fail-closed rollout unchanged.
Run Stage 10 in `SUGGEST_ONLY` observer mode only. Do not enable `AUTO_BOUNDED`,
mainnet, automatic execution, deeper grids, or hard-limit changes from this
component. The next evidence-gated step is to produce and validate a real
execution journal, then review the observer output before implementing diagnosis.

## Machine-readable artifacts

The current Phase 1 command writes only useful non-redundant artifacts:

- `reports/stage10/performance_windows.csv`
- `reports/stage10/observer_supportability.json`
- `data/self_tuning_status.json`

Diagnosis, experiment, champion-history, parameter-change, fixed-vs-adaptive, and
rollback-event files are intentionally not fabricated while those phases are
deferred.

Run again with:

```bash
.venv/bin/python tools/run_stage10_observer.py \
  --data-dir /Users/wilfred/Documents/Hummingbot/condor/data
```
