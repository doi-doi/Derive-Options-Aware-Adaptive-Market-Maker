# Stage 5 — Derive testnet execution controller

Status: implemented; the pure dry-run path and isolated Hummingbot container
were verified on 2026-08-24. The Stage 5C full-container dry-run is stable on
Derive testnet with execution disabled; no testnet order was enabled or
placed by this run.

## 1. Installed runtime and architecture decision

The host virtual environments do not contain Hummingbot. The running Docker
image was inspected directly. The installed Hummingbot distribution reports
version `20260729`, with source under `/home/hummingbot/hummingbot`.

The selected executor is one native `PositionExecutor` per Stage 4 level:

```text
buy_0  -> PositionExecutor(entry=quantized buy_0, LIMIT_MAKER, native TP)
buy_1  -> PositionExecutor(entry=quantized buy_1, LIMIT_MAKER, native TP)
sell_0 -> PositionExecutor(entry=quantized sell_0, LIMIT_MAKER, native TP)
```

`GridExecutor` was not selected because its current implementation generates a
linear range internally, while Stage 4 supplies exact geometric levels.
`OrderExecutor` was not selected because it manages a single entry order and
does not provide the native filled-position take-profit lifecycle needed here.
`PositionExecutor` accepts an exact `entry_price`, supports `LIMIT_MAKER`
entries and take-profit orders, exposes `is_active`/`is_trading`/`custom_info`/
`timestamp`/`config`/`close_type`, and preserves the filled position while its
exit is managed.

The controller was import-validated inside the installed Hummingbot image:

```text
derive_perpetual_testnet BTC-USDC False False
DeriveAdaptiveGrid PositionExecutorConfig
```

The two boolean values are `allow_mainnet_trading` and `execution_enabled`.

## 2. Files created

- `integrations/hummingbot/derive_adaptive_grid/__init__.py` — lazy package export so pure logic can be tested on the host.
- `integrations/hummingbot/derive_adaptive_grid/execution_logic.py` — deterministic parsing, official-quantizer boundary, maker checks, risk gates, reconciliation, and journal writer.
- `integrations/hummingbot/derive_adaptive_grid/derive_adaptive_grid.py` — Hummingbot V2 `ControllerBase` adapter and native `PositionExecutor` action construction.
- `integrations/hummingbot/derive_adaptive_grid/derive_adaptive_grid_testnet.example.yml` — safe dry-run configuration.
- `integrations/hummingbot/mirror_grid_plan.py` — atomic Stage 4 JSONL-to-bot-volume bridge; it never calls an exchange.
- `tests/test_stage5_execution_logic.py` — pure execution tests.
- `reports/stage5_execution.md` — this report.

Stage 1–4 algorithms and the currently running Stage 4 Condor process were not
modified. The local `hummingbot-api` checkout was also not overwritten; the
package-copy step is an explicit deployment action for a new bot instance.

## 3. Controller contract

The fixed execution boundary is:

| Setting | Value/default |
|---|---|
| Connector | `derive_perpetual_testnet` |
| Pair | `BTC-USDC` (Hummingbot mapping for Derive `BTC-PERP`) |
| Position mode | `ONEWAY` |
| Leverage | `1` |
| Mainnet guard | `allow_mainnet_trading: false`, validator rejects true |
| Execution gate | `execution_enabled: false` |
| Entry type | `LIMIT_MAKER`, `post_only: true` |
| Rollout cap | `execution_max_levels_per_side: 1` |
| Rollout scale | `testnet_order_scale: 0.05` |
| Active caps | `max_active_grid_levels: 2`, `max_active_executors: 2` |
| Plan path in bot | `/home/hummingbot/data/derive_grid_plans.jsonl` |
| Journal path in bot | `/home/hummingbot/data/derive_execution_events.jsonl` |

The controller validates the connector name, connector domain, connector
readiness, configured/exchange position mode, best bid/ask, trading rules,
available collateral, and readable account positions before creating entries.
It never changes credentials and never logs secrets. Leverage and position
mode are configured through the normal V2 controller workflow; the controller
does not repeatedly set them on every tick.

## 4. Quantization and maker safety

For every Stage 4 level, the adapter calls the installed provider methods:

```python
market_data_provider.get_trading_rules(connector_name, trading_pair)
market_data_provider.quantize_order_price(connector_name, trading_pair, price)
market_data_provider.quantize_order_amount(connector_name, trading_pair, amount)
```

Amount is computed from the Stage 4 quote allocation divided by the
quantized price, then passed through the official amount quantizer. The result
is rejected when price/amount/notional is non-positive, amount is below
`min_order_size`, or notional is below `min_notional_size`.

Buy prices at or above the executable ask are moved outward by one official
price increment and re-quantized; sell prices at or below the executable bid
are moved outward in the opposite direction. If a safe price cannot be
produced, the level is skipped with an explicit reason. No manual `round()` is
used for exchange values.

## 5. Reconciliation and lifecycle

Each controller tick performs this sequence:

1. Tail complete JSONL lines and parse the newest valid Stage 4 `GridPlan`.
2. Reject stale, missing, invalid, disabled, PAUSE, or wrong-pair plans.
3. Verify testnet connector/account/market-data/trading-rule state.
4. Classify active executors by stable `level_id` and filled/unfilled state.
5. Quantize only the desired Stage 4 levels; Stage 5 does not recalculate signals, widths, spacing, mode, or allocation.
6. Keep an acceptable unfilled level and prevent duplicate active levels.
7. Stop obsolete or materially changed unfilled levels before replacement creation.
8. Defer replacement creation until the stop has cleared on a later tick.
9. Apply active-executor, side/total exposure, pending-order, collateral, and minimum-order gates.
10. Return native `StopExecutorAction`/`CreateExecutorAction` objects only when `execution_enabled` is true.

