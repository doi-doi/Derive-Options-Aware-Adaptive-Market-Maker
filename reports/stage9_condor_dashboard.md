# Stage 9 — Condor dashboard implementation report

Date: 2026-08-25
Scope: local Condor configuration, risk, and grid-preview dashboard
Execution status: committed profile remains testnet-only and execution-disabled

## A. Architecture

Stage 9 adds a dedicated local Streamlit entry point at `dashboard/app.py`. The
dashboard is intentionally outside the Condor runtime process: it reads the
existing Condor JSONL files, loads two local YAML configuration sources, and
writes only local configuration/history artifacts. It does not import an exchange
connector, Hummingbot API client, wallet provider, or order/executor action.

The data path is:

```text
Condor JSONL streams
        -> bounded JsonlTailReader
        -> status / latest-by-asset / churn view
        -> existing Stage 4 build_grid_plan preview

YAML profile + strategy overlay
        -> typed DashboardConfig
        -> staged edits
        -> validation + field diff + consequence previews
        -> atomic apply + redacted history/event log
```

The current Condor monitor constructs its `MultiAssetConfig` in process at
startup, has no safe runtime reload hook, and reports no runtime configuration
hash. The dashboard therefore reports `RESTART_REQUIRED` after an apply and never
pretends that a running process has consumed the staged file.

## B. Files created

- `dashboard/__init__.py`
- `dashboard/app.py` — Streamlit pages and staged-edit workflow.
- `dashboard/config_schema.py` — typed Stage 9 bundle, runtime paths, and safe
  presets.
- `dashboard/state_reader.py` — bounded, resilient JSONL reader and runtime
  freshness/churn summaries.
- `dashboard/config_validation.py` — validation, stable hashes, diffs,
  classification, and secret redaction.
- `dashboard/config_store.py` — atomic writes, backups, history, events, and
  append-only rollback.
- `dashboard/consequence_preview.py` — risk, order-size, and historical stability
  previews.
- `dashboard/grid_preview.py` — non-mutating Stage 4 planner adapter.
- `dashboard/portfolio_preview.py` — portfolio/collateral presentation helpers.
- `dashboard/history.py` — history and rollback presentation helpers.
- `configs/stage9_strategy.yml` — reviewed strategy overlay for the existing
  Stage 8 parameters.
- `scripts/run_dashboard.sh` — local launch script.
- `docs/dashboard.md` — operator guide and safety boundary.
- `tests/test_dashboard_config.py` — staged config, atomic history, rollback,
  hash, and redaction tests.
- `tests/test_dashboard_status.py` — JSONL safety and missing-stream tests.
- `tests/test_dashboard_preview.py` — Stage 4 preview and consequence tests.

## C. Files modified

- `pyproject.toml` — added the optional `dashboard` extra containing Streamlit.
- `README.md` — added the Stage 9 capability, launch path, and report links.

The existing Stage 1–8 strategy engines and the Condor routine were not changed
by Stage 9. Existing dirty-worktree changes remain preserved.

## D. Pages

1. **Overview** — TESTNET/read-only status, stream freshness, BTC/ETH/SOL/HYPE
   cards, global risk, portfolio utilization, and recent mode/plan information.
2. **Strategy** — existing IV/RV, relationship, direction, volatility, grid
   geometry, and center-shift controls.
3. **Risk** — collateral reserve, gross/beta, directional, inventory, drawdown,
   asset-limit, and allocation controls with consequences.
4. **Execution** — execution flags, post-only/leverage guardrails, one-level
   sizing, lifetime/cooldown/deadband controls, order-rule preview, and
   historical stability estimate. It has no order action.
5. **Assets** — ETH, SOL, and HYPE enablement and existing independent asset
   limits; BTC remains signal-only.
6. **Config History** — redacted snapshots, hashes, changed fields, operator
   notes, and rollback.
7. **Advanced** — local paths, stream statuses, redacted exports, reload boundary,
   mode explanation, and security statement.

## E. Configuration source of truth

The dashboard reads:

- `configs/competition_800_usdc.yml` for competition risk, execution guards,
  portfolio/asset limits, and refresh deadbands.
- `configs/stage9_strategy.yml` for the typed Stage 8 strategy overlay.

The overlay is a dashboard/review source. Because the current Condor monitor
still builds its strategy configuration in Python at startup, this implementation
does not claim that the monitor consumes the overlay without a supported runtime
integration/restart.

