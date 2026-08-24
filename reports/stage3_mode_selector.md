# Stage 3 — Read-only grid mode selector

## Conclusion

Stage 3 is complete at the boundary:

```text
MarketState -> GridModeDecision
```

The selector chooses only one symbolic behavior mode:

```text
NORMAL | DEFENSIVE | LONG_BIAS | SHORT_BIAS | PAUSE
```

It does not calculate grid centers, widths, levels, prices, order amounts, or
execution instructions. Stage 4 remains blocked.

## Existing interfaces reused

The selector consumes the existing `derive_options_mm.state_engine.MarketState`
model and reuses its `VolatilityState`, `DirectionState`, and `InventoryState`
enums. It uses the Stage 2 `volatility_score`, `direction_score`,
`inventory_ratio`, `confidence`, and `state_valid` fields, plus the Stage 2.5
`iv_ratio` field. It does not duplicate volatility, direction, inventory, or
ATM-IV calculations.

## Files

Created:

- `src/derive_options_mm/mode_selector.py` — pure candidate rules,
  `GridModeDecision`, configuration, and stateful hysteresis.
- `integrations/condor/derive_grid_mode.py` — read-only Stage 2 JSONL tailer and
  Stage 3 mode JSONL writer.
- `tests/test_mode_selector.py` — priority, gates, confidence, and hysteresis
  tests.
- `tests/test_derive_grid_mode.py` — tailer, persistence, lifecycle, and
  no-trading-boundary tests.

Modified:

- `README.md` — Stage 3 deployment and safety boundary.
- `reports/research_log.md` — ST3-001 research record.

## Priority and rules

The ordered candidate hierarchy is:

1. `PAUSE`: invalid state; missing inventory/account data; critical confidence
   failure; hard inventory limit; unavailable/negative volatility score; or
   extreme volatility score.
2. `DEFENSIVE`: high volatility; elevated but non-extreme volatility score;
   elevated IV ratio; inventory at the soft limit; or confidence below the
   normal-operation threshold.
3. `LONG_BIAS`: normal-risk state, bullish direction score at least `0.25`,
   confidence at least `0.85`, and inventory below `+0.40`.
4. `SHORT_BIAS`: normal-risk state, bearish direction score at most `-0.25`,
   confidence at least `0.85`, and inventory above `-0.40`.
5. `NORMAL`: the safe fallback when no higher-priority rule applies.

The default inventory limits are `+/-0.60` soft and `+/-0.90` hard. A
directional bias that would worsen inventory is blocked at `+/-0.40`; it does
not automatically reverse the direction. Risk always overrides direction.

The default confidence gates are `0.75` for normal operation, `0.85` for
directional bias, and `0.50` as the critical pause threshold. Missing OFI and
missing ATM IV do not pause a state that remains valid and has a realized-
volatility fallback.

## Hysteresis and recovery

- Candidate mode confirmation defaults to two consecutive observations.
- The active mode must last at least ten seconds before a non-hard transition.
- `PAUSE` is immediate, regardless of mode duration.
- Leaving `PAUSE` requires three consecutive safe `NORMAL` candidates by
  default; an optional recovery-time gate can also be configured.
- Leaving `DEFENSIVE` requires two consecutive non-defensive observations and
  returns to `NORMAL` first. It does not jump directly into a directional bias.
- Existing Stage 2 volatility hysteresis remains the source of the volatility
  state; Stage 3 does not recalculate it.

## Persistence and logging

The Condor routine reads:

```text
data/derive_market_states.jsonl
```

and writes:

```text
data/derive_grid_modes.jsonl
```

Each record contains the timestamp, pair, active/previous mode, transition
flag, state summaries, confidence, validity, deterministic reasons, and a
symbolic recommended profile. It does not duplicate the Stage 1 order book or
Stage 2 raw feature stream.

Example log shape:

```text
[GRID MODE]
Pair: BTC-USDC
Previous: NORMAL
Current: LONG_BIAS
Volatility:
  NORMAL
  Score: 0.91
Direction:
  BULLISH
  Score: +0.38
Inventory:
  NEUTRAL
  Ratio: +0.06
Confidence: 0.93
Valid: true
Transition: NORMAL -> LONG_BIAS
Reasons:
  bullish direction confirmed at +0.38; inventory +0.06 permits long bias
  mode transition NORMAL -> LONG_BIAS confirmed for 2 observations
```

## Representative decisions

| MarketState condition | GridMode | Profile |
| --- | --- | --- |
| Normal volatility, neutral direction, safe inventory | `NORMAL` | `standard` |
| High volatility, even with bullish direction | `DEFENSIVE` | `risk_reduced` |
| Normal volatility, strong bullish direction, inventory below `+0.40` | `LONG_BIAS` | `long_bias` |
| Normal volatility, strong bearish direction, inventory above `-0.40` | `SHORT_BIAS` | `short_bias` |
| Invalid state, critical confidence, hard inventory, or extreme volatility | `PAUSE` | `disabled` |

## Verification

Focused Stage 3 tests:

```text
28 passed
```

Ruff passes for the new selector, routine, and tests. The suite covers all
five modes, risk priority, soft/hard inventory gates, confidence, missing OFI
and IV fallback, mode confirmation, minimum duration, immediate pause,
defensive exit confirmation, delayed pause recovery, malformed state input,
JSONL tailing, persistence, and the no-trading surface.

A bounded replay smoke consumed the live Condor Stage 2 stream on 2026-08-24
and wrote one canonical record to
`/Users/wilfred/Documents/Hummingbot/condor/data/derive_grid_modes.jsonl`:
`NORMAL`, profile `standard`, `valid=true`, volatility score `1.088`, and
bearish direction score `-0.234`. The smoke stopped after that single
decision; it did not leave a second long-running selector process.

Run the routine through Condor after deploying the symlink documented in the
README. It is safe to replay existing Stage 2 states for selector warm-up;
only newly appended states are persisted as new mode decisions.

Exact PyCharm-terminal command:

```bash
cd /Users/wilfred/Documents/Hummingbot/condor
PYTHONPATH=/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/src \
/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/.venv/bin/python \
/Users/wilfred/Documents/Hummingbot/Derive-Options-Aware-Adaptive-Market-Maker-phase2-market-snapshot/integrations/condor/derive_grid_mode.py
```

## Limitations and stop decision

- Thresholds are transparent starting values, not calibrated strategy
  parameters.
- `GridModeDecision.valid` mirrors the input `MarketState.state_valid`; a valid
  state can still produce `PAUSE` because of a hard safety gate.
- The routine does not cancel existing orders or enforce a pause. A later
  execution boundary would have to interpret `PAUSE` explicitly.
- No backtest, fill, PnL, or deployment claim is made.

**Decision: STOP.** Do not add grid parameter generation or execution in Stage
3.