The default minimum lifetime is 30 seconds and maximum lifetime is 600
seconds. A new plan version alone does not refresh an order. Price deviation,
amount deviation, mode change, crossing safety, obsolete level, or maximum age
must justify replacement, and young orders are retained unless a hard safety
condition requires stopping them.

Filled entries are kept even when the plan recenters. The active executor is
classified as exposure rather than as a missing unfilled level. PAUSE cancels
unfilled entry executors, creates nothing, and keeps filled position executors
managed unless the explicit emergency setting is enabled. `manual_kill_switch`
uses the same no-create/cancel-unfilled path.

## 6. Exit behavior

The native `PositionExecutor` is configured with `LIMIT_MAKER` for both entry
and take-profit orders. For a filled buy, the executor manages a sell exit
above the entry; for a filled sell, it manages a buy exit below the entry.
The default `take_profit_mode: adjacent_grid` uses the next inner Stage 4
level (or the Stage 4 center for level zero) to derive the distance. `fixed`
and a configurable multiplier are available. Stop loss is disabled by default;
time limit is optional; trailing stop is not added in V1.

The implementation preserves the lifecycle boundary, but live fill and
realized-exit validation are intentionally not claimed here because the live
gate was not enabled in this run.

## 7. Inventory, pending orders, and balance

The controller reads Derive perpetual account positions and marks long
notional positive and short notional negative. Before each create it includes
unfilled active buy/sell notional in potential exposure. It rejects a level if
the resulting side or total notional would exceed the configured hard limits.
It also reserves `collateral_safety_buffer_pct` (10% by default) and skips
levels whose leverage-adjusted pending notional exceeds available collateral.
Stage 5 skips invalid/minimum/balance levels; it does not rescale Stage 4's
strategy allocation.

The current Derive testnet rule observed in the installed runtime requires
approximately `0.01 BTC`. A `$100` Stage 4 level at five-percent execution
scale is about `$5`, so it is correctly reported as below minimum rather than
sent. A live one-level test therefore requires an explicit larger scale and
sufficient testnet collateral; that is a manual decision, not an automatic
default.

After `max_consecutive_order_errors` terminal failures, new entry creation is
paused for `order_error_pause_seconds`. Health must be good before the counter
is reset. Rejected/minimum/balance/position reasons are journaled once per
level/plan/reason key to avoid repeated spam.

## 8. Structured status and journal

`processed_data` and `get_custom_info()` expose plan version/mode, desired and
active levels, filled levels, levels to create/stop, inventory, potential long
and short exposure, blocked-level categories, caps, error count, testnet
verification, pause reason, order/fill counters, and realized/unrealized PnL
when Hummingbot supplies it. A representative status is:

```text
╔ DERIVE ADAPTIVE GRID ═════════════════╗
Pair: BTC-USDC  Mode: normal  Plan: v14
Grid valid: YES  Enabled: YES
Reference: 77109.04  Width: 0.01447
Desired: BUY 1 / SELL 1
Active entries: 0  Filled: 0
Inventory: 0  Potential long: 5  Potential short: 5
Errors: 0  Testnet: YES
Execution enabled: False  Pause: none
╚════════════════════════════════════════╝
```

Journal entries are append-only JSONL and contain no credentials:

```json
{"event":"CREATE_REQUEST","level_id":"buy_0","plan_version":14,"price":"77070.5","amount":"0.000064","execution_enabled":false}
{"event":"PAUSE","level_id":null,"plan_version":14,"mode":"pause","reason":"GridPlan PAUSE"}
{"event":"ENTRY_FILLED","level_id":"buy_0","executor_id":"...","price":"77070.5","amount":"0.01"}
```

## 9. Dry-run deployment

The existing API uses the normal V2 controller script and a configured
credential profile. First copy the three controller package files into the
API controller mount and start the separate plan mirror:

```bash
STAGE5_ROOT=/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
API_ROOT=/Users/wilfred/Documents/Hummingbot/hummingbot-api
mkdir -p "$API_ROOT/bots/controllers/market_making/derive_adaptive_grid"
cp "$STAGE5_ROOT/integrations/hummingbot/derive_adaptive_grid/derive_adaptive_grid.py" \
  "$STAGE5_ROOT/integrations/hummingbot/derive_adaptive_grid/execution_logic.py" \
  "$STAGE5_ROOT/integrations/hummingbot/derive_adaptive_grid/__init__.py" \
  "$API_ROOT/bots/controllers/market_making/derive_adaptive_grid/"

PYTHONPATH="$STAGE5_ROOT/integrations/hummingbot" \
  "$STAGE5_ROOT/.venv/bin/python" "$STAGE5_ROOT/integrations/hummingbot/mirror_grid_plan.py" \
  --source /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_plans.jsonl \
  --target /Users/wilfred/Documents/Hummingbot/hummingbot-api/bots/instances/INSTANCE_NAME/data/derive_grid_plans.jsonl
```

Create the controller config through the authenticated API, keeping the
existing local `.env` credentials in shell variables only:

