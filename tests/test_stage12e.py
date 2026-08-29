"""Stage 12E public-trade and root-cause audit tests."""

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
from derive_options_mm.shadow_baseline import ShadowBaselineSession  # noqa: E402
from derive_options_mm.stage12e import (  # noqa: E402
    INSUFFICIENT_TRADE_EVIDENCE,
    PUBLIC_TRADE_STREAM_SUSPECT,
    TRADE_THROUGH_FILLED,
    TRADE_THROUGH_OBSERVED_NO_FILL,
    build_pause_episodes,
    build_plan_invalid_rows,
    build_plan_oscillation,
    build_risk_root_causes,
    build_trade_stream_diagnostics,
    canonical_trade_rows,
    classify_fill_contract,
    legacy_fill_reconciliation,
    normalize_timestamp,
    order_evidence_coverage,
    summarize_order_evidence_coverage,
    timestamp_unit,
    write_stage12e_artifacts,
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


def test_timestamp_units_and_canonical_trade_deduplication() -> None:
    assert timestamp_unit(1_700_000_000) == "seconds"
    assert timestamp_unit(1_700_000_000_000) == "milliseconds"
    assert timestamp_unit(1_700_000_000_000_000) == "microseconds"
    assert timestamp_unit(1_700_000_000_000_000_000) == "nanoseconds"
    epoch, unit = normalize_timestamp("2026-08-28T00:00:00Z")
    assert epoch is not None
    assert unit == "iso8601"

    result = canonical_trade_rows(
        [
            {
                "instrument_name": "ETH-PERP",
                "trade_id": "t-1",
                "timestamp": 1_700_000_000_000,
                "trade_price": 99,
                "trade_amount": 1,
                "direction": "sell",
                "liquidity_role": "maker",
            },
            {
                "instrument_name": "ETH-PERP",
                "trade_id": "t-1",
                "timestamp": 1_700_000_000_000,
                "trade_price": 99,
                "trade_amount": 1,
                "direction": "sell",
                "liquidity_role": "taker",
            },
            {
                "instrument_name": "ETH-PERP",
                "trade_id": "t-2",
                "timestamp": 1_700_000_001_000,
                "trade_price": 100,
                "trade_amount": 2,
                "direction": "buy",
            },
        ],
        instrument_name="ETH-PERP",
    )
    assert result["raw_count"] == 3
    assert result["canonical_count"] == 2
    assert result["duplicate_count"] == 1
    assert result["rows"][0]["liquidity_role"] == "taker"
    assert result["rows"][0]["timestamp"] == 1_700_000_000.0


def test_trade_through_without_shadow_fill_is_not_promoted() -> None:
    order = _order(terminal_epoch=101.0)
    frames = [
        _frame(101.0, collection_start=100.0, collection_end=101.0),
        _frame(
            102.0,
            trades=(ShadowTrade(102.0, 98.0, 1.0, "sell", "t-after-cancel"),),
            collection_start=101.0,
            collection_end=102.0,
        ),
    ]
    result = classify_fill_contract(order, frames, [], end_timestamp=102.0)
    assert result["status"] == TRADE_THROUGH_OBSERVED_NO_FILL
    assert result["actual_shadow_fill_count"] == 0
    assert result["qualifying_trade_after_terminal_count"] == 1
    assert "no conservative ShadowFill" in result["reason"]

    filled = classify_fill_contract(
        order,
        frames,
        [{"fill_id": "fill-1", "shadow_order_id": "order-1"}],
        end_timestamp=102.0,
    )
    assert filled["status"] == TRADE_THROUGH_FILLED
    assert filled["actual_shadow_fill_count"] == 1


def test_quiet_connected_market_has_coverage_but_no_event_evidence() -> None:
    order = _order(terminal_epoch=110.0)
    frames = [
        _frame(105.0, status="CONNECTED_NO_TRADES", collection_start=100.0, collection_end=105.0),
        _frame(110.0, status="CONNECTED_NO_TRADES", collection_start=105.0, collection_end=110.0),
    ]
    coverage = order_evidence_coverage(order, frames, end_timestamp=110.0)
    assert coverage["covered_seconds"] == 10.0
    assert coverage["event_observation_seconds"] == 0.0
    result = classify_fill_contract(order, frames, [], end_timestamp=110.0)
    assert result["status"] == INSUFFICIENT_TRADE_EVIDENCE
    assert result["connection_status"] == "HEALTHY"


def test_quiet_market_with_changing_bbo_and_depth_flags_suspect_stream() -> None:
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
    diagnostics = build_trade_stream_diagnostics([first, second])
    assert diagnostics[0]["trade_silence_classification"] == "FUNCTIONING_BUT_MARKET_SPARSE"
    assert diagnostics[1]["trade_silence_classification"] == PUBLIC_TRADE_STREAM_SUSPECT
    assert diagnostics[1]["market_bbo_valid"] is True
    assert diagnostics[1]["depth_data_available"] is True


def test_order_evidence_coverage_summary_reports_percentiles_and_buckets() -> None:
    summary = summarize_order_evidence_coverage(
        [
            {"record_type": "ORDER", "model": "CONSERVATIVE", "coverage_pct": 10.0},
            {"record_type": "ORDER", "model": "CONSERVATIVE", "coverage_pct": 90.0},
            {"record_type": "ORDER", "model": "CONSERVATIVE", "coverage_pct": 100.0},
            {"record_type": "ORDER", "model": "TOUCH_OPTIMISTIC", "coverage_pct": 0.0},
        ]
    )
    assert summary["orders_total"] == 3
    assert summary["orders_measured"] == 3
    assert summary["orders_unmeasured"] == 0
    assert summary["zero_lifetime_orders"] == 0
    assert summary["median_coverage_pct"] == 90.0
    assert summary["orders_ge_95_pct"] == 1
    assert summary["orders_80_to_95_pct"] == 1
    assert summary["orders_lt_50_pct"] == 1


class _PagedTradeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, method: str, params: dict[str, object]) -> dict[str, object]:
        assert method == "public/get_trade_history"
        self.calls.append(params)
        if params["page"] == 1:
            rows = [
                {
                    "instrument_name": "BTC-PERP",
                    "trade_id": "t-1",
                    "timestamp": 950_000,
                    "trade_price": 99,
                    "trade_amount": 1,
                    "direction": "sell",
                    "liquidity_role": "maker",
                },
                {
                    "instrument_name": "BTC-PERP",
                    "trade_id": "t-1",
                    "timestamp": 950_000,
                    "trade_price": 99,
                    "trade_amount": 1,
                    "direction": "sell",
                    "liquidity_role": "taker",
                },
            ]
        else:
            rows = [
                {
                    "instrument_name": "BTC-PERP",
                    "trade_id": "t-2",
                    "timestamp": 960_000,
                    "trade_price": 100,
                    "trade_amount": 2,
                    "direction": "buy",
                }
            ]
        return {"trades": rows, "pagination": {"num_pages": 2, "count": 3}}


