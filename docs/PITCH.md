# Hackathon pitch

## 30-second version

Derive Adaptive State Grid is an interpretable market-making system for Derive
perpetuals. It combines the perpetual book, realized volatility, direction,
inventory, and Derive ATM options implied volatility into a point-in-time state. That
state selects a mode and produces a versioned grid plan: width, center, density,
allocation, and size. Hummingbot V2 owns the order lifecycle and native adjacent-grid
exits. We proved the safety-critical one-level maker loop on Derive testnet, and we
audited static, RV-only, and IV-aware replay. The result is intentionally honest: IV
changes risk geometry, but this sample does not prove profitability or live fills.

## Two-minute version

Most grids are geometric constants. They keep quoting the same shape when realized
volatility rises, options reprice risk, or inventory becomes unbalanced. That is the
problem we targeted.

Our system builds a point-in-time `MarketSnapshot` from Derive perpetual data, ATM
options IV, and account state. The next layer derives realized-volatility and IV
ratios, direction features, and inventory risk. A small, interpretable governor turns
those signals into `NORMAL`, `DEFENSIVE`, directional-bias, or `PAUSE`. The final
`GridPlan` changes width, center, number of levels, side allocation, and quote size.
Execution is deliberately outside that logic: Hummingbot V2 `PositionExecutor`
instances reconcile logical grid levels, keep eligible makers, cancel stale entries,
and create native adjacent-grid maker exits after a fill.

Options IV is not a direction oracle here. It is a forward-looking volatility and risk
geometry input. In the latest 4,017-frame audit, it changed candidate mode 175 times
and changed width by more than 5% in 1,306 frames. RV still explained most combined
score variance, which is why the project does not overstate the options contribution.

We also separated proof classes. On Derive testnet we authenticated through
Hummingbot, placed one passive post-only maker entry per side with real exchange IDs,
and verified KEEP, duplicate prevention, safe cancel/replace, DEFENSIVE, PAUSE,
recovery, and cleanup. No natural maker fill occurred, so we do not claim live PnL,
live take-profit, or live inventory feedback. A separate deterministic Hummingbot
simulation verifies the fill-dependent executor contract.

The latest conservative replay is not a victory lap: static total PnL is +2.06225,
RV-only is -25.28121, and IV-aware is -23.48627. IV-aware improves the adaptive
comparison modestly, while static wins on this short sample. The deliverable is a
reproducible, auditable state-to-execution framework with a bounded testnet proof—not
a claim of deployable alpha.
