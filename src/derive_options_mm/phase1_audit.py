"""Reproduce the public-data portion of the Phase 1 readiness audit."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from derive_options_mm.derive_public import DerivePublicClient

DAY = 86_400


def iso_utc(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    seconds = float(timestamp) / 1000 if float(timestamp) > 10_000_000_000 else float(timestamp)
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")


def _timestamp_edges(rows: Iterable[dict[str, Any]], key: str) -> dict[str, str | None]:
    timestamps = [row[key] for row in rows if row.get(key) is not None]
    return {
        "earliest": iso_utc(min(timestamps)) if timestamps else None,
        "latest": iso_utc(max(timestamps)) if timestamps else None,
    }


def summarize_trade_candles(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate real traded bars from the API's zero-volume backfilled bars."""

    traded = [
        row
        for row in rows
        if float(row.get("volume_contracts", 0)) > 0 or float(row.get("volume_usd", 0)) > 0
    ]
    return {
        "returned_rows": len(rows),
        "nonzero_volume_rows": len(traded),
        "returned_range": _timestamp_edges(rows, "timestamp_bucket"),
        "usable_range": _timestamp_edges(traded, "timestamp_bucket"),
        "fields": sorted(rows[0]) if rows else [],
        "quality_note": (
            "Use nonzero-volume rows for coverage. The endpoint may emit constant-price, "
            "zero-volume bars before genuine trading begins."
        ),
    }


def _paginated_edges(
    client: DerivePublicClient,
    method: str,
    params: dict[str, Any],
    list_key: str,
    timestamp_key: str,
) -> dict[str, Any]:
    first = client.post(method, {**params, "page": 1, "page_size": 1000})
    pagination = first["pagination"]
    page_count = int(pagination["num_pages"])
    last = (
        client.post(method, {**params, "page": page_count, "page_size": 1000})
        if page_count > 1
        else first
    )
    rows = list(first.get(list_key, [])) + list(last.get(list_key, []))
    return {
        "raw_row_count": int(pagination["count"]),
        "page_count_at_1000": page_count,
        "range": _timestamp_edges(rows, timestamp_key),
        "fields": sorted(rows[0]) if rows else [],
    }


def audit_asset(
    client: DerivePublicClient,
    asset: str,
    start_timestamp: int,
    end_timestamp: int,
) -> dict[str, Any]:
    instrument_name = f"{asset}-PERP"
    index_rows = client.post(
        "public/get_index_chart_data",
        {
            "currency": asset,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "period": DAY,
        },
    )
    perp_rows = client.post(
        "public/get_tradingview_chart_data",
        {
            "instrument_name": instrument_name,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "period": DAY,
        },
    )
    funding = client.post(
        "public/get_funding_rate_history",
        {
            "instrument_name": instrument_name,
            "start_timestamp": 0,
            "end_timestamp": end_timestamp * 1000,
            "period": 3600,
        },
    )["funding_rate_history"]

    current_instruments = client.post(
        "public/get_all_instruments",
        {
            "currency": asset,
            "instrument_type": "option",
            "expired": False,
            "page_size": 1000,
        },
    )
    all_instruments = client.post(
        "public/get_all_instruments",
        {
            "currency": asset,
            "instrument_type": "option",
            "expired": True,
            "page_size": 1,
        },
    )
    instruments = {
        "active_now": sum(
            bool(instrument.get("is_active"))
            for instrument in current_instruments["instruments"]
        ),
        "non_expired": int(current_instruments["pagination"]["count"]),
        "all_including_expired": int(all_instruments["pagination"]["count"]),
    }

    trade_params = {
        "currency": asset,
        "from_timestamp": 0,
        "to_timestamp": end_timestamp * 1000,
    }
    return {
        "index_daily": {
            "row_count": len(index_rows),
            "range": _timestamp_edges(index_rows, "timestamp"),
            "fields": sorted(index_rows[0]) if index_rows else [],
            "quality_note": (
                "API documentation states missing index buckets can be forward/back-filled."
            ),
        },
        "perp_trade_candles_daily": summarize_trade_candles(perp_rows),
        "perp_trade_history": _paginated_edges(
            client,
            "public/get_trade_history",
            {**trade_params, "instrument_type": "perp"},
            "trades",
            "timestamp",
        ),
        "option_trade_history": _paginated_edges(
            client,
            "public/get_trade_history",
            {**trade_params, "instrument_type": "option"},
            "trades",
            "timestamp",
        ),
        "funding_rate_1h_last_30d": {
            "row_count": len(funding),
            "range": _timestamp_edges(funding, "timestamp"),
            "fields": sorted(funding[0]) if funding else [],
        },
        "option_instruments": instruments,
    }