def test_rest_trade_history_paginates_and_deduplicates() -> None:
    client = _PagedTradeClient()
    source = MainnetPublicDataSource(
        client=client,
        trade_transport="rest",
        trade_window_seconds=60,
    )
    trades, metadata = source._rest_trades("BTC", "BTC-PERP", 1_000.0)
    assert [trade.trade_id for trade in trades] == ["t-1", "t-2"]
    assert metadata["page_count"] == 2
    assert metadata["raw_count"] == 3
    assert metadata["canonical_count"] == 2
    assert metadata["duplicate_count"] == 1
    assert [call["page"] for call in client.calls] == [1, 2]


def test_rest_trade_failure_does_not_reuse_stale_trade_rows() -> None:
    class _FailingClient:
        def post(self, method: str, params: dict[str, object]) -> dict[str, object]:
            del method, params
            raise RuntimeError("upstream timeout")

    source = MainnetPublicDataSource(client=_FailingClient(), trade_transport="rest")
    first, first_meta = source._rest_trades("BTC", "BTC-PERP", 1_000.0)
    second, second_meta = source._rest_trades("BTC", "BTC-PERP", 1_005.0)
    assert first == second == ()
    assert first_meta["status"] == second_meta["status"] == "ERROR"
    assert first_meta["raw_count"] == second_meta["raw_count"] == 0