```bash
cd /Users/wilfred/Documents/Hummingbot/hummingbot-api
api_user=$(sed -n 's/^USERNAME=//p' .env | head -1)
api_pass=$(sed -n 's/^PASSWORD=//p' .env | head -1)

curl -fsS --user "$api_user:$api_pass" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"derive_adaptive_grid_testnet",
    "controller_name":"derive_adaptive_grid",
    "controller_type":"market_making",
    "connector_name":"derive_perpetual_testnet",
    "trading_pair":"BTC-USDC",
    "leverage":1,
    "position_mode":"ONEWAY",
    "allow_mainnet_trading":false,
    "execution_enabled":false,
    "execution_max_levels_per_side":1,
    "testnet_order_scale":"0.05",
    "post_only":true,
    "grid_plan_path":"/home/hummingbot/data/derive_grid_plans.jsonl",
    "execution_journal_path":"/home/hummingbot/data/derive_execution_events.jsonl",
    "max_total_position_notional":"1000",
    "max_side_position_notional":"1000",
    "max_active_grid_levels":2,
    "max_active_executors":2,
    "stale_plan_timeout_seconds":30,
    "collateral_safety_buffer_pct":"0.10"
  }' \
  http://127.0.0.1:8000/controllers/configs/derive_adaptive_grid_testnet
```

Deploy it as a new dry-run instance; do not replace the currently running
Stage 4 or prior controller instance:

```bash
curl -fsS --user "$api_user:$api_pass" \
  -H 'Content-Type: application/json' \
  -d '{
    "instance_name":"derive-adaptive-grid-testnet",
    "credentials_profile":"master_account",
    "controllers_config":["derive_adaptive_grid_testnet"],
    "max_global_drawdown_quote":100,
    "max_controller_drawdown_quote":100,
    "image":"hummingbot/hummingbot:latest",
    "headless":false
  }' \
  http://127.0.0.1:8000/bot-orchestration/deploy-v2-controllers
```

Expected dry-run evidence is a current `plan_version`, verified testnet
status, quantized desired levels, `WOULD CREATE`/`WOULD STOP` logs, and zero
returned exchange actions. The bot container must be started after the
controller package is copied. The plan mirror must be pointed at the actual
timestamped instance data directory returned by the deployment response.

## 10. Manual one-level testnet gate

Only after the dry-run evidence is reviewed, update the controller config to
`execution_enabled: true`. Keep `execution_max_levels_per_side: 1`,
`post_only: true`, `allow_mainnet_trading: false`, and leverage `1`.

The current approximately `0.01 BTC` minimum means `testnet_order_scale: 0.05`
will normally skip a `$100` Stage 4 level. An explicit first live request must
choose a scale that reaches the exchange minimum (roughly `7.7` for a `$100`
level at a `$77,000` BTC price) and must raise the total notional limit only if
the account balance and the user-approved risk budget support two sides. This
is intentionally not encoded as the default command. Verify the testnet
orders are passive before increasing the cap to `2`.

Stop through the normal API orchestration route with order cancellation
enabled. Do not kill the container as a substitute for graceful executor
cleanup.

## 11. Verification and limitations

Verified in this worktree:

- Hummingbot image import/config smoke: passed.
- Pure Stage 5 execution tests: `26 passed`.
- Full repository suite: `150 passed`.
- Stage 5 package lint: Ruff passed.
- Stage 5 bytecode compile: passed.
- `git diff --check`: passed.

Not claimed yet:

- A live testnet buy and sell were not placed.
- A real fill, native take-profit fill, realized PnL, and position feedback
  loop were not observed in this implementation run.
- The package is not copied into the existing API checkout automatically.
- The existing Stage 4 Condor output is not mounted into a bot automatically;
  run the mirror utility or deliberately configure a new Stage 4 deployment
  to write into the bot data volume.
- Current Stage 4 quote allocations are below Derive's observed minimum at the
  default five-percent scale.

This is therefore a dry-run-ready, testnet-gated Stage 5 implementation, not
a mainnet or production-readiness claim.

## 12. Pre-fix validation run: 2026-08-24

### A. Result

The deterministic Stage 5 controller passes host tests, lint, Hummingbot
import/configuration smoke, and a controller-level dry-run against the current
mirrored Stage 4 plan and read-only Derive testnet data. The full Hummingbot
container did not reach the controller tick loop because its Derive connector
failed to receive the initial BTC-PERP WebSocket order-book snapshot. No live
testnet lifecycle was attempted.

### B. Verification evidence

- Full suite: `150 passed`.
- Stage 5 tests: `27 passed`.
- Ruff: passed.
- `git diff --check`: passed.
- The package loader initially rejected the lazy package export with
  `No configuration class found in the module derive_adaptive_grid`. The
  package now eagerly re-exports `DeriveAdaptiveGridConfig` inside Hummingbot
  and retains the host-only lazy fallback; the container then accepted the
  controller configuration.
- The controller-level smoke used the current mirrored GridPlan, current
  read-only BTC-USDC book values, testnet rules, available collateral, and the
  observed existing position. It produced exactly one desired BUY and one
  desired SELL at the one-level-per-side cap, returned zero executor actions
  with `execution_enabled: false`, and wrote `CREATE_REQUEST` events with
  `reason: dry_run` to the smoke journal.
- Native executor construction passed for both sides. Entry and adjacent
  take-profit order types were `LIMIT_MAKER`; quantities were quantized above
  the observed `0.01 BTC` minimum in the smoke configuration.

### C. Safety state

The validation configuration used `derive_perpetual_testnet`, `BTC-USDC`,
`ONEWAY`, leverage `1`, `post_only: true`,
`allow_mainnet_trading: false`, `execution_enabled: false`, and
`execution_max_levels_per_side: 1`. No trading order endpoint was called. At
the final read-only check, the account showed one existing `0.0165 BTC` long
and zero active orders; that existing exposure was not changed by this run.

