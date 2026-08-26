# Stage 9 local Condor dashboard

Stage 9 provides a local configuration, risk, and grid-preview control panel for
the existing Derive Adaptive State Grid. It is an operator aid around the Condor
JSONL streams; it is not a second trading engine.

## Safety boundary

The dashboard imports the repository's typed configuration and Stage 4 planner. It
does not import an exchange connector, Hummingbot API client, wallet provider, or
order/executor action. It can read local Condor output and write local YAML,
version history, backups, and redacted change events only.

The committed competition profile remains:

```yaml
market_environment: testnet
connector_name: derive_perpetual_testnet
allow_mainnet_trading: false
execution_enabled: false
post_only: true
max_levels_per_side_per_asset: 1
```

The **Environment** page is the single network selector for this dashboard. It
stages the canonical connector and environment boundaries together:

- `TESTNET` → `derive_perpetual_testnet` (the default).
- `MAINNET` → `derive_perpetual`, read-only/canary posture only.

Changing networks always stages `execution_enabled: false` and
`allow_mainnet_trading: false`, preserves the Stage 4 theoretical allocations,
and requires a controller restart before the new YAML is consumed. Switching to
mainnet also requires an explicit read-only acknowledgement in the sidebar. The
dashboard does not grant mainnet trading authority; real orders still require the
separate Hummingbot canary configuration and all of its authenticated account,
environment-consistency, risk-budget, and acknowledgement gates.
The existing Condor Stage 8 monitor remains testnet-only; this selector does not
retarget its four JSONL input streams.

When Apply is used, the dashboard also writes the generated, fail-closed
controller artifact to `configs/derive_adaptive_grid_controller.yml`. Copy or
deploy that artifact through the normal Hummingbot API workflow and restart the
bot; the dashboard never hot-reloads a running controller.

## Install and run

From the project checkout:

```bash
cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
source .venv/bin/activate
python -m pip install -e '.[dev,dashboard]'
export CONDOR_DATA_DIR=/Users/wilfred/Documents/Hummingbot/condor/data
./scripts/run_dashboard.sh
```

Open [http://localhost:8501](http://localhost:8501). The script accepts the same
arguments as the app, for example:

```bash
./scripts/run_dashboard.sh --data-dir /Users/wilfred/Documents/Hummingbot/condor/data
```

If the data directory is absent or a stream is not present yet, the page remains
usable and labels that stream `MISSING`, `STALE`, or `DEGRADED`. The reader is
bounded and tolerates malformed records, partial trailing lines, rotation, and
truncation.

## Local source files

The control-panel source of truth is deliberately split into two reviewed files:

- `configs/competition_800_usdc.yml` — competition risk, execution guardrails,
  portfolio limits, asset limits, and refresh deadbands.
- `configs/stage9_strategy.yml` — the typed Stage 8 strategy overlay used for
  strategy forms and Stage 4 grid previews.
- `configs/derive_adaptive_grid_controller.yml` — generated only when Apply is
  used; selected-environment controller artifact with execution disabled.

The current Condor monitor still constructs its `MultiAssetConfig` in process at
startup and does not expose a runtime configuration hash or safe hot-reload hook.
Consequently, the dashboard labels applied changes `RESTART_REQUIRED`. It does not
claim that a running Condor process has consumed a new file. A supported process
restart and a fresh status check are required before treating a change as runtime
state.

## Pages

- **Overview** — selected environment/read-only status, stream freshness, four asset cards,
  global risk, portfolio utilization, and recent mode/plan state.
- **Environment** — one testnet/mainnet selector, canonical connector mapping,
  explicit mainnet read-only acknowledgement, and restart boundary.
- **Self-tuning** — Phase 1 observer-only supportability, live-versus-replay
  evidence labels, bounded performance metrics, source health, and locked
  human-only parameters. It has no proposal or configuration-mutation controls.
- **Strategy** — existing Stage 8 IV/RV, relationship, direction, volatility,
  grid-width, level-count, center-shift, and defensive controls.
- **Risk** — reserve, gross/beta, directional, inventory, drawdown, asset-limit,
  and allocation controls with a deterministic consequence preview.
- **Execution** — selected-environment execution flags, post-only/leverage
  guardrails, one-level sizing, lifetime/cooldown/deadband controls, order-size
  rule preview, and historical refresh stability estimate. This page cannot trade;
  mainnet remains read-only here.
- **Assets** — ETH, SOL, and HYPE enablement and existing independent asset limits;
  BTC remains signal-only. The page also shows recorded BTC relationship data.
- **Config History** — redacted version snapshots, changed-field hashes, operator
  notes, and append-only rollback through the same validate/apply path.
- **Advanced** — exact local paths, stream status, redacted YAML export, reload
  boundary, mode explanation, and security statement.

## Safe edit workflow

1. Load the current or competition profile into the staged form.
2. Edit fields in one page; the saved YAML is not changed while editing.
3. Review the validation result and the field-level diff in the sidebar.
4. Review the risk/order-size/grid previews before applying.
5. Add an operator note. Risk-increasing changes and an execution-enable
   transition require the explicit acknowledgement checkbox.
6. Apply only after the diff is understood. Writes use temporary files, fsync, and
   atomic replacement. Existing files receive a `.bak` copy.
7. Confirm the new version and redacted event in Config History.
8. Restart the existing Condor process only through the normal operator workflow,
   then refresh the dashboard and verify fresh stream timestamps. The dashboard
   never restarts it automatically.

Rollback creates a new version from the selected redacted snapshot; it never
overwrites or deletes prior history. `data/config_change_events.jsonl` contains
only hashes and changed paths, not secret values.

## Grid and portfolio previews

Grid Preview calls the existing pure `derive_options_mm.grid_engine.build_grid_plan`
with a deep-copied snapshot/state/mode. Proposed plans are therefore advisory and
non-mutating. It displays the current/proposed center, width, level count, side
allocation, and level rows, including a synthetic inventory/direction preview.

The stability panel compares consecutive recorded plans using the configured price
and amount deadbands and reports estimated `KEEP`, `REFRESH`, `NEW`, and `REMOVED`
counts. It is labeled historical because it is not a live executor decision and is
not a future fill or churn guarantee.

## Verification

Run from the project checkout:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

The dashboard's focused tests cover staged-config immutability, validation and
secret redaction, atomic history/rollback, malformed/partial JSONL handling,
status degradation, deterministic consequences, historical stability, and the
non-mutating Stage 4 planner call.

## Known limitations

The current Condor monitor has no safe runtime reload contract, so the dashboard
cannot truthfully show a runtime config hash or apply a change to an already
running process. No `derive_execution_events.jsonl` stream was present in the
observed local data directory, so execution-event cards remain unavailable until
that stream is produced. The dashboard does not turn the existing read-only
monitor into an execution route and does not prove fills, PnL, or live exchange
behavior.

## Stage 10 observer artifacts

The same local data can be inspected without opening Streamlit:

```bash
.venv/bin/python tools/run_stage10_observer.py \
  --data-dir /Users/wilfred/Documents/Hummingbot/condor/data
```

This writes `reports/stage10/performance_windows.csv`,
`reports/stage10/observer_supportability.json`, and the local
`data/self_tuning_status.json`. The command is Phase 1 only, defaults to
`SUGGEST_ONLY`, and never writes strategy configuration or contacts an exchange.
