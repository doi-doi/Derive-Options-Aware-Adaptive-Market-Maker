# Derive Adaptive Grid — Public Edition

A tiny, read-only demo for people who want to see an adaptive paper grid
without learning Hummingbot, Condor, wallets, or trading infrastructure.

## Run it

Requirements: Python 3.11+ and an internet connection.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m derive_options_mm.public_demo --asset BTC
```

Watch the market continuously:

```bash
python -m derive_options_mm.public_demo --asset ETH --watch
```

Choose `BTC`, `ETH`, `SOL`, or `HYPE`. Use `--levels 2` to show two paper
levels on each side.

## What it shows

The demo reads Derive's public perpetual ticker and instrument rules, then
prints:

- bid, ask, midpoint, spread, and 24-hour movement;
- a simple `NORMAL`, `CAUTION`, or `DEFENSIVE` paper label;
- suggested buy and sell prices, amounts, and notionals.

The grid is for education and observation. It is not a trading recommendation.

## Safety

This public edition cannot trade. It has no wallet connection, no API-key
handling, no private endpoint, and no order-placement code. Nothing is sent to
Derive except requests for public market data.

## License

MIT. See [LICENSE](LICENSE).
