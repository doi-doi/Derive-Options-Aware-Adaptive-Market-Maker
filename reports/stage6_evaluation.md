# Stage 6 Evaluation, Replay, Baselines, and Hackathon Evidence

## 1. Executive summary

The deterministic evaluation joined 3,244 plan frames over 4.51 common hours. It measures the existing Stage 1–4 behavior and runs separate offline replays for a static geometric grid, RV-only adaptive grid, and full Derive options-aware adaptive grid.

No parameters were optimized. Simulated fills, PnL, TP exits, and inventory feedback are replay evidence only; they are not live Derive results.

## 2. Dataset

| stream | records | start | end | duration_h | median_interval_s | missing_iv_pct | data_invalid_pct | account_available_pct | trade_available_pct | duplicates | out_of_order |
|---|---|---|---|---|---|---|---|---|---|---|---|
| snapshots | 16295 | 2026-08-23T09:07:27.521Z | 2026-08-24T08:30:31.180Z | 23.384 | 5.002 | 70.309911 | 0.055232 | 99.987726 | 100.0 | 0 | 0 |
| states | 5196 | 2026-08-24T01:10:18.096Z | 2026-08-24T08:30:31.180Z | 7.337 | 5.001 | 7.621247 | None | None | None | 0 | 0 |
| modes | 3658 | 2026-08-24T03:11:41.150Z | 2026-08-24T08:30:31.180Z | 5.314 | 5.001 | None | None | None | None | 0 | 0 |
| plans | 6401 | 2026-08-24T04:00:11.875Z | 2026-08-24T08:30:31.180Z | 4.505 | 4.992 | None | None | None | None | 3157 | 0 |


Common evaluation window: `2026-08-24T04:00:11.875Z` to `2026-08-24T08:30:31.180Z`. Stage 1–4 as-of frames: `3,244`. Source warnings: `no raw trade stream supplied; BBO fill models are used`.

## 3. Methodology

Recorded behavior uses latest-at-or-before timestamp joins. No state, mode, plan, or option value is forward-filled from the future. Performance replay warms the existing deterministic State → Mode → GridPlan chain, starts with zero simulated inventory, and feeds simulated fills back into Stage 2 inventory before the next plan.

Shared replay assumptions: `{"amount_increment": "0.0001", "initial_capital": "10000", "maker_adverse_fill_buffer_bps": "0", "maker_fee_bps": "0", "markout_horizons_seconds": [5, 30, 60], "max_active_levels": 10, "max_side_position_notional": "5000", "max_total_position_notional": "10000", "maximum_order_lifetime_seconds": 600.0, "min_notional_size": "0", "min_order_size": "0.01", "minimum_order_lifetime_seconds": 30.0, "order_scale": "9.30", "price_increment": "0.1", "refresh_amount_tolerance_pct": "0.05", "refresh_price_tolerance_bps": "5"}`.
The 9.30x quote scale is a documented offline capacity normalization based on the observed 0.01 BTC testnet minimum; Stage 4 quote allocations are not modified.

## 4. Important limitations

- The collected snapshots do not include a raw public trade-by-trade execution stream; conservative and touch BBO models are therefore separated.
- Conservative BUY requires a future best ask strictly below the resting bid; conservative SELL requires a future best bid strictly above the resting offer.
- Touch replay is optimistic and may overstate queue fills. Neither BBO model proves a maker fill.
- Partial fills, queue priority, and adverse selection are not directly observed. Maker fee defaults to configurable 0 bps because no reliable local Derive fee schedule was supplied; gross and fee-adjusted values are both emitted.
- The canonical Condor streams are append-only and may change while routines are running; the manifest records hashes and read-time mutation warnings.

## 5. Strategy behavior

| value | records | record_percentage | time_percentage | duration_seconds |
|---|---|---|---|---|
| defensive | 504 | 13.778020776380536 | 13.172881716117276 | 2520.6349999904633 |
| long_bias | 16 | 0.4373974849644614 | 0.4183688038164205 | 80.05500030517578 |
| normal | 2950 | 80.64516129032258 | 81.49512796421267 | 15594.11799955368 |
| pause | 87 | 2.3783488244942594 | 2.2737877949307195 | 435.08999943733215 |
| short_bias | 101 | 2.761071623838163 | 2.6398337209229097 | 505.1330008506775 |


Mode transitions: `170`. Transition matrix: `{"defensive": {"defensive": 0, "long_bias": 0, "normal": 42, "pause": 3, "short_bias": 0}, "long_bias": {"defensive": 0, "long_bias": 0, "normal": 7, "pause": 0, "short_bias": 0}, "normal": {"defensive": 43, "long_bias": 7, "normal": 0, "pause": 3, "short_bias": 29}, "pause": {"defensive": 0, "long_bias": 0, "normal": 7, "pause": 0, "short_bias": 0}, "short_bias": {"defensive": 2, "long_bias": 0, "normal": 26, "pause": 1, "short_bias": 0}}`.

