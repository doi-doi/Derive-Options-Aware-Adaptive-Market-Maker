# Stage 12: Derive mainnet shadow baseline

Stage 12 measures the unchanged adaptive-grid strategy against public Derive
mainnet data. It runs two isolated paper ledgers:

- `conservative_trade_through` is the primary result;
- `touch_optimistic` is sensitivity evidence only and is never averaged into
  the primary result.

The session is fail-closed. The profile cannot enable a private Derive client,
real exchange execution, mainnet trading, or self-tuning application. The
committed profile is disabled; the explicit command is the human gate that
enables the process-local paper session.

Stage 12D uses the explicit canonical profile path below so profile resolution
is unambiguous and does not use a `--no-trades` shortcut:

```bash
PYTHONPATH=src:. .venv/bin/python -m condor.shadow_baseline \
  --profile configs/shadow_competition_800_usdc.yml \
  --duration 60m --interval 5
```

## Smoke and baseline commands

From the repository root:

```bash
PYTHONPATH=src:. .venv/bin/python -m condor.shadow_baseline \
  --profile configs/shadow_competition_800_usdc.yml \
  --duration 15m
```

Once the smoke is clean, start the preferred baseline:

```bash
PYTHONPATH=src:. .venv/bin/python -m condor.shadow_baseline \
  --profile competition_800_usdc \
  --duration 48h
```

Use `--duration 24h` for the minimum meaningful observation. `--resume
<session_id>` marks the old session interrupted and starts a new clean session;
it never merges virtual state across sessions. The `--no-trades` option skips
the optional public trade-history request and leaves trade-based evidence
explicitly unavailable. The conservative trade-through model never falls back
to BBO touch/cross evidence; only the separate touch-optimistic sensitivity
model may use BBO evidence.

Each run prints its session ID, frozen configuration hash, markets, fill models,
dashboard command, and `REAL EXCHANGE MUTATIONS: 0`. It writes a checkpoint at
the configured interval, writes the baseline control manifest at session start,
and produces:

`reports/shadow_baseline/<session_id>/`

with `summary.md`, `summary.json`, `baseline_manifest.json`, `data_quality.csv`,
`equity.csv`, `hourly_metrics.csv`,
`orders.csv`, `fills.csv`, `cancels.csv`, `cycles.csv`, `markouts.csv`,
`inventory.csv`, `portfolio_exposure.csv`, `risk_events.csv`,
`fill_model_comparison.csv`, and `self_tuning_suggestions.csv`.

Stage 12C additionally writes the observability report and bounded machine-readable
artifacts under `reports/stage12c/`: exact cancel taxonomy counts, resting
lifetimes, replacement deviation/context, risk-episode summaries, public-trade
coverage, fill-eligibility attribution, reconciliation decisions, and the latest
`smoke_summary.json`. The separate `data/shadow_order_lifecycle.jsonl` stream
contains the bounded order lifecycle events used by the dashboard and audit.

## Dashboard

Start the local Streamlit dashboard against the same data directory printed by
the runner:

```bash
PYTHONPATH=src:. .venv/bin/streamlit run dashboard/app.py -- \
  --data-dir data
```

Open `SHADOW TRADING`. The page shows the fixed banner:

```text
DERIVE MAINNET DATA
SHADOW EXECUTION
PAPER FUNDS ONLY
REAL EXCHANGE MUTATIONS: 0
```

It exposes accounting reconciliation, explicit fee-model status, volume and
order lifecycle, cancellation taxonomy/churn, markouts and sample counts,
cycles/capital recycling, time-weighted inventory and portfolio exposure,
conservative-vs-touch comparison, config-freeze status, health checks, and
unapplied self-tuning suggestions.

For the Stage 12D real 60-minute smoke, use the frozen profile with
`--duration 60m --interval 5`. The dashboard must continue to show `SHADOW EXECUTION`,
`REAL EXCHANGE MUTATIONS: 0`, gross paper PnL when fees are unknown, and
`VERIFIED NET PNL: UNKNOWN` unless a fee model has been verified.

The completed Stage 12D evidence is recorded in
`reports/stage12d_mainnet_shadow_smoke.md`.

## Interpretation boundary

`current_equity = starting_equity + realized_pnl + unrealized_pnl - fees` is
checked at each checkpoint. When fees are not verified, the report marks the
fee model `UNKNOWN`, reports gross PnL and deterministic fee sensitivity, and
does not claim net PnL. Markouts use only actual future public observations;
missing session-end observations remain missing.

The final classification is deterministic and evidence-based. A short run can
be `PROMISING BUT INSUFFICIENT EVIDENCE` even when its paper PnL is positive.
The required next step remains `NOT READY FOR OPTIMIZATION` until measurement
quality and sample sufficiency are established. This stage does not tune grid
geometry, quote distance, TP, order size, cancellation thresholds, exposure,
or live execution.

Final verification:

```bash
PYTHONPATH=src:. .venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```