### D. Full-container limitation

The isolated Hummingbot instance accepted the controller after the loader fix,
connected its Derive testnet services, and subscribed to the public/private
channels, but Hummingbot's order-book tracker failed with:

`Failed to receive orderbook snapshot for BTC-USDC after 100 attempts.`

The same local Derive testnet WebSocket endpoint returned valid public
BTC-PERP order-book messages in a separate read-only probe, and the existing
bot/API tracker remained healthy. This leaves the multi-instance Hummingbot
snapshot path as an environment/runtime integration issue rather than a
validated Stage 5 controller failure.

### E. Verdict

Stage 5 is dry-run-validated at the controller/reconciliation boundary and is
not approved for live testnet lifecycle validation yet. Keep the execution cap
at one per side and keep `execution_enabled: false` until the Hummingbot
order-book snapshot issue is resolved and the complete pause/resume,
cancel/replace, passive fill, adjacent exit, and inventory feedback lifecycle
is separately observed.

## 13. Stage 5C post-fix validation: 2026-08-24

### A. Root cause

The installed Derive data source has two consumers for the same raw
`order_book_snapshot` queue: `OrderBookTracker._init_order_books()` waits for
the first snapshot while `listen_for_order_book_snapshots()` also waits on the
queue. When the listener wins the startup race, its parser correctly maps
`BTC-PERP` to `BTC-USDC` and stores the parsed message in
`_snapshot_messages`, but the original `_request_order_book_snapshot()` call
does not re-check that cache. It waits through 100 one-second attempts and
raises `Failed to receive orderbook snapshot for BTC-USDC after 100 attempts`.

This is a connector/runtime queue-consumer race, not a Derive endpoint outage,
symbol mismatch, or Stage 1–4 strategy issue. A second deployment boundary
issue was also found: the isolated bot had a stale private copy of the Stage 4
plan file, so it correctly paused with `GridPlan stale` until the live Condor
file was mirrored into the bot volume.

### B. Healthy versus failing diff

- Both instances use the same `hummingbot/hummingbot:latest` image digest,
  host networking, connector configuration, testnet endpoint, and
  `BTC-USDC`/`BTC-PERP` mapping.
- The healthy instance had already completed tracker initialization and kept
  receiving snapshots; the new instance exposed the nondeterministic startup
  race.
- A public REST/WebSocket probe from the host and both container contexts
  received live Derive testnet data. The failing log had no WebSocket parser or
  subscription error before the queue timeout.
- The new instance initially read an old instance-local plan file. The fixed
  run uses the explicit read-only mirror bridge from Condor to the instance
  data volume.

### C. Code and configuration changes

Project files changed or added:

- `integrations/hummingbot/derive_adaptive_grid/orderbook_snapshot_compat.py`
  — cache-aware wrapper around the installed connector request.
- `integrations/hummingbot/derive_adaptive_grid/__init__.py` — installs the
  wrapper before Hummingbot creates the connector and still supports host-only
  imports without Hummingbot installed.
- `tests/test_stage5_orderbook_snapshot_compat.py` — regression test for the
  listener-cached snapshot race.
- `tools/diagnose_derive_orderbook.py` — sanitized public REST/WebSocket
  diagnostic; it never loads credentials or private channels.

The same two compatibility files were copied into the API controller mount at
`hummingbot-api/bots/controllers/market_making/derive_adaptive_grid/`.
No installed Docker image source, credentials, Stage 1–4 algorithm, or live
order endpoint was modified. The existing isolated instance remains
`execution_enabled: false`, `allow_mainnet_trading: false`, testnet-only,
`post_only: true`, leverage `1`, and one level per side. The stale plan file
was preserved as `derive_grid_plans.jsonl.stale-before-shared-20260824-133929`
before the live mirror was started.

### D. Tests

- Targeted compatibility test: `1 passed`.
- Full project pytest: `151 passed`.
- Ruff: passed (`ruff check .`).
- `git diff --check`: passed.
- Source/API compatibility file comparison: identical SHA-256.
- In-container controller import smoke: `DeriveAdaptiveGrid` and
  `DeriveAdaptiveGridConfig` exported; compatibility flag applied.

### E. Read-only probes

- REST: HTTP 200 from `https://api-demo.lyra.finance/public/get_all_currencies`;
  installed connector public methods returned 12 perpetual instruments and 56
  trading-rule records. Current BTC-USDC rules were `min_order_size=0.01`,
  `min_price_increment=0.1`, and `min_base_amount_increment=0.0001`.
- WebSocket: `wss://api-demo.lyra.finance/ws` accepted
  `trades.BTC-PERP`, `orderbook.BTC-PERP.10.10`, and
  `ticker_slim.BTC-PERP.1000`; messages contained `BTC-PERP`, ten bids, and
  ten asks with no subscription errors.
- Parser/queue regression: an intentionally listener-first in-container
  probe returned `get_new_order_book=ok`, `patch_applied=true`, a live update
  id, and valid best bid/ask (`77087.0` / `77165.0`) from the cached snapshot.
- Tracker: the isolated log recorded `Initialized order book for BTC-USDC.
  1/1 completed.`; subsequent controller ticks continued with live plan
  versions and no snapshot error. The healthy API-side tracker diagnostic also
  reported ready/connected, but it was not used as the isolated-bot proof.

### F. Full-container dry-run