## 6. ATM IV effect

ATM IV coverage in the snapshot stream is `29.69%`; IV ratio versus volatility-score correlation is `-0.004343726030234725` and IV ratio versus grid-width correlation is `-0.025259011258640586`. These are associations, not causality.

| iv_regime | records | average_grid_width_pct | average_level_count_total | average_effective_quote_amount | mode_distribution |
|---|---|---|---|---|---|
| normal | 3243 | 0.011102239566099369 | 9.221708294788776 | 908.4181313598519 | {'defensive': 446, 'long_bias': 16, 'normal': 2631, 'pause': 73, 'short_bias': 77} |
| unknown | 1 | 0.0 | 0 | 0.0 | {'pause': 1} |


Exploratory IV lead/lag correlations (not significance-tested):

| horizon_seconds | observations | iv_change_vs_future_absolute_log_return_correlation | exploratory_only |
|---|---|---|---|
| 30 | 3236 | -0.007753923931247932 | True |
| 60 | 3230 | 0.02290297766560209 | True |
| 300 | 3182 | -0.002904419928473149 | True |


## 7. Mode analysis

| bucket | records | average_volatility_score | average_grid_width_pct | volatility_state_distribution | mode_distribution |
|---|---|---|---|---|---|
| rv_high_iv_high | 532 | 1.5359153681875368 | 0.01576253567259517 | {'normal': 296, 'high': 236} | {'normal': 289, 'defensive': 189, 'pause': 42, 'short_bias': 8, 'long_bias': 4} |
| rv_high_iv_low | 756 | 1.4213273256489312 | 0.015449384848854079 | {'high': 271, 'normal': 485} | {'defensive': 239, 'normal': 469, 'short_bias': 22, 'pause': 23, 'long_bias': 3} |
| rv_low_iv_high | 890 | 0.7183557176522192 | 0.008107950525565817 | {'normal': 890} | {'normal': 846, 'defensive': 9, 'short_bias': 27, 'pause': 6, 'long_bias': 2} |
| rv_low_iv_low | 1065 | 0.7342896015822553 | 0.008190688283147755 | {'normal': 1065} | {'normal': 1027, 'defensive': 9, 'short_bias': 20, 'pause': 2, 'long_bias': 7} |


Direction-score/center-shift correlation: `0.24200275685843867`; direction/buy-allocation: `0.03784251324020933`; direction/sell-allocation: `-0.040510397846199774`. Inventory-ratio/center-shift correlation: `None`. Directional states without a selected bias mode: `344`.

## 8. Grid geometry

Geometry rows and summary are machine-readable in `evaluation_summary.json`; the mean full width is `0.011098817174124614`.

## 9. Replay methodology

Each resting entry must exist before later BBO evidence can fill it. Filled entries remain occupied while their native adjacent-grid LIMIT_MAKER TP is managed. PAUSE cancels unfilled entries without forcing liquidation. Significant refreshes cancel first and defer replacement to a later replay tick.

## 10. Fill-model assumptions

Results are always shown separately as `conservative_cross_through` and `touch_optimistic`.

## 11–13. Static, RV-only, and full IV-adaptive strategies

The static baseline uses fixed Stage 4 base width, five geometric levels, and 50/50 allocation. The RV-only ablation uses the existing State → Mode → GridPlan architecture with only the ATM-IV weight removed. The full variant uses the existing options-aware configuration.

## 14. Performance comparison

| strategy | fill_model | entries | cycles | net_realized | total_pnl | max_dd | max_inventory | cancel_create |
|---|---|---|---|---|---|---|---|---|
| static_geometric_grid | conservative_cross_through | 12 | 10 | 4.71385 | -5.56655 | 12.01867 | 0.0361 | 0.974 |
| static_geometric_grid | touch_optimistic | 12 | 10 | 4.71385 | -5.56655 | 12.01867 | 0.0361 | 0.974 |
| rv_only_adaptive_grid | conservative_cross_through | 17 | 12 | 5.39561 | -14.92829 | 41.40466 | 0.0436 | 0.9696 |
| rv_only_adaptive_grid | touch_optimistic | 17 | 12 | 5.39561 | -14.92829 | 41.40466 | 0.0436 | 0.9696 |
| iv_adaptive_grid | conservative_cross_through | 21 | 16 | 7.45642 | -12.82601 | 40.77134 | 0.0436 | 0.9608 |
| iv_adaptive_grid | touch_optimistic | 19 | 14 | 6.59871 | -13.68372 | 40.77033 | 0.0436 | 0.9667 |


Mode-specific replay summaries are stored under `performance_by_entry_mode` in `evaluation_summary.json`; small samples should not be generalized.

## 15. Inventory and risk comparison

