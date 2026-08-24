# Stage 6.5 Validation: Stage 6 Audit and Robustness

## 1. Audit verdict

**conditionally_presentable**. The audit keeps the Stage 6 strategy and Stage 5 live controller unchanged. It finds a timestamp-conflicted plan stream that requires an explicit canonical view, confirms no future as-of input selection, and retains the limits of BBO-only replay. No parameter optimization, mainnet action, or live execution change was performed.

Common window: `2026-08-24T04:00:11.875Z` to `2026-08-24T09:34:57.372Z`; canonical complete frames: `4017`.

## 2. Data deduplication

| raw_record_count | canonical_record_count | duplicate_timestamp_count | exact_duplicate_record_count | duplicate_plan_version_count | conflicting_timestamp_count | controlled_record_count |
|---|---|---|---|---|---|---|
| 8098 | 4017 | 4081 | 270 | 7010 | 3252 | 0 |

The duplicate timestamp count is **extra rows beyond the first row in each timestamp group**, not a count of unique timestamps. Exact duplicates are structurally identical JSON objects after sorted-key canonicalization. Repeated `plan_version` values are a separate diagnostic: Stage 4 intentionally keeps a plan version across insignificant updates, so repeated versions are not independently treated as data corruption. Conflicting timestamp groups are retained in `deduplication_report.json`; the canonical rule selects the last production source row after excluding explicit validation-only markers.

## 3. Dataset contamination

no explicit controlled validation markers were present in the canonical Condor plan file

The canonical Condor plan rows contained `0` explicit controlled markers. Isolated validation artifacts, where supplied, are listed separately and are not merged into the production stream. Original JSONL files were not deleted or rewritten.

External validation artifacts:

| path | exists | records | controlled_records | validation_stages |
|---|---|---|---|---|
| /Users/wilfred/Documents/Hummingbot/condor/data/stage5e-feedback-20260824/derive_grid_plans.jsonl | True | 43 | 2 | {'unspecified': 41, 'stage5e': 2} |

## 4. IV freshness

Snapshot ATM-IV coverage was `32.874%`; state ATM-IV coverage was `93.366%`; common-window state coverage was `99.975%`. As-of carried IV age summary: `{'count': 4016, 'mean': 0.0, 'median': 0.0, 'p90': 0.0, 'p95': 0.0, 'maximum': 0.0, 'minimum': 0.0, 'stdev': 0.0}`. The state stream can carry a prior state observation into a later plan frame, so raw snapshot coverage and frame-level state coverage are different; the report does not call carried observations fresh without an age rule.

Freshness sensitivity:

| threshold_seconds | fresh_iv_frames | stale_iv_frames | missing_iv_frames | rv_fallback_frames | entry_fills | total_pnl | maximum_drawdown |
|---|---|---|---|---|---|---|---|
| 30 | 4016 | 0 | 1 | 0 | 21 | -23.48627 | 40.77134 |
| 60 | 4016 | 0 | 1 | 0 | 21 | -23.48627 | 40.77134 |
| 120 | 4016 | 0 | 1 | 0 | 21 | -23.48627 | 40.77134 |
| 300 | 4016 | 0 | 1 | 0 | 21 | -23.48627 | 40.77134 |

## 5. Volatility decomposition

The Stage 2 score is audited as the weight-renormalized combination of `realized_volatility_ratio` and `iv_ratio`. The Stage 4 width formula is checked against each recorded plan's own volatility and mode multipliers, then separately compared with the as-of state/mode inputs selected at that timestamp. Maximum score formula error: `0.0`. Maximum recorded-plan width formula error: `6.938893903907228e-18`. Score formula pass: `True`; recorded-plan width formula pass: `True`. The maximum as-of input width mismatch is `0.013538399160843269` across `1498` frames; these mismatches are retained as timestamp-join/conflict evidence rather than hidden.

Mean absolute RV contribution: `0.7617026027455229`. Mean absolute IV contribution: `0.24973573125501133`. Contribution-series variance shares: RV `0.9999719469089059`, IV `2.805309109415981e-05`.

## 6. Options counterfactual impact

The counterfactual holds the recorded frame inputs constant and compares a stateless full-IV candidate with the same candidate after removing IV. It is not a retuned strategy and does not re-fit thresholds.

