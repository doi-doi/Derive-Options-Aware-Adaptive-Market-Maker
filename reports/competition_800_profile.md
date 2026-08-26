# Derive Adaptive State Grid — 48-Hour Competition Risk Profile

## Scope and safety

This is a configuration and risk-management deliverable for an approximately
800-USDC, 48-hour competition account. It does not redesign Stage 1–4 signal,
volatility, BTC ATM IV, direction scoring, mode rules, or geometric grid
construction. The committed profile remains `execution_enabled: false`,
`derive_perpetual_testnet`, `allow_mainnet_trading: false`, post-only, and one
level per side. No live order endpoint was called by the validation command.

## A. Configuration

| Setting | Value |
| --- | ---: |
| Starting equity reference | $800 |
| Collateral reserve | 20% ($160) |
| Leverage capability | 2x |
| Soft gross / hard gross | $900 / $1100 |
| Soft BTC-beta / hard BTC-beta | $600 / $800 |
| Per-asset hard limits | ETH $280 / SOL $280 / HYPE $220 |
| Target / max single order | $70 / $100 |
| Levels / active executors | 1 per side / 2 per asset |
| Portfolio active executors | 6 |
| New-risk creates per cycle | 2 |
| Directional allocation bound | 65/35 |

2x leverage provides margin flexibility; portfolio gross exposure remains
bounded separately.

## B. Current Derive testnet rules

Observed at the timestamps recorded in `reports/competition_800/dry_run.json`.

| Market | Minimum amount | Amount step | Price increment | Reference price |
| --- | ---: | ---: | ---: | ---: |
| BTC-USDC | 0.01 | 0.0001 | 0.1 | 78970 |
| ETH-USDC | 0.1 | 0.01 | 0.01 | 2468 |
| SOL-USDC | 1 | 0.1 | 0.01 | 74.065 |
| HYPE-USDC | 10 | 1 | 0.001 | 79.697177 |

## C. BTC signal-only decision

BTC continues to supply perpetual returns, correlation/beta reference, ATM IV,
IV ratio, and global risk state. Current BTC minimum order notional is above
$100, so BTC execution is disabled and remains signal-only.

## D. Minimum order notionals

| Market | Minimum valid amount | Minimum valid notional | Result |
| --- | ---: | ---: | --- |
| BTC-USDC | 0.01 | $789.7000 | BLOCKED | MIN_ORDER_EXCEEDS_BUDGET |
| ETH-USDC | 0.1 | $246.8000 | BLOCKED | MIN_ORDER_EXCEEDS_BUDGET |
| SOL-USDC | 1 | $74.0650 | ELIGIBLE | - |
| HYPE-USDC | 10 | $796.9718 | BLOCKED | MIN_ORDER_EXCEEDS_BUDGET |

ETH and HYPE also exceed this competition order budget at their current exchange
minimums; SOL fits at its current observed reference price. No scale was
increased to force an over-budget market.

## E. Collateral calculation

The authenticated read showed available collateral of
`$800.0000` and equity of
`$800.0000`. The 20% reserve is
`$160.0000`, leaving
`$640.0000` before leverage capacity is
considered. The hypothetical two-sided minimum-order margin requirement is
`$1117.8368`;
the reserve and gross/beta governors therefore remain independent safeguards.

## F. Portfolio risk limits

The governor counts signed positions plus pending buy/sell orders before every
candidate create. Missing/invalid BTC beta uses a conservative magnitude-1.0
fallback. Risk-reducing exits are allowed through the beta hard limit and do not
consume the new-risk capacity or two-create burst budget. Candidates are ordered
by risk reduction, position correction, state confidence, lower beta contribution,
then stable pair/level ordering.

Gross exposure is capped at $1100 with a $900 soft threshold; the portfolio may
hold at most six active executors, at most two per asset, and at most two new
risk-increasing creates per controller cycle.

## G. BTC-beta risk examples

The configured BTC-beta soft/hard limits are $600 / $800, with independent
$800 long and $800 short ceilings. Missing or invalid measured beta uses a
conservative magnitude-1.0 fallback. If every minimum-size candidate were
filled, the dry-run worst-case all-buy or all-sell beta magnitude would be
`$1117.8368`;
this is blocked by the portfolio limits before new exposure is created.

## H. Per-asset limits and inventory

ETH and SOL use $200 soft / $280 hard net-position notional. HYPE uses $160 soft
/ $220 hard. Inventory ratios are 0.50 soft, 0.70 defensive, and 0.90 hard;
hard inventory blocks only worsening new exposure while correction/exit orders
remain manageable. The defensive capital multiplier is 0.50 and does not
replace Stage 4 geometry.

