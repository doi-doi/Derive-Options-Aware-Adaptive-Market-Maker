"""Read-only Derive testnet REST and WebSocket order-book diagnostics.

This utility deliberately uses only public endpoints. It does not load
Hummingbot credentials, connect to private channels, or call any trading API.
It is intended for comparing host and container networking/parser inputs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from typing import Any

import requests
import websockets

REST_URL = "https://api-demo.lyra.finance/public/get_all_currencies"
WS_URL = "wss://api-demo.lyra.finance/ws"
TRADING_PAIR = "BTC-USDC"
EXCHANGE_SYMBOL = "BTC-PERP"
SUBSCRIPTION_CHANNELS = [
    f"trades.{EXCHANGE_SYMBOL}",
    f"orderbook.{EXCHANGE_SYMBOL}.10.10",
    f"ticker_slim.{EXCHANGE_SYMBOL}.1000",
]


def _instrument_summary(instrument: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "instrument_name",
        "instrument_type",
        "base_currency",
        "quote_currency",
        "settlement_currency",
        "is_active",
    )
    return {field: instrument.get(field) for field in fields if field in instrument}


def probe_rest() -> dict[str, Any]:
    started = time.perf_counter()
    response = requests.post(
        REST_URL,
        json={"expired": True, "instrument_type": "perp", "page": 1, "page_size": 1000},
        timeout=15,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") or {}
    if isinstance(result, dict):
        instruments = result.get("instruments")
    elif isinstance(result, list):
        instruments = result
    else:
        instruments = None
    instruments = instruments if isinstance(instruments, list) else []
    matches = [
        _instrument_summary(item)
        for item in instruments
        if isinstance(item, dict) and item.get("instrument_name") == EXCHANGE_SYMBOL
    ]
    return {
        "url": REST_URL,
        "http_status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "top_level_keys": sorted(payload.keys()),
        "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
        "instrument_count": len(instruments),
        "expected_pair": TRADING_PAIR,
        "expected_exchange_symbol": EXCHANGE_SYMBOL,
        "matching_instruments": matches,
    }


def _message_summary(message: dict[str, Any]) -> dict[str, Any] | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    channel = params.get("channel")
    data = params.get("data")
    if not isinstance(channel, str) or not isinstance(data, dict):
        return None
    summary: dict[str, Any] = {
        "channel": channel,
        "data_keys": sorted(data.keys()),
    }
    if "instrument_name" in data:
        summary["instrument_name"] = data["instrument_name"]
    if "publish_id" in data:
        summary["publish_id"] = data["publish_id"]
    if "timestamp" in data:
        summary["timestamp"] = data["timestamp"]
    if isinstance(data.get("bids"), list) and isinstance(data.get("asks"), list):
        summary["bid_count"] = len(data["bids"])
        summary["ask_count"] = len(data["asks"])
        summary["best_bid"] = data["bids"][0] if data["bids"] else None
        summary["best_ask"] = data["asks"][0] if data["asks"] else None
    return summary


async def probe_websocket(duration_seconds: float) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    started = time.perf_counter()
    async with websockets.connect(WS_URL, ping_timeout=10, close_timeout=2) as websocket:
        await websocket.send(
            json.dumps({"method": "subscribe", "params": {"channels": SUBSCRIPTION_CHANNELS}})
        )
        deadline = time.perf_counter() + duration_seconds
        while time.perf_counter() < deadline:
            timeout = max(0.1, deadline - time.perf_counter())
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            except TimeoutError:
                break
            message = json.loads(raw)
            if message.get("error"):
                errors.append(message["error"])
                continue
            summary = _message_summary(message)
            if summary is None:
                if "result" in message:
                    counts["subscription_ack"] += 1
                continue
            counts[summary["channel"]] += 1
            if len(samples) < 3:
                samples.append(summary)
    return {
        "url": WS_URL,
        "channels": SUBSCRIPTION_CHANNELS,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "message_counts": dict(counts),
        "errors": errors,
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=float, default=5.0, help="WebSocket observation window in seconds"
    )
    args = parser.parse_args()
    result = {"rest": probe_rest(), "websocket": asyncio.run(probe_websocket(args.duration))}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
