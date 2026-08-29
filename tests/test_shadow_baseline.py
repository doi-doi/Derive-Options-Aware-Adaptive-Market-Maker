"""Stage 12 accounting, exposure, isolation, and report-contract tests."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

SRC_PATH = Path(__file__).parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from dashboard.shadow_reader import read_shadow_state  # noqa: E402
from derive_options_mm.shadow import (  # noqa: E402
    PositionLedger,
    ShadowConfig,
    ShadowMarketFrame,
)
from derive_options_mm.shadow_baseline import (  # noqa: E402
    CONSERVATIVE_MODEL,
    ShadowBaselineSession,
    TimeWeightedExposure,
    reconcile_paper_equity,
)
from integrations.hummingbot.derive_adaptive_grid.execution_logic import (  # noqa: E402
    TradingRuleView,
)

RULE = TradingRuleView(
    min_order_size=Decimal("0.01"),
    min_base_amount_increment=Decimal("0.01"),
    min_price_increment=Decimal("0.01"),
)


def _frame(timestamp: float, *, bid: float = 98.0, ask: float = 100.0) -> ShadowMarketFrame:
    return ShadowMarketFrame(
        timestamp=timestamp,
        trading_pair="ETH-USDC",
        environment="mainnet",
        best_bid=bid,
        best_ask=ask,
        rule=RULE,
    )


def _config(tmp_path: Path, **overrides: object) -> ShadowConfig:
    values: dict[str, object] = {
        "enabled": True,
        "markets": ("BTC-USDC", "ETH-USDC"),
        "enabled_markets": ("ETH-USDC",),
        "sqlite_path": str(tmp_path / "shadow_execution.sqlite3"),
        "event_path": str(tmp_path / "shadow_execution_events.jsonl"),
        "report_root": str(tmp_path / "reports"),
        "checkpoint_interval_seconds": 1,
        "minimum_fill_samples": 1,
        "minimum_markout_samples": 1,
        "minimum_cycle_samples": 1,
    }
    values.update(overrides)
    return ShadowConfig(**values)


def test_pnl_reconciliation_separates_realized_unrealized_and_fees() -> None:
    ledger = PositionLedger(Decimal("100"), fees_known=True, maker_fee_bps=Decimal("10"))
    ledger.apply_fill("ETH-USDC", "buy", Decimal("10"), Decimal("1"))
    ledger.mark({"ETH-USDC": Decimal("12")})
    reconciliation = reconcile_paper_equity(ledger)
    assert reconciliation.status == "PASS"
    assert reconciliation.realized_pnl == Decimal("0")
    assert reconciliation.unrealized_pnl == Decimal("2")
    assert reconciliation.fees == Decimal("0.01")
    assert reconciliation.total_pnl == Decimal("1.99")
    assert reconciliation.current_equity == reconciliation.expected_equity


def test_pnl_reconciliation_handles_short_cycle_and_zero_position() -> None:
    ledger = PositionLedger(Decimal("100"), fees_known=True, maker_fee_bps=Decimal("10"))
    assert reconcile_paper_equity(ledger).current_equity == Decimal("100")
    ledger.apply_fill("ETH-USDC", "sell", Decimal("10"), Decimal("10"))
    ledger.mark({"ETH-USDC": Decimal("9")})
    assert ledger.unrealized_inventory_pnl == Decimal("10")
    ledger.apply_fill("ETH-USDC", "buy", Decimal("9"), Decimal("10"))
    ledger.mark({"ETH-USDC": Decimal("9")})
    reconciliation = reconcile_paper_equity(ledger)
    assert ledger.position("ETH-USDC").amount == 0
    assert reconciliation.realized_pnl == Decimal("10")
    assert reconciliation.fees == Decimal("0.19")
    assert reconciliation.current_equity == Decimal("109.81")
    assert reconciliation.status == "PASS"


def test_time_weighted_exposure_uses_the_timeline_not_end_state() -> None:
    exposure = TimeWeightedExposure(CONSERVATIVE_MODEL)
    exposure.add(
        0,
        assets={
            "ETH-USDC": {
                "amount": 1,
                "mid_price": 100,
                "beta": 2,
                "inventory_ratio": 0.125,
            }
        },
    )
    exposure.add(10, assets={"ETH-USDC": {"amount": 0, "mid_price": 100, "beta": 2}})
    summary = exposure.summary(start=0, end=20)
    assert summary["capital_time_quote_seconds"] == 1000
    assert summary["average_gross_exposure"] == 50
    assert summary["average_absolute_inventory"] == 50
    assert summary["average_btc_beta_exposure"] == 100


def test_baseline_models_are_isolated_and_reports_keep_future_markout_unknown(
    tmp_path: Path,
) -> None:
    session = ShadowBaselineSession(
        _config(tmp_path), session_id="baseline-isolation", project_root=tmp_path
    )
    session.start(timestamp=1000)
    session.assert_isolated()
    for model_session in session.sessions.values():
        model_session.engine.create_order(
            trading_pair="ETH-USDC",
            level_id="buy_0",
            side="buy",
            price=99,
            amount=0.1,
            timestamp=1000,
            best_bid=98,
            best_ask=100,
        )
    # Touch fills on equality; conservative trade-through correctly does not.
    session.run_cycle(
        {
            "BTC-USDC": ShadowMarketFrame(1001, "BTC-USDC", "mainnet", 98, 100, rule=RULE),
            "ETH-USDC": _frame(1001, bid=98, ask=99),
        },
        timestamp=1001,
    )
    session.run_cycle(
        {
            "BTC-USDC": ShadowMarketFrame(1006, "BTC-USDC", "mainnet", 98, 100, rule=RULE),
            "ETH-USDC": _frame(1006),
        },
        timestamp=1006,
    )
    report = session.stop(timestamp=1010, reason="TEST")
    summary = session.summary()
    assert summary["metrics"]["fills"] == 0
    assert summary["touch_optimistic_metrics"]["fills"] == 1
    assert summary["metrics"]["pnl_reconciliation_status"] == "PASS"
    markout_rows = (report.parent / "markouts.csv").read_text(encoding="utf-8")
    assert "MISSING_SESSION_END" in markout_rows
    summary_text = (report.parent / "summary.md").read_text(encoding="utf-8")
    assert "| Asset | Active (h) | Volume | PnL |" in summary_text
    assert "## Sample counts" in summary_text
    assert "PUBLIC TRADE EVIDENCE: UNAVAILABLE" in summary_text
    assert "CONSERVATIVE FILLS: UNAVAILABLE" in summary_text
    assert "9. Sample sufficient for tuning:" in summary_text
    expected_files = {
        "summary.md",
        "summary.json",
        "equity.csv",
        "hourly_metrics.csv",
        "orders.csv",
        "fills.csv",
        "cancels.csv",
        "cycles.csv",
        "markouts.csv",
        "inventory.csv",
        "portfolio_exposure.csv",
        "risk_events.csv",
        "fill_model_comparison.csv",
        "self_tuning_suggestions.csv",
        "data_quality.csv",
        "baseline_manifest.json",
    }
    assert expected_files.issubset({path.name for path in report.parent.iterdir()})
    persisted_summary = json.loads((report.parent / "summary.json").read_text(encoding="utf-8"))
    assert persisted_summary["stage12f"]["shadow_config_hash"] == summary["config_hash"]
    assert (
        persisted_summary["stage12f"]["strategy_config_hash"]
        == summary["strategy_config_hash"]
    )
    assert (report.parent / "stage12f" / "diagnostic_summary.json").is_file()
    manifest = json.loads((report.parent / "baseline_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_type"] == "BASELINE_CONTROL"
    assert manifest["status"] == "COMPLETE"
    assert manifest["config_hash"] == summary["config_hash"]
    assert "strategy_parameters" in manifest
    state = read_shadow_state(tmp_path)
    assert state.available is True
    assert state.metrics["pnl_reconciliation_status"] == "PASS"
    assert state.metrics["real_exchange_mutation_calls"] == 0
    assert state.baseline_records
    assert state.checkpoints


def test_frozen_config_detects_source_change(tmp_path: Path) -> None:
    source = tmp_path / "shadow.yml"
    source.write_text("shadow:\n  enabled: false\n", encoding="utf-8")
    session = ShadowBaselineSession(
        _config(tmp_path),
        session_id="config-freeze",
        config_source_path=source,
    )
    session.start(timestamp=1000)
    source.write_text("shadow:\n  enabled: true\n", encoding="utf-8")
    assert session.check_config_frozen() is False
    assert session.config_contaminated is True


def test_baseline_reports_cancellation_deviation_and_primary_live_metrics(tmp_path: Path) -> None:
    session = ShadowBaselineSession(_config(tmp_path), session_id="baseline-cancel-metrics")
    session.start(timestamp=1000)
    engine = session.sessions[CONSERVATIVE_MODEL].engine
    order = engine.create_order(
        trading_pair="ETH-USDC",
        level_id="buy_0",
        side="buy",
        price=99,
        amount=0.1,
        timestamp=1000,
        best_bid=98,
        best_ask=100,
        mid_price=99,
    )
    engine.cancel_order(
        order.shadow_order_id, timestamp=1002, reason="PRICE_DEVIATION", market_mid=100
    )
    metrics = session.metrics(now=1002)
    assert metrics["median_cancellation_deviation_bps"] == 100.0
    assert metrics["cancel_reason_counts"]["PRICE_DEVIATION"] == 1
    assert metrics["quote_lifetime_by_cancel_reason"]["PRICE_DEVIATION"]["sample_count"] == 1
    assert "markout_by_asset" in metrics
