# FAQ

## Is this a profitable live bot?

No claim is made. The live testnet run proved safe one-level maker order lifecycle
behavior, but did not observe a natural maker fill. PnL numbers are offline replay
outputs.

## What does options IV do?

ATM IV contributes to the volatility score through an IV ratio. It can change the
grid's width, mode, density, and sizing. It is a forward-looking volatility/risk
input, not a directional forecast.

## Does IV beat realized volatility?

Not established. In the current audit, IV-aware modestly improved RV-only replay, but
RV still dominated score variance and static geometric had the best total PnL on the
sample.

## Why use `BTC-USDC` if Derive calls it `BTC-PERP`?

The installed Hummingbot connector maps Derive's BTC perpetual instrument to the
Hummingbot trading pair `BTC-USDC`. The mapping is documented and verified in the
Stage 5 evidence.

## Does PAUSE liquidate a filled position?

No. PAUSE cancels unfilled entry orders and stops new entries. Filled PositionExecutor
instances remain managed; the example configuration does not force liquidation.

## How are duplicate entries prevented?

The controller reconciles existing executors by logical grid level and lifecycle
state. An eligible live entry is kept; a stale unfilled entry is cancelled before a
replacement is created. A filled executor is not treated as a missing entry.

## Why is the live rollout one level per side?

It minimizes risk while validating connector semantics, order IDs, passive placement,
KEEP, replacement, pause/recovery, and cleanup. The package does not increase live
depth to 2 or 5 levels.

## Where is the dashboard?

Run `./scripts/run_dashboard.sh` and open `http://localhost:8501`. The local
dashboard reads Condor streams and local YAML only; it does not place or cancel
orders. Its Environment page can stage a testnet or mainnet read-only profile,
but applied changes require the normal Hummingbot controller restart.

## Can I run this against mainnet?

The dashboard can stage the Derive mainnet connector for read-only connectivity
review and generates a fail-closed controller artifact. It does not authorize
mainnet trading: execution and `allow_mainnet_trading` remain off, and a real
mainnet canary still requires the separate controller gates, account verification,
risk budgets, and explicit human authorization.

## Are the fill and take-profit tests live?

No. The fill-dependent path is covered by deterministic local simulation using a real
Hummingbot `PositionExecutor` configuration and an injected event. It is labeled
OFFLINE SIMULATION.

## Are credentials stored here?

No. Use the local Hummingbot API or protected environment configuration. Never commit
`.env` content, keys, private wallet material, or authorization headers.