## I. Drawdown ladder

Session equity is captured at profile start, not from historical account PnL.
The ladder is CAUTION at -$40, REDUCE at -$60, DEFENSIVE at -$80, and
HARD_STOP_NEW_RISK at -$100. Multipliers are 1.00, 0.80, 0.60, 0.40, and
0.00. The hard stop is latched until an explicit operator reset and never
market-liquidates; take-profits, exits, and risk-reducing maker orders remain
possible.

## J. Cancel/replace policy

The competition execution overrides are: 120-second minimum order lifetime,
60-second replacement cooldown, 12-bps price deadband, 15% amount deadband,
and 900-second maximum age. Mode labels alone do not cancel an order. PAUSE,
marketability/post-only safety, stale critical data, unavailable account state,
hard inventory/beta/drawdown, environment mismatch, and manual kill switch are
immediate safety exceptions. Replacement stops are issued before any later
replacement create so exposure cannot double.

## K. Dry-run candidates

Account source: `authenticated_local_hummingbot_api`; current available collateral
was `$800.0000` and equity was
`$800.0000`. The Hummingbot account had
`0` positions at read time.

| Market | Reference price | Minimum amount | Candidate notional | Status | Reason |
| --- | ---: | ---: | ---: | --- | --- |
| ETH-USDC | 2468 | 0.1 | $246.8000 | BLOCKED | MIN_ORDER_EXCEEDS_BUDGET |
| SOL-USDC | 74.065 | 1 | $74.0650 | ELIGIBLE | - |
| HYPE-USDC | 79.697177 | 10 | $796.9718 | BLOCKED | MIN_ORDER_EXCEEDS_BUDGET |

If every minimum-size buy and sell across the three target markets filled, the
gross notional would be approximately
`$2235.6735`;
the worst-case all-buy/all-sell beta magnitude under the fallback is
`$1117.8368`.
After the 20% reserve, usable collateral was
`$640.0000` and the two-sided minimum-order
margin requirement would be
`$1117.8368`.

## L. Stress-test results

| Scenario | Result |
| --- | --- |
| A_all_bullish | PASS |
| B_all_three_buy_entries_filled | PASS |
| C_portfolio_plus_700_beta_long | PASS |
| D_portfolio_minus_700_beta_short | PASS |
| E_drawdown_minus_65 | PASS |
| F_drawdown_minus_85 | PASS |
| G_drawdown_minus_101 | PASS |
| H_small_five_second_plan_movement | PASS |

Machine-readable full decisions are in `reports/competition_800/stress_results.json`.

## M. Offline replay — not live PnL

The Stage 8 deterministic replay was run with this exact profile as the
execution-side route. It is an offline BBO-crossing simulation, not live Derive
PnL, and it does not prove queue priority or profitability.

| Metric | Result |
| --- | ---: |
| Ticks | 140 |
| Maximum gross notional | 81.641608 |
| Maximum BTC-beta equivalent | 61.559515 |
| Maximum long / short beta | 61.559515 / 50.164878 |
| Portfolio drawdown | 0.654745 |
| Risk blocks | 395 |
| Simulated total PnL | -0.654745 |

The replay output is at `reports/competition_800/replay.json`. It is evidence
of deterministic routing and accounting only; no parameter was optimized from
the simulated PnL.

## N. Tests

The final repository validation completed after report generation:

- `pytest -q`: 205 passed
- focused profile, Stage 5, and Stage 8 tests: 54 passed
- `ruff check .`: PASS
- `git diff --check`: PASS

## O. Remaining risks

Exchange price-band behavior, queue position, partial fills, live account
changes, and the fact that BTC-beta is estimated rather than guaranteed remain
unresolved live-testnet risks. ETH/HYPE minimums currently exceed the $100
competition order budget, so they remain blocked until the rules or budget
change under explicit review.

## P. Exact activation steps

1. Review this report and the machine-readable rule/dry-run/stress/replay files.
2. Re-query Derive rules and authenticated account collateral immediately before
   any activation decision.
3. Review each market minimum against the $100 max; do not auto-fund or auto-scale
   BTC, ETH, or HYPE.
4. If later approved, create separate local runtime configuration with explicit
   operator acknowledgement. Do not change this committed profile and do not
   enable mainnet.

## Stop condition

This task stops here. The profile remains execution-disabled; no live execution
or mainnet activation is performed.