def audit_global(
    client: DerivePublicClient,
    end_timestamp: int,
) -> dict[str, Any]:
    settlements = _paginated_edges(
        client,
        "public/get_option_settlement_history",
        {},
        "settlements",
        "expiry",
    )
    liquidations = client.post(
        "public/get_liquidation_history",
        {"start_timestamp": 0, "end_timestamp": end_timestamp * 1000, "page_size": 1000},
    )
    auctions = liquidations["auctions"]
    interest = _paginated_edges(
        client,
        "public/get_interest_rate_history",
        {"from_timestamp_sec": 0, "to_timestamp_sec": end_timestamp},
        "interest_rates",
        "timestamp_sec",
    )
    maker_programs = client.post("public/get_maker_programs", {})
    return {
        "option_settlement_history": settlements,
        "liquidation_history": {
            "raw_event_count": int(liquidations["pagination"]["count"]),
            "returned_auction_count": len(auctions),
            "range": _timestamp_edges(auctions, "start_timestamp"),
            "fields": sorted(auctions[0]) if auctions else [],
        },
        "interest_rate_history": interest,
        "maker_programs": {
            "count": len(maker_programs),
            "fields": sorted(maker_programs[0]) if maker_programs else [],
            "range": {
                "earliest_start": iso_utc(
                    min(program["start_timestamp"] for program in maker_programs)
                )
                if maker_programs
                else None,
                "latest_end": iso_utc(max(program["end_timestamp"] for program in maker_programs))
                if maker_programs
                else None,
            },
        },
    }


def scan_settlements_by_asset(
    client: DerivePublicClient,
    workers: int = 4,
) -> dict[str, Any]:
    """Optional full scan; this is intentionally not part of the quick audit."""

    first = client.post("public/get_option_settlement_history", {"page": 1, "page_size": 1000})
    page_count = int(first["pagination"]["num_pages"])
    raw_counts: Counter[str] = Counter()
    unique_contracts: set[tuple[str, str, int, str]] = set()
    ranges: dict[str, list[int]] = {}

    def ingest(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            asset = row["instrument_name"].split("-", 1)[0]
            raw_counts[asset] += 1
            unique_contracts.add(
                (asset, row["instrument_name"], int(row["expiry"]), row["settlement_price"])
            )
            ranges.setdefault(asset, []).append(int(row["expiry"]))

    ingest(first["settlements"])
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                client.post,
                "public/get_option_settlement_history",
                {"page": page, "page_size": 1000},
            ): page
            for page in range(2, page_count + 1)
        }
        for future in as_completed(futures):
            ingest(future.result()["settlements"])

    unique_counts = Counter(item[0] for item in unique_contracts)
    return {
        asset: {
            "raw_rows": raw_counts[asset],
            "unique_contract_settlements": unique_counts[asset],
            "range": {
                "earliest_expiry": iso_utc(min(ranges[asset])),
                "latest_expiry": iso_utc(max(ranges[asset])),
            },
        }
        for asset in sorted(raw_counts)
    }


def run_audit(
    client: DerivePublicClient,
    assets: list[str],
    start_timestamp: int,
    end_timestamp: int,
    include_settlement_scan: bool = False,
) -> dict[str, Any]:
    output = {
        "observed_at_utc": iso_utc(end_timestamp),
        "base_url": client.base_url,
        "assets": {
            asset: audit_asset(client, asset, start_timestamp, end_timestamp) for asset in assets
        },
        "global": audit_global(client, end_timestamp),
        "notes": [
            "All calls are public and read-only.",
            "Trade-history counts are raw API rows and must be deduplicated by trade_id.",
            "Coverage is an observation at audit time, not a provider retention guarantee.",
        ],
    }
    if include_settlement_scan:
        output["global"]["settlements_by_asset"] = scan_settlements_by_asset(client)
    return output


def _parse_date(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(parsed.timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--base-url", default="https://api.lyra.finance")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--scan-settlements",
        action="store_true",
        help="Scan every settlement page to calculate asset-specific counts; this is slow.",
    )
    args = parser.parse_args()

    result = run_audit(
        client=DerivePublicClient(base_url=args.base_url),
        assets=[asset.upper() for asset in args.assets],
        start_timestamp=_parse_date(args.start_date),
        end_timestamp=int(time.time()),
        include_settlement_scan=args.scan_settlements,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
