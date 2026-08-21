# Derive Options-Aware Adaptive Market Maker

Evidence-first research toward an options-aware adaptive market maker for Derive BTC and ETH perpetuals.

## Current status

Phase 1 is complete. The repository contains a reproducible public-data audit and a data/infrastructure readiness report. It intentionally does **not** contain a strategy, backtest, collector daemon, or trading code yet.

- [Phase 1 readiness report](reports/phase1_readiness.md)
- [Machine-readable readiness snapshot](artifacts/phase1/readiness_snapshot.json)
- [Research log](reports/research_log.md)

The next phase is blocked on explicit approval.

## Run locally

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
derive-phase1-audit --assets BTC ETH --output artifacts/phase1/generated-latest.json
```

The audit uses only allowlisted `public/*` Derive endpoints. It cannot place, cancel, or modify an order.

For a full currency-level scan of option settlements, add `--scan-settlements`. That scan currently requires hundreds of paginated public requests and is intentionally opt-in.

## Project sequence

```text
data validation -> research -> features -> hypotheses -> baseline backtest
-> adaptive strategy -> robustness -> testnet -> deployment
```

No phase is promoted solely because code runs. Each stage needs a written conclusion, known limitations, and approval to continue.
