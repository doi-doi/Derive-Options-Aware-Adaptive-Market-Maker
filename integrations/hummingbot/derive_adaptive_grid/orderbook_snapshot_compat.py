"""Compatibility guard for the Derive initial order-book snapshot race.

The installed Hummingbot Derive data source has two consumers for the same
snapshot queue: the initial order-book request and the snapshot listener. If
the listener wins the race, it caches the snapshot and the initial request can
wait until its 100-attempt timeout without checking that cache. This adapter
keeps the installed connector unchanged while allowing the initializer to use
the snapshot as soon as the listener has cached it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def _cached_snapshot_payload(data_source: Any, trading_pair: str) -> dict[str, Any] | None:
    cached_snapshot = getattr(data_source, "_snapshot_messages", {}).get(trading_pair)
    if cached_snapshot is None:
        return None

    instrument_name = await data_source._connector.exchange_symbol_associated_to_pair(trading_pair)
    return {
        "params": {
            "data": {
                "instrument_name": instrument_name,
                "publish_id": cached_snapshot.update_id,
                "bids": cached_snapshot.bids,
                "asks": cached_snapshot.asks,
                "timestamp": cached_snapshot.timestamp * 1000,
            }
        }
    }


async def _request_with_cache_fallback(
    data_source: Any,
    trading_pair: str,
    original_request: Callable[[Any, str], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run the installed request and notice a listener-cached snapshot."""

    cached_payload = await _cached_snapshot_payload(data_source, trading_pair)
    if cached_payload is not None:
        return cached_payload

    request_task = asyncio.create_task(original_request(data_source, trading_pair))
    try:
        while not request_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(request_task), timeout=0.1)
            except TimeoutError:
                pass

            if request_task.done():
                break

            cached_payload = await _cached_snapshot_payload(data_source, trading_pair)
            if cached_payload is not None:
                return cached_payload

        cached_payload = await _cached_snapshot_payload(data_source, trading_pair)
        if cached_payload is not None:
            return cached_payload
        return await request_task
    finally:
        if not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)


def install_derive_orderbook_snapshot_compatibility() -> None:
    """Install the race guard once in the standalone Hummingbot process."""

    from hummingbot.connector.derivative.derive_perpetual import (
        derive_perpetual_api_order_book_data_source,
    )
    DerivePerpetualAPIOrderBookDataSource = (
        derive_perpetual_api_order_book_data_source.DerivePerpetualAPIOrderBookDataSource
    )

    if getattr(DerivePerpetualAPIOrderBookDataSource, "_codex_snapshot_race_patch_applied", False):
        return

    original_request = DerivePerpetualAPIOrderBookDataSource._request_order_book_snapshot

    async def patched_request(data_source: Any, trading_pair: str) -> dict[str, Any]:
        return await _request_with_cache_fallback(data_source, trading_pair, original_request)

    DerivePerpetualAPIOrderBookDataSource._request_order_book_snapshot = patched_request
    DerivePerpetualAPIOrderBookDataSource._codex_snapshot_race_patch_applied = True
