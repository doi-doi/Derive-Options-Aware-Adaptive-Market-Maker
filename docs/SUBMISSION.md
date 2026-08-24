# Submission package

## Project title

**Derive Adaptive State Grid**

## Short description

An interpretable, options-aware adaptive grid for Derive perpetuals. Derive ATM IV,
realized volatility, direction, and inventory become a versioned grid plan that
controls width, center, density, allocation, and size. Hummingbot V2 owns the
post-only maker lifecycle and native adjacent-grid exits.

## What is novel or useful

- Options IV is integrated as a forward-looking risk-geometry input rather than a
  directional prediction.
- The strategy is split into auditable state, mode, and plan contracts.
- Inventory and volatility can reduce participation, widen quotes, or pause new
  entries without forcing liquidation of filled positions.
- Execution is reconciled by logical level, so KEEP, cancel/replace, and duplicate
  prevention are observable behaviors instead of hidden side effects.
- The live rollout is bounded and honest: one level per side on Derive testnet,
  post-only, testnet guard, and no mainnet path.

## Evidence summary

| Evidence class | Included proof |
| --- | --- |
| LIVE TESTNET | Authenticated Hummingbot submission; real Derive IDs; passive post-only entries; KEEP; no duplicates; cancel/replace; DEFENSIVE; PAUSE/recovery; cleanup |
| RECORDED | Stage 1--4 JSONL lineage, state/mode/plan streams, IV coverage and staleness, mode transitions, controller behavior |
| OFFLINE REPLAY | Static/RV/IV comparison, IV ablation, fee sensitivity, markouts, inventory and position accounting, no-lookahead audit |
| DETERMINISTIC SIMULATION | Hummingbot `PositionExecutor` fill-dependent state and adjacent-grid exit contract |
| NOT OBSERVED LIVE | Natural maker fill, live position feedback, live take-profit, realized PnL, queue quality, profitability |

## Evaluation headline

On the latest conservative replay window, static geometric total PnL was +2.06225,
RV-only was -25.28121, and IV-aware was -23.48627. IV-aware modestly improved the
RV-only adaptive result and changed grid geometry, but static was best on this
sample. This project does not claim that IV-aware adaptation beats static or that
the strategy is deployable.

## Demo and judging order

1. Run [the safe demo](DEMO.md).
2. Show the architecture and compact strategy diagram in the [README](../README.md).
3. Open [the live evidence matrix](LIVE_EVIDENCE.md).
4. Open the IV contribution and cumulative PnL charts.
5. Explain the static/RV/IV table and its limitations.
6. Finish with [the reproduction commands](REPRODUCE.md) and the one-level testnet
   boundary.

## Files to submit or link

- `README.md`
- `docs/DEMO.md`
- `docs/JUDGE_WALKTHROUGH.md`
- `docs/PITCH.md`
- `docs/REPRODUCE.md`
- `docs/FAQ.md`
- `docs/LIVE_EVIDENCE.md`
- `docs/PRESENTATION_OUTLINE.md`
- `reports/stage5_execution.md`
- `reports/stage6_5_validation.md`
- `reports/stage6_5/validated_claims.md`
- `reports/stage6_5/iv_ablation.csv`
- `reports/stage6_5/audit_summary.json`
- `reports/stage6_5/charts/`

## Safety and disclosure

The example config has `execution_enabled=false`, `allow_mainnet_trading=false`,
leverage 1, post-only enabled, and one live level per side. Credentials are not part
of the repository. The live testnet run was stopped after safe order lifecycle proof;
the absence of a live fill is disclosed rather than filled with a synthetic claim.

## Final handoff checklist

- [x] README answers what, why, how, evidence, and limitations.
- [x] Mermaid architecture and strategy diagrams render as source diagrams.
- [x] Demo is read-only and uses current JSONL/report values.
- [x] Live and simulated evidence are separated in every handoff document.
- [x] Latest Stage 6.5 machine outputs are the source of evaluation numbers.
- [x] Reproduction commands include tests, lint, and diff hygiene.
- [x] No mainnet enablement or live-depth increase is included.
- [ ] Live maker fill and live feedback loop remain pending authorization and access.