The committed safety values remain `market_environment: testnet`,
`connector_name: derive_perpetual_testnet`, `allow_mainnet_trading: false`,
`execution_enabled: false`, `post_only: true`, and one level per side per asset.

## F. Hot reload versus restart

No hot reload is claimed. The status bar displays `Runtime config hash: UNKNOWN`
because the current monitor does not emit one. Applying a change updates local
files and returns `RESTART_REQUIRED`; it does not send a signal to Condor, restart
the process, or alter an executor.

## G. Validation

Typed Pydantic validation covers the competition profile and strategy overlay,
including testnet/mainnet guards, positive sizes, one-level bounds, allocation
consistency, reserve/risk relationships, and the existing Stage 8 configuration
conversion. Invalid staged data blocks Apply. Risk-increasing field changes are
flagged and require explicit acknowledgement.

## H. Diff workflow

Edits stay in Streamlit session state until Apply. The sidebar shows a field-level
diff with old value, proposed value, and `RESTART_REQUIRED` classification. The
dashboard provides deterministic consequence previews before the Apply button is
used. No form widget writes the source file directly.

## I. History and rollback

Apply creates a versioned redacted snapshot under `data/config_history/vNNNN.json`,
keeps a `.bak` copy for each replaced YAML file, and appends a hash/path-only event
to `data/config_change_events.jsonl`. Rollback loads a prior redacted version and
passes it through the normal validate/diff/apply path, creating a new version
instead of overwriting history.

## J. Grid preview

`dashboard/grid_preview.py` deep-copies the latest snapshot, state, and mode before
calling the existing pure `derive_options_mm.grid_engine.build_grid_plan`. It
supports current-versus-proposed parameter comparison and a synthetic inventory /
direction preview. It does not mutate Stage 4 state, persist a plan, create an
executor, or place an order.

## K. Portfolio consequence preview

The Risk page compares old and staged gross, beta-equivalent, and collateral
reference values. It also presents the existing drawdown ladder, collateral
reserve, asset caps, and allocation table. These are configuration consequences,
not account measurements and not authorization to trade.

## L. Churn estimate

When plan history exists, the dashboard compares consecutive recorded levels using
the configured price and amount deadbands and reports estimated `KEEP`, `REFRESH`,
`NEW`, and `REMOVED` counts. The result is explicitly labeled historical: it does
not predict future executor behavior, fills, queue position, or PnL.

## M. Secrets

The dashboard has no exchange/auth client and does not display `.env` files. The
redactor masks keys matching private key, API key/secret, password, token, auth,
and wallet-secret patterns before hashing, export, history, or display. No
credential values are included in this report or the committed dashboard files.

## N. Tests

The focused Stage 9 suite covers:

- staged edits do not mutate source files;
- invalid relationships block validation;
- atomic history, backups, append-only rollback, and version events;
- stable secret-free hashing and redaction;
- missing, malformed, partial, truncated, and rotated JSONL safety;
- degraded stream status;
- deterministic risk and historical stability previews;
- non-mutating invocation of the existing Stage 4 planner.
- dashboard script import bootstrap from an unrelated working directory.

The final repository verification is:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

Final result: `216 passed`; Ruff reported `All checks passed`; and `git diff
--check` was clean. Streamlit `AppTest` rendered Overview, Strategy, Risk,
Execution, Assets, Config History, and Advanced with zero exceptions/errors. A
Strategy form interaction produced the expected one-field pending diff without
writing the source YAML. The launched server returned HTTP 200 at
`http://localhost:8501`.

## O. Run command

```bash
cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
source .venv/bin/activate
python -m pip install -e '.[dev,dashboard]'
export CONDOR_DATA_DIR=/Users/wilfred/Documents/Hummingbot/condor/data
./scripts/run_dashboard.sh
```

Then open [http://localhost:8501](http://localhost:8501). The default data source
is `/Users/wilfred/Documents/Hummingbot/condor/data`, and it can be overridden by
`--data-dir` or `CONDOR_DATA_DIR`.

## P. Known limitations

- Current Condor runtime configuration is code-constructed and has no safe reload
  or runtime config hash; applied files require a separately reviewed restart
  path.
- The observed local data directory did not contain
  `derive_execution_events.jsonl`, so execution-event cards are unavailable until
  that stream exists.
- The dashboard remains a local analytics/control surface. It does not prove
  Derive fills, PnL, inventory feedback, queue position, or live execution.
- Mainnet is not enabled or supported by this Stage 9 change.
- The rollout remains one level per side; no depth increase was made.