| metric | count | mean | median | p90 | maximum |
|---|---|---|---|---|---|
| score | 4016 | -0.004165136326829636 | 0.03356403676471431 | 0.14985250238111186 | 0.25092403107250255 |
| grid_width | 4017 | -0.00024653820644640183 | 0.0 | 0.00045892153676707284 | 0.03 |
| capital | 4017 | 7.0948469006721435 | 0.0 | 0.0 | 500.0 |
| level_count | 4017 | 0.070201643017177 | 0.0 | 0.0 | 6.0 |

Frames with candidate mode changes: `175`. Frames with grid-width changes greater than 5%: `1306`. Frames with level-count changes: `175`.

## 7. IV regime label audit

`relative_iv_bucket` uses low `< 0.90`, normal `0.90–1.10`, and high `> 1.10`. `rv_iv_joint_bucket` uses the separate boundary `1.0` for each RV/IV component. The two labels are intentionally distinct and are not interchangeable.

Boundary consistency pass: `True`. Observed frame buckets: `{'normal': 4016, 'unknown': 1}`.

## 8. Look-ahead audit

The as-of audit found `0` future-input violations. The replay fill path requires a future snapshot timestamp strictly greater than order creation; same-timestamp evidence is rejected. Result: **True**.

## 9. Fill-model audit

| strategy | conservative_fill_count | touch_fill_count | fills_satisfying_both_models | touch_only_fills | conservative_only_fills | touch_is_distinct |
|---|---|---|---|---|---|---|
| static_geometric_grid | 25 | 25 | 25 | 0 | 0 | True |
| rv_only_adaptive_grid | 32 | 32 | 32 | 0 | 0 | True |
| iv_adaptive_grid | 38 | 34 | 28 | 6 | 10 | True |

The models are distinct: conservative BUY/SELL use strict cross-through inequalities, while touch uses inclusive inequalities. A touch-only fill is therefore valid sensitivity evidence, not evidence that the conservative condition was accidentally reused.

## 10. Baseline fairness

all strategy variants share capital, scale, minimums, tick, fee, exposure, fill model, TP lifecycle, lifetime, timestamps, and reconciliation code; intended differences are geometry and adaptive state use

The static baseline recenters each replay tick around the current reference, but it goes through the same Stage 5-equivalent KEEP/refresh/cancel, order lifetime, scale, minimum, exposure, fee, fill, and TP path. Its intended difference is fixed Stage 4 base width, five levels per side, and 50/50 allocation without adaptive state logic.

## 11. PnL decomposition

| strategy | entry_fills | completed_grid_cycles | gross_realized_pnl | fees | net_realized_pnl | unrealized_pnl_end | total_pnl | maximum_drawdown |
|---|---|---|---|---|---|---|---|---|
| iv_adaptive_grid | 21 | 17 | 7.83999 | 0.0 | 7.83999 | -31.32626 | -23.48627 | 40.77134 |
| rv_only_adaptive_grid | 18 | 14 | 6.09257 | 0.0 | 6.09257 | -31.37378 | -25.28121 | 41.40466 |
| static_geometric_grid | 13 | 12 | 5.53705 | 0.0 | 5.53705 | -3.4748 | 2.0622499999999997 | 12.01867 |

Formula: `total_pnl = realized_grid_capture_gross - fees + open_position_unrealized_pnl`. Positive realized capture alongside negative total PnL means the remaining open inventory was marked below its entry cost by the replay endpoint; it is not a contradiction. The default maker fee is 0 bps because no reliable local Derive fee schedule was supplied; fee sensitivity is hypothetical.

## 12. Inventory accounting

| strategy | ending_inventory_base | average_entry_cost | ending_mark_price | weighted_ledger_unrealized_pnl | recorded_unrealized_pnl | unrealized_model_difference | total_pnl_model_difference | position_accounting_total_pass | liquidation_at_end_hypothetical_total_pnl |
|---|---|---|---|---|---|---|---|---|---|
| iv_adaptive_grid | -0.0315 | 77379.78252654717325020715521 | 77457.5 | -2.448100413764042618474610885 | -31.32626 | 28.87815958623595738152538912 | 3.8E-25 | True | -23.50201999999999999999999962 |
| rv_only_adaptive_grid | -0.0315 | 77366.06367151059881532418771 | 77457.5 | -2.880244347416137317288087135 | -31.37378 | 28.49353565258386268271191286 | 7.4E-25 | True | -25.29695999999999999999999926 |
| static_geometric_grid | 0.0119 | 77091.9 | 77457.5 | 4.35064 | -3.47480 | 7.82544 | 3.2E-26 | True | 2.056299999999999999999999968 |