| Check | Result |
|---|---|
| `derive_perpetual_testnet` connector ready | PASS |
| `BTC-USDC` / `BTC-PERP` mapping | PASS |
| Initial order-book snapshot | PASS |
| OrderBookTracker ready | PASS |
| Best bid / best ask | PASS; provider gates passed and in-container probe returned live values |
| Trading rules | PASS |
| Balance readable | PASS; balance gate did not block the controller |
| Position readable | PASS; inventory notional was populated |
| Latest GridPlan | PASS; live plan versions advanced (`358` → `373`) |
| Controller tick | PASS; status timestamp advanced repeatedly |
| `WOULD CREATE buy_0` | PASS; `levels_to_create` included `buy_0` |
| `WOULD CREATE sell_0` | PASS; `levels_to_create` included `sell_0` |
| Zero live exchange actions | PASS; `execution_enabled=false`, created/cancelled/fills all `0` |

The log emits `WOULD CREATE 2 maker grid entries` because the two one-level
side requests are reported in one dry-run event. The append-only journal also
contains `CREATE_REQUEST` records with `reason: dry_run` and
`execution_enabled: false` for both level IDs.

### G. Observation

The fixed isolated container remained up for approximately nine minutes at
the final check. Plan versions and controller timestamps advanced throughout;
the post-fix log contained no new `Failed to receive orderbook snapshot`,
`Unexpected error`, or WebSocket-close errors. No order placement or
cancellation endpoint was called.

### H. Exact fixed-run commands

Use protected API credentials only in local shell variables:

```bash
STAGE5_ROOT=/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot
API_ROOT=/Users/wilfred/Documents/Hummingbot/hummingbot-api
INSTANCE_NAME=derive-adaptive-grid-dryrun-20260824-050100

mkdir -p "$API_ROOT/bots/controllers/market_making/derive_adaptive_grid"
cp "$STAGE5_ROOT/integrations/hummingbot/derive_adaptive_grid/"{__init__.py,derive_adaptive_grid.py,execution_logic.py,orderbook_snapshot_compat.py} \
  "$API_ROOT/bots/controllers/market_making/derive_adaptive_grid/"

api_user=$(sed -n 's/^USERNAME=//p' "$API_ROOT/.env" | head -1)
api_pass=$(sed -n 's/^PASSWORD=//p' "$API_ROOT/.env" | head -1)

tmux has-session -t stage5-grid-mirror 2>/dev/null || tmux new-session -d -s stage5-grid-mirror -c "$API_ROOT" \
  "python3 $STAGE5_ROOT/integrations/hummingbot/mirror_grid_plan.py --source /Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_plans.jsonl --target $API_ROOT/bots/instances/$INSTANCE_NAME/data/derive_grid_plans.jsonl --interval 1"

docker start "$INSTANCE_NAME"

curl -fsS -u "$api_user:$api_pass" \
  "http://127.0.0.1:8000/bot-orchestration/$INSTANCE_NAME/status"

rg -n -uuu 'Initialized order book|WOULD CREATE|Failed to receive orderbook|Unexpected error' \
  "$API_ROOT/bots/instances/$INSTANCE_NAME/logs"
```

For a new instance, first use the authenticated controller-config and
`/bot-orchestration/deploy-v2-controllers` payload in Section 9, with the
four-file package copy above and `execution_enabled: false`; then replace
`INSTANCE_NAME` with the timestamped deployment name returned by the API.

### I. Remaining limitations

- No live testnet order, fill, cancellation, adjacent-grid take-profit, or
  realized-PnL lifecycle was attempted.
- The compatibility guard is a narrow runtime patch for the installed
  Hummingbot image; it should be removed or upstreamed when the connector
  natively rechecks its snapshot cache.
- The mirror bridge is an explicit local process and must remain running for
  continuous live-plan input; it is not a mainnet or production deployment
  mechanism.
- The existing healthy Derive testnet instance was left running and was not
  stopped or modified.

## 14. Stage 5D one-level live Derive testnet lifecycle: 2026-08-24

### A. Scope and safety gates

The final isolated test instance was:

`derive-adaptive-grid-live-testnet-final-20260824-20260824-061325`

It used `derive_perpetual_testnet` / `BTC-USDC` with the following gates:

| Gate | Value | Result |
|---|---:|---|
| `allow_mainnet_trading` | `false` | PASS; no mainnet connector or endpoint used |
| `leverage` | `1` | PASS; Derive confirmed leverage 1 |
| `post_only` | `true` | PASS |
| `execution_max_levels_per_side` | `1` | PASS |
| `max_active_executors` | `2` | PASS |
| `max_active_grid_levels` | `2` | PASS |
| `execution_enabled` | `true` during the test | PASS |
| `emergency_close_positions_on_pause` | `false` | PASS; no forced liquidation |

The isolated instance was stopped after the evidence window with order
cancellation enabled. Its container is no longer running, and the final
SQLite database contains zero non-cancelled orders. The pre-existing healthy
Derive testnet instance was not stopped or modified.

### B. Live rules, collateral, and hard limits

Authenticated Hummingbot connector rules returned:

```text
BTC-USDC min_order_size=0.01 BTC
BTC-USDC min_base_amount_increment=0.0001 BTC
BTC-USDC min_price_increment=0.1 USDC
supports_limit_orders=true
buy_order_collateral_token=USDC
sell_order_collateral_token=USDC
```

