# Five-minute safe demo

This demo is deliberately read-only. It reads the latest local Condor JSONL records,
the checked-in Stage 6.5 audit summary, and the fail-closed example controller config.
It does not import Hummingbot, call an exchange, open a browser, read credentials, or
place an order.

## What the judge sees

The demo prints one compact state card containing:

1. The latest `MarketSnapshot`: pair, mid price, spread, ATM IV, and record time.
2. The latest `MarketState`: RV/IV score, IV ratio, direction, and inventory state.
3. The latest `GridModeDecision`: mode and reasons.
4. The latest `GridPlan`: center, total width, levels, allocations, and plan version.
5. The execution safety posture: testnet connector, `LIMIT_MAKER`, post-only, dry run,
   one-level cap, and mainnet guard.
6. The latest Stage 6.5 replay comparison: audit status and the three strategy totals.

The values are read from the files at run time. They are not hardcoded presentation
fixtures.

## Run it

From the repository root:

```bash
source .venv/bin/activate
export CONDOR_DATA_DIR=/Users/wilfred/Documents/Hummingbot/condor/data
./scripts/demo.sh
```

If the data lives elsewhere:

```bash
CONDOR_DATA_DIR=/path/to/condor/data ./scripts/demo.sh
```

The script verifies that the four Stage 1--4 JSONL streams and
`reports/stage6_5/audit_summary.json` exist before it starts. A missing or empty
stream is a visible error rather than an invented value.

## Suggested narration

Use the latest card as the bridge from the code to the strategy:

- “The perpetual and ATM-options feeds become one point-in-time snapshot.”
- “The state layer separates realized volatility, IV ratio, direction, and inventory.”
- “The mode governor can widen and reduce the grid or pause new entries.”
- “The plan is the only object the Hummingbot controller consumes.”
- “The live proof is testnet-only and one level per side; the PnL table is replay-only.”

Then open these artifacts in a browser or IDE:

- [Architecture and strategy](../README.md#architecture)
- [Live evidence](LIVE_EVIDENCE.md)
- [IV ablation chart](../reports/stage6_5/charts/01_rv_vs_iv_contribution.svg)
- [Strategy comparison chart](../reports/stage6_5/charts/04_cumulative_pnl_by_strategy.svg)
- [Evaluation table](../reports/stage6_5/iv_ablation.csv)

## What not to do during the demo

Do not enable `execution_enabled`, increase live levels, switch to mainnet, paste a
secret into the terminal, or claim a fill that is not present in the evidence files.
The optional live procedure belongs to the isolated Stage 5 runbook and is not part
of this presentation script.