Liquidation-at-end values are a separate weighted-net hypothetical mark at the final touch and are not the default result. Long PnL has the correct positive sign when mark exceeds cost; short PnL has the reverse sign. The replay keeps filled positions as per-lot objects, while the independent audit ledger uses weighted-net crossing; their unrealized components can differ when opposite lots coexist. The total pre-fee PnL reconciliation is the invariant, and it passes in the machine output. Weighted-average crossing, additions, reductions, and zero-crossing are covered by focused `PositionLedger` tests; partial fills remain outside the Stage 6 replay model. Same-timestamp entry and adjacent-TP fills are aggregated when checking that inventory is visible before the next State -> Mode -> GridPlan call.

## 13. Replay lifecycle parity

The replay timeline audit pass is `True` across `6` strategy/fill-model runs. Sampled timelines are in `replay_timelines.json`. Stage 5 adjacent-grid TP parity pass is `True` with `48` samples. Inventory feedback pass is `True`. The TP comparison uses the same previous-level/center rule with the configured one-step multiplier.

## 14. Subperiod robustness

The common window is split into 30-minute and one-hour chronological windows. Results are descriptive only:

| window_label | window_index | strategy | entry_fills | completed_cycles | total_pnl | unrealized_pnl_end | maximum_drawdown | maximum_absolute_inventory_base |
|---|---|---|---|---|---|---|---|---|
| 30m | 0 | iv_adaptive_grid | 3 | 1 | -0.9266399999999999 | -1.38984 | 1.53664 | 0.0244 |
| 30m | 0 | rv_only_adaptive_grid | 4 | 1 | -0.8129 | -1.2761 | 1.53664 | 0.0244 |
| 30m | 0 | static_geometric_grid | 1 | 1 | 0.4632 | 0.0 | 0.2316 | 0.012 |
| 30m | 1 | iv_adaptive_grid | 8 | 3 | -8.78509 | -10.68136 | 10.41525 | 0.036 |
| 30m | 1 | rv_only_adaptive_grid | 7 | 2 | -9.44989 | -10.73584 | 10.82325 | 0.036 |
| 30m | 1 | static_geometric_grid | 3 | 1 | -6.22868 | -6.8724 | 11.39588 | 0.0361 |
| 30m | 2 | iv_adaptive_grid | 6 | 2 | -5.8735800000000005 | -7.15782 | 9.3226 | 0.029 |
| 30m | 2 | rv_only_adaptive_grid | 5 | 1 | -6.85675 | -7.31875 | 9.3226 | 0.029 |
| 30m | 2 | static_geometric_grid | 5 | 3 | -2.3076 | -3.6936 | 4.2816 | 0.024 |
| 30m | 3 | iv_adaptive_grid | 4 | 4 | 1.85666 | 0.0 | 1.6488 | 0.0121 |
| 30m | 3 | rv_only_adaptive_grid | 4 | 3 | -0.8065 | -2.20382 | 1.6488 | 0.012 |
| 30m | 3 | static_geometric_grid | 5 | 5 | 2.316 | 0.0 | 1.6488 | 0.012 |
| 30m | 4 | iv_adaptive_grid | 3 | 1 | -1.1844 | -1.6476 | 2.538 | 0.012 |
| 30m | 4 | rv_only_adaptive_grid | 4 | 2 | 0.4914 | -0.435 | 1.405 | 0.012 |
| 30m | 4 | static_geometric_grid | 5 | 3 | -0.6107999999999998 | -2.0016 | 2.4084 | 0.012 |
| 30m | 5 | iv_adaptive_grid | 7 | 4 | 1.0861299999999998 | -0.66288 | 1.06608 | 0.0288 |
| 30m | 5 | rv_only_adaptive_grid | 7 | 4 | 1.14373 | -0.60528 | 1.00848 | 0.0288 |
| 30m | 5 | static_geometric_grid | 2 | 1 | 0.5628 | 0.0996 | 1.2672 | 0.012 |