The refreshed testnet account state showed `99,956.61680366543 USDC`
available. The account already had an unrelated `0.0165 BTC` LONG position;
the Stage 5D run created no additional position. The configured side and
total notional limits were `$5,000` and `$10,000`; the normal one-level plan
reported approximately `$2,204.56` potential long and `$929.27` potential
short exposure, below both limits. The defensive plan reported approximately
`$2,049.48` long and `$774.37` short exposure.

`testnet_order_scale` was set to `'9.30'`. This was the smallest tested scale
that kept the unchanged Stage 4 defensive theoretical allocation
(`$83.33333333333333` quote per level) at or above the live `0.01 BTC`
minimum near the observed BTC price. It produced `0.010 BTC` defensive
entries and `0.012 BTC` normal entries. Stage 4 plan allocations and source
file were not changed.

### C. Authenticated order submission fix

The first execution-enabled attempt reached Derive but was rejected with
`Signature does not match data`; it produced no exchange order IDs. The
installed Hummingbot image signs the legacy Derive testnet action with a
newer trade-module address/domain. A narrow testnet-only compatibility module
was added at:

- `integrations/hummingbot/derive_adaptive_grid/derive_perpetual_signing_compat.py`
- `hummingbot-api/bots/controllers/market_making/derive_adaptive_grid/derive_perpetual_signing_compat.py`

Both copies use the legacy testnet module/domain and canonical 18-decimal
wire values; mainnet signing is left on the original implementation. After
the patch, live order submissions were accepted and the final logs contained
no signature errors or order errors.

### D. Maker entry evidence

At the final stable normal-plan observation, the controller reported:

```text
mode=normal, plan_version=531
active_entry_levels=[buy_0, sell_0]
active_entry_count=2
filled_position_levels=[]
levels_to_create=[]
levels_to_stop=[]
blocked_level_reasons=[]
orders_created=26, orders_cancelled=24, fills=0
execution_enabled=true, testnet_verified=true
```

The final two accepted records in the instance Hummingbot SQLite database
were:

```text
buy_0  client=0xd76257a8f93825dc75ae26c17d5fdeb0
       exchange=89a73274-a123-4c44-89ca-3c4a892e93b2
       LIMIT_MAKER  0.012 BTC @ 77273.0

sell_0 client=0x91985f4fab02450fb311c8371a19457b
       exchange=59daac24-ff18-42f6-80b2-a3a56a328ac6
       LIMIT_MAKER  0.012 BTC @ 77350.0
```

The same entries had real executor IDs in the append-only execution journal:

```text
...__buy_0__v531__mode_normal__1787553149305
...__sell_0__v531__mode_normal__1787553149307
```

At the matching order-book sample, best bid/ask were `77270.0` / `77336.0`.
The buy was below the ask and the sell was above the ask, so neither order
crossed the book. The order type, accepted exchange IDs, and controller state
jointly provide the passive LIMIT_MAKER evidence.

### E. KEEP and safe replacement

Across three four-second controller observations, the same two levels were
retained with no new creates or stops:

```text
active_entry_levels=[buy_0, sell_0]
levels_to_create=[]
levels_to_stop=[]
orders_created=22
orders_cancelled=20
fills=0
```

A material plan movement from normal `v517` to defensive `v520` generated
`STOP_REQUEST` / `STOP_SUCCESS` for both old executors before the replacement
`CREATE_REQUEST` / `CREATE_SUCCESS` pair. The active cap remained one per
side; the database never showed more than two non-cancelled Stage 5D entry
orders. This verifies stale-entry cancellation and replacement without
doubled entry exposure.

### F. DEFENSIVE, PAUSE, and recovery

The controlled defensive plan was appended only to the isolated Stage 5
target by `tools/emit_controlled_stage5_plan.py`; the Condor Stage 4 source
was read-only. It used three wider theoretical levels per side and
`$83.33333333333333` quote allocation per level. Execution still produced
only `buy_0` and `sell_0`, at `0.010 BTC` each, while filled position
executors would remain managed by the controller.

When the plan mirror was stopped long enough for the plan to become stale,
the controller entered PAUSE with reason `GridPlan stale — new entry creation
blocked`, canceled both unfilled entries, and reported zero active entries.
The account position remained managed and no liquidation action was sent.

After the mirror was restored, a valid normal/defensive plan resumed the
controller and repopulated exactly one `buy_0` and one `sell_0`, with no
duplicates. The run ended safely with all 26 recorded orders in
`OrderCancelled` state and zero non-cancelled orders.

### G. Fill-dependent lifecycle boundary

No entry filled during the bounded maker observation window. Therefore the
following requirements remain unverified by live Derive evidence:

- `PositionExecutor` becoming filled/trading from a real entry fill;
- Derive position change and Stage 2 `inventory_ratio` feedback;
- native adjacent-grid take-profit placement after a fill;
- exit fill, realized PnL, inventory decrease, and lifecycle repopulation;
- the complete `fill -> Derive position -> Stage 1 -> Stage 2 -> Stage 3 ->
  Stage 4 -> Stage 5` feedback loop.

The controller is configured for the native adjacent-grid `PositionExecutor`
take-profit path, with LIMIT_MAKER entry/exit settings, but no fill occurred
that would justify claiming those live behaviors. No order was crossed or
artificially made taker to force a fill.

### H. Verification and verdict

In the Stage 5 checkout after the code changes:

- `pytest -q` — PASS;
- `ruff check .` — PASS;
- `git diff --check` — PASS;
- authenticated live rule, collateral, position, order-book, controller,
  journal, SQLite, and Docker-stop checks — PASS.

