# Stage 4 — Adaptive Grid Parameter Engine

Status: implemented and locally verified on 2026-08-24.

Stage 4 consumes the existing Stage 1 `MarketSnapshot`, Stage 2 `MarketState`,
and Stage 3 `GridModeDecision` JSONL boundaries. It produces theoretical
`GridPlan` records only. It does not place orders, create executors, cancel
orders, change leverage, or modify a live controller.

## Reused interfaces

- `integrations/condor/derive_market_snapshot.py::MarketSnapshot`
- `src/derive_options_mm/state_engine.py::MarketState`
- `src/derive_options_mm/mode_selector.py::GridModeDecision`
- `src/derive_options_mm/mode_selector.py::GridMode`
- Existing append-only JSONL and bounded-tail processing conventions

The installed Hummingbot grid controller shape was inspected for future
compatibility. Stage 4 intentionally remains independent of
`GridExecutorConfig` and controller/executor APIs.

## Files

Created:

- `src/derive_options_mm/grid_engine.py`
- `integrations/condor/derive_grid_plan.py`
- `tests/test_grid_engine.py`
- `tests/test_derive_grid_plan.py`
- `reports/stage4_grid_parameter_engine.md`

## Formulas

Reference price uses this hierarchy:

1. Explicit `microprice`, when positive and finite.
2. Derived microprice from Stage 1 quotes and sizes:
   `((ask * bid_size) + (bid * ask_size)) / (bid_size + ask_size)`.
3. Stage 1 `mid_price`.

Directional center adjustment is zero in `NORMAL` and `DEFENSIVE`. In a bias
mode:

`direction_shift_bps = clamp(direction_score, -1, +1) * 20 bps`.

Inventory adjustment applies in every active mode:

`inventory_shift_bps = -clamp(inventory_ratio, -1, +1) * 30 bps`.

The final shift is clamped to ±40 bps and the center is:

`center_price = reference_price * (1 + total_shift_bps / 10000)`.

Volatility width is continuous and clamped:

`volatility_width_multiplier = clamp(volatility_score, 0.75, 2.0)`.

The total grid width is:

`clamp(1.0% * volatility_width_multiplier * mode_width_multiplier, 0.4%, 3.0%)`.

The inner distance is:

`max(configured_min_inner_distance_bps, spread_bps / 2 + maker_safety_buffer_bps)`.

`DEFENSIVE` multiplies that distance by 1.5. The outer distance is half the
total grid width.

For `N > 1`, geometric distances are generated with:

`ratio = (outer_distance_pct / inner_distance_pct) ** (1 / (N - 1))`

`distance_i = inner_distance_pct * ratio ** i`.

Buy prices are `center * (1 - distance_i)` and sell prices are
`center * (1 + distance_i)`. Price calculations use `Decimal`; JSONL output
converts final finite values to JSON numbers.

## Mode profiles

| Mode | Width multiplier | Size multiplier | Levels/side | Initial bias |
|---|---:|---:|---:|---:|
| NORMAL | 1.0x | 1.0x | 5 | 0.00 |
| DEFENSIVE | 1.5x | 0.5x | 3 | 0.00 |
| LONG_BIAS | 1.0x | 1.0x | 5 | +0.20 |
| SHORT_BIAS | 1.0x | 1.0x | 5 | -0.20 |
| PAUSE | disabled | 0 | 0 | 0.00 |

The allocation overlay is:

`net_bias = clamp(mode_bias - inventory_ratio * 0.50, -0.50, +0.50)`

`buy_allocation = 0.50 + net_bias / 2`

`sell_allocation = 1 - buy_allocation`.

Both sides are clamped to 10%–90%. Inventory therefore overrides directional
intent when the two conflict.

## Plan change logic

`plan_change_significant` is true when mode, enabled/valid state, level count,
center movement, width, or buy allocation changes beyond configured
thresholds. `plan_version` increments only for a significant change; repeated
plans retain the previous version.

## Persistence and run command

Plans are appended to:

`/Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_plans.jsonl`

From the Condor working directory:

```bash
cd /Users/wilfred/Documents/Hummingbot/condor
PYTHONPATH=/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/src \
/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/.venv/bin/python \
/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/integrations/condor/derive_grid_plan.py
```

The same command is running in PyCharm's `Local (3)` terminal tab. It is
currently consuming the existing Stage 1–3 processes and writing valid plans.

## Verification

- Focused Stage 4 tests: 40 passed.
- Full repository tests: 123 passed.
- Ruff: all checks passed.
- `git diff --check`: passed.
- Live read-only join: produced valid NORMAL and DEFENSIVE plans.
- Live persistence: 14 plans observed in `derive_grid_plans.jsonl` at handoff.
- No order or executor API is imported or called.

## Sample observed plans

NORMAL produced 5 buy and 5 sell levels, 0.75% total width, 50%/50%
allocation, and 1000 quote units effective capital.

DEFENSIVE produced 3 buy and 3 sell levels, a 3.0% capped total width, 50%
capital, and 500 quote units effective capital.

Representative deterministic V1 profiles are:

| Mode | Center shift example | Width at volatility 1.0 | Levels | Effective quote | Allocation |
|---|---:|---:|---:|---:|---:|
| NORMAL | 0 bps | 1.0% | 5 / 5 | 1000 | 50% / 50% |
| DEFENSIVE | 0 bps | 1.5% | 3 / 3 | 500 | 50% / 50% |
| LONG_BIAS | +10 bps at direction score +0.5 | 1.0% | 5 / 5 | 1000 | 60% / 40% |
| SHORT_BIAS | -10 bps at direction score -0.5 | 1.0% | 5 / 5 | 1000 | 40% / 60% |
| PAUSE | disabled | none | 0 / 0 | 0 | none |

The LONG_BIAS and SHORT_BIAS allocations are before the continuous inventory
overlay. Inventory can reverse that weighting. PAUSE and all invalid-input
paths produce no theoretical levels.

Representative JSONL record shape:

```json
{
  "timestamp": "2026-08-24T04:00:16.873Z",
  "trading_pair": "BTC-USDC",
  "mode": "defensive",
  "enabled": true,
  "reference_price": 77019.30764671013,
  "center_price": 77019.30764671013,
  "center_shift_bps": 0.0,
  "total_grid_width_pct": 0.03,
  "inner_distance_bps": 7.5,
  "buy_levels_count": 3,
  "sell_levels_count": 3,
  "effective_quote_amount": 500.0,
  "buy_allocation_pct": 0.5,
  "sell_allocation_pct": 0.5,
  "buy_levels": [
    {"side": "buy", "level_index": 0, "theoretical_price": 76961.5431659751}
  ],
  "sell_levels": [
    {"side": "sell", "level_index": 0, "theoretical_price": 77077.07212744516}
  ],
  "valid": true,
  "plan_change_significant": false,
  "plan_version": 1
}
```

## Remaining limitations

- Stage 4 does not quantize prices to Derive tick size.
- Stage 4 does not enforce minimum order amount/notional, collateral,
  leverage, or post-only exchange rules; those belong to Stage 5.
- The runner joins the latest pair-matched records from the three JSONL
  streams; source timestamp alignment and execution refresh policy remain
  Stage 5 concerns.
- The illustrative defaults are not optimized and do not establish trading
  profitability or deployment readiness.
