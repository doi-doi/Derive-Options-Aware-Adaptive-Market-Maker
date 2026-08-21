# Phase 1 — Data and infrastructure readiness

Observed on **2026-08-21 UTC**. This report covers discovery only; no order was created, cancelled, or modified.

## Outcome

The project is ready for a **small BTC baseline research experiment**, but it is not ready for a credible options-aware market-making backtest or deployment.

The green path is:

1. use Derive production public history for BTC research;
2. build a conservative event-based baseline that does not assume touch equals fill;
3. reconcile Derive fees from instrument metadata and observed trades;
4. collect Derive option/perpetual live snapshots forward in time;
5. only then test whether options data adds out-of-sample value.

The present Condor/Hummingbot backtester is useful for controller smoke tests, not for proving market-making economics.

## Local Condor and connector audit

### Verified services

- Condor dashboard: `http://127.0.0.1:8088/login` returned HTTP 200.
- Hummingbot API: `http://127.0.0.1:8000/docs` and `openapi.json` returned HTTP 200.
- Docker services for API, PostgreSQL, Gateway, and broker were running; the database and broker reported healthy.
- Existing modified Condor and Hummingbot API checkouts were treated as read-only evidence. This project was created in a separate repository.

### Available relevant Condor capabilities

Condor's generic `get_market_data` routine exposes only:

- latest prices;
- candles;
- current funding information;
- order-book snapshots and book-volume queries.

Relevant reusable routines found locally include `backtest_chart`, `backtest_compare`, `market_analyzer`, and `mm_dashboard`. The market analyzer depends on candles, so it cannot run against Derive through the generic routine until a separate history source is wired in.

The Hummingbot API reports 76 connectors, including:

- `derive_perpetual`;
- `derive_perpetual_testnet`;
- `derive`;
- `derive_testnet`.

It reports 29 candle connectors. **Neither Derive nor Deribit is on that list.** Therefore `get_market_data(data_type="candles", connector_name="derive_perpetual...")` is a hard unsupported-capability error. Retrying it will not connect candles.

### Verified Derive testnet state

Read-only calls against `derive_perpetual_testnet` succeeded for:

- a 5-level BTC-USDC order book;
- current funding rate, mark price, index price, and next funding time;
- cached collateral state for `master_account`;
- current perpetual positions;
- active orders.

At observation time there were no open BTC-USDC positions or active orders. Exact balances are intentionally omitted from this repository.

Both production and testnet connectors advertise `LIMIT`, `LIMIT_MAKER`, and `MARKET`. Current BTC-USDC connector rules expose a minimum order size of `0.01 BTC`, amount increment `0.0001 BTC`, and price increment `0.1 USDC`.

The direct Derive instrument metadata differed by environment:

| Environment | Maker fee rate | Taker fee rate | Base fee | BTC minimum |
|---|---:|---:|---:|---:|
| Production | 0.0001 | 0.0003 | 0.01 | 0.01 BTC |
| Testnet | 0.0005 | 0.0010 | 0.1 | 0.01 BTC |

These fields are inputs, not yet a verified fee formula. Phase 2 must reconcile `trade_fee` from real public trades before calculating P&L.

### Account and portfolio-margin boundary

Condor/Hummingbot currently exposes collateral balances, open orders, and basic position fields: side, amount, entry price, unrealized P&L, and leverage. The local API's position endpoint claims broader margin information in its docstring, but its actual mapper does **not** return initial margin, maintenance margin, liquidation price, funding fees, or the Derive PM/PM2/SM manager state.

Full portfolio-margin telemetry therefore requires a dedicated read-only Derive private-account adapter in a later phase. It is not available through the current generic Condor tools.

## Derive historical data audit

Counts below are observations, not retention guarantees. Public trade and settlement counts are raw API rows; a matched trade can appear for both participating subaccounts, so research must deduplicate by `trade_id`.

### Spot/index candles