The API checkout has no local pytest or Ruff executable; its mounted
compatibility module was exercised by the live Hummingbot Docker run, and its
`git diff --check` passed.

The one-level live testnet entry lifecycle is proven through authenticated
order acceptance, passive maker verification, KEEP behavior, cancel/replace,
DEFENSIVE mode, PAUSE, recovery, and safe shutdown. Fill-dependent lifecycle
and feedback-loop proof is not complete because no practical fill occurred.
Keep the rollout at one level per side. Do not increase it to two or five
levels, and do not enable mainnet.

## 15. Stage 5E fill-dependent lifecycle validation: 2026-08-24

### A. Scope and live safety gates

The isolated Stage 5E instance was:

`derive-adaptive-grid-live-testnet-stage5e-20260824-065301`

It used `derive_perpetual_testnet` / `BTC-USDC` with:

```text
allow_mainnet_trading=false
leverage=1
post_only=true
execution_enabled=true
execution_max_levels_per_side=1
max_active_executors=2
max_active_grid_levels=2
emergency_close_positions_on_pause=false
testnet_order_scale=9.30
```

The isolated Stage 5E container was stopped after cancellation cleanup. The
separate healthy Derive testnet instance was not stopped or modified. No
mainnet connector, order, or endpoint was used.

### B. Live rules, collateral, and position baseline

The authenticated installed Hummingbot connector returned:

```text
min_order_size=0.01 BTC
min_base_amount_increment=0.0001 BTC
min_price_increment=0.1 USDC
min_notional_size=0
supports LIMIT_MAKER=true
```

At the final account check, Derive reported approximately `99,964.03 USDC`
collateral and an existing `0.01 BTC-PERP` long. Stage 5E's SQLite database
contained zero `TradeFill` rows and the account amount was unchanged by this
run; the existing position is not attributed to Stage 5E.

### C. Real passive maker entry evidence

The latest controlled validation-only plan kept exactly one `buy_0` and one
`sell_0`. During the active controller window it reported:

```text
plan_version=10
active_entry_levels=[buy_0, sell_0]
active_entry_count=2
filled_position_levels=[]
levels_to_create=[]
levels_to_stop=[]
filled_executor_count=0
orders_created=6
orders_cancelled=4
fills=0
order_errors=0
```

The latest real Derive orders were:

```text
buy_0  client=0xdca98243c2ee88847aa46783b5a68fd5
       exchange=7422e33f-22ce-4833-a94d-154b2eebfd77
       LIMIT_MAKER -> limit/post_only 0.0119 BTC @ 77540.0
       filled_amount=0, final status=cancelled

sell_0 client=0x6e917a730f3112386e4c2a290ef5b534
       exchange=562c914a-f068-44a8-92d2-c1ed0de404b5
       LIMIT_MAKER -> limit/post_only 0.0120 BTC @ 77550.0
       filled_amount=0, final status=cancelled
```

The prices were wire-aware passive prices for the observed `77549 / 77550`
book. Both orders had real exchange IDs, used `time_in_force=post_only`, and
were never submitted as takers. The earlier v8 and v9 windows produced the
same passive exchange-side evidence.

### D. KEEP, stale cancellation, and replacement

With both entries active, repeated controller status reads showed no levels
to create or stop, so the acceptable entries were retained without
duplication. When each bounded plan was allowed to exceed the configured
30-second stale timeout, the journal recorded `PAUSE`, `STOP_REQUEST`, and
`STOP_SUCCESS` for the unfilled executors. The next validation plan recreated
one `buy_0` and one `sell_0`; no filled executor existed and no exposure was
doubled. The final Derive `get_open_orders` response was empty after cleanup.

### E. Fill-dependent lifecycle boundary

No natural testnet fill occurred during the bounded passive observation
windows. Consequently these Stage 5E requirements remain **not proven by live
exchange state**:

- entry `PositionExecutor` becoming filled/trading;
- a Stage 5E-caused Derive position delta;
- Stage 1 account snapshot -> Stage 2 `inventory_ratio` feedback;
- Stage 3 mode/inventory gating and Stage 4 plan response to that fill;
- native adjacent-grid maker take-profit creation;
- exit fill, realized PnL, inventory decrease, and post-completion
  repopulation.

The code path is configured for the native adjacent-grid `PositionExecutor`
take-profit, but no fill occurred that would justify claiming its live
behavior. The isolated Stage 1-4 feedback artifacts remain at
`/Users/wilfred/Documents/Hummingbot/condor/data/stage5e-feedback-20260824`;
they establish the testnet baseline only, not a post-fill feedback loop.

### F. Verification and verdict

The Stage 5 checkout checks after the compatibility/helper changes were:

- `pytest -q` — PASS;
- `ruff check .` — PASS;
- `git diff --check` — PASS;
- authenticated Derive rules, collateral, positions, exact order status,
  controller status, journal, SQLite, and cleanup checks — PASS.

The live one-level maker-entry, KEEP, stale-cancel, safe replacement, and
cleanup portions are proven. The full fill-dependent Stage 5E lifecycle is
**not proven** because no practical maker fill occurred. Keep the rollout at
one level per side; do not increase it to two or five levels and do not enable
mainnet.

## 16. Stage 5F maker-fill fallback validation: 2026-08-24

### A. Price discrepancy and root cause

Stage 5E observed a public touch of approximately `best_bid=77549` and
`best_ask=77550`. The controlled Stage 4 validation plan supplied
`buy_0=77549` and `sell_0=77551`. Stage 5 called Hummingbot's official
`quantize_order_price` callback; the installed Derive implementation rounded
through five significant digits, leaving those values unchanged. Its order
submission method then serialized the wire price with:

