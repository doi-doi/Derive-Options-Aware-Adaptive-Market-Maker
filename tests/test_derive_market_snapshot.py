from __future__ import annotations

import asyncio
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROUTINE_PATH = (
    Path(__file__).parents[1] / "integrations" / "condor" / "derive_market_snapshot.py"
)


def _load_routine():
    spec = importlib.util.spec_from_file_location("derive_market_snapshot", ROUTINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


routine = _load_routine()


def _book(timestamp: float = 1_700_000_000.0) -> dict:
    return {
        "timestamp": timestamp,
        "bids": [
            {"price": "100.0", "amount": "2"},
            {"price": "99.5", "amount": "3"},
            {"price": "99.0", "amount": "5"},
        ],
        "asks": [
            {"price": "101.0", "amount": "1"},
            {"price": "101.5", "amount": "2"},
            {"price": "102.0", "amount": "2"},
        ],
    }


def _diagnostics(*, ready: bool = True, snapshot_age: float = 0.5) -> dict:
    return {
        "tracker_ready": ready,
        "websocket_status": "connected" if ready else "not_connected",
        "trading_pairs": ["BTC-USDC"] if ready else [],
        "metrics": {
            "tracker_start_time": 100.0,
            "uptime_seconds": 50.0,
            "per_pair_metrics": {
                "BTC-USDC": {"last_snapshot_timestamp": 150.0 - snapshot_age}
            },
        },
    }


def test_config_defaults_are_safe_testnet_btc() -> None:
    config = routine.Config()
    assert config.connector_name == "derive_perpetual_testnet"
    assert config.trading_pair == "BTC-USDC"
    assert config.book_depth_levels == 5
    assert config.max_data_age_seconds == 15.0
    assert config.snapshot_interval_seconds == 5.0
    assert config.output_path == "data/derive_market_snapshots.jsonl"


def test_pure_price_and_spread_functions() -> None:
    bid = Decimal("100")
    ask = Decimal("101")
    mid = routine.calculate_mid_price(bid, ask)
    spread = routine.calculate_spread_abs(bid, ask)
    assert mid == Decimal("100.5")
    assert spread == Decimal("1")
    assert routine.calculate_spread_bps(spread, mid) == Decimal("10000") / Decimal("100.5")


def test_top_level_and_depth_features() -> None:
    features = routine.calculate_book_features(
        _book(), depth=2, data_age_seconds=0.5, now=1_700_000_001.0
    )
    assert features.best_bid == Decimal("100.0")
    assert features.best_ask == Decimal("101.0")
    assert features.best_bid_size == Decimal("2")
    assert features.best_ask_size == Decimal("1")
    assert features.bid_depth == Decimal("5")
    assert features.ask_depth == Decimal("3")
    assert features.top_level_imbalance == Decimal("1") / Decimal("3")
    assert features.depth_imbalance == Decimal("1") / Decimal("4")
    assert routine.aggregate_depth(
        [(Decimal("100"), Decimal("2")), (Decimal("99"), Decimal("3"))], 2
    ) == Decimal("5")


def test_zero_denominators_are_safe() -> None:
    assert routine.safe_imbalance(Decimal("0"), Decimal("0")) is None
    book = _book()
    for level in book["bids"] + book["asks"]:
        level["amount"] = "0"
    features = routine.calculate_book_features(
        book, depth=5, data_age_seconds=0.5, now=1_700_000_001.0
    )
    assert features.bid_depth == Decimal("0")
    assert features.ask_depth == Decimal("0")
    assert features.top_level_imbalance is None
    assert features.depth_imbalance is None


def test_invalid_crossed_book_fails_closed() -> None:
    book = _book()
    book["asks"][0]["price"] = "100.0"
    with pytest.raises(routine.MarketDataError, match="locked or crossed"):
        routine.calculate_book_features(
            book, depth=5, data_age_seconds=0.5, now=1_700_000_001.0
        )


def test_order_flow_imbalance() -> None:
    assert routine.calculate_order_flow_imbalance(Decimal("3"), Decimal("1")) == Decimal("0.5")
    assert routine.calculate_order_flow_imbalance(Decimal("0"), Decimal("0")) is None


def test_snapshot_validation_marks_missing_crossed_and_stale_data() -> None:
    snapshot = routine.MarketSnapshot(
        timestamp="2026-08-23T00:00:00.000Z",
        connector="derive_perpetual_testnet",
        trading_pair="BTC-USDC",
        book_depth_levels=5,
        best_bid=101.0,
        best_ask=100.0,
        mid_price=100.5,
        spread_abs=-1.0,
        spread_bps=-99.0,
        data_age_seconds=16.0,
    )
    errors = routine.validate_snapshot(snapshot, max_data_age_seconds=15.0)
    assert "best bid is greater than or equal to best ask" in errors
    assert "data is stale (16.0s old; limit 15.0s)" in errors
    assert "absolute spread is invalid" in errors

    missing = routine.MarketSnapshot(
        timestamp="2026-08-23T00:00:00.000Z",
        connector="derive_perpetual_testnet",
        trading_pair="BTC-USDC",
        book_depth_levels=5,
    )
    assert "best bid is missing" in routine.validate_snapshot(missing, 15.0)


def test_diagnostics_staleness_is_reported() -> None:
    age, errors = routine._diagnostic_errors(
        _diagnostics(snapshot_age=16.0), routine.Config(max_data_age_seconds=15.0)
    )
    assert age == 16.0
    assert any("stale order-book tracker" in error for error in errors)


class _MarketData:
    async def get_order_book_diagnostics(self, connector_name: str) -> dict:
        assert connector_name == "derive_perpetual_testnet"
        return _diagnostics()

    async def get_order_book(self, *, connector_name: str, trading_pair: str, depth: int) -> dict:
        assert (connector_name, trading_pair, depth) == (
            "derive_perpetual_testnet",
            "BTC-USDC",
            5,
        )
        return _book()


class _Trading:
    async def get_open_positions(self, account_name: str, connector_name: str) -> dict:
        assert (account_name, connector_name) == ("master_account", "derive_perpetual_testnet")
        return {
            "data": [
                {
                    "trading_pair": "BTC-USDC",
                    "side": "LONG",
                    "amount": 0.0388,
                }
            ]
        }


class _Portfolio:
    async def get_state(self, **kwargs) -> dict:
        assert kwargs["account_names"] == ["master_account"]
        assert kwargs["connector_names"] == ["derive_perpetual_testnet"]
        assert kwargs["skip_gateway"] is True
        assert kwargs["refresh"] is True
        return {
            "master_account": {
                "derive_perpetual_testnet": [
                    {"token": "USDC", "available_units": 99916.5, "units": 99916.5}
                ]
            }
        }


class _OptionsProvider:
    async def snapshot(self, reference_price: float | None, *, now: float):
        assert reference_price == pytest.approx(100.5)
        return routine.OptionsVolatilitySnapshot(
            timestamp="2023-11-14T22:13:21.000Z",
            underlying="BTC",
            reference_price=reference_price,
            expiry="2026-08-28",
            days_to_expiry=4.25,
            atm_strike=100.0,
            atm_distance_pct=0.005,
            call_instrument="BTC-C",
            put_instrument="BTC-P",
            atm_call_iv=0.48,
            atm_put_iv=0.52,
            atm_iv=0.50,
            option_data_timestamp="2023-11-14T22:13:20.500Z",
            option_data_age_seconds=0.5,
            source="test-options",
            environment="production",
            data_available=True,
            confidence=0.99,
        )


def test_collect_snapshot_integrates_read_only_atm_iv_provider() -> None:
    client = SimpleNamespace(
        market_data=_MarketData(), trading=_Trading(), portfolio=_Portfolio()
    )
    snapshot = asyncio.run(
        routine.collect_snapshot_once(
            routine.Config(enable_recent_trades=False),
            client,
            options_provider=_OptionsProvider(),
            now=1_700_000_001.0,
        )
    )

    assert snapshot.iv_data_available is True
    assert snapshot.atm_iv == 0.50
    assert snapshot.atm_call_iv == 0.48
    assert snapshot.atm_put_iv == 0.52
    assert snapshot.option_expiry == "2026-08-28"
    assert snapshot.atm_strike == 100.0
    assert snapshot.iv_confidence == 0.99
    assert snapshot.option_data_source == "test-options"


def test_collect_snapshot_is_normalized_and_account_reads_are_read_only() -> None:
    client = SimpleNamespace(
        market_data=_MarketData(), trading=_Trading(), portfolio=_Portfolio()
    )
    snapshot = asyncio.run(
        routine.collect_snapshot_once(
            routine.Config(enable_recent_trades=False),
            client,
            now=1_700_000_001.0,
        )
    )
    assert snapshot.data_valid is True
    assert snapshot.mid_price == 100.5
    assert snapshot.spread_abs == 1.0
    assert snapshot.spread_bps == pytest.approx(99.50248756)
    assert snapshot.bid_depth == 10.0
    assert snapshot.ask_depth == 5.0
    assert snapshot.current_position == pytest.approx(0.0388)
    assert snapshot.position_notional == pytest.approx(3.8994)
    assert snapshot.available_balance == pytest.approx(99916.5)
    assert snapshot.account_data_available is True
    assert snapshot.trade_data_available is False
    assert snapshot.iv_data_available is False


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages = asyncio.Queue()
        self.subscriptions: list[tuple] = []

    async def subscribe_trades(self, connector: str, pair: str, update_interval: float) -> str:
        self.subscriptions.append((connector, pair, update_interval))
        return "trades_sub"

    async def receive(self) -> dict:
        return await self.messages.get()


class _FakeWebSocketContext:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket
        self.closed = False

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *args) -> None:
        self.closed = True


