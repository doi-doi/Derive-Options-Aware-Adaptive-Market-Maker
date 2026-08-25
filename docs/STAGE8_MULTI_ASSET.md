# Stage 8 multi-asset dry run

Stage 8 extends the BTC Adaptive State Grid to a testnet-only BTC/ETH/SOL/HYPE
research basket. It is an analytics and replay boundary:

- BTC ATM IV is collected once and represented as one shared `GlobalRiskState`.
- Each asset retains its own realized volatility, direction, inventory, mode, and
  versioned theoretical grid plan.
- Rolling synchronized BTC correlation, beta, residual volatility, and a bounded
  transmission coefficient annotate non-BTC assets.
- `PortfolioRiskGovernor` evaluates filled and pending exposure and preserves
  risk-reducing sides when a limit is reached.
- Pair-qualified level IDs (`ETH-USDC::buy_0`) prevent cross-market executor or
  routing collisions.
- Execution is disabled and mainnet is rejected by configuration validation.

## Two-asset Hummingbot controller

For a live-controller-compatible basket containing only BTC and HYPE, use the
separate `derive_adaptive_grid_portfolio` controller. It is hard-scoped to
`BTC-USDC` and `HYPE-USDC`, keeps `derive_perpetual_testnet`, leverage `1`,
post-only orders, and one level per side, and defaults to
`execution_enabled: false`. Its portfolio gate applies shared gross, beta,
collateral, executor, and per-asset limits before any create action.

The source and testnet template are:

```text
integrations/hummingbot/derive_adaptive_grid/derive_adaptive_grid_portfolio.py
integrations/hummingbot/derive_adaptive_grid/derive_adaptive_grid_portfolio_testnet.example.yml
```

The Hummingbot API checkout contains the same controller under
`bots/controllers/market_making/`; load it with controller name
`derive_adaptive_grid_portfolio`. This adapter is separate from the Condor
monitor below: the Condor monitor may still display the four-asset analytics
basket, while the two-asset Hummingbot controller ignores ETH and SOL. Do not
set `execution_enabled` to true until the existing one-level testnet canary
checks have been completed.

## Deterministic local demo

From the repository root:

```bash
source .venv/bin/activate
PYTHONPATH=src python tools/run_stage8_demo.py \
  --output reports/stage8 \
  --report reports/stage8_multi_asset.md \
  --count 140
```

The demo writes a global-risk summary, relationship statistics, per-asset state
statistics, BTC-IV ablation rows, portfolio-risk events, replay comparisons, and
a BTC regression summary under `reports/stage8/`. The relationship window
sensitivity output compares the configured 15m/30m/60m windows without selecting
one by replay PnL. The synthetic run is a behavior check, not a performance claim
or an exchange execution record.

## Condor read-only monitor

The Condor routine is:

```text
routines/derive_adaptive_grid_monitor.py
```

Its default `trading_pairs` are `BTC-USDC`, `ETH-USDC`, `SOL-USDC`, and
`HYPE-USDC`. It uses `derive_perpetual_testnet`, keeps `execution_enabled=false`
and `allow_mainnet_trading=false`, and writes the four existing bounded JSONL
streams with pair-scoped records. To run a one-pair compatibility view, configure
both `trading_pair` and `trading_pairs` to the same supported pair.

The Condor dashboard shows the shared BTC options state, each local asset state and
grid, rolling BTC relationship values, and portfolio governor blocks. It does not
submit, cancel, or create orders.

## Evaluation boundary

`evaluation/multi_asset_replay.py` uses a conservative BBO crossing model and only
allows an order created on an earlier tick to fill. It rejects out-of-order input,
records risk blocks and exposure metrics, and compares:

1. independent per-asset grids;
2. shared BTC IV with the portfolio governor;
3. local RV only with the portfolio governor.

These outputs demonstrate orchestration, accounting, and look-ahead controls. They
do not validate live fills, queue position, profitability, mainnet readiness, or a
production allocation.