def test_plan_invalid_transitions_pause_episodes_and_risk_trace() -> None:
    valid_plan = {
        "valid": True,
        "enabled": True,
        "mode": "NORMAL",
        "plan_version": 1,
        "buy_levels": [{"level_id": "buy_0"}],
        "sell_levels": [{"level_id": "sell_0"}],
    }
    invalid_plan = {
        "valid": False,
        "enabled": False,
        "mode": "PAUSE",
        "plan_version": 2,
        "reasons": ["grid validation failed closed: quantization"],
    }
    base = {"states": {"ETH-USDC": {"state_valid": True, "reasons": []}}}
    cycles = [
        {
            **base,
            "cycle_id": "c-1",
            "timestamp": "1970-01-01T00:01:40Z",
            "plans": {"ETH-USDC": valid_plan},
            "decisions": {"ETH-USDC": {"mode": "normal", "reasons": []}},
            "portfolio_risk": {},
        },
        {
            **base,
            "cycle_id": "c-2",
            "timestamp": "1970-01-01T00:01:50Z",
            "plans": {"ETH-USDC": invalid_plan},
            "decisions": {
                "ETH-USDC": {"mode": "pause", "reasons": ["plan invalid"]}
            },
            "portfolio_risk": {},
        },
        {
            **base,
            "cycle_id": "c-3",
            "timestamp": "1970-01-01T00:02:00Z",
            "plans": {"ETH-USDC": valid_plan},
            "decisions": {"ETH-USDC": {"mode": "normal", "reasons": []}},
            "portfolio_risk": {},
        },
    ]
    rows = build_plan_invalid_rows(cycles, [_frame(100.0, status="CONNECTED")])
    assert [row["transition"] for row in rows] == [
        "INITIAL_VALID",
        "VALID_TO_INVALID",
        "INVALID_TO_VALID",
    ]
    assert rows[1]["reason_category"] == "GRID_VALIDATION"
    episodes = build_pause_episodes(rows)
    assert len(episodes) == 1
    assert episodes[0]["strategy_or_gate_driven"] is True
    oscillation = build_plan_oscillation(rows)
    assert oscillation[0]["valid_to_invalid_count"] == 1
    assert oscillation[0]["invalid_to_valid_count"] == 1

    risk = build_risk_root_causes(
        [
            {
                "model": "CONSERVATIVE",
                "trading_pair": "ETH-USDC",
                "category": "MIN_EXCHANGE_SIZE",
                "candidate_notional": 5,
                "exposure_before": 10,
                "exposure_after_candidate": 15,
                "pending_entries": {"buy": 10},
            }
        ],
        [
            {
                "model": "CONSERVATIVE",
                "trading_pair": "ETH-USDC",
                "reason": "MIN_EXCHANGE_SIZE",
            }
        ],
    )
    assert risk[0]["candidate_trace_consistent"] is True
    assert risk[0]["pending_exposure_in_trace"] is True
    assert risk[0]["episode_count"] == 1


def test_legacy_contradictory_rows_are_reconciled_without_fabrication(tmp_path: Path) -> None:
    legacy = tmp_path / "shadow-baseline-old"
    legacy.mkdir()
    (legacy / "orders.csv").write_text(
        "model,shadow_order_id,trading_pair,level_id,side,fill_eligibility_status\n"
        "CONSERVATIVE,old-1,ETH-USDC,buy_0,buy,TRADED_THROUGH_FILLED\n",
        encoding="utf-8",
    )
    (legacy / "fills.csv").write_text(
        "model,shadow_order_id,fill_id\nTOUCH_OPTIMISTIC,old-1,touch-fill\n",
        encoding="utf-8",
    )
    rows = legacy_fill_reconciliation(legacy)
    assert rows[0]["status"] == TRADE_THROUGH_OBSERVED_NO_FILL
    assert rows[0]["actual_shadow_fill_count"] == 0
    assert rows[0]["raw_trade_trace_available"] is False


def test_legacy_session_selector_skips_newer_session_without_eligible_rows(
    tmp_path: Path,
) -> None:
    report_root = tmp_path / "reports"
    older = report_root / "shadow-baseline-older"
    newer = report_root / "shadow-baseline-newer"
    completed = report_root / "shadow-baseline-completed-stage12e"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    completed.mkdir(parents=True)
    (completed / "stage12e").mkdir()
    header = "model,shadow_order_id,fill_eligibility_status\n"
    (older / "orders.csv").write_text(
        header + "CONSERVATIVE,old-1,TRADED_THROUGH_FILLED\n", encoding="utf-8"
    )
    (newer / "orders.csv").write_text(header, encoding="utf-8")
    (completed / "orders.csv").write_text(
        header + "CONSERVATIVE,completed-1,TRADED_THROUGH_FILLED\n", encoding="utf-8"
    )
    session = object.__new__(ShadowBaselineSession)
    session.config = SimpleNamespace(report_root=report_root)
    session.project_root = tmp_path
    session.session_id = "shadow-baseline-current"

    assert session._latest_legacy_session_root() == older


def test_stage12e_writer_creates_required_artifacts(tmp_path: Path) -> None:
    order = _order()
    item = SimpleNamespace(
        orders=[order],
        fills=[],
        risk_events=[],
        risk_episodes=[],
        reconciliation_decisions=[],
    )
    summary = write_stage12e_artifacts(
        project_root=tmp_path,
        session_id="stage12e-test",
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
    assert summary["safety"]["status"] == "PASS"
    report_root = tmp_path / "reports"
    required = {
        "fill_contract_audit.csv",
        "trade_pipeline_audit.csv",
        "trade_gap_crosscheck.csv",
        "order_evidence_coverage.csv",
        "plan_invalid_transitions.csv",
        "pause_episodes.csv",
        "plan_oscillation.csv",
        "risk_root_causes.csv",
        "diagnostic_summary.json",
    }
    assert required.issubset({path.name for path in (tmp_path / "reports" / "stage12e").iterdir()})
    assert (tmp_path / "reports" / "stage12e_root_cause.md").is_file()
    assert report_root.joinpath("stage12e", "diagnostic_summary.json").is_file()