```python
float(f"{price:.4g}")
```

At this price level both `77549` and `77551` serialize to `77550`. The BUY
therefore became marketable at the ask and was rejected by Derive's post-only
self-cross protection. The Stage 5E `77540` BUY was not produced by Stage 4
or by a hidden strategy buffer; it was a deliberate safe compensation for
the connector's four-significant-digit wire representation. This is a
connector precision limitation, not a change to the Stage 4 grid formula.

`tools/emit_stage5f_touch_plan.py` now mirrors the installed five-significant-
digit quantizer and four-significant-digit wire conversion and chooses the
closest representable passive price. It is validation tooling only and does
not modify the Stage 1-4 algorithms or production grid formulas.

### B. Fill environment and counterparty decision

The authenticated live Derive rules remained:

```text
min_order_size=0.01 BTC
min_base_amount_increment=0.0001 BTC
min_price_increment=0.1 USDC
min_notional_size=0
supports LIMIT_MAKER=true
```

The public testnet WebSocket produced order-book updates but no executions:

| Window | `orderbook.BTC-PERP` updates | `trades.BTC-PERP` messages | Result |
|---|---:|---:|---|
| pre-test 30 seconds | 30 | 0 | no public trades |
| pre-test 60 seconds | 59 | 0 | no public trades |
| Stage 5F resting-order 90 seconds | 89 | 0 | no public trades |

The only local Derive credential profile is `master_account`. No separate
authorized testnet account/subaccount was available, so no counterparty order
was submitted and no same-account self-trade was attempted.

### C. Live touch attempt

The isolated Stage 5F instance was:

`derive-adaptive-grid-live-testnet-stage5f-20260824-20260824-073622`

The validation-only plan was plan version `708`, written only to that
instance's `data/derive_grid_plans.jsonl`. With an observed touch of
`77480 / 77536`, the planner selected:

```text
buy theoretical/quantized/wire = 77480 / 77480 / 77480
sell theoretical/quantized/wire = 77536 / 77536 / 77540
```

The real exchange orders were accepted and resting:

```text
BUY  client=0x2e56dd65a85271a6e4a40dad50e24a16
     exchange=0e34c975-f179-46dc-85dd-e64fbfa4d2a4
     limit/post_only 0.0120 BTC @ 77480

SELL client=0x0b015be8c6e24d05c44d4c537a561433
     exchange=86d2bc5f-174f-4f8e-8896-f9704be6ca4b
     limit/post_only 0.0119 BTC @ 77540
```

Derive confirmed both as `order_type=limit`, `time_in_force=post_only`,
`order_status=open`, and `filled_amount=0` while active. The controller
reported exactly two active entries, one per side, with no order errors. The
configured 30-second stale-plan guard then cancelled both unfilled orders;
the final exact exchange status was `cancelled/user_request` with zero fill.

### D. Live result and cleanup

The Stage 5F live maker fill is **not available** in the current Derive
testnet environment: the public trade channel produced zero trades and no
authorized counterparty existed. The final authenticated account state had:

```text
open_orders=[]
BTC-PERP amount=0.01
collateral_value ~= 99964.03 USDC
```

Stage 5F SQLite contained zero `TradeFill` rows. No Stage 5F position delta,
TP, realized PnL, or exit exposure was created. The isolated Stage 5F
container was stopped after cancellation cleanup and is exited; the unrelated
healthy Derive testnet container remained running.

### E. Deterministic Hummingbot simulation (SIMULATED / NOT LIVE EXCHANGE EVIDENCE)

Because live filling was unavailable and no separate counterparty was
authorized, `tools/simulate_stage5f_lifecycle.py` was run inside the installed
Hummingbot API container. It made zero exchange calls and used official
`OrderFilledEvent` objects plus the real controller's
`PositionExecutorConfig` construction:

```text
entry event: OrderFilledEvent, LIMIT_MAKER, 0.012 BTC
before: buy_0 active, not filled
after: buy_0 classified filled/trading
after-fill reconciliation: keeps buy_0 and sell_0, creates none
entry/TP executor order types: LIMIT_MAKER / LIMIT_MAKER
adjacent-grid TP: 77480 -> 77508, pct=0.0003613835828600929
simulated TP: LIMIT_MAKER SELL @ 77536, PnL before fees=0.672 USDC
after exit: filled levels empty, exactly one buy_0 eligible to repopulate
```

This simulation proves the controller's fill classification, duplicate
prevention, native TP configuration, deterministic exit accounting, and
post-completion repopulation logic. It does not prove a live Derive fill,
live position feedback, or live Stage 1 -> Stage 4 inventory propagation.

### F. Stage 5F verdict and verification

| Area | Result |
|---|---|
| Maker-only touch placement | PASS live |
| Real maker entry fill | NOT AVAILABLE: zero public trades |
| Separate counterparty | NO authorized account available |
| PositionExecutor fill classification | PASS simulated only |
| Adjacent-grid LIMIT_MAKER TP | PASS simulated only |
| Exit/PnL/repopulation | PASS simulated only; not live |
| Stage 1-4 real inventory feedback | NOT PROVEN: no live fill |
| Cleanup/orphan-order check | PASS |

Stage 5F stop condition B is satisfied: live filling is unavailable in this
bounded Derive testnet environment, and the fill-dependent controller path was
validated separately with an explicit simulation label. Keep the rollout at
one level per side. Do not enable mainnet or weaken maker-only execution.
