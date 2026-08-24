# Reproduce the package

All commands below run from the repository root. Stages 1--6.5 use local JSONL data
and do not contact an exchange.

## 1. Install the research environment

```bash
cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export CONDOR_DATA_DIR=/Users/wilfred/Documents/Hummingbot/condor/data
```

The input directory should contain:

```text
derive_market_snapshots.jsonl
derive_market_states.jsonl
derive_grid_modes.jsonl
derive_grid_plans.jsonl
```

The current Condor directory is outside this repository on purpose. It is a runtime
data source, not a place to commit credentials or generated private state.

## 2. Run the safe demo

```bash
./scripts/demo.sh
```

This only reads local files. It is the recommended first command for a judge.

## 3. Run Stage 6 replay

```bash
python -m evaluation.run \
  --market-snapshots "$CONDOR_DATA_DIR/derive_market_snapshots.jsonl" \
  --states "$CONDOR_DATA_DIR/derive_market_states.jsonl" \
  --modes "$CONDOR_DATA_DIR/derive_grid_modes.jsonl" \
  --plans "$CONDOR_DATA_DIR/derive_grid_plans.jsonl" \
  --trading-pair BTC-USDC \
  --order-scale 9.30 \
  --maker-fee-bps 0 \
  --maker-adverse-fill-buffer-bps 0 \
  --output reports/stage6 \
  --report reports/stage6_evaluation.md
```

The replay runs the static, RV-only, and IV-aware comparisons under the selected
fill-model outputs. It does not use live order APIs.

## 4. Run the Stage 6.5 audit

The audit can run from only the four streams above:

```bash
python -m evaluation.run_stage6_5 \
  --market-snapshots "$CONDOR_DATA_DIR/derive_market_snapshots.jsonl" \
  --states "$CONDOR_DATA_DIR/derive_market_states.jsonl" \
  --modes "$CONDOR_DATA_DIR/derive_grid_modes.jsonl" \
  --plans "$CONDOR_DATA_DIR/derive_grid_plans.jsonl" \
  --trading-pair BTC-USDC \
  --output reports/stage6_5 \
  --report reports/stage6_5_validation.md
```

To reproduce the latest isolated Stage 5 validation-plan comparison when that
artifact is available, add it explicitly. It is kept outside the production plan
stream and is not silently merged:

```bash
STAGE5E_PLANS="$CONDOR_DATA_DIR/stage5e-feedback-20260824/derive_grid_plans.jsonl"
python -m evaluation.run_stage6_5 \
  --market-snapshots "$CONDOR_DATA_DIR/derive_market_snapshots.jsonl" \
  --states "$CONDOR_DATA_DIR/derive_market_states.jsonl" \
  --modes "$CONDOR_DATA_DIR/derive_grid_modes.jsonl" \
  --plans "$CONDOR_DATA_DIR/derive_grid_plans.jsonl" \
  --validation-plans "$STAGE5E_PLANS" \
  --trading-pair BTC-USDC \
  --output reports/stage6_5 \
  --report reports/stage6_5_validation.md
```

## 5. Optional Stage 5 dry-run / lifecycle check

The pure Stage 5 execution logic is covered by the host test suite. The
fill-dependent `PositionExecutor` simulation imports the installed Hummingbot
runtime, so it must be run inside the pinned Hummingbot API/Docker container with
this repository mounted; the research virtualenv intentionally does not vendor the
Hummingbot package:

```bash
# Run inside the installed Hummingbot API container, with REPO mounted at /repo.
PYTHONPATH=/repo/integrations/hummingbot:/repo \
  python /repo/tools/simulate_stage5f_lifecycle.py
```

It makes zero exchange calls and is useful for the simulated `OrderFilledEvent` /
`PositionExecutor` contract. It is not a substitute for a live Derive fill. The
full controller-level dry-run and container evidence are documented in
[reports/stage5_execution.md](../reports/stage5_execution.md).

## 6. Run repository checks

```bash
pytest -q
ruff check .
git diff --check
```

The generated report directories are intentionally retained for review. Do not
discard an audit or overwrite a live evidence log merely to make a summary look
cleaner.

## 7. Stage 5 boundary

The Hummingbot controller and its testnet example are in
`integrations/hummingbot/derive_adaptive_grid/`. The example is fail-closed with
`execution_enabled=false`, `allow_mainnet_trading=false`, `post_only=true`, and one
level per side. A live run needs separately authorized testnet credentials, a running
Hummingbot/Docker API, and a readback of collateral, hard position limits, order IDs,
post-only status, and cleanup. Follow the evidence boundary in
[LIVE_EVIDENCE.md](LIVE_EVIDENCE.md) and [reports/stage5_execution.md](../reports/stage5_execution.md).

No mainnet command is provided by this package. Never place credentials in a report,
README, shell history, or committed `.env` file.