The full subperiod table is in `subperiod_results.csv`; it should be read before making any aggregate claim.

## 15. Scale and capital sensitivity

The scale comparison keeps the Stage 4 theoretical allocations unchanged and reports native scale `1.0` against the testnet-minimum-normalized scale `9.30`. Deployed notional is measured from simulated open position plus pending entry notional; capital percentages use the replay's fixed initial-capital assumption.

| order_scale | strategy | entry_creates | minimum_order_blocks | entry_fills | maximum_deployed_notional | maximum_deployed_capital_pct | total_pnl | maximum_drawdown |
|---|---|---|---|---|---|---|---|---|
| 1.0 | iv_adaptive_grid | 0 | 37242 | 0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 1.0 | rv_only_adaptive_grid | 0 | 36282 | 0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 1.0 | static_geometric_grid | 0 | 40160 | 0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 9.30 | iv_adaptive_grid | 759 | 2023 | 21 | 5615.96452 | 56.15964520000001 | -23.48627 | 40.77134 |
| 9.30 | rv_only_adaptive_grid | 815 | 2265 | 18 | 5616.3735 | 56.163734999999996 | -25.28121 | 41.40466 |
| 9.30 | static_geometric_grid | 687 | 0 | 13 | 5562.258 | 55.62258 | 2.0622499999999997 | 12.01867 |

## 16. Fee sensitivity

Fee values `['-1', '0', '1', '2']` are hypothetical maker rebates/fees, not a Derive schedule. Full results are in `fee_sensitivity.csv`.

## 17. Final static vs RV vs IV comparison

| strategy | entry_fills | completed_grid_cycles | net_realized_pnl | unrealized_pnl_end | total_pnl | maximum_drawdown | maximum_absolute_inventory_base | cancel_create_ratio |
|---|---|---|---|---|---|---|---|---|
| iv_adaptive_grid | 21 | 17 | 7.83999 | -31.32626 | -23.48627 | 40.77134 | 0.0436 | 0.9696969696969697 |
| rv_only_adaptive_grid | 18 | 14 | 6.09257 | -31.37378 | -25.28121 | 41.40466 | 0.0436 | 0.9754601226993865 |
| static_geometric_grid | 13 | 12 | 5.53705 | -3.4748 | 2.0622499999999997 | 12.01867 | 0.0361 | 0.975254730713246 |

## 18. What IV actually contributed

IV is measured as a geometry/state input, not a required winner. The central RV-only versus IV-aware table is in `iv_ablation.csv`; the per-frame score, width, capital, level, and candidate-mode counterfactual is in `counterfactual_impact.csv`. The honest conclusion is sample-specific: IV can materially change geometry while static may still have the better total PnL and drawdown on this short sample.

## 19. Validated hackathon claims

See `validated_claims.md`. The claims are split into proven live, proven recorded behavior, simulated replay, and not proven. No simulated PnL number is presented as live Derive performance.

## 20. Remaining limitations

| limitation |
|---|
| no raw public trade-by-trade stream was supplied; BBO fills are not queue-aware |
| partial fills are not modeled by Stage 6 ReplayEngine |
| maker fee schedule was not locally verified; fee sensitivity is hypothetical |
| the common window is short and is not a statistical validation sample |
| recorded plan timestamp conflicts are preserved in the conflict ledger; canonical selection is a deterministic audit view |
| ReplayEngine marks per-lot positions while PositionLedger is weighted-net; component unrealized marks can differ even when total pre-fee PnL reconciles |

## 21. Reproduction

```bash
cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
PYTHONPATH=src:. .venv/bin/python -m evaluation.run_stage6_5 \
  --market-snapshots /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_snapshots.jsonl \
  --states /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_states.jsonl \
  --modes /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_modes.jsonl \
  --plans /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_plans.jsonl \
  --validation-plans /Users/wilfred/Documents/Hummingbot/condor/data/stage5e-feedback-20260824/derive_grid_plans.jsonl \
  --output reports/stage6_5
```
