# Derive mainnet canary readiness

Status: **NOT READY — STOP**

Audit timestamp: `2026-08-24T15:20:24Z`
Audit command: `tools/derive_mainnet_canary_readiness.py --max-order-notional 800 --max-total-position-notional 1600 --max-loss-quote 20`

This report is a read-only readiness result. No mainnet order, cancel, leverage
change, position-mode change, approval token, or authenticated account request
was made.

## Environment

- Installed Hummingbot connector: `derive_perpetual`.
- Installed connector domain: `derive_perpetual`.
- Mainnet chain ID: `957`.
- Mainnet REST source: `https://api.lyra.finance`.
- Mainnet WebSocket source: `wss://api.lyra.finance/ws`.
- Installed connector mapping: Derive `BTC-PERP` -> Hummingbot `BTC-USDC`.
- Public market environment: mainnet.
- Public options environment: production/mainnet.
- Account environment: **UNKNOWN**; the public-only command did not query an
  authenticated account.
- Execution environment: configured mainnet in the separate template, but
  execution remains disabled.

The four-way consistency gate therefore remains blocked by unknown account
state. Testnet and mainnet plans, journals, and configuration paths are
separate.

## Public options

The mainnet public options adapter returned usable BTC ATM data:

- ATM IV: `0.47936` (approximately `47.936%`).
- Selected expiry: `2026-08-28`.
- Source environment: `production`.
- Options data errors: none in this observation.

## Authenticated account state

Not performed. Before any real canary approval, independently read and record
sanitized values for available collateral, BTC position, open BTC orders,
subaccount/account identity, position mode, and leverage. Any existing BTC
position, open order, or other active bot is a hard stop until explicitly
resolved and acknowledged.

## Public trading rules and fees

The live BTC-PERP public instrument response reported:

- Minimum amount: `0.01 BTC`.
- Amount increment: `0.0001 BTC`.
- Price tick size: `0.1`.
- Maker fee rate: `0.0001` (reported by the public ticker; confirm against the
  authenticated account/fee schedule before using it for PnL).
- Taker fee rate: `0.0003`.

## Mainnet dry run

The public ticker observation was bid `79679.2`, ask `79775.1`, midpoint
`79727.15`, spread `95.9` quote units. The read-only tool mirrored the
installed connector's price wire quantization and checked strict passivity.
The latest Stage 4 plan supplied `100.0` quote units per side; its theoretical
geometry was not changed.

| side | theoretical quote | wire price | minimum amount | wire notional | required scale | passive |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| BUY | 100.0 | 79651.0 | 0.01 | 796.510 | 7.9651 | yes |
| SELL | 100.0 | 79731.0 | 0.01 | 797.310 | 7.9731 | yes |

The required mainnet scale is therefore approximately `7.9651` to `7.9731`
for this observation and plan. The existing testnet scale (`9.30` in prior
testnet evidence) was not reused. The exact scale must be recalculated at
canary time from the live price and rule response.

Both dry-run wire prices were strictly non-crossing: BUY wire price was below
the ask and SELL wire price was above the bid. No order payload was submitted.

## Canary gates

The separate template is intentionally disabled:

- `allow_mainnet_trading: false`.
- `execution_enabled: false`.
- `execution_max_levels_per_side: 1`.
- `post_only: true`.
- `leverage: 1`.
- `max_active_grid_levels: 2`.
- `max_active_executors: 2`.
- `emergency_close_positions_on_pause: false`.
- `mainnet_canary_order_scale: null` until the final live calculation.
- `mainnet_canary_max_order_notional: null` in the checked-in template.
- `mainnet_canary_max_total_position_notional: null` in the checked-in template.
- `mainnet_canary_max_loss_quote: null` in the checked-in template.
- `mainnet_canary_ack: null`.
- `mainnet_account_state_verified: false`.
- `stop_loss_pct: null`; a loss budget without a configured loss-control
  boundary is not treated as a hard canary limit.

The sample audit budgets (`800`, `1600`, and `20` quote units) were passed only
as command-line dry-run inputs; they were not written to configuration and do
not constitute approval. With those sample budgets, the minimum public rule
amount fits, but the canary remains blocked by missing authenticated account
state, environment consistency, and configured loss control.

The only valid acknowledgement string is:

`MAINNET_CANARY_ACK=I_UNDERSTAND_REAL_FUNDS_ARE_AT_RISK`

It must be supplied in addition to `allow_mainnet_trading=true` and
`execution_enabled=true`; no one of the three is sufficient by itself.

## Orders and fills

- Mainnet orders placed: `0`.
- Real Derive/Hummingbot order IDs: none.
- Fills: none.
- Position changes: none.
- Realized PnL: none.

The one-level lifecycle, fill reconciliation, native adjacent-grid exit, and
cleanup remain unproven on mainnet by design. The existing testnet lifecycle
code remains capped at one level per side.

## Cleanup and stop behavior

No mainnet cleanup was needed because no mainnet order was created. If a future
explicitly approved canary is run, cleanup must be performed through Hummingbot
and Derive, with the mainnet journal kept separate; do not force a taker exit or
liquidate a pre-existing position during this readiness stage.

## Verification

- Installed mainnet connector source inspected in the running `hummingbot-api`
  container, including constants, signing branch, symbol mapping, public rules,
  order types, and WebSocket URL selection.
- Public-only mainnet audit completed twice with no order/cancel/private calls.
- Targeted readiness, Stage 5 execution, and snapshot-compatibility tests:
  `37 passed`.
- Full `pytest -q`: `177 passed`.
- Full `ruff check .`: passed.
- `git diff --check`: passed.

## Limitations and next gate

This is mainnet read-only validation, not a live canary approval. Before any
real order is considered, obtain fresh authenticated account evidence, confirm
the exact fee schedule and hard position limits, populate explicit canary
budgets, recalculate the minimum scale, and receive explicit human approval.
Only then may a separate run place one passive BUY level and one passive SELL
level, observe briefly, and stop without scaling.
