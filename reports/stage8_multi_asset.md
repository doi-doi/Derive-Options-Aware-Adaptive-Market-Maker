# Stage 8 — Multi-Asset Shared BTC-Options Risk and Portfolio Grid Risk

## 1. Executive summary

This offline dry run routes BTC-USDC, ETH-USDC, SOL-USDC, and HYPE-USDC through
one shared BTC ATM IV state, local asset state engines, independent grid plans,
and a portfolio risk governor. Execution remains disabled and no exchange order
endpoint is contacted.

## 2. Architecture

`GlobalRiskState(BTC options)` -> `BTCTransmissionState(asset)` -> local
`MarketState` -> local `GridModeDecision` -> local `GridPlan` -> portfolio
`PortfolioRiskDecision` -> pair-scoped dry-run routing.

## 3. Shared BTC options risk

BTC ATM IV is fetched/processed once per common clock tick. Non-BTC assets do
not receive copied absolute IV; they receive the relative BTC IV ratio scaled by
their measured BTC relationship.

## 4. Asset-local state

Each asset retains its own book imbalance, OFI, returns, inventory, data quality,
direction, mode, and grid geometry. Direction does not use BTC IV or BTC
direction.

## 5. BTC correlation/beta

| Pair | Last correlation | Last beta | Transmission | Observations |
| --- | ---: | ---: | ---: | ---: |
| BTC-USDC | 1.0 | 1.0 | 1.0 | 0 |
| ETH-USDC | 0.8424127368165728 | 0.7458990155658781 | 0.6283548310916388 | 139 |
| SOL-USDC | 0.7063662621072867 | 1.2675650379734107 | 0.895365177851159 | 139 |
| HYPE-USDC | -0.07696582553859276 | -0.18031155022714854 | 0.01387782731737592 | 139 |

The relationship engine uses synchronized log returns, minimum observations,
zero-variance guards, staleness checks, and a documented beta clip of
`+/-3.0`. The 15m/30m/60m sensitivity windows are
reported by the engine and are not selected by PnL.

## 6. Transmission formula

For non-BTC assets:

`transmission = min(transmission_max, confidence * abs(correlation) * abs(clipped_beta))`.

The coefficient is bounded and correlation sign is diagnostic only; negative
correlation does not invert volatility. The transmitted component is
`btc_iv_ratio * transmission`.

## 7. Portfolio risk governor

The governor includes filled signed positions, pending entry notional, gross
notional, net notional, beta-equivalent exposure, long/short beta exposure, and
per-asset limits. It blocks exposure-increasing sides while allowing
risk-reducing sides and filled-position management.

Latest dry-run portfolio state: gross `83.245911`;
beta-equivalent `40.730281`;
blocked pairs `BTC-USDC, ETH-USDC, HYPE-USDC, SOL-USDC`.

## 8. Hierarchical PAUSE behavior

Missing asset snapshots disable that asset only. Missing or stale BTC IV defaults
to local-RV-only with reduced confidence. A configured `pause` fallback can make
the affected asset invalid. Portfolio limits block worsening sides without
force-liquidating filled positions.

## 9. Per-asset GridPlans

| Pair | Mean local RV ratio | Mean transmitted BTC IV | Mean width | Defensive frames |
| --- | ---: | ---: | ---: | ---: |
| BTC-USDC | 0.9756010721613897 | None | 0.009634545695359107 | 16 |
| ETH-USDC | 0.951908225654142 | 0.5550978678409852 | 0.00929204625904668 | 17 |
| SOL-USDC | 0.9942210022231701 | 0.7616132926354691 | 0.009099589422126901 | 13 |
| HYPE-USDC | 0.9964758698425431 | 0.010999642629536047 | 0.006594440371510871 | 0 |

Plan versions are maintained by one `GridParameterEngine` per pair.

## 10. Execution routing

Dry-run level keys are pair-qualified, for example `BTC-USDC::buy_0` and
`ETH-USDC::buy_0`. The existing Hummingbot adapter still uses one controller
instance per pair and rejects unsupported symbols; it remains the execution
boundary and is not started by this demo.

## 11. BTC regression compatibility

`121` valid frames compared; state mismatches
`0`, mode mismatches `0`,
plan mismatches `0` at tolerance
`1e-12`. Result: **PASS**.

## 12–14. BTC + ETH, BTC + ETH + SOL, and full four-asset dry run

The same common-clock coordinator supports staged enablement through
`MultiAssetConfig.enabled_markets`. This run used all four configured markets;
unavailable markets would be disabled independently.

## 15–17. Multi-asset evaluation, IV ablation, and portfolio-governor ablation

| Scenario | Portfolio PnL | Max drawdown | Risk blocks |
| --- | ---: | ---: | ---: |
| independent_per_asset_grids | -0.782046 | 1.484239 | 0 |
| shared_btc_iv_with_portfolio_governor | -0.072986 | 0.261100 | 3375 |
| local_rv_only_with_portfolio_governor | -0.108114 | 0.261100 | 3365 |

These metrics are deterministic replay diagnostics, not performance claims.
The ablation changes only the BTC-IV input or governor limits; it does not tune
parameters to maximize PnL.

## 18. Risk metrics

Machine-readable outputs include global risk, relationship statistics, relationship
window sensitivity, per-asset
state statistics, portfolio risk summaries, ablation rows, and portfolio risk
events under `reports/stage8/`. The point-in-time JSONL ledgers are
`derive_btc_relationship_states.jsonl` and `derive_portfolio_risk_states.jsonl`.

## 19. Tests

Focused Stage 8 tests cover shared-state reuse, relationship bounds and
zero-variance handling, stale-IV fallback, local direction, plan-version
isolation, side-specific portfolio blocking, and pair-scoped IDs. Full project
verification is reported separately after execution.

## 20. Limitations

Correlation is time-varying; beta is estimated rather than guaranteed; BTC IV
may not help every asset; HYPE can have substantial idiosyncratic risk; BTC
options are a systematic volatility signal rather than direct ETH/SOL/HYPE IV;
historical correlation does not imply future correlation; beta-equivalent risk
is an approximation; and BBO replay remains simulated without raw queue trades.

## 21. Proposed testnet rollout

Do not enable multi-asset live execution from this Stage 8 demo. If separately
authorized later, validate BTC + ETH one level per side on testnet first, then
review before adding SOL or HYPE. Mainnet remains disabled.
