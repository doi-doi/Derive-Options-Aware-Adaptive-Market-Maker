import asyncio
from dataclasses import dataclass

from integrations.hummingbot.derive_adaptive_grid.orderbook_snapshot_compat import (
    _request_with_cache_fallback,
)


@dataclass
class CachedSnapshot:
    update_id: int = 42
    bids: list[list[str]] | None = None
    asks: list[list[str]] | None = None
    timestamp: float = 123.5

    def __post_init__(self):
        self.bids = self.bids or [["77000", "1"]]
        self.asks = self.asks or [["77010", "1"]]


class FakeConnector:
    async def exchange_symbol_associated_to_pair(self, trading_pair: str) -> str:
        assert trading_pair == "BTC-USDC"
        return "BTC-PERP"


class FakeDataSource:
    def __init__(self):
        self._connector = FakeConnector()
        self._snapshot_messages = {}


def test_cached_snapshot_wins_when_original_request_is_still_waiting():
    async def scenario():
        data_source = FakeDataSource()

        async def original_request(_data_source, _trading_pair):
            await asyncio.sleep(10)
            raise AssertionError(
                "the installed request should be cancelled after the cache is populated"
            )

        async def populate_cache():
            await asyncio.sleep(0.05)
            data_source._snapshot_messages["BTC-USDC"] = CachedSnapshot()

        populate_task = asyncio.create_task(populate_cache())
        result = await _request_with_cache_fallback(data_source, "BTC-USDC", original_request)
        await populate_task
        return result

    result = asyncio.run(scenario())

    assert result["params"]["data"] == {
        "instrument_name": "BTC-PERP",
        "publish_id": 42,
        "bids": [["77000", "1"]],
        "asks": [["77010", "1"]],
        "timestamp": 123500.0,
    }
