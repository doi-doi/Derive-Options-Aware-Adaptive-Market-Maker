"""Deterministic tests for the mainnet shadow execution boundary."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from derive_options_mm.multi_asset import MultiAssetConfig  # noqa: E402
from derive_options_mm.shadow import (  # noqa: E402
    SHADOW_MODE_EXCHANGE_MUTATION_BLOCKED,
    MainnetPublicDataSource,
    ShadowConfig,
    ShadowEnvironmentError,
    ShadowExecutionEngine,
    ShadowMarketFrame,
    ShadowModeExchangeMutationBlocked,
    ShadowOrderStatus,
    ShadowStore,
    ShadowTrade,
    check_shadow_environment,
)
from derive_options_mm.stage12g import build_zero_lifetime_root_causes  # noqa: E402
from integrations.hummingbot.derive_adaptive_grid.execution_logic import (  # noqa: E402
    TradingRuleView,
)


class _MutationClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_order(self, **_: object) -> None:
        self.calls.append("create_order")

    def ping(self) -> str:
        return "ok"


class _PublicClient:
    def post(self, method: str, _params: object) -> dict[str, object]:
        if method == "public/get_instruments":
            return {
                "instruments": [
                    {
                        "instrument_name": "BTC-PERP",
                        "is_active": True,
                        "minimum_amount": "0.001",
                        "amount_step": "0.001",
                        "tick_size": "0.1",
                    }
                ]
            }
        if method == "public/get_tickers":
            return {
                "tickers": {
                    "BTC-PERP": {
                        "instrument_ticker": {
                            "timestamp": 100.0,
                            "best_bid_price": "99",
                            "best_ask_price": "100",
                        }
                    }
                }
            }
        raise AssertionError(f"unexpected public method: {method}")


def _config(tmp_path: Path, **overrides: object) -> ShadowConfig:
    values: dict[str, object] = {
        "enabled": True,
        "fee_model": "explicit",
        "maker_fee_bps": 1.0,
        "event_path": str(tmp_path / "events.jsonl"),
        "sqlite_path": str(tmp_path / "shadow.sqlite3"),
        "report_root": str(tmp_path / "reports"),
    }
    values.update(overrides)
    return ShadowConfig(**values)


def _frame(
    timestamp: float,
    *,
    best_bid: float = 98.0,
    best_ask: float = 100.0,
    trades: tuple[ShadowTrade, ...] = (),
) -> ShadowMarketFrame:
    return ShadowMarketFrame(
        timestamp=timestamp,
        trading_pair="ETH-USDC",
        environment="mainnet",
        best_bid=best_bid,
        best_ask=best_ask,
        trades=trades,
        rule=TradingRuleView(
            min_order_size=Decimal("0.01"),
            min_base_amount_increment=Decimal("0.01"),
            min_price_increment=Decimal("0.01"),
        ),
    )


def test_shadow_create_is_virtual_and_mutation_barrier_is_exact(tmp_path: Path) -> None:
    client = _MutationClient()
    config = _config(tmp_path)
    store = ShadowStore(config.sqlite_path, config.event_path)
    try:
        engine = ShadowExecutionEngine(
            config,
            session_id="shadow-test",
            store=store,
            exchange_client=client,
        )
        order = engine.create_order(
            trading_pair="ETH-USDC",
            level_id="buy_0",
            side="buy",
            price=99.0,
            amount=0.1,
            timestamp=100.0,
            best_bid=98.0,
            best_ask=100.0,
        )
        assert order.shadow_order_id.startswith("shadow::shadow-test::")
        assert order.order_type == "LIMIT_MAKER"
        assert order.status is ShadowOrderStatus.RESTING
        assert client.calls == []
        assert engine.real_exchange_mutation_calls == 0
        assert engine.exchange is not None
        with pytest.raises(ShadowModeExchangeMutationBlocked) as error:
            engine.exchange.create_order(symbol="ETH-USDC")
        assert str(error.value) == SHADOW_MODE_EXCHANGE_MUTATION_BLOCKED
        assert error.value.method == "create_order"
        assert engine.exchange.ping() == "ok"
    finally:
        store.close()


def test_repeated_exchange_timestamp_is_not_a_same_cycle_cancel(tmp_path: Path) -> None:
    engine = ShadowExecutionEngine(_config(tmp_path), session_id="cycle-aware")
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=99.0,
        amount=0.1,
        timestamp=100.0,
        controller_timestamp=100.0,
        cycle_id="cycle-1",
        best_bid=98.0,
        best_ask=100.0,
    )
    engine.cancel_order(
        order.shadow_order_id,
        timestamp=100.0,
        controller_timestamp=105.0,
        reason="GridPlan invalid",
        reason_code="PLAN_LEVEL_REMOVED",
        decision_context={
            "cycle_id": "cycle-2",
            "plan_valid": False,
            "plan_enabled": False,
            "new_level_present": False,
            "new_mode": "pause",
        },
    )

    assert order.same_cycle_create_cancel is False
    assert order.cancel_cycle_id == "cycle-2"
    assert order.controller_terminal_epoch == 105.0
    assert build_zero_lifetime_root_causes([order.to_record()]) == []


def test_public_ticker_receipt_time_sequences_repeated_source_timestamp() -> None:
    source = MainnetPublicDataSource(
        client=_PublicClient(),
        trade_history_enabled=False,
        market_data_stale_seconds=15.0,
    )
    frame = source.fetch_frame("BTC-USDC", now=101.0)
    assert frame.timestamp == 101.0
    assert frame.source_timestamp_epoch == 100.0
    assert frame.source_timestamp_age_seconds == pytest.approx(1.0)
    assert frame.source_timestamp_stale is False
    assert frame.to_strategy_snapshot()["data_valid"] is True

    stale = source.fetch_frame("BTC-USDC", now=120.0)
    assert stale.timestamp == 120.0
    assert stale.source_timestamp_stale is True
    assert stale.to_strategy_snapshot()["data_valid"] is False


def test_post_only_cross_is_rejected_without_a_fill(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = ShadowExecutionEngine(config)
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=100.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=99.0,
        best_ask=100.0,
    )
    assert order.status is ShadowOrderStatus.REJECTED
    assert engine.metrics(now=100.0)["orders_rejected"] == 1
    assert any(event.get("reason") == "WOULD_VIOLATE_POST_ONLY" for event in engine.events)


def test_conservative_fill_does_not_fallback_to_bbo_without_public_trade(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    engine = ShadowExecutionEngine(config)
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=99.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=98.0,
        best_ask=100.0,
    )
    fills = engine.process_frame(_frame(101.0, best_bid=98.0, best_ask=98.9))
    assert fills == []
    assert order.status is ShadowOrderStatus.RESTING


def test_conservative_fill_requires_future_trade_through_and_native_tp(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = ShadowExecutionEngine(config)
    entry = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=99.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=98.0,
        best_ask=100.0,
        take_profit_price=100.0,
    )
    assert engine.process_frame(_frame(100.0, best_bid=98.0, best_ask=99.0)) == []
    fills = engine.process_frame(
        _frame(
            101.0,
            best_bid=98.0,
            best_ask=98.9,
            trades=(ShadowTrade(101.0, 98.5, 1.0, "sell"),),
        )
    )
    assert len(fills) == 1
    assert fills[0].entry_exit == "entry"
    assert entry.status is ShadowOrderStatus.FILLED
    take_profit = next(order for order in engine.orders.values() if order.is_exit)
    assert take_profit.side == "sell"
    assert take_profit.price > entry.price
    assert take_profit.status is ShadowOrderStatus.CLOSE_RESTING
    exit_fills = engine.process_frame(
        _frame(
            102.0,
            best_bid=100.1,
            best_ask=100.2,
            trades=(ShadowTrade(102.0, 100.1, 1.0, "buy"),),
        )
    )
    assert len(exit_fills) == 1
    assert exit_fills[0].entry_exit == "exit"
    assert take_profit.status is ShadowOrderStatus.FILLED
    assert entry.status is ShadowOrderStatus.COMPLETE
    assert engine.ledger.position("ETH-USDC").amount == 0
    assert engine.metrics(now=102.0)["real_exchange_mutation_calls"] == 0


def test_environment_and_mainnet_config_guards_fail_closed() -> None:
    assert check_shadow_environment([{"environment": "mainnet"}]).consistent
    mixed = check_shadow_environment(
        [{"environment": "mainnet"}, {"option_environment": "testnet"}]
    )
    assert mixed.consistent is False
    nested_mixed = check_shadow_environment(
        [{"environment": "mainnet", "option_snapshot": {"environment": "testnet"}}]
    )
    assert nested_mixed.consistent is False
    with pytest.raises(ValueError, match="execution_mode=SHADOW"):
        MultiAssetConfig(market_environment="mainnet")
    shadow = MultiAssetConfig(market_environment="mainnet", execution_mode="SHADOW")
    assert shadow.execution_enabled is False
    with pytest.raises(ShadowEnvironmentError, match="testnet API URL"):
        MainnetPublicDataSource(base_url="https://api-demo.lyra.finance")


def test_shadow_store_persists_events_across_reader_connection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = ShadowStore(config.sqlite_path, config.event_path)
    engine = ShadowExecutionEngine(config, session_id="persistent", store=store)
    engine.create_order(
        trading_pair="ETH-USDC",
        level_id="sell_0",
        side="sell",
        price=101.0,
        amount=0.1,
        timestamp=100.0,
        best_bid=100.0,
        best_ask=102.0,
    )
    store.close()
    assert Path(config.event_path).read_text(encoding="utf-8").count("ORDER_CREATE") == 1
    assert Path(config.sqlite_path).is_file()
