# Stage 6.5 validated claims

## PROVEN LIVE

- Derive testnet `LIMIT_MAKER` submission, post-only behavior, and authenticated order IDs are documented in the separate Stage 5 evidence.
- Stage 5 testnet cancel/replace, KEEP, PAUSE, recovery, and one-level safety gates are documented in the separate Stage 5 evidence.

## PROVEN FROM RECORDED BEHAVIOR

- Stage 1--4 JSONL streams contain measurable mode frequencies, transitions, grid widths, allocations, and plan lifecycle actions.
- The Stage 6.5 audit records duplicate timestamps, exact duplicate rows, repeated plan versions, and conflicting timestamp records without deleting the source file.
- IV materially changes the Stage 2 combined volatility score and/or Stage 4 geometry only to the extent shown in the machine-readable counterfactual output; it is not assumed to improve PnL.

## SIMULATED / REPLAY

- Static, RV-only, and IV-aware PnL, drawdown, markout, inventory, TP cycles, fee sensitivity, and subperiod results are offline BBO-model replay outputs.
- Simulated inventory is fed back before the next replay State -> Mode -> GridPlan decision.
- Adjacent-grid TP lifecycle and position-accounting invariants are tested offline.

## NOT PROVEN

- Live profitability, live maker fill quality, queue position, partial-fill behavior, or live TP fills.
- Any mainnet behavior or production capital safety beyond the documented testnet gates.
- Statistical significance or out-of-sample robustness from the short common history.
