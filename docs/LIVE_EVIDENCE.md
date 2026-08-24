# Stage 5 live testnet evidence

This is the sanitized handoff for the constrained Derive testnet run. It deliberately
omits client IDs, account identifiers, API keys, wallet material, and private logs.
The full local evidence remains in [reports/stage5_execution.md](../reports/stage5_execution.md).

## Guardrails that were active

| Setting | Value |
| --- | --- |
| Environment | Derive testnet through Hummingbot V2 |
| Hummingbot pair | `BTC-USDC` (`BTC-PERP` on Derive) |
| Leverage | 1 |
| `allow_mainnet_trading` | `false` |
| `post_only` | `true` |
| Maximum live levels per side | 1 |
| Maximum active executors | 2 |
| Pause behavior | Cancel unfilled entries; keep filled executors managed |
| Shutdown behavior | Cancel test orders; no forced liquidation |
| Cleanup result | Zero open/orphan test orders |

## Rule, collateral, and hard-limit readback

Before the live gate was enabled, the testnet connector reported a minimum base
order size of **0.01 BTC**, a base amount increment of **0.0001 BTC**, and a price
increment of **0.1**. The isolated controller used a testnet scale high enough for
that rule without changing Stage 4 theoretical allocations.

The account readback showed approximately **99,964.03 USDC** collateral value and a
`BTC-PERP` amount of **0.01** after cleanup. That position readback is not attributed
to a Stage 5F fill: the Stage 5F SQLite journal contained zero `TradeFill` rows and
no Stage 5F position delta. The controller hard gates remained **1,000 USDC total
position notional**, **1,000 USDC per-side notional**, **2 active grid levels**, and
**2 active executors**.

## Accepted orders

The following are real Derive exchange order IDs from the Stage 5F observation. They
are included so a reviewer can reconcile the local journal without exposing account
credentials.

| Logical level | Side | Amount | Price | Exchange order ID | Order properties |
| --- | --- | ---: | ---: | --- | --- |
| `buy_0` | BUY | 0.0120 BTC | 77480.0 | `0e34c975-f179-46dc-85dd-e64fbfa4d2a4` | LIMIT / LIMIT_MAKER, post-only, passive |
| `sell_0` | SELL | 0.0119 BTC | 77540.0 | `86d2bc5f-174f-4f8e-8896-f9704be6ca4b` | LIMIT / LIMIT_MAKER, post-only, passive |

Both orders were outside the touch at acceptance, remained open with zero filled
amount, and were later cancelled by the controlled cleanup. They did not immediately
cross the book.

## Lifecycle evidence matrix

| Check | Classification | Result |
| --- | --- | --- |
| Authenticated Hummingbot submission | LIVE TESTNET | PASS |
| Real Derive exchange IDs | LIVE TESTNET | PASS |
| Derive `BTC-PERP` to Hummingbot `BTC-USDC` mapping | LIVE TESTNET | PASS |
| LIMIT_MAKER / post-only request | LIVE TESTNET | PASS |
| Passive, non-crossing placement | LIVE TESTNET | PASS |
| Exactly one entry per side | LIVE TESTNET | PASS |
| Multiple controller cycles | LIVE TESTNET | PASS |
| Existing `buy_0` and `sell_0` not duplicated | LIVE TESTNET | PASS |
| Eligible orders produce KEEP behavior | LIVE TESTNET | PASS |
| Stale entry cancel and safe replacement | LIVE TESTNET | PASS |
| Exposure not doubled during replacement | LIVE TESTNET | PASS |
| DEFENSIVE mode with fewer/wider/smaller entries | LIVE TESTNET | PASS |
| PAUSE cancels unfilled entries without forced liquidation | LIVE TESTNET | PASS |
| PAUSE recovery repopulates missing entries without duplicates | LIVE TESTNET | PASS |
| Graceful cleanup and no orphan test orders | LIVE TESTNET | PASS |
| Natural maker entry fill | NOT OBSERVED LIVE | The public BTC-PERP testnet feed had no executions during the authorized window |
| Live position delta and inventory feedback | NOT OBSERVED LIVE | Requires a live fill |
| Live native take-profit fill and realized PnL | NOT OBSERVED LIVE | Requires a live fill and exit |

## Separate deterministic lifecycle simulation

`tools/simulate_stage5f_lifecycle.py` made zero exchange calls and injected a
synthetic `OrderFilledEvent` into a real Hummingbot `PositionExecutor` configuration.
It verified:

- a buy entry becomes filled/trading;
- the adjacent native exit is a maker sell above the entry;
- an exit can complete with simulated PnL accounting; and
- a completed lifecycle makes one entry level eligible to repopulate.

Those checks are useful for the controller contract, but they are not live Derive
observations. The repository intentionally does not promote them to a live fill
claim.

## Why the test stopped here

The rollout remains one level per side. No mainnet access was enabled, no second
account/counterparty was authorized, and no order-size or depth increase was used to
force a fill. The next live experiment needs explicit authorization and must preserve
the same testnet-only, post-only, one-level guardrails.
