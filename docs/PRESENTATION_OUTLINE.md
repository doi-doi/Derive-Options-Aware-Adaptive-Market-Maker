# Five-minute presentation outline

## Slide 1 — Title and honest status (30 seconds)

**Derive Adaptive State Grid**

Options-aware adaptive market-making geometry for Derive perpetuals. Say immediately:
testnet lifecycle proof is one level per side; no natural live fill was observed.

## Slide 2 — The problem (35 seconds)

Static grids do not respond to changing volatility, option-implied risk, direction, or
inventory. A market maker needs a small number of interpretable controls that can
adapt without turning execution into an opaque prediction loop.

## Slide 3 — The state-to-plan pipeline (45 seconds)

Show the README Mermaid diagram. Walk through:

`MarketSnapshot -> MarketState -> GridModeDecision -> GridPlan -> PositionExecutor`.

Point out that ATM IV and account inventory enter before the plan, while Hummingbot
owns order lifecycle after the plan.

## Slide 4 — What IV changes (40 seconds)

Show the IV contribution chart. Mention 175 candidate-mode changes and 1,306
greater-than-5%-width changes across 4,017 canonical frames. Explain that RV still
dominates variance, so IV is treated as a risk-geometry input rather than alpha.

## Slide 5 — Safety modes and execution (45 seconds)

Show the safe demo. Explain NORMAL, DEFENSIVE, bias modes, and PAUSE. Then show that
the example config is dry-run, testnet-only, post-only, and capped at one level per
side.

## Slide 6 — Live testnet evidence (55 seconds)

Show the sanitized live evidence matrix and two real exchange IDs. Explain passive
placement, KEEP, duplicate prevention, cancel/replace, DEFENSIVE, PAUSE/recovery,
and cleanup. State that there was no natural maker fill.

## Slide 7 — Replay and ablation (45 seconds)

Show cumulative PnL and the table. State the result exactly: static +2.06225, RV-only
-25.28121, IV-aware -23.48627. IV-aware modestly improves RV-only but static wins on
this sample. This is not a profitability claim.

## Slide 8 — Close and next experiment (25 seconds)

The deliverable is an auditable state-to-execution framework with bounded testnet
proof. The next experiment is an explicitly authorized one-level testnet fill, then
position/inventory reconciliation. Mainnet and deeper live grids are out of scope.