Official replacements: [`get_index_chart_data`](https://docs.derive.xyz/reference/post_public-get-index-chart-data) and [`get_tradingview_chart_data`](https://docs.derive.xyz/reference/post_public-get-tradingview-chart-data).

- Fields: `price`, `open_price`, `high_price`, `low_price`, `close_price`, `timestamp`, `timestamp_bucket`.
- Supported periods: 60, 300, 900, 1800, 3600, 14400, 28800, 86400, and 604800 seconds.
- BTC and ETH daily audit: 990 rows each, from 2023-12-06 through 2026-08-21.
- Pagination: none; caller supplies a start, end, and period.
- Missing-data warning: the documented index endpoint forward-fills and can back-fill missing buckets. Preserve the original `timestamp` and reject stale/back-filled predictors.
- Suitability: usable for reference/index returns after explicit fill-quality checks; not a source for executable spread or fill simulation.

The older [`get_spot_feed_history`](https://docs.derive.xyz/reference/post_public-get-spot-feed-history) and [`get_spot_feed_history_candles`](https://docs.derive.xyz/reference/post_public-get-spot-feed-history-candles) are still callable but officially deprecated. New research should not depend on them.

### Perpetual trade candles

- Fields: `open_price`, `high_price`, `low_price`, `close_price`, `volume_contracts`, `volume_usd`, `timestamp`, `timestamp_bucket`.
- Same nine periods as the index chart endpoint.
- A broad query returned 2,425 daily rows from 2020-01-01, but the pre-launch rows were constant-price and zero-volume. They are not real market history.
- BTC usable nonzero-volume rows: 982; earliest 2023-12-14; latest 2026-08-21.
- ETH usable nonzero-volume rows: 961; earliest 2023-12-14; latest 2026-08-21.
- Pagination: none.
- Suitability: usable for volatility/reference-price research after excluding zero-volume backfill; insufficient for queue-aware market-making fills.

### Trade history

Official endpoint: [`get_trade_history`](https://docs.derive.xyz/reference/post_public-get-trade-history).

- Fields: `trade_id`, `instrument_name`, `timestamp`, `direction`, `trade_price`, `trade_amount`, `trade_fee`, `expected_rebate`, `extra_fee`, `liquidity_role`, `mark_price`, `index_price`, `subaccount_id`, `wallet`, `tx_hash`, `tx_status`, `quote_id`, `rfq_id`, `realized_pnl`, `realized_pnl_excl_fees`.
- Frequency: event-driven, millisecond timestamps.
- Pagination: page/page_size; maximum 1,000 rows per page.
- BTC perpetual: 1,933,694 raw rows, 2024-01-11 20:28:44 UTC to 2026-08-21 02:18:35 UTC.
- ETH perpetual: 3,450,998 raw rows, 2024-01-11 20:22:18 UTC to 2026-08-21 02:16:56 UTC.
- BTC options: 294,207 raw rows, 2024-01-11 18:08:14 UTC to 2026-08-21 02:21:32 UTC.
- ETH options: 784,606 raw rows, 2024-01-11 21:40:42 UTC to 2026-08-21 02:21:45 UTC.
- Missing/duplication: deduplicate by `trade_id`; account-side rows are not unique market events. No historical bid/ask, queue position, or IV is attached.
- Suitability: the strongest available event-level source for conservative trade-through fills, volume, aggressor direction, and post-fill adverse selection. Options IV can only be reconstructed from trade price plus contract terms and a clearly documented model.

### Funding-rate history

Official endpoint: [`get_funding_rate_history`](https://docs.derive.xyz/reference/post_public-get-funding-rate-history).

- Fields: `timestamp`, `funding_rate`.
- Periods: 900, 3600, 14400, 28800, and 86400 seconds.
- Retention/query restriction: start is limited to 30 days before end/current time.
- BTC and ETH 1-hour audit: 721 rows each, approximately 2026-07-22 02:00 UTC through 2026-08-21 02:00 UTC.
- Pagination: none.
- Suitability: usable for recent funding state and short-window funding P&L; not enough for long-regime training.

### Liquidation history

Official endpoint: [`get_liquidation_history`](https://docs.derive.xyz/reference/post_public-get-liquidation-history).

- Auction fields: `auction_id`, `auction_type`, `start_timestamp`, `end_timestamp`, `fee`, `subaccount_id`, `tx_hash`, and nested bids/positions.
- Frequency: event-driven.
- Pagination: page/page_size; maximum 1,000 raw events. Pagination counts auction starts, bids, and ends, so the count is larger than returned auction rows.
- Observed response: 228 raw events representing 68 auctions, from 2026-08-14 07:39:41 UTC through 2026-08-21 01:51:06 UTC.
- Missing/coverage: no direct currency filter and only a short retained window was observed. Asset exposure must be parsed from nested liquidation amounts.
- Suitability: useful as a recent stress-event label, not as a complete historical liquidation factor.

### Option settlement history

Official endpoint: [`get_option_settlement_history`](https://docs.derive.xyz/reference/post_public-get-option-settlement-history).

- Fields: `instrument_name`, `expiry`, `amount`, `settlement_price`, `option_settlement_pnl`, `option_settlement_pnl_excl_fees`, `subaccount_id`.
- Frequency: event-driven at option expiry/settlement.
- Pagination: page/page_size; maximum 1,000 rows. There is no currency or time filter.
- Global: 243,252 raw rows; observed expiry range 2023-12-08 through 2026-08-20.
- BTC: 72,870 raw account rows and 14,633 unique contract/expiry/settlement-price combinations; earliest expiry 2023-12-13.
- ETH: 157,499 raw account rows and 20,920 unique contract/expiry/settlement-price combinations; earliest expiry 2023-12-08.
- Missing/duplication: this is subaccount settlement P&L history, not an IV surface or clean option-price series.
- Suitability: useful for settlement validation and expiry labels; unsuitable as historical options IV or quote data.

### Interest-rate history

Official endpoint: [`get_interest_rate_history`](https://docs.derive.xyz/reference/post_public-get-interest-rate-history).

- Fields: `block`, `timestamp_sec`, `borrow_apy`, `supply_apy`, `total_borrow`, `total_supply`.
- Frequency: irregular/event-driven chain updates.
- Pagination: page/page_size; maximum 1,000 rows.
- Observed: 7,382 rows from 2023-12-15 11:37:05 UTC through 2026-08-21 00:00:37 UTC.
- Suitability: potential slow protocol-liquidity context; not a first-pass market-making feature.

### Instrument metadata

Official endpoint: [`get_all_instruments`](https://docs.derive.xyz/reference/public-get_all_instruments).

- Core fields: instrument name/type, active schedule, base/quote currency, amount/tick increments, minimum/maximum amount, maker/taker/base fees, FIFO/pro-rata parameters, option expiry/strike/type/settlement, and current perp funding configuration.
- Pagination: page/page_size; maximum 1,000.
- BTC options: 674 active now, 890 non-expired/scheduled, and 45,944 including expired.
- ETH options: 656 active now, 852 non-expired/scheduled, and 41,268 including expired.
- Frequency: current metadata snapshot, not a versioned time series.
- Suitability: mandatory for contract parsing, fee hypotheses, and valid order sizing. Historical metadata changes are not directly available.

### Maker programs and snapshots

[`get_maker_programs`](https://docs.derive.xyz/reference/public-get_maker_programs) returned 75 past/current programs spanning 2024-11-20 through a latest scheduled end of 2026-08-26.

[`get_detailed_maker_snapshot_history`](https://docs.derive.xyz/reference/public-get_detailed_maker_snapshot_history) is available, but it requires a wallet, program name, and epoch start. Snapshot fields include timestamp, instrument, side, quotes, best/mid/index prices, notional, BBO/instrument/coverage/quality factors, and scaled/deducted notionals. The official documentation warns that option snapshots are available only for the last few days.

This can evaluate a known maker wallet's reward/quality history, but it is not a general historical order-book archive and is not yet wired into Condor.

## Testnet history

The testnet is suitable for execution mechanics, not model training.

Over the latest 30-day probe:

| Dataset | BTC | ETH |
|---|---:|---:|
| 1-hour perp bars returned | 720 | 720 |
| Nonzero-volume 1-hour bars | 49 | 46 |
| Raw perp trade rows | 264 | 374 |
| Funding rows | 716 | 708 |

The large share of zero-volume bars makes testnet history unrepresentative of production liquidity and adverse selection.

## Live Derive data

Direct production WebSocket subscriptions were tested successfully for `ticker_slim`, `orderbook`, and a live BTC option instrument.

### `ticker_slim.{instrument}.{100|1000}`

Official schema: [`ticker_slim`](https://docs.derive.xyz/reference/ticker_slim-instrument_name-interval).

The 100 ms channel emits every 100 ms while BBO changes and otherwise at one second; the 1,000 ms channel emits once per second.

Verified fields:

- best bid/ask and amounts: `b`, `a`, `B`, `A`;
- mark/index: `M`, `I`;
- current funding for perps: `f`;
- option pricing: bid IV `bi`, ask IV `ai`, mark IV `i`, delta `d`, gamma `g`, vega `v`, theta `t`, rho `r`, forward `f`, discount factor `df`, option mark `m`;
- 24-hour statistics: contracts `c`, high `h`, low `l`, trade count `n`, open interest `oi`, percent change `p`, premium volume `pr`, notional volume `v`;
- snapshot timestamps: outer `timestamp` and ticker `t`.

This satisfies the requested live option-ticker field set. It does **not** provide historical snapshots unless we collect them ourselves.

### Other live channels/state

- Perpetual order book: verified directly and through `derive_perpetual_testnet`; Derive supports grouped depths 1, 10, 20, and 100.
- Trades: [`trades.{instrument}`](https://docs.derive.xyz/reference/trades-instrument_name) provides taker direction, trade/index/mark prices, amount, ID, and millisecond timestamp.
- Positions, open orders, collateral: available through current authenticated Hummingbot API calls.
- Full portfolio-margin state: not exposed by current Condor/Hummingbot mapping.

Condor's generic routine can consume the perpetual book and funding state, but it cannot expose the option ticker/Greeks without a new read-only adapter and collector.

## External Deribit options data

No `deribit` connector, candle connector, routine, or options-specific adapter was found in the current Condor installation. Therefore Condor cannot currently retrieve Deribit options history/live data through its installed tools.

Deribit itself has public option summary, ticker, trade, and TradingView candle APIs, including [`get_book_summary_by_currency`](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency) and [`get_tradingview_chart_data`](https://docs.deribit.com/api-reference/market-data/public-get_tradingview_chart_data). A custom read-only collector is technically possible, but that is Phase 2 infrastructure—not an existing Condor capability.

## What Condor's installed backtester can simulate

Installed Hummingbot package version: `20260729`. The audited engine is the exact package running inside the local `hummingbot-api` container.

| Mechanic | Installed behavior | Readiness |
|---|---|---|
| Market/limit-chaser order | Fills on first candle | Optimistic proxy |
| Limit-maker/order executor | Full fill when candle close crosses; fill price is candle close | Not book-realistic |
| Grid entry | Full fill when candle low/high touches level | Touch-equals-fill |
| Grid take profit | Full fill on high/low touch; processed before entry and can recycle within same bar | Intrabar ambiguity/optimism |
| Queue position | Not modeled | Missing |
| Partial fills | Not modeled | Missing |
| Network/exchange latency | Not modeled | Missing |
| Cancel/reprice | Controller actions only at candle steps; no cancel latency | Coarse proxy |
| Fees/rebates | One flat `trade_cost`; grid charges twice per round trip | No Derive schedule/base-fee model |
| Funding accrual | Not applied to simulated P&L | Missing |
| Forced-reduction slippage | Not modeled | Missing |

The engine sets the controller's current price to each backtesting candle close and evaluates actions once per candle. The finest ordinary Condor resolution is one minute. That is not enough to support claims about fill rate, realized spread, maker share, adverse selection, or P&L per maker volume.

Any Phase 2 use of this engine must be labelled a **controller logic smoke test**. A market-making result needs a custom conservative event simulator or forward paper/testnet evidence.

## Missing infrastructure

1. A timestamped Derive live collector for option tickers plus synchronized perp BBO/book/trades.
2. Immutable raw storage and normalized Parquet/DuckDB tables with receipt timestamps and schema versions.
3. Historical option quote/IV/Greek/OI snapshots. Derive does not provide this archive through the audited public endpoints.
4. A queue-aware or deliberately conservative event-level fill model with latency, partial fills, cancel races, maker/taker/base fees, funding, and forced-inventory slippage.
5. Full Derive portfolio-margin telemetry in Condor.
6. A Condor Deribit adapter if external options history is approved.
7. Reproducible execution packaging: the current Hummingbot API contains an uncommitted local Derive testnet market-order/signing compatibility patch. It worked for read-only connectivity but is not yet an upstream/reproducible deployment dependency.

## Smallest viable BTC baseline experiment

**B0 — Data and accounting baseline, no options features**

What it determines: whether a simple inventory-aware BTC-PERP quoting policy can be evaluated without manufacturing fills or fee edge.

- Asset: production `BTC-PERP` research data; no live order placement.
- Window: 14 recent complete UTC days, followed by a separate 7-day holdout.
- Inputs: deduplicated event trades, one-minute trade candles, one-minute index candles, and hourly funding.
- Policy: one bid and one ask, fixed symmetric spread, fixed `0.01 BTC` size, 5-second decision clock, soft inventory limit `0.02 BTC`, hard limit `0.03 BTC`; stop the inventory-increasing side at the hard limit.
- Small grid: quote half-widths of 4, 6, and 8 bps only. This is a sensitivity check, not an optimization campaign.
- Conservative fill rule: a quote becomes eligible only after a fixed latency; require a subsequent aggressor trade to pass through the quote by at least one tick; never fill merely because a candle touched; cap fill by observed trade amount; model cancel/replace latency.
- Costs: reconcile the Derive fee formula first, then apply maker/taker/base fees, hourly funding, and taker slippage for forced inventory reductions.
- Required outputs: all requested P&L, volume, fill, spread, adverse-selection, inventory, drawdown, and capital-efficiency metrics, plus a complete decision/fill ledger.

Success at B0 means the data, accounting, and conservative simulation are reproducible and produce enough fills for evaluation. It does **not** mean the strategy is profitable or ready for testnet. Options-aware work begins only after this baseline is valid.

## Phase gate

**Phase 1 decision: READY WITH GAPS.**

Stop here. Phase 2 should begin only after approval of the B0 experiment and the conservative fill/accounting contract above.