def test_recent_trade_collector_uses_official_api_websocket() -> None:
    async def scenario() -> None:
        websocket = _FakeWebSocket()
        context = _FakeWebSocketContext(websocket)
        client = SimpleNamespace(ws=SimpleNamespace(market_data=lambda: context))
        collector = routine.RecentTradeCollector(routine.Config())

        await collector.start(client)
        await websocket.messages.put(
            {
                "type": "trades",
                "data": [
                    {"side": "buy", "amount": 3},
                    {"side": "sell", "amount": 1},
                ],
            }
        )
        await asyncio.sleep(0.01)
        window = await collector.drain()
        assert collector.available is True
        assert window.buy_volume == Decimal("3")
        assert window.sell_volume == Decimal("1")
        assert routine.calculate_order_flow_imbalance(
            window.buy_volume, window.sell_volume
        ) == Decimal("0.5")
        await collector.stop()
        assert context.closed is True

    asyncio.run(scenario())


def test_jsonl_persistence_and_bounded_rotation(tmp_path: Path) -> None:
    snapshot = routine.MarketSnapshot(
        timestamp="2026-08-23T00:00:00.000Z",
        connector="derive_perpetual_testnet",
        trading_pair="BTC-USDC",
        book_depth_levels=5,
        best_bid=100.0,
        best_ask=101.0,
        mid_price=100.5,
        spread_abs=1.0,
        spread_bps=99.5,
        data_valid=True,
    )
    path = tmp_path / "snapshots.jsonl"
    routine.append_snapshot(snapshot, path, max_file_bytes=1, max_rotated_files=2)
    routine.append_snapshot(snapshot, path, max_file_bytes=1, max_rotated_files=2)
    assert path.exists()
    assert path.with_name("snapshots.jsonl.1").exists()
    record = json.loads(path.read_text().splitlines()[0])
    assert record["trading_pair"] == "BTC-USDC"
    assert record["data_valid"] is True


def test_console_summary_exposes_unavailable_optional_data() -> None:
    snapshot = routine.MarketSnapshot(
        timestamp="2026-08-23T00:00:00.000Z",
        connector="derive_perpetual_testnet",
        trading_pair="BTC-USDC",
        book_depth_levels=5,
        best_bid=100.0,
        best_ask=101.0,
        mid_price=100.5,
        spread_abs=1.0,
        spread_bps=99.5,
        bid_depth=10.0,
        ask_depth=5.0,
        data_valid=True,
    )
    output = routine.format_console_summary(snapshot)
    assert "[DERIVE DATA]" in output
    assert "OFI: unavailable / unavailable" in output
    assert "ATM IV: unavailable / unavailable" in output
    assert "Data valid: true" in output


def test_routine_has_no_mutating_trading_surface() -> None:
    source = ROUTINE_PATH.read_text()
    for forbidden in (
        ".place_order(",
        ".cancel_order(",
        ".set_leverage(",
        ".set_position_mode(",
        ".create_executor(",
        ".stop_executor(",
    ):
        assert forbidden not in source