Synthetic inventory stress cases are parameter-response evidence, not performance. They use ratios from -1.00 to +1.00 without changing production history:

| inventory_ratio | mode | enabled | center_shift_bps | buy_allocation_pct | sell_allocation_pct |
|---|---|---|---|---|---|
| -1.0 | pause | False | 0.0 | 0.0 | 0.0 |
| -0.75 | defensive | True | 22.5 | 0.6875 | 0.3125 |
| -0.5 | defensive | True | 15.0 | 0.625 | 0.375 |
| -0.25 | defensive | True | 7.5 | 0.5625 | 0.4375 |
| 0.0 | defensive | True | 0.0 | 0.5 | 0.5 |
| 0.25 | defensive | True | -7.5 | 0.4375 | 0.5625 |
| 0.5 | defensive | True | -15.0 | 0.375 | 0.625 |
| 0.75 | defensive | True | -22.5 | 0.3125 | 0.6875 |
| 1.0 | pause | False | 0.0 | 0.0 | 0.0 |


## 16. Execution stability

Recorded plan queue estimate: `{"actions": {"keep": 27242, "new": 218, "refresh": 2446, "removed": 208}, "per_level": {"buy_0:keep": 2942, "buy_0:new": 7, "buy_0:refresh": 220, "buy_0:removed": 6, "buy_1:keep": 2926, "buy_1:new": 7, "buy_1:refresh": 236, "buy_1:removed": 6, "buy_2:keep": 2896, "buy_2:new": 7, "buy_2:refresh": 266, "buy_2:removed": 6, "buy_3:keep": 2468, "buy_3:new": 44, "buy_3:refresh": 211, "buy_3:removed": 43, "buy_4:keep": 2387, "buy_4:new": 44, "buy_4:refresh": 292, "buy_4:removed": 43, "sell_0:keep": 2942, "sell_0:new": 7, "sell_0:refresh": 220, "sell_0:removed": 6, "sell_1:keep": 2923, "sell_1:new": 7, "sell_1:refresh": 239, "sell_1:removed": 6, "sell_2:keep": 2897, "sell_2:new": 7, "sell_2:refresh": 265, "sell_2:removed": 6, "sell_3:keep": 2471, "sell_3:new": 44, "sell_3:refresh": 208, "sell_3:removed": 43, "sell_4:keep": 2390, "sell_4:new": 44, "sell_4:refresh": 289, "sell_4:removed": 43}, "rates": {"keep": 90.46290761771934, "new": 0.723915786677293, "refresh": 8.12246795510394, "removed": 0.6907086404994355}, "total_actions": 30114}`. This estimates KEEP/REFRESH/REMOVED/NEW using Stage 5 price, amount, mode, and 30-second lifetime thresholds.

## 17. Options ablation result

Compare `iv_adaptive_grid` minus `rv_only_adaptive_grid` in the machine-readable summaries for PnL, drawdown, inventory, markout, width, and fills. The result is a sample-specific ablation, not proof that IV causes performance.

## 18. Conclusions

This dataset supports an honest description of adaptive geometry, state/mode frequency, IV-conditioned width, quote stability, and replay sensitivity. It does not support a live profitability claim or a queue-aware execution claim.

## 19. What is LIVE evidence vs SIMULATED evidence

### LIVE DERIVE TESTNET EVIDENCE

Stage 5 separately proved authenticated testnet LIMIT_MAKER submission with real Derive/Hummingbot IDs, post-only status, passive placement, KEEP, cancel/replace, DEFENSIVE, PAUSE/recovery, and cleanup. The bounded window produced no public BTC-PERP trade and no authorized counterparty was available, so no live fill, TP, realized PnL, or live inventory feedback is claimed.

### SIMULATED / REPLAY EVIDENCE

This Stage 6 package produces BBO-model fills, adjacent-grid TP exits, simulated PnL, inventory feedback, markout, risk, churn, and static/RV/IV comparisons. Every such number is simulated/replay evidence and must not be described as live Derive PnL.

## 20. Reproduction commands

```bash
cd /Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
PYTHONPATH=src:. .venv/bin/python -m evaluation.run \
  --market-snapshots /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_snapshots.jsonl \
  --states /Users/wilfred/Documents/Hummingbot/condor/data/derive_market_states.jsonl \
  --modes /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_modes.jsonl \
  --plans /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_plans.jsonl \
  --output reports/stage6
```

Charts:

- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/01_mid_price_by_mode.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/02_atm_iv_and_ratio.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/03_volatility_and_width.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/04_direction_and_center_shift.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/05_allocations.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/06_mode_frequency.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/07_cumulative_simulated_pnl.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/08_inventory_by_strategy.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/09_drawdown_by_strategy.svg`
- `/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/reports/stage6/charts/10_iv_ratio_vs_grid_width.svg`

The prior Stage 5 live report remains the source for live execution evidence; this report deliberately keeps that boundary separate.
