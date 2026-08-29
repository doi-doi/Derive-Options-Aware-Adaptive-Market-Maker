"""Stage 12F public-trade repair and measurement-quality tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from derive_options_mm.shadow import (  # noqa: E402
    MainnetPublicDataSource,
    ShadowMarketFrame,
    ShadowTrade,
)
from derive_options_mm.stage12f import (  # noqa: E402
    build_order_evidence_quality,
    build_pause_count_reconciliation,
    build_pause_episodes_stage12f,
    build_trade_collector_audit,
    build_trade_crosscheck_rows,
    reconcile_trade_sets,
    write_stage12f_artifacts,
)


def _frame(
    timestamp: float,
    *,
    trades: tuple[ShadowTrade, ...] = (),
    status: str = "CONNECTED",
    collection_start: float | None = None,
    collection_end: float | None = None,
) -> ShadowMarketFrame:
    return ShadowMarketFrame(
        timestamp=timestamp,
        trading_pair="ETH-USDC",
        environment="mainnet",
        best_bid=98.0,
        best_ask=100.0,
        trades=trades,
        trade_collection_status=status,
        trade_collection_start_epoch=collection_start,
        trade_collection_end_epoch=collection_end,
        trade_sample_interval_seconds=5.0,
    )


def _order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
        "shadow_order_id": "order-1",
        "trading_pair": "ETH-USDC",
        "level_id": "buy_0",
        "side": "buy",
        "price": 99.0,
        "created_epoch": 100.0,
        "resting_start_epoch": 100.0,
        "terminal_epoch": None,
        "is_exit": False,
        "notional": 10.0,
    }
    order.update(overrides)
    return order


def test_no_id_composite_keeps_size_and_side_distinct() -> None:
    primary = [
        {"timestamp": 100.0, "price": 99.0, "amount": 1.0, "aggressor_side": "buy"},
        {"timestamp": 100.0, "price": 99.0, "amount": 2.0, "aggressor_side": "buy"},
        {"timestamp": 100.0, "price": 99.0, "amount": 1.0, "aggressor_side": "sell"},
    ]
    result = reconcile_trade_sets(primary, primary)
    assert result["primary_count"] == 3
    assert result["reference_count"] == 3
    assert result["matched_count"] == 3
    assert result["mismatches"] == []

    missing_variant = reconcile_trade_sets(primary[:1], primary[1:])
    assert missing_variant["matched_count"] == 0
    assert {row["reason"] for row in missing_variant["mismatches"]} == {
        "DEDUPE_COLLISION"
    }


def test_mismatch_classification_uses_window_boundary_and_timestamp_units() -> None:
    boundary = reconcile_trade_sets(
        [],
        [{"trade_id": "t-boundary", "timestamp": 100.0, "price": 99.0, "amount": 1.0}],
        window_start=100.0,
        window_end=110.0,
    )
    assert boundary["mismatches"][0]["reason"] == "TIMESTAMP_WINDOW_BOUNDARY"

    unit_error = reconcile_trade_sets(
        [{"trade_id": "t-unit", "timestamp": 100.0, "price": 99.0, "amount": 1.0}],
        [{"trade_id": "t-unit", "timestamp": 100_000.0, "price": 99.0, "amount": 1.0}],
    )
    assert unit_error["mismatches"][0]["reason"] == "TIMESTAMP_UNIT_ERROR"


class _TradeClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, object]] = []

    def post(self, method: str, params: dict[str, object]) -> dict[str, object]:
        assert method == "public/get_trade_history"
        self.calls.append(params)
        return {"trades": self.rows, "pagination": {"num_pages": 1, "count": len(self.rows)}}


def test_websocket_crosscheck_repairs_extra_composite_trade() -> None:
    rest_rows = [
        {
            "instrument_name": "ETH-PERP",
            "timestamp": 105_000,
            "trade_price": 99.0,
            "trade_amount": 1.0,
            "direction": "buy",
        },
        {
            "instrument_name": "ETH-PERP",
            "timestamp": 105_000,
            "trade_price": 99.0,
            "trade_amount": 2.0,
            "direction": "buy",
        },
    ]
    client = _TradeClient(rest_rows)
    source = MainnetPublicDataSource(
        client=client,
        trade_transport="websocket",
        trade_crosscheck_interval_seconds=1.0,
    )
    source.trade_stream = SimpleNamespace(
        connection_status="CONNECTED",
        reconnect_count=0,
        connected_since_epoch=100.0,
        last_error=None,
        snapshot=lambda currency, start, end: (
            ShadowTrade(105.0, 99.0, 1.0, "buy"),
            ShadowTrade(105.0, 99.0, 2.0, "buy"),
            ShadowTrade(105.0, 99.0, 3.0, "buy"),
        ),
    )

    trades, metadata = source._trades("ETH", "ETH-PERP", 110.0)

    assert len(trades) == 2
    assert metadata["crosscheck_raw_status"] == "MISMATCH"
    assert metadata["crosscheck_status"] == "REPAIRED"
    assert metadata["crosscheck_raw_extra_in_collector"] == 1
    assert metadata["recovery_status"] == "REST_AUTHORITATIVE_REPAIR"


def test_reconnect_requests_bounded_backfill_and_marks_completion() -> None:
    rest_rows = [
        {
            "instrument_name": "ETH-PERP",
            "trade_id": "t-1",
            "timestamp": 109_000,
            "trade_price": 99.0,
            "trade_amount": 1.0,
            "direction": "buy",
        }
    ]
    client = _TradeClient(rest_rows)
    source = MainnetPublicDataSource(
        client=client,
        trade_transport="websocket",
        trade_crosscheck_interval_seconds=999.0,
        trade_safety_overlap_seconds=2.0,
    )
    source._last_request_end_epoch["ETH"] = 108.0
    source._last_reconnect_count["ETH"] = 0
    source.trade_stream = SimpleNamespace(
        connection_status="CONNECTED",
        reconnect_count=1,
        connected_since_epoch=100.0,
        last_error=None,
        snapshot=lambda currency, start, end: (),
    )

    _trades, metadata = source._trades("ETH", "ETH-PERP", 110.0)

    assert metadata["backfill_attempted"] is True
    assert metadata["backfill_complete"] is True
    assert metadata["backfill_trades_found"] == 1
    assert metadata["recovery_status"] == "BACKFILL_COMPLETE"
    assert client.calls[0]["from_timestamp"] == 106_000


def test_order_evidence_excludes_zero_lifetime_from_percentiles() -> None:
    orders = [
        _order(shadow_order_id="rejected", resting_start_epoch=None, status="REJECTED"),
        _order(
            shadow_order_id="same-frame",
            terminal_epoch=100.0,
            lifecycle_state="CANCELLED_AFTER_RESTING",
            status="CANCELLED",
        ),
        _order(shadow_order_id="measured", terminal_epoch=110.0),
    ]
    quality = build_order_evidence_quality(
        orders,
        [_frame(105.0, status="CONNECTED_NO_TRADES", collection_start=100.0, collection_end=105.0),
         _frame(110.0, status="CONNECTED_NO_TRADES", collection_start=105.0, collection_end=110.0)],
        end_timestamp=110.0,
        minimum_samples=2,
    )

    assert quality["zero_lifetime_orders"] == 2
    assert quality["coverage_sample_n"] == 1
    assert quality["coverage_health"] == "INSUFFICIENT SAMPLE"
    assert quality["zero_lifetime_counts"]["NEVER_RESTED"] == 1
    assert quality["zero_lifetime_counts"]["CREATED_AND_REMOVED_SAME_FRAME"] == 1


def test_pause_episode_reconciliation_distinguishes_observations_and_episodes() -> None:
    rows = [
        {
            "timestamp": "1970-01-01T00:01:40Z",
            "timestamp_epoch": 100.0,
            "trading_pair": "ETH-USDC",
            "transition": "VALID_TO_INVALID",
            "plan_valid": False,
            "is_paused": True,
            "reason_category": "GRID_VALIDATION",
            "reason": "quantization",
            "desired_level_ids": [],
            "removed_level_ids": ["buy_0"],
            "added_level_ids": [],
            "stop_count": 1,
        },
        {
            "timestamp": "1970-01-01T00:01:45Z",
            "timestamp_epoch": 105.0,
            "trading_pair": "ETH-USDC",
            "transition": "INVALID_CONTINUED",
            "plan_valid": False,
            "is_paused": True,
            "reason_category": "GRID_VALIDATION",
            "reason": "quantization",
            "desired_level_ids": [],
            "removed_level_ids": ["buy_0"],
            "added_level_ids": [],
            "stop_count": 1,
        },
        {
            "timestamp": "1970-01-01T00:01:50Z",
            "timestamp_epoch": 110.0,
            "trading_pair": "ETH-USDC",
            "transition": "INVALID_TO_VALID",
            "plan_valid": True,
            "is_paused": False,
            "reason_category": "NONE",
            "reason": "",
            "desired_level_ids": ["buy_0"],
            "removed_level_ids": [],
            "added_level_ids": ["buy_0"],
            "stop_count": 0,
        },
    ]

    episodes = build_pause_episodes_stage12f(rows)
    reconciliation = build_pause_count_reconciliation(
        rows,
        episodes,
        start_timestamp=100.0,
        end_timestamp=110.0,
    )

    assert len(episodes) == 1
    assert episodes[0]["raw_pause_observation_count"] == 2
    assert episodes[0]["orders_cancelled"] == 2
    assert episodes[0]["same_level_returned_afterward"] is True
    assert reconciliation[0]["pause_observations"] == 2
    assert reconciliation[0]["unique_asset_pause_episodes"] == 1
    assert reconciliation[0]["count_reconciliation_pass"] is True


def test_crosscheck_rows_include_raw_and_repaired_status() -> None:
    frame = _frame(110.0)
    frame = ShadowMarketFrame(
        **{
            **frame.__dict__,
            "trade_crosscheck_raw_status": "MISMATCH",
            "trade_crosscheck_status": "REPAIRED",
            "trade_crosscheck_raw_collector_count": 3,
            "trade_crosscheck_raw_rest_count": 2,
            "trade_crosscheck_raw_missing_from_collector": 0,
            "trade_crosscheck_raw_extra_in_collector": 1,
            "trade_crosscheck_matched_count": 2,
            "trade_crosscheck_extra_ids": ("row:105.0:99.0:3.0:buy",),
        }
    )

    crosschecks, mismatches = build_trade_crosscheck_rows([frame])

    assert crosschecks[0]["raw_status"] == "MISMATCH"
    assert crosschecks[0]["status"] == "REPAIRED"
    assert crosschecks[0]["repaired"] is True
    assert mismatches[0]["reason"] in {"REST_MISSING_TRADE", "PRIMARY_MISSING_TRADE"}


def test_collector_audit_counts_suspect_quiet_frames() -> None:
    first = ShadowMarketFrame(
        timestamp=100.0,
        trading_pair="ETH-USDC",
        environment="mainnet",
        best_bid=98.0,
        best_ask=100.0,
        best_bid_size=2.0,
        best_ask_size=3.0,
        trade_collection_status="CONNECTED_NO_TRADES",
    )
    second = ShadowMarketFrame(
        timestamp=105.0,
        trading_pair="ETH-USDC",
        environment="mainnet",
        best_bid=98.5,
        best_ask=100.5,
        best_bid_size=2.0,
        best_ask_size=4.0,
        trade_collection_status="CONNECTED_NO_TRADES",
    )

    rows = build_trade_collector_audit(
        [first, second], start_timestamp=100.0, end_timestamp=110.0
    )

    assert rows[0]["suspect_frame_count"] == 1
    assert rows[0]["classification"] == "PUBLIC_TRADE_STREAM_SUSPECT"


def test_stage12f_writer_creates_required_artifacts(tmp_path: Path) -> None:
    item = SimpleNamespace(
        orders=[_order()],
        fills=[],
        risk_events=[],
        risk_episodes=[],
        reconciliation_decisions=[],
    )
    summary = write_stage12f_artifacts(
        project_root=tmp_path,
        session_id="stage12f-test",
        config={
            "market_environment": "mainnet",
            "execution_mode": "SHADOW",
            "execution_backend": "SHADOW",
            "execution_enabled": False,
            "allow_mainnet_trading": False,
            "report_root": str(tmp_path / "reports"),
        },
        frames=[
            _frame(
                105.0,
                status="CONNECTED_NO_TRADES",
                collection_start=100.0,
                collection_end=105.0,
            ),
            _frame(
                110.0,
                status="CONNECTED_NO_TRADES",
                collection_start=105.0,
                collection_end=110.0,
            ),
        ],
        model_metrics={"CONSERVATIVE": item, "TOUCH_OPTIMISTIC": item},
        cycles_by_model={"CONSERVATIVE": []},
        start_timestamp=100.0,
        end_timestamp=110.0,
    )

    assert summary["stage"] == "12F"
    assert (tmp_path / "reports" / "stage12f_trade_pipeline.md").is_file()
    root = tmp_path / "reports" / "stage12f"
    required = {
        "trade_collector_architecture.md",
        "trade_collector_audit.csv",
        "trade_crosscheck.csv",
        "trade_mismatch_reasons.csv",
        "trade_gap_recovery.csv",
        "order_evidence_coverage.csv",
        "zero_lifetime_root_causes.csv",
        "pause_count_reconciliation.csv",
        "pause_episodes.csv",
        "plan_oscillation.csv",
        "fill_contract_summary.csv",
        "diagnostic_summary.json",
    }
    assert required.issubset({path.name for path in root.iterdir()})
    assert (
        tmp_path / "reports" / "stage12f-test" / "stage12f" / "diagnostic_summary.json"
    ).is_file()
