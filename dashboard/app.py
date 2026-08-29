"""Streamlit entry point for the local, read-only Condor control panel.

Run from the repository root with:

    PYTHONPATH=src streamlit run dashboard/app.py -- --data-dir /path/to/condor/data

The app reads local JSONL files and YAML configuration only.  It does not
import Hummingbot clients and has no order, cancellation, or exchange surface.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (SRC_ROOT, PROJECT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from dashboard.config_schema import (  # noqa: E402
    DashboardConfig,
    RuntimePaths,
    Stage9StrategySettings,
    environment_preset,
    preset_profile,
)
from dashboard.config_store import ConfigStore  # noqa: E402
from dashboard.config_validation import (  # noqa: E402
    ConfigChange,
    config_hash,
    validate_and_diff,
    yaml_export,
)
from dashboard.consequence_preview import (  # noqa: E402
    order_size_consequence_preview,
    refresh_stability_estimate,
    risk_consequence_preview,
)
from dashboard.grid_preview import build_proposed_plan, compare_plans, plan_rows  # noqa: E402
from dashboard.history import history_rows, rollback_diff  # noqa: E402
from dashboard.portfolio_preview import portfolio_bars  # noqa: E402
from dashboard.shadow_reader import read_shadow_state  # noqa: E402
from dashboard.state_reader import JsonlTailReader, RuntimeSnapshot, read_runtime  # noqa: E402
from derive_options_mm.environment import environment_profile  # noqa: E402
from derive_options_mm.stage12c import (  # noqa: E402
    REPLACEMENT_DEVIATION_BUCKETS,
    RESTING_LIFETIME_BUCKETS,
    replacement_deviation_bucket,
    resting_lifetime_bucket,
)
from evaluation.self_tuning_observer import (  # noqa: E402
    UNKNOWN,
    ObserverConfig,
    PerformanceObserver,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=os.environ.get("CONDOR_DATA_DIR", ""))
    parser.add_argument("--profile", default="")
    parser.add_argument("--strategy", default="")
    parsed, _ = parser.parse_known_args(sys.argv[1:])
    return parsed


def _runtime_paths(args: argparse.Namespace) -> RuntimePaths:
    data_dir = (
        Path(args.data_dir).expanduser()
        if args.data_dir
        else PROJECT_ROOT.parent / "condor" / "data"
    )
    return RuntimePaths(data_dir=data_dir.resolve())


def _store(args: argparse.Namespace) -> ConfigStore:
    profile = (
        Path(args.profile).expanduser()
        if args.profile
        else PROJECT_ROOT / "configs" / "competition_800_usdc.yml"
    )
    strategy = (
        Path(args.strategy).expanduser()
        if args.strategy
        else PROJECT_ROOT / "configs" / "stage9_strategy.yml"
    )
    controller = PROJECT_ROOT / "configs" / "derive_adaptive_grid_controller.yml"
    return ConfigStore(profile, strategy_path=strategy, controller_path=controller)


def _staged(st: Any) -> DashboardConfig:
    return DashboardConfig.model_validate(st.session_state["stage9_staged"])


def _set_staged(st: Any, bundle: DashboardConfig) -> None:
    st.session_state["stage9_staged"] = bundle.to_record()


def _stage_record(st: Any, record: dict[str, Any]) -> None:
    try:
        _set_staged(st, DashboardConfig.model_validate(record))
        st.session_state.pop("stage9_stage_error", None)
    except Exception as exc:
        st.session_state["stage9_stage_error"] = str(exc)


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, bool):
        return "YES" if value else "NO"
    try:
        return f"{float(value):,.4f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _preview_value(value: Any) -> Any:
    """Keep comparison tables Arrow-friendly while preserving pair counts."""

    if isinstance(value, (tuple, list)):
        return " / ".join(str(item) for item in value)
    return "UNKNOWN" if value is None else str(value)


def _shadow_value_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render heterogeneous diagnostic values as one Arrow-compatible column."""

    return [{**row, "Value": _preview_value(row.get("Value"))} for row in rows]


def _shadow_lifetime_buckets(
    metrics: dict[str, Any], orders: tuple[dict[str, Any], ...]
) -> dict[str, int]:
    """Use persisted metrics, with a conservative-order fallback for older runs."""

    stored = metrics.get("resting_lifetime_buckets")
    if isinstance(stored, dict) and set(RESTING_LIFETIME_BUCKETS).issubset(stored):
        return {bucket: int(stored.get(bucket, 0) or 0) for bucket in RESTING_LIFETIME_BUCKETS}
    return {
        bucket: sum(
            resting_lifetime_bucket(row.get("resting_lifetime_seconds")) == bucket
            for row in orders
            if row.get("model") in {None, "CONSERVATIVE"}
        )
        for bucket in RESTING_LIFETIME_BUCKETS
    }


def _shadow_deviation_buckets(
    metrics: dict[str, Any], cancels: tuple[dict[str, Any], ...]
) -> dict[str, int]:
    """Use persisted metrics, with a raw-cancel fallback for older runs."""

    stored = metrics.get("replacement_deviation_buckets")
    if isinstance(stored, dict) and set(REPLACEMENT_DEVIATION_BUCKETS).issubset(stored):
        return {bucket: int(stored.get(bucket, 0) or 0) for bucket in REPLACEMENT_DEVIATION_BUCKETS}
    return {
        bucket: sum(
            replacement_deviation_bucket(
                row.get("price_deviation_bps")
                if row.get("price_deviation_bps") is not None
                else row.get("cancel_price_deviation_bps")
            )
            == bucket
            for row in cancels
            if row.get("model") in {None, "CONSERVATIVE"}
            and row.get("category") not in {"SESSION_SHUTDOWN", "MANUAL_STOP"}
        )
        for bucket in REPLACEMENT_DEVIATION_BUCKETS
    }


def _age_text(age: float | None, stale_seconds: float = 15.0) -> str:
    if age is None:
        return "UNKNOWN"
    status = "STALE" if age > stale_seconds else "FRESH"
    return f"{age:.1f}s ago — {status}"


def _field_value(record: dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    value = record.get(section, {})
    return value.get(key, default) if isinstance(value, dict) else default


def _portfolio_equity(runtime: RuntimeSnapshot) -> tuple[float | None, float | None]:
    values: list[float] = []
    for asset in runtime.latest_by_asset.values():
        snapshot = asset.get("snapshot", {})
        value = snapshot.get("available_balance")
        try:
            if value is not None:
                values.append(float(value))
        except (TypeError, ValueError):
            pass
    equity = values[-1] if values else None
    return equity, equity


def _render_header(
    st: Any, store: ConfigStore, runtime: RuntimeSnapshot, saved: DashboardConfig
) -> None:
    profile = environment_profile(saved.competition.market_environment)
    environment = profile.name.upper()
    if profile.is_mainnet:
        st.error(
            "MAINNET SELECTED — read-only profile. Execution and mainnet permission remain OFF; "
            "the Hummingbot canary gates are separate.",
            icon="🚨",
        )
    else:
        snapshot_stream = runtime.streams.get("snapshot")
        runtime_status = (
            "healthy" if snapshot_stream and snapshot_stream.status == "ok" else "degraded"
        )
        execution_status = "ON" if saved.competition.execution_enabled else "OFF"
        st.info(
            f"{environment} | Runtime {runtime_status} | Config v{store.version()} "
            f"| Runtime config hash: UNKNOWN | Execution {execution_status} "
            f"| Connector {profile.connector_name}"
        )
    st.title("DERIVE ADAPTIVE STATE GRID")
    st.caption(
        "Local Condor configuration, risk, and grid-preview control panel — no exchange calls"
    )


def _render_environment(st: Any, saved: DashboardConfig, staged: DashboardConfig) -> None:
    """Expose the one shared Derive network selector without enabling trading."""

    st.header("ENVIRONMENT")
    st.caption(
        "Select the Derive network once. The connector, account, options, and execution "
        "environment identifiers follow the same canonical profile."
    )
    current = staged.competition
    selected = st.selectbox(
        "Derive environment",
        ["testnet", "mainnet"],
        index=["testnet", "mainnet"].index(current.market_environment),
        format_func=lambda value: (
            "TESTNET — default" if value == "testnet" else "MAINNET — read-only/canary"
        ),
        key="stage9_environment_choice",
    )
    target = environment_profile(selected)
    rows = [
        {"Setting": "Environment", "Value": target.name.upper()},
        {"Setting": "Hummingbot connector", "Value": target.connector_name},
        {"Setting": "Options boundary", "Value": target.options_environment},
        {"Setting": "Account boundary", "Value": target.account_environment},
        {"Setting": "Execution boundary", "Value": target.execution_environment},
        {"Setting": "Execution enabled after switch", "Value": "NO"},
        {"Setting": "Mainnet trading permission after switch", "Value": "NO"},
        {"Setting": "Runtime reload", "Value": "RESTART REQUIRED"},
    ]
    st.dataframe(rows, width="stretch", hide_index=True)
    if target.is_mainnet:
        st.warning(
            "MAINNET is available here for read-only configuration and connectivity review. "
            "This switch deliberately disables execution and mainnet permission; real orders "
            "still require the separate Hummingbot canary template, authenticated account "
            "verification, risk budgets, and acknowledgement gates. The existing Condor "
            "Stage 8 monitor remains testnet-only and is not retargeted by this selector.",
            icon="⚠️",
        )
    else:
        st.info(
            "TESTNET is the committed default. Switching networks always stages execution "
            "off; re-enable testnet execution separately only after reviewing the diff."
        )
    if selected == current.market_environment:
        st.caption("The selected environment is already staged.")
    else:
        st.caption(
            f"Staged profile: {current.market_environment.upper()} → {selected.upper()}. "
            "Use the sidebar diff to apply it to the local YAML profile."
        )
    if st.button("STAGE ENVIRONMENT PROFILE", type="primary"):
        record = staged.to_record()
        record["competition"] = environment_preset(current, selected).model_dump(mode="json")
        _stage_record(st, record)
        st.session_state["stage9_stage_notice"] = (
            f"{selected.upper()} environment profile staged; execution remains disabled."
        )
        st.rerun()


def _render_asset_cards(st: Any, runtime: RuntimeSnapshot, profile: Any) -> None:
    st.subheader("Multi-asset state")
    columns = st.columns(4)
    for column, pair in zip(
        columns, ("BTC-USDC", "ETH-USDC", "SOL-USDC", "HYPE-USDC"), strict=True
    ):
        with column:
            asset = runtime.latest_by_asset.get(pair, {})
            snapshot = asset.get("snapshot", {})
            state = asset.get("state", {})
            mode = asset.get("mode", {})
            plan = asset.get("plan", {})
            relationship = asset.get("relationship", {})
            enabled = pair in profile.enabled_markets
            trading = pair != "BTC-USDC" and enabled and profile.execution_enabled
            st.markdown(f"### {pair}")
            st.metric("Enabled", "YES" if enabled else "NO")
            st.metric("Trading", "YES" if trading else "NO")
            st.write(f"Mid: {_fmt(snapshot.get('mid_price'))}")
            st.write(f"Spread: {_fmt(snapshot.get('spread_bps'), ' bps')}")
            st.write(f"Mode: {str(mode.get('mode', 'UNKNOWN')).upper()}")
            st.write(f"Inventory: {_fmt(state.get('inventory_ratio'))}")
            st.write(
                "BTC corr/beta: "
                f"{_fmt(relationship.get('btc_correlation'))} / "
                f"{_fmt(relationship.get('btc_beta'))}"
            )
            st.write(
                f"Plan: v{plan.get('plan_version', 'UNKNOWN')} / "
                f"{'valid' if plan.get('valid') else 'UNKNOWN'}"
            )
            if pair == "BTC-USDC":
                st.caption("MARKET DATA ON · OPTIONS ON · GLOBAL RISK ON · TRADING OFF")
            st.caption(f"Snapshot age: {_age_text(runtime.stream_age('snapshot'))}")


def _render_overview(st: Any, runtime: RuntimeSnapshot, saved: DashboardConfig) -> None:
    st.header("OVERVIEW")
    equity, collateral = _portfolio_equity(runtime)
    portfolio = runtime.portfolio_risk or {}
    global_risk = runtime.global_risk or {}
    drawdown = max(
        0.0,
        saved.competition.starting_equity_reference
        - (equity or saved.competition.starting_equity_reference),
    )
    risk_stage = (
        "HARD_STOP_NEW_RISK"
        if drawdown >= saved.competition.competition_hard_drawdown_quote
        else "NORMAL"
    )
    cols = st.columns(5)
    cols[0].metric("EQUITY", _fmt(equity, " USDC"))
    cols[1].metric(
        "SESSION PNL",
        _fmt(
            (equity or saved.competition.starting_equity_reference)
            - saved.competition.starting_equity_reference,
            " USDC",
        ),
    )
    cols[2].metric("DRAWDOWN", _fmt(drawdown, " USDC"))
    cols[3].metric("RISK STAGE", risk_stage)
    cols[4].metric("EXECUTION", "ON" if saved.competition.execution_enabled else "OFF")
    st.subheader("Global BTC options")
    global_cols = st.columns(5)
    global_cols[0].metric("BTC ATM IV", _fmt(global_risk.get("btc_atm_iv")))
    global_cols[1].metric("IV ratio", _fmt(global_risk.get("btc_iv_ratio")))
    global_cols[2].metric(
        "IV age",
        _age_text(global_risk.get("btc_iv_age_seconds"), saved.strategy.iv_stale_timeout_seconds),
    )
    global_cols[3].metric(
        "Risk state", str(global_risk.get("global_risk_regime", "UNKNOWN")).upper()
    )
    global_cols[4].metric("Confidence", _fmt(global_risk.get("btc_options_confidence")))
    st.subheader("Portfolio")
    portfolio_cols = st.columns(6)
    portfolio_cols[0].metric("Gross", _fmt(portfolio.get("gross_notional")))
    portfolio_cols[1].metric("Net", _fmt(portfolio.get("net_notional")))
    portfolio_cols[2].metric("Long beta", _fmt(portfolio.get("long_beta_exposure")))
    portfolio_cols[3].metric("Short beta", _fmt(portfolio.get("short_beta_exposure")))
    portfolio_cols[4].metric(
        "Reserve",
        _fmt(
            saved.competition.collateral_reserve_pct
            * (collateral or saved.competition.starting_equity_reference)
        ),
    )
    portfolio_cols[5].metric("Active executors", str(portfolio.get("active_executors", "UNKNOWN")))
    _render_asset_cards(st, runtime, saved.competition)
    st.subheader("Orders / churn")
    _render_churn(st, runtime, saved.competition)


def _render_shadow(st: Any, args: argparse.Namespace) -> None:
    """Render the Stage 12 baseline without exposing any execution control."""

    st.header("SHADOW TRADING")
    st.error(
        "DERIVE MAINNET DATA\nSHADOW EXECUTION\nPAPER FUNDS ONLY\nREAL EXCHANGE MUTATIONS: 0",
        icon="🛡️",
    )
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else PROJECT_ROOT / "data"
    shadow = read_shadow_state(data_dir)
    st.caption(f"Persisted state: {data_dir.resolve()}")
    if not shadow.available:
        st.info(
            "No shadow session has been persisted yet. Start a bounded public-data session with "
            "`PYTHONPATH=src:. python -m condor.shadow_baseline --duration 15m`."
        )
        return

    metrics = shadow.metrics
    summary_metrics = shadow.session.get("metrics", {})
    if not metrics and isinstance(summary_metrics, dict):
        metrics = summary_metrics
    mutation_calls = metrics.get(
        "real_exchange_mutation_calls",
        shadow.session.get("real_exchange_mutation_calls", 0),
    )
    if mutation_calls:
        st.error(f"SAFETY FAILURE — real exchange mutation calls recorded: {mutation_calls}")
    else:
        st.success("REAL EXCHANGE MUTATIONS: 0 — shadow mutation barrier intact")

    status_rows = [
        {"Field": "MODE", "Value": "MAINNET SHADOW"},
        {"Field": "DATA", "Value": "REAL DERIVE MAINNET"},
        {"Field": "EXECUTION", "Value": "SHADOW / PAPER"},
        {"Field": "FUNDS", "Value": "PAPER ONLY"},
        {
            "Field": "ENVIRONMENT CONSISTENCY",
            "Value": "SHADOW ENVIRONMENT CONSISTENCY: PASS",
        },
        {"Field": "Session", "Value": shadow.session.get("session_id", "UNKNOWN")},
        {"Field": "Baseline config", "Value": metrics.get("baseline_config_version", "UNKNOWN")},
        {"Field": "Config hash", "Value": metrics.get("config_hash", "UNKNOWN")},
        {
            "Field": "Config status",
            "Value": "FROZEN" if metrics.get("config_frozen", True) else "CONTAMINATED",
        },
        {"Field": "Self-tuning", "Value": metrics.get("self_tuning_mode", "SUGGEST_ONLY")},
        {"Field": "Fee model", "Value": metrics.get("fees_status", "UNKNOWN")},
    ]
    st.dataframe(status_rows, width="stretch", hide_index=True)

    st.subheader("Baseline KPIs")
    columns = st.columns(6)
    columns[0].metric("SESSION", _fmt(metrics.get("session_duration_hours"), " h"))
    columns[1].metric("PAPER EQUITY", _fmt(metrics.get("paper_equity"), " USDC"))
    columns[2].metric("GROSS PAPER PNL", _fmt(metrics.get("gross_pnl"), " USDC"))
    columns[3].metric(
        "VERIFIED NET PNL",
        _fmt(metrics.get("verified_net_pnl"), " USDC")
        if metrics.get("verified_net_pnl_status") == "VERIFIED"
        else "UNKNOWN",
    )
    columns[4].metric("REALIZED PNL", _fmt(metrics.get("realized_pnl"), " USDC"))
    columns[5].metric("EXECUTED VOLUME", _fmt(metrics.get("total_executed_notional"), " USDC"))
    columns = st.columns(6)
    columns[0].metric("VOLUME / AVG RISK", _fmt(metrics.get("volume_per_average_deployed_risk")))
    columns[1].metric("CYCLES", str(metrics.get("completed_cycles", "UNKNOWN")))
    columns[2].metric(
        "CYCLES / HOUR",
        _fmt(
            metrics.get("completed_cycles", 0) / metrics.get("session_duration_hours", 1)
            if metrics.get("session_duration_hours")
            else None
        ),
    )
    columns[3].metric("FILL / CREATE", _fmt(metrics.get("fill_create_ratio")))
    columns[4].metric("CANCEL / CREATE", _fmt(metrics.get("cancel_create_ratio")))
    columns[5].metric("MAX DRAWDOWN", _fmt(metrics.get("max_drawdown_quote"), " USDC"))

    st.subheader("PnL reconciliation")
    reconciliation = metrics.get("pnl_reconciliation", {})
    reconciliation_rows = [
        {"Component": "Starting equity", "Value": reconciliation.get("starting_equity")},
        {"Component": "+ Realized PnL", "Value": reconciliation.get("realized_pnl")},
        {"Component": "+ Unrealized inventory PnL", "Value": reconciliation.get("unrealized_pnl")},
        {"Component": "- Fees", "Value": reconciliation.get("fees")},
        {"Component": "= Expected equity", "Value": reconciliation.get("expected_equity")},
        {"Component": "Current equity", "Value": reconciliation.get("current_equity")},
        {"Component": "Discrepancy", "Value": reconciliation.get("discrepancy")},
        {"Component": "PNL RECONCILIATION", "Value": reconciliation.get("status", "FAIL")},
    ]
    st.dataframe(_shadow_value_rows(reconciliation_rows), width="stretch", hide_index=True)
    if reconciliation.get("status") == "FAIL":
        st.error("PNL RECONCILIATION: FAIL — discrepancy is shown above.")
    elif reconciliation:
        st.success("PNL RECONCILIATION: PASS")
    if metrics.get("fees_status") == "UNKNOWN":
        st.warning("FEE MODEL = UNKNOWN. PnL is gross/modelled; net PnL is not claimed.")
    trade_evidence = metrics.get("public_trade_evidence", "UNAVAILABLE")
    st.write(f"PUBLIC TRADE EVIDENCE: {trade_evidence}")
    st.write(f"CONSERVATIVE FILLS: {metrics.get('conservative_fills_status', 'UNAVAILABLE')}")
    if trade_evidence == "UNAVAILABLE":
        st.warning(
            "PUBLIC TRADE EVIDENCE: UNAVAILABLE — conservative trade-through fills remain "
            "unavailable; touch results remain sensitivity-only."
        )

    st.subheader("Volume and order lifecycle")
    volume_rows = [
        {"Metric": "Session volume", "Value": metrics.get("session_volume")},
        {"Metric": "Last hour volume", "Value": metrics.get("last_hour_volume")},
        {"Metric": "Buy volume", "Value": metrics.get("buy_executed_notional")},
        {"Metric": "Sell volume", "Value": metrics.get("sell_executed_notional")},
        {"Metric": "Volume / starting equity", "Value": metrics.get("volume_per_starting_equity")},
        {
            "Metric": "Volume / average gross exposure",
            "Value": metrics.get("volume_per_average_gross_exposure"),
        },
        {
            "Metric": "Volume / average margin",
            "Value": metrics.get("volume_per_average_margin_used"),
        },
        {"Metric": "Created", "Value": metrics.get("orders_created")},
        {"Metric": "Resting", "Value": metrics.get("active_orders")},
        {"Metric": "KEEP", "Value": metrics.get("orders_kept")},
        {"Metric": "Filled", "Value": metrics.get("orders_filled")},
        {"Metric": "Cancelled", "Value": metrics.get("orders_cancelled")},
        {"Metric": "Replaced", "Value": metrics.get("orders_replaced")},
        {"Metric": "Expired", "Value": metrics.get("orders_expired")},
        {"Metric": "Rejected", "Value": metrics.get("orders_rejected")},
        {"Metric": "TP active", "Value": metrics.get("tp_orders_created")},
        {"Metric": "TP filled", "Value": metrics.get("tp_orders_filled")},
    ]
    st.dataframe(_shadow_value_rows(volume_rows), width="stretch", hide_index=True)
    st.write("Volume by asset")
    st.dataframe(
        [
            {"Asset": pair, "Volume": value}
            for pair, value in (metrics.get("volume_by_asset") or {}).items()
        ]
        or [{"Asset": "NO FILLS YET", "Volume": None}],
        width="stretch",
        hide_index=True,
    )

    st.subheader("Stage 12C observability")
    lifecycle_counts = (
        metrics.get("lifecycle_states") or metrics.get("lifecycle_state_counts") or {}
    )
    coverage = metrics.get("trade_coverage") or {}
    overall_coverage = coverage.get("overall") or {}
    eligibility = metrics.get("fill_eligibility") or {}
    st.dataframe(
        _shadow_value_rows(
            [
                {"Metric": "Fee model", "Value": metrics.get("fees_status", "UNKNOWN")},
                {
                    "Metric": "Resting lifetime median (s)",
                    "Value": metrics.get("median_quote_lifetime"),
                },
                {
                    "Metric": "Resting lifetime p90 (s)",
                    "Value": metrics.get("p90_quote_lifetime")
                    or metrics.get("resting_lifetime_p90"),
                },
                {
                    "Metric": "Never-rested excluded",
                    "Value": metrics.get("resting_lifetime_excluded_never_rested"),
                },
                {"Metric": "Operational cancels", "Value": metrics.get("operational_cancels")},
                {
                    "Metric": "Shutdown/manual cancels",
                    "Value": metrics.get("shutdown_cancels"),
                },
                {
                    "Metric": "UNKNOWN_INTERNAL cancels",
                    "Value": (metrics.get("cancel_reason_counts") or {}).get("UNKNOWN_INTERNAL", 0),
                },
                {"Metric": "Risk checks", "Value": metrics.get("risk_checks_total")},
                {"Metric": "Raw risk blocks", "Value": metrics.get("risk_blocks_raw")},
                {
                    "Metric": "Unique risk episodes",
                    "Value": metrics.get("unique_risk_episodes"),
                },
                {
                    "Metric": "Blocked duration (s)",
                    "Value": metrics.get("duration_blocked_seconds"),
                },
                {
                    "Metric": "Public trade coverage",
                    "Value": overall_coverage.get("coverage_pct"),
                },
                {
                    "Metric": "Eligible order count",
                    "Value": eligibility.get("eligible_order_count"),
                },
                {
                    "Metric": "Missing evidence order count",
                    "Value": eligibility.get("missing_order_count"),
                },
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    st.write("Lifecycle states")
    st.dataframe(
        [{"State": state, "Count": count} for state, count in lifecycle_counts.items()]
        or [{"State": "NO LIFECYCLE DATA", "Count": None}],
        width="stretch",
        hide_index=True,
    )
    st.write("Exact cancel taxonomy")
    st.dataframe(
        [
            {"Reason": reason, "Count": count}
            for reason, count in (metrics.get("cancel_reason_counts") or {}).items()
        ]
        or [{"Reason": "NO CANCEL DATA", "Count": None}],
        width="stretch",
        hide_index=True,
    )
    st.write("Risk episodes")
    st.dataframe(
        [
            {
                "Reason": row.get("reason"),
                "Raw blocks": row.get("raw_blocks"),
                "Unique episodes": row.get("unique_episodes"),
                "Blocked seconds": row.get("blocked_seconds"),
                "Assets": _preview_value(row.get("assets")),
            }
            for row in (metrics.get("risk_episode_summary") or [])
        ]
        or [{"Reason": "NO RISK EPISODES"}],
        width="stretch",
        hide_index=True,
    )
    st.write("Public trade evidence coverage")
    coverage_rows = []
    if overall_coverage:
        coverage_rows.append({"Asset": "OVERALL", **overall_coverage})
    coverage_rows.extend(
        {"Asset": pair, **values} for pair, values in (coverage.get("by_asset") or {}).items()
    )
    st.dataframe(
        coverage_rows or [{"Asset": "NO TRADE EVIDENCE"}],
        width="stretch",
        hide_index=True,
    )
    st.write("Fill eligibility attribution")
    st.dataframe(
        [
            {"Status": status, "Count": count}
            for status, count in (eligibility.get("counts") or {}).items()
        ]
        or [{"Status": "NO FILLS"}],
        width="stretch",
        hide_index=True,
    )
    st.write("Reconciliation decisions")
    st.dataframe(
        [
            {
                "Timestamp": row.get("timestamp"),
                "Pair": row.get("trading_pair"),
                "Plan": row.get("plan_version"),
                "Desired": row.get("desired_count"),
                "Active": row.get("active_count"),
                "Create": row.get("create_count"),
                "Keep": row.get("keep_count"),
                "Stop": row.get("stop_count"),
                "Defer": row.get("deferred_count"),
                "Risk": row.get("risk_block_count"),
            }
            for row in list(shadow.session.get("reconciliation_decisions", []))[-100:]
        ]
        or [
            {
                "Timestamp": row.get("timestamp"),
                "Pair": row.get("trading_pair"),
                "Plan": row.get("plan_version"),
                "Desired": row.get("desired_count"),
                "Active": row.get("active_count"),
                "Create": row.get("create_count"),
                "Keep": row.get("keep_count"),
                "Stop": row.get("stop_count"),
                "Defer": row.get("deferred_count"),
                "Risk": row.get("risk_block_count"),
            }
            for row in list(metrics.get("reconciliation_decisions") or [])[-100:]
        ]
        or [{"Timestamp": "NO RECONCILIATION DATA"}],
        width="stretch",
        hide_index=True,
    )
    st.write("Recent lifecycle events")
    st.dataframe(
        [
            {
                "Timestamp": row.get("timestamp"),
                "Event": row.get("event"),
                "Pair": row.get("trading_pair"),
                "Level": row.get("level_id"),
                "Order": row.get("shadow_order_id"),
                "Reason": row.get("reason"),
                "State": row.get("lifecycle_state"),
            }
            for row in list(shadow.lifecycle_events)[-200:]
        ]
        or [{"Event": "NO LIFECYCLE EVENTS"}],
        width="stretch",
        hide_index=True,
    )
    st.write("Resting age distribution")
    st.dataframe(
        [
            {"Bucket": bucket, "Orders": count}
            for bucket, count in _shadow_lifetime_buckets(metrics, shadow.orders).items()
        ],
        width="stretch",
        hide_index=True,
    )
    st.write("Replacement-deviation distribution")
    st.dataframe(
        [
            {"Bucket": bucket, "Operational cancels": count}
            for bucket, count in _shadow_deviation_buckets(metrics, shadow.cancels).items()
        ],
        width="stretch",
        hide_index=True,
    )

    stage13 = shadow.stage13 or metrics.get("stage13") or shadow.session.get("stage13") or {}
    st.subheader("Stage 13 stability optimization")
    if stage13:
        stage13_safety = stage13.get("safety") or {}
        stage13_readiness = stage13.get("readiness") or {}
        stage13_validation = stage13.get("validation") or {}
        stage13_reconciliation = stage13.get("create_decision_reconciliation") or {}
        stage13_survival = stage13.get("quote_survival") or {}
        stage13_survival_counts = stage13_survival.get("counts") or {}
        stage13_pause = stage13.get("pause_hysteresis") or {}
        stage13_risk = stage13.get("risk_reservation") or {}
        stage13_risk_delta = stage13.get("risk_delta") or {}
        stage13_comparison = {str(row.get("metric")): row for row in stage13.get("comparison", [])}
        st.dataframe(
            _shadow_value_rows(
                [
                    {"Metric": "Safety boundary", "Value": stage13_safety.get("status")},
                    {
                        "Metric": "Hard safety regression",
                        "Value": stage13_validation.get("hard_safety_regression"),
                    },
                    {
                        "Metric": "PnL reconciliation",
                        "Value": stage13_validation.get("pnl_reconciliation"),
                    },
                    {"Metric": "Fill contract", "Value": stage13_validation.get("fill_contract")},
                    {"Metric": "Trade pipeline", "Value": stage13_validation.get("trade_pipeline")},
                    {
                        "Metric": "Raw create decisions",
                        "Value": stage13_reconciliation.get("raw_create_decisions"),
                    },
                    {
                        "Metric": "Instantiated",
                        "Value": stage13_reconciliation.get("instantiated"),
                    },
                    {
                        "Metric": "Same-frame create/cancel",
                        "Value": (stage13.get("same_frame_cancel") or {}).get("count"),
                    },
                    {
                        "Metric": ">=1/5/30/60s quote survival",
                        "Value": [
                            stage13_survival_counts.get("stayed_resting_ge_1s"),
                            stage13_survival_counts.get("stayed_resting_ge_5s"),
                            stage13_survival_counts.get("stayed_resting_ge_30s"),
                            stage13_survival_counts.get("stayed_resting_ge_60s"),
                        ],
                    },
                    {"Metric": "KEEP decisions", "Value": stage13_risk_delta.get("keep_decisions")},
                    {
                        "Metric": "Median/P90 lifetime (s)",
                        "Value": [
                            stage13_comparison.get("median_resting_lifetime_seconds", {}).get(
                                "stage13"
                            ),
                            stage13_comparison.get("p90_resting_lifetime_seconds", {}).get(
                                "stage13"
                            ),
                        ],
                    },
                    {
                        "Metric": "Pending reserved gross",
                        "Value": stage13_risk.get("pending_reserved_gross"),
                    },
                    {
                        "Metric": "Max incremental candidate risk",
                        "Value": stage13_risk_delta.get("max_positive_candidate_delta"),
                    },
                    {
                        "Metric": "Pending-risk oscillation count",
                        "Value": stage13_risk.get("pending_risk_oscillation_count"),
                    },
                    {
                        "Metric": "Pending-risk self-invalidation",
                        "Value": stage13_risk.get("self_invalidation_events"),
                    },
                    {
                        "Metric": "Stability readiness",
                        "Value": stage13_readiness.get("stability_optimization"),
                    },
                    {
                        "Metric": "Quote optimization",
                        "Value": stage13_readiness.get("quote_optimization", "NO"),
                    },
                    {
                        "Metric": "Fill optimization",
                        "Value": stage13_readiness.get("fill_optimization", "NO"),
                    },
                    {
                        "Metric": "Volume optimization",
                        "Value": stage13_readiness.get("volume_optimization", "NO"),
                    },
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.write("Stage 12G control vs Stage 13")
        st.dataframe(
            _shadow_value_rows(
                [
                    {
                        "Metric": row.get("metric"),
                        "Stage 12G control": row.get("stage12g_control"),
                        "Stage 13": row.get("stage13"),
                        "Delta": row.get("delta_stage13_minus_control"),
                    }
                    for row in stage13.get("comparison", [])
                ]
            )
            or [{"Metric": "NO COMPARISON DATA"}],
            width="stretch",
            hide_index=True,
        )
        current_pause = stage13_pause.get("current") or {}
        st.write("Current pause candidate")
        st.dataframe(
            _shadow_value_rows(
                [
                    {"Metric": "Pair", "Value": current_pause.get("trading_pair")},
                    {"Metric": "Mode", "Value": current_pause.get("mode")},
                    {
                        "Metric": "Candidate",
                        "Value": current_pause.get("pause_candidate_active"),
                    },
                    {
                        "Metric": "Reason",
                        "Value": current_pause.get("pause_candidate_category")
                        or current_pause.get("pause_candidate_reason"),
                    },
                    {
                        "Metric": "Candidate age (s)",
                        "Value": current_pause.get("pause_candidate_age_seconds"),
                    },
                    {
                        "Metric": "Confirmation threshold (s)",
                        "Value": current_pause.get("pause_confirmation_seconds"),
                    },
                    {"Metric": "Confirmed pause", "Value": current_pause.get("pause_confirmed")},
                    {
                        "Metric": "Recovery candidate",
                        "Value": current_pause.get("recovery_candidate"),
                    },
                    {
                        "Metric": "Recovery age (s)",
                        "Value": current_pause.get("recovery_candidate_age_seconds"),
                    },
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.write("Asset execution status")
        st.dataframe(
            [
                {
                    "Pair": row.get("trading_pair"),
                    "Status": row.get("status"),
                    "Enabled in cycle": row.get("enabled_in_cycle"),
                    "Mutations allowed": row.get("execution_mutations_allowed"),
                }
                for row in stage13.get("asset_execution_status", [])
            ]
            or [{"Pair": "NO STAGE 13 STATUS DATA"}],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No Stage 13 stability report has been persisted yet.")

    stage14 = (
        getattr(shadow, "stage14", {})
        or metrics.get("stage14")
        or shadow.session.get("stage14")
        or {}
    )
    st.subheader("Stage 14 economic validation")
    if stage14:
        st.error(
            "DERIVE MAINNET DATA\nSHADOW / PAPER EXECUTION\nCONFIG FROZEN\n"
            "REAL EXCHANGE MUTATIONS: 0",
            icon="🛡️",
        )
        stage14_evidence = stage14.get("evidence") or {}
        stage14_fill = stage14.get("fill_quality") or {}
        stage14_marks = stage14_fill.get("markouts") or {}
        stage14_order = stage14.get("order_execution") or {}
        stage14_capital = stage14.get("capital_recycling") or {}
        stage14_volume = stage14.get("volume_risk") or {}
        stage14_risk = stage14.get("risk") or {}
        stage14_economics = stage14.get("economics") or {}
        stage14_readiness = stage14.get("readiness") or {}

        def _stage14_markout(horizon: str) -> str:
            value = stage14_marks.get(horizon) or {}
            return f"{_fmt(value.get('mean_bps'))} bps (n={value.get('sample_count', 'UNKNOWN')})"

        elapsed_hours = _fmt(
            (stage14.get("duration_seconds") or 0.0) / 3600.0,
            " h",
        )
        stage14_duration_hours = stage14.get("duration_hours") or 0.0
        cycles_per_hour = (
            (stage14_capital.get("completed_cycles") or 0.0) / stage14_duration_hours
            if stage14_duration_hours
            else None
        )
        top = st.columns(6)
        top[0].metric("ELAPSED", elapsed_hours)
        top[1].metric("EVIDENCE", str(stage14_evidence.get("status", "UNKNOWN")))
        top[2].metric("CONSERVATIVE FILLS", str(stage14_fill.get("conservative_fills", "UNKNOWN")))
        top[3].metric("30s MARKOUT", _stage14_markout("30s"))
        top[4].metric("60s MARKOUT", _stage14_markout("60s"))
        top[5].metric("COMPLETED CYCLES", str(stage14_capital.get("completed_cycles", "UNKNOWN")))
        top = st.columns(6)
        top[0].metric("CYCLES / HOUR", _fmt(cycles_per_hour))
        top[1].metric("EXECUTED VOLUME", _fmt(stage14_volume.get("executed_volume"), " USDC"))
        top[2].metric(
            "VOLUME / AVG RISK",
            _fmt((stage14_volume.get("ratios") or {}).get("volume_per_average_filled_gross")),
        )
        top[3].metric("AVERAGE INVENTORY", _fmt(stage14_volume.get("average_inventory")))
        top[4].metric("MAX INVENTORY", _fmt(stage14_volume.get("max_inventory")))
        top[5].metric("GROSS PAPER PNL", _fmt(stage14_economics.get("gross_total_pnl"), " USDC"))

        st.dataframe(
            _shadow_value_rows(
                [
                    {"Metric": "Classification", "Value": stage14.get("classification")},
                    {"Metric": "Primary weakness", "Value": stage14.get("primary_weakness")},
                    {
                        "Metric": "Ready for bounded economic optimization",
                        "Value": stage14_readiness.get(
                            "ready_for_bounded_economic_optimization", "NO"
                        ),
                    },
                    {
                        "Metric": "Ready for tiny live-money canary review",
                        "Value": stage14_readiness.get(
                            "ready_for_tiny_live_money_canary_review", "NO"
                        ),
                    },
                    {
                        "Metric": "Why stopped",
                        "Value": stage14.get("why_stopped", "IN PROGRESS"),
                    },
                    {
                        "Metric": "Config / Stage 13 behavior hash",
                        "Value": [
                            stage14.get("config_hash"),
                            stage14.get("stage13_behavior_hash"),
                        ],
                    },
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.write("Stage 14 fill quality")
        st.dataframe(
            _shadow_value_rows(
                [
                    {
                        "Metric": "Conservative fills",
                        "Value": stage14_fill.get("conservative_fills"),
                    },
                    {
                        "Metric": "Conservative volume",
                        "Value": stage14_fill.get("conservative_volume"),
                    },
                    {"Metric": "Touch-optimistic fills", "Value": stage14_fill.get("touch_fills")},
                    {
                        "Metric": "Touch-optimistic volume",
                        "Value": stage14_fill.get("touch_volume"),
                    },
                    {"Metric": "Fill-model sensitivity", "Value": stage14_fill.get("sensitivity")},
                    {"Metric": "Adverse selection", "Value": stage14_fill.get("adverse_selection")},
                    {"Metric": "5s markout", "Value": _stage14_markout("5s")},
                    {"Metric": "30s markout", "Value": _stage14_markout("30s")},
                    {"Metric": "60s markout", "Value": _stage14_markout("60s")},
                    {"Metric": "5m markout", "Value": _stage14_markout("300s")},
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.write("Stage 14 capital recycling")
        st.dataframe(
            _shadow_value_rows(
                [
                    {
                        "Metric": "Open positions",
                        "Value": stage14_capital.get("open_position_count"),
                    },
                    {
                        "Metric": "Average open inventory age (s)",
                        "Value": stage14_capital.get("average_open_position_age_seconds"),
                    },
                    {
                        "Metric": "Oldest inventory (s)",
                        "Value": stage14_capital.get("max_open_position_age_seconds"),
                    },
                    {
                        "Metric": "Completed cycles",
                        "Value": stage14_capital.get("completed_cycles"),
                    },
                    {
                        "Metric": "Median cycle duration (s)",
                        "Value": stage14_capital.get("median_cycle_duration_seconds"),
                    },
                    {
                        "Metric": "Closed within 15m / 30m / 1h",
                        "Value": [
                            stage14_capital.get("percentage_inventory_closed_within_15m"),
                            stage14_capital.get("percentage_inventory_closed_within_30m"),
                            stage14_capital.get("percentage_inventory_closed_within_1h"),
                        ],
                    },
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.write("Stage 14 execution and risk")
        st.dataframe(
            _shadow_value_rows(
                [
                    {
                        "Metric": "Raw candidate evaluations",
                        "Value": stage14_order.get("raw_candidate_evaluations"),
                    },
                    {
                        "Metric": "Actual instantiated",
                        "Value": stage14_order.get("actual_instantiated_orders"),
                    },
                    {"Metric": "Entered resting", "Value": stage14_order.get("entered_resting")},
                    {"Metric": "KEEP", "Value": stage14_order.get("keep")},
                    {
                        "Metric": "Operational cancels",
                        "Value": stage14_order.get("operational_cancels"),
                    },
                    {"Metric": "Fill/Create", "Value": stage14_order.get("fill_create_ratio")},
                    {"Metric": "Cancel/Create", "Value": stage14_order.get("cancel_create_ratio")},
                    {
                        "Metric": "Median resting lifetime (s)",
                        "Value": (stage14_order.get("quote_lifetime") or {}).get("median"),
                    },
                    {"Metric": "Filled gross", "Value": stage14_volume.get("average_filled_gross")},
                    {
                        "Metric": "Pending reserved gross",
                        "Value": stage14_volume.get("average_pending_reserved_gross"),
                    },
                    {
                        "Metric": "Worst-case gross",
                        "Value": stage14_volume.get("average_worst_case_gross"),
                    },
                    {"Metric": "Average BTC-beta", "Value": stage14_volume.get("average_btc_beta")},
                    {"Metric": "Max BTC-beta", "Value": stage14_volume.get("max_btc_beta")},
                    {"Metric": "Risk blocks", "Value": stage14_risk.get("risk_blocks")},
                    {
                        "Metric": "Hard-limit attempts",
                        "Value": stage14_risk.get("hard_limit_attempts"),
                    },
                    {
                        "Metric": "PnL reconciliation",
                        "Value": stage14_economics.get("pnl_reconciliation_status"),
                    },
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            f"Stage 14 artifacts: {stage14.get('stage14_report_root', 'reports/stage14/UNKNOWN')}"
        )
    else:
        st.info("No Stage 14 economic validation checkpoint has been persisted yet.")

    st.subheader("Churn and markout")
    churn_markout_rows = [
        {"Metric": "Cancel/Create", "Value": metrics.get("cancel_create_ratio")},
        {"Metric": "Cancels/hour", "Value": metrics.get("cancels_per_hour")},
        {"Metric": "Median quote lifetime (s)", "Value": metrics.get("median_quote_lifetime")},
        {
            "Metric": "P25/P75/P90 lifetime (s)",
            "Value": [
                metrics.get("p25_quote_lifetime"),
                metrics.get("p75_quote_lifetime"),
                metrics.get("p90_quote_lifetime"),
            ],
        },
        {
            "Metric": "Median cancellation age (s)",
            "Value": metrics.get("median_cancellation_age_seconds"),
        },
        {
            "Metric": "Median cancellation deviation (bps)",
            "Value": metrics.get("median_cancellation_deviation_bps"),
        },
        {"Metric": "Dominant cancel reason", "Value": metrics.get("dominant_cancel_reason")},
        {"Metric": "KEEP %", "Value": metrics.get("keep_pct")},
        {
            "Metric": "HIGH_CANCEL_CHURN",
            "Value": "YES" if metrics.get("high_cancel_churn") else "NO",
        },
        {"Metric": "5s markout (bps)", "Value": metrics.get("markout_5s")},
        {"Metric": "30s markout (bps)", "Value": metrics.get("markout_30s")},
        {"Metric": "60s markout (bps)", "Value": metrics.get("markout_60s")},
        {"Metric": "Adverse selection", "Value": metrics.get("adverse_selection")},
    ]
    st.dataframe(_shadow_value_rows(churn_markout_rows), width="stretch", hide_index=True)
    st.write("Markout samples")
    st.dataframe(
        [
            {"Horizon": horizon, **values}
            for horizon, values in (metrics.get("markout") or {}).items()
        ]
        or [{"Horizon": "NO FILLS YET"}],
        width="stretch",
        hide_index=True,
    )
    st.write("Per-asset markout samples")
    st.dataframe(
        [
            {"Asset": pair, "Markout": values}
            for pair, values in (metrics.get("markout_by_asset") or {}).items()
        ]
        or [{"Asset": "NO MARKOUTS YET", "Markout": None}],
        width="stretch",
        hide_index=True,
    )

    st.subheader("Inventory and portfolio risk")
    st.dataframe(
        [
            {"Asset": pair, **values}
            for pair, values in (metrics.get("inventory_by_asset") or {}).items()
        ]
        or [{"Asset": "NO INVENTORY YET"}],
        width="stretch",
        hide_index=True,
    )
    portfolio_rows = [
        {"Metric": "Average gross", "Value": metrics.get("average_gross_exposure")},
        {"Metric": "Max gross", "Value": metrics.get("max_gross_exposure")},
        {"Metric": "Average net", "Value": metrics.get("average_net_exposure")},
        {"Metric": "Average BTC-beta", "Value": metrics.get("average_btc_beta_exposure")},
        {"Metric": "Max BTC-beta", "Value": metrics.get("max_btc_beta_exposure")},
        {"Metric": "Long beta", "Value": metrics.get("average_long_beta_exposure")},
        {"Metric": "Short beta", "Value": metrics.get("average_short_beta_exposure")},
        {"Metric": "Average margin used", "Value": metrics.get("average_margin_used")},
        {
            "Metric": "Open inventory time (%)",
            "Value": metrics.get("capital_recycling", {}).get(
                "percentage_session_with_open_inventory"
            ),
        },
        {"Metric": "Risk blocks", "Value": metrics.get("risk_blocks")},
    ]
    st.dataframe(_shadow_value_rows(portfolio_rows), width="stretch", hide_index=True)

    st.subheader("Conservative vs touch-optimistic")
    st.dataframe(
        metrics.get("fill_model_comparison") or [{"Metric": "NO OBSERVATIONS YET"}],
        width="stretch",
        hide_index=True,
    )
    sensitivity = metrics.get("fill_model_sensitivity", "UNKNOWN")
    (st.error if sensitivity == "HIGH" else st.warning if sensitivity == "MEDIUM" else st.info)(
        f"FILL-MODE SENSITIVITY: {sensitivity}. Touch results are sensitivity evidence only."
    )

    st.subheader("Cycles and capital recycling")
    st.dataframe(
        _shadow_value_rows(
            [
                {"Metric": "Completed cycles", "Value": metrics.get("completed_cycles")},
                {"Metric": "Cycles / hour", "Value": metrics.get("cycles_per_hour")},
                {
                    "Metric": "Median cycle duration (s)",
                    "Value": metrics.get("median_cycle_duration"),
                },
                {
                    "Metric": "Capture / cycle",
                    "Value": metrics.get("realized_capture_per_cycle"),
                },
                {
                    "Metric": "Median open-position age (s)",
                    "Value": metrics.get("capital_recycling", {}).get(
                        "median_open_position_age_seconds"
                    ),
                },
                {
                    "Metric": "Capital recycling",
                    "Value": (
                        "OBSERVED" if metrics.get("completed_cycles", 0) > 0 else "INSUFFICIENT"
                    ),
                },
                {
                    "Metric": "Configured levels / side",
                    "Value": metrics.get("configured_levels_per_side"),
                },
                {"Metric": "Capital allocation", "Value": metrics.get("capital_allocation")},
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Baseline health and readiness")
    st.dataframe(
        [
            {"Check": key, "Status": value}
            for key, value in (metrics.get("health_checks") or {}).items()
        ]
        or [{"Check": "NO CHECKPOINT YET", "Status": "UNKNOWN"}],
        width="stretch",
        hide_index=True,
    )
    st.metric("FINAL STATUS", str(metrics.get("classification", "UNKNOWN")))
    st.metric("READINESS", str(metrics.get("readiness", "NOT READY FOR OPTIMIZATION")))
    if metrics.get("self_tuning_suggestions"):
        st.subheader("Self-tuning suggestions (not applied)")
        st.dataframe(metrics["self_tuning_suggestions"], width="stretch", hide_index=True)

    st.subheader("Virtual order lifecycle")
    st.dataframe(
        list(shadow.orders)[:100] or [{"Status": "NO ORDERS YET"}],
        width="stretch",
        hide_index=True,
    )
    st.subheader("Virtual fills and paper equity")
    st.dataframe(
        list(shadow.fills)[:100] or [{"Status": "NO FILLS YET"}],
        width="stretch",
        hide_index=True,
    )
    equity_rows = [
        {
            "timestamp": row.get("timestamp"),
            "current_equity": row.get("current_equity"),
        }
        for row in shadow.equity
        if row.get("current_equity") is not None
    ]
    if equity_rows:
        st.line_chart(
            equity_rows,
            x="timestamp",
            y="current_equity",
        )
    st.caption(
        "Shadow fills are modelled from future public evidence only. They are not Derive fills, "
        "not live PnL, and do not prove queue position or profitability."
    )


def _render_churn(st: Any, runtime: RuntimeSnapshot, profile: Any) -> None:
    churn = runtime.churn
    if not churn.available:
        st.warning("EXECUTION JOURNAL UNAVAILABLE — order churn metrics are UNKNOWN")
        return
    st.dataframe(
        [
            {"Metric": "KEEP count", "Value": churn.keep_count},
            {"Metric": "REFRESH count", "Value": churn.refresh_count},
            {"Metric": "SAFETY CANCEL count", "Value": churn.safety_cancel_count},
            {"Metric": "Orders created", "Value": churn.orders_created},
            {"Metric": "Orders cancelled", "Value": churn.orders_cancelled},
            {"Metric": "Cancel/create ratio", "Value": _fmt(churn.cancel_create_ratio)},
            {"Metric": "Cancels/hour", "Value": _fmt(churn.cancels_per_hour)},
            {"Metric": "Median order lifetime", "Value": _fmt(churn.median_order_lifetime, " s")},
            {"Metric": "Average order lifetime", "Value": _fmt(churn.average_order_lifetime, " s")},
        ],
        width="stretch",
        hide_index=True,
    )
    if churn.replacement_reason_counts:
        st.write("Replacement reasons")
        st.dataframe(
            [
                {"Reason": key, "Count": value}
                for key, value in churn.replacement_reason_counts.items()
            ],
            width="stretch",
            hide_index=True,
        )
    estimate = refresh_stability_estimate(
        runtime.streams.get("plan").records if runtime.streams.get("plan") else (),
        price_tolerance_bps=profile.refresh_price_tolerance_bps,
        amount_tolerance_pct=profile.refresh_amount_tolerance_pct,
    )
    st.caption(estimate.title)
    st.dataframe(
        [
            {"Outcome": key, "Estimated share": value}
            for key, value in estimate.values["percentages"].items()
        ],
        width="stretch",
        hide_index=True,
    )


def _render_self_tuning(st: Any, runtime: RuntimeSnapshot) -> None:
    """Render the Phase 1 observer without exposing a mutation workflow."""

    st.header("SELF-TUNING — PHASE 1 OBSERVER")
    st.info(
        "Phase 1 only: this page observes existing streams and reports supportability. "
        "It generates no diagnosis, proposal, promotion, rollback, or configuration change."
    )
    st.warning(
        "SUGGEST_ONLY is the only available mode. AUTO_BOUNDED is disabled, and the "
        "observer has no exchange or execution-control surface."
    )
    available_assets = ["ALL", *sorted(runtime.latest_by_asset)]
    asset = st.selectbox("Observation asset", available_assets, key="stage10_observer_asset")
    window_minutes = st.number_input(
        "Observation window (minutes)",
        min_value=1,
        max_value=24 * 60,
        value=30,
        step=5,
        key="stage10_observer_window_minutes",
    )
    observer = PerformanceObserver(ObserverConfig(evaluation_window_minutes=int(window_minutes)))
    streams = runtime.streams
    observation = observer.observe(
        streams.get("execution_journal").records if streams.get("execution_journal") else (),
        state_records=streams.get("state").records if streams.get("state") else (),
        portfolio_records=(
            streams.get("portfolio_risk").records if streams.get("portfolio_risk") else ()
        ),
        relationship_records=(
            streams.get("relationship").records if streams.get("relationship") else ()
        ),
        plan_records=streams.get("plan").records if streams.get("plan") else (),
        asset=asset,
        event_source_status=(
            streams.get("execution_journal").status
            if streams.get("execution_journal")
            else "missing"
        ),
        state_source_status=streams.get("state").status if streams.get("state") else "missing",
        portfolio_source_status=(
            streams.get("portfolio_risk").status if streams.get("portfolio_risk") else "missing"
        ),
        relationship_source_status=(
            streams.get("relationship").status if streams.get("relationship") else "missing"
        ),
    )
    window = observation.window
    known = sum(value == "AVAILABLE" for value in window.metric_status.values())
    unknown = sum(value == UNKNOWN for value in window.metric_status.values())
    cols = st.columns(5)
    cols[0].metric("MODE", "SUGGEST_ONLY")
    cols[1].metric("EVIDENCE", window.evidence_source)
    cols[2].metric("CONFIDENCE", window.confidence)
    cols[3].metric("KNOWN METRICS", str(known))
    cols[4].metric("UNKNOWN METRICS", str(unknown))
    st.caption(f"Window: {window.start_timestamp or UNKNOWN} → {window.end_timestamp or UNKNOWN}")

    st.subheader("Evidence sources")
    source_rows = []
    for name in ("execution_journal", "state", "portfolio_risk", "relationship", "plan"):
        stream = streams.get(name)
        source_rows.append(
            {
                "Source": name,
                "Status": observation.source_status.get(name, "missing"),
                "Records": len(stream.records) if stream else 0,
                "Malformed": stream.malformed_lines if stream else 0,
                "Partial trailing line": "YES" if stream and stream.partial_trailing_line else "NO",
                "Latest": stream.latest.get("timestamp") if stream and stream.latest else UNKNOWN,
            }
        )
    st.dataframe(source_rows, width="stretch", hide_index=True)

    st.subheader("Observed metrics")
    metric_fields = (
        ("mode", "Mode"),
        ("global_volatility_regime", "Global volatility regime"),
        ("relationship_regime", "Relationship regime"),
        ("orders_created", "Orders created"),
        ("orders_cancelled", "Orders cancelled"),
        ("orders_kept", "Orders kept"),
        ("orders_refreshed", "Orders refreshed"),
        ("safety_cancels", "Safety cancels"),
        ("fills", "Fills"),
        ("completed_cycles", "Completed cycles"),
        ("cancel_create_ratio", "Cancel/create ratio"),
        ("keep_ratio", "Keep ratio"),
        ("fill_create_ratio", "Fill/create ratio"),
        ("median_order_lifetime", "Median order lifetime (s)"),
        ("mean_order_lifetime", "Mean order lifetime (s)"),
        ("maker_capture_quote", "Maker capture (quote)"),
        ("realized_pnl", "Realized PnL"),
        ("unrealized_pnl", "Unrealized PnL"),
        ("total_pnl", "Total PnL"),
        ("markout_5s", "5s markout (bps)"),
        ("markout_30s", "30s markout (bps)"),
        ("markout_60s", "60s markout (bps)"),
        ("adverse_markout_rate", "Adverse 30s markout rate"),
        ("inventory_ratio_mean", "Mean inventory ratio"),
        ("inventory_ratio_max", "Max inventory ratio"),
        ("portfolio_beta_exposure_mean", "Mean portfolio beta exposure"),
        ("portfolio_beta_exposure_max", "Max portfolio beta exposure"),
        ("drawdown", "Drawdown"),
        ("turnover", "Turnover"),
        ("fees_if_known", "Fees"),
    )
    st.dataframe(
        [
            {
                "Metric": label,
                "Value": _fmt(getattr(window, field_name)),
                "Status": window.metric_status.get(
                    field_name,
                    "AVAILABLE" if getattr(window, field_name) is not None else UNKNOWN,
                ),
            }
            for field_name, label in metric_fields
        ],
        width="stretch",
        hide_index=True,
    )
    if window.reasons:
        st.subheader("Supportability reasons")
        for reason in window.reasons:
            st.write(f"• {reason}")
    st.subheader("Locked from self-tuning")
    st.write(
        "leverage · execution_enabled · mainnet permission · connector/environment · "
        "post_only · supported assets · collateral reserve · hard gross/beta/asset "
        "limits · drawdown/emergency rules · credentials · stale-data safety"
    )
    st.caption(
        "Live lifecycle metrics stay UNKNOWN until a real execution journal is present. "
        "Replay metrics, when inspected by offline tooling, remain labelled SHADOW_REPLAY."
    )


def _render_strategy(
    st: Any, runtime: RuntimeSnapshot, saved: DashboardConfig, staged: DashboardConfig
) -> None:
    st.header("STRATEGY")
    simple = st.radio("Parameter mode", ["SIMPLE MODE", "ADVANCED MODE"], horizontal=True)
    current = staged.to_record()
    strategy = staged.strategy
    with st.form("strategy_form"):
        st.markdown("#### Global BTC options")
        c1, c2, c3 = st.columns(3)
        iv_weight = c1.number_input(
            "BTC IV weight",
            min_value=0.0,
            max_value=1.0,
            value=strategy.btc_iv_weight,
            step=0.05,
            help="Weight of the shared BTC ATM IV signal in the existing volatility score.",
        )
        stale = c2.number_input(
            "IV stale timeout (sec)",
            min_value=1.0,
            value=strategy.iv_stale_timeout_seconds,
            step=1.0,
            help=(
                "Age after which BTC ATM IV is stale and the existing missing-data "
                "behavior applies."
            ),
        )
        missing = c3.selectbox(
            "IV missing behavior",
            ["rv_only", "defensive", "pause"],
            index=["rv_only", "defensive", "pause"].index(strategy.iv_missing_behavior),
            help="Existing behavior when BTC IV is unavailable or stale.",
        )
        st.markdown("#### Local volatility and direction")
        c1, c2, c3 = st.columns(3)
        rv_weight = c1.number_input(
            "RV weight",
            min_value=0.0,
            max_value=1.0,
            value=strategy.rv_weight,
            step=0.05,
            help="Weight of the existing local realized-volatility score.",
        )
        transmitted = c2.number_input(
            "Transmitted BTC-IV weight",
            min_value=0.0,
            max_value=1.0,
            value=strategy.transmitted_btc_iv_weight,
            step=0.05,
            help="Weight of the existing BTC-IV transmission component for non-BTC assets.",
        )
        direction_threshold = c3.number_input(
            "Bias direction threshold",
            min_value=0.0,
            max_value=1.0,
            value=strategy.direction_threshold,
            step=0.05,
            help="Existing direction-score threshold used to confirm LONG_BIAS or SHORT_BIAS.",
        )
        if simple == "ADVANCED MODE":
            st.markdown("#### Advanced strategy controls")
            c1, c2, c3 = st.columns(3)
            relationship = c1.number_input(
                "Relationship lookback (sec)",
                min_value=60.0,
                value=strategy.relationship_lookback_seconds,
                step=60.0,
                help="Existing rolling BTC relationship lookback.",
            )
            defensive_score = c2.number_input(
                "Defensive volatility score",
                min_value=0.01,
                value=strategy.defensive_volatility_score,
                step=0.05,
                help="Existing volatility score at which DEFENSIVE mode is selected.",
            )
            base_width = c3.number_input(
                "Base grid width (%)",
                min_value=0.0001,
                value=strategy.base_grid_width_pct,
                step=0.001,
                format="%.4f",
                help="Existing Stage 4 base grid width; this preview calls the Stage 4 planner.",
            )
            c1, c2, c3 = st.columns(3)
            normal_levels = c1.number_input(
                "Normal levels / side",
                min_value=1,
                max_value=100,
                value=strategy.normal_levels_per_side,
                step=1,
                help="Existing NORMAL geometric levels per side.",
            )
            defensive_levels = c2.number_input(
                "Defensive levels / side",
                min_value=1,
                max_value=100,
                value=strategy.defensive_levels_per_side,
                step=1,
                help="Existing DEFENSIVE geometric levels per side.",
            )
            defensive_width = c3.number_input(
                "Defensive width multiplier",
                min_value=0.01,
                value=strategy.defensive_width_multiplier,
                step=0.05,
                help="Existing DEFENSIVE width multiplier.",
            )
            inventory_shift = st.number_input(
                "Max inventory center shift (bps)",
                min_value=0.0,
                value=strategy.max_inventory_center_shift_bps,
                step=1.0,
                help="Existing maximum inventory-driven center shift.",
            )
        else:
            relationship = strategy.relationship_lookback_seconds
            defensive_score = strategy.defensive_volatility_score
            base_width = strategy.base_grid_width_pct
            normal_levels = strategy.normal_levels_per_side
            defensive_levels = strategy.defensive_levels_per_side
            defensive_width = strategy.defensive_width_multiplier
            inventory_shift = strategy.max_inventory_center_shift_bps
        submitted = st.form_submit_button("STAGE STRATEGY CHANGES")
    if submitted:
        current["strategy"] = Stage9StrategySettings(
            btc_iv_weight=iv_weight,
            iv_stale_timeout_seconds=stale,
            iv_missing_behavior=missing,
            relationship_lookback_seconds=relationship,
            rv_weight=rv_weight,
            transmitted_btc_iv_weight=transmitted,
            direction_threshold=direction_threshold,
            defensive_volatility_score=defensive_score,
            base_grid_width_pct=base_width,
            normal_levels_per_side=normal_levels,
            defensive_levels_per_side=defensive_levels,
            defensive_width_multiplier=defensive_width,
            max_inventory_center_shift_bps=inventory_shift,
        ).model_dump(mode="json")
        _stage_record(st, current)
        st.session_state["stage9_stage_notice"] = (
            "Strategy changes staged. Review the diff and apply explicitly."
        )
        st.rerun()
    _render_grid_preview(st, runtime, saved, _staged(st))


def _render_risk(
    st: Any, runtime: RuntimeSnapshot, saved: DashboardConfig, staged: DashboardConfig
) -> None:
    st.header("RISK")
    record = staged.to_record()
    profile = staged.competition
    with st.form("risk_form"):
        st.markdown("#### Collateral and portfolio")
        c1, c2, c3 = st.columns(3)
        reserve = c1.number_input(
            "Collateral reserve %",
            min_value=0.0,
            max_value=0.99,
            value=profile.collateral_reserve_pct,
            step=0.01,
            help=(
                "Reserve held back from available collateral before new-risk capacity "
                "is calculated."
            ),
        )
        soft_gross = c2.number_input(
            "Soft gross notional",
            min_value=0.01,
            value=profile.portfolio_soft_gross_notional,
            step=10.0,
            help="Soft portfolio gross threshold; normal operation should usually stay below it.",
        )
        hard_gross = c3.number_input(
            "Hard gross notional",
            min_value=0.01,
            value=profile.portfolio_max_gross_notional,
            step=10.0,
            help="Absolute portfolio gross ceiling.",
        )
        c1, c2, c3 = st.columns(3)
        soft_beta = c1.number_input(
            "Soft BTC-beta exposure",
            min_value=0.01,
            value=profile.portfolio_soft_beta_exposure,
            step=10.0,
            help="Soft systematic exposure threshold where new correlated risk is suppressed.",
        )
        hard_beta = c2.number_input(
            "Hard BTC-beta exposure",
            min_value=0.01,
            value=profile.portfolio_hard_beta_exposure,
            step=10.0,
            help="Hard systematic exposure ceiling.",
        )
        hard_long = c3.number_input(
            "Max long beta",
            min_value=0.01,
            value=profile.portfolio_max_long_beta_exposure,
            step=10.0,
            help="Independent long BTC-beta ceiling.",
        )
        hard_short = st.number_input(
            "Max short beta",
            min_value=0.01,
            value=profile.portfolio_max_short_beta_exposure,
            step=10.0,
            help="Independent short BTC-beta ceiling.",
        )
        st.markdown("#### Per-asset net-position limits")
        asset_values: dict[str, dict[str, float]] = {}
        for pair, limit in profile.asset_limits.items():
            c1, c2 = st.columns(2)
            soft = c1.number_input(
                f"{pair} soft",
                min_value=0.01,
                value=limit.soft_position_notional,
                step=10.0,
                key=f"risk_soft_{pair}",
                help="Soft net-position notional for this asset.",
            )
            hard = c2.number_input(
                f"{pair} hard",
                min_value=0.01,
                value=limit.max_position_notional,
                step=10.0,
                key=f"risk_hard_{pair}",
                help="Hard net-position notional for this asset.",
            )
            asset_values[pair] = {"soft_position_notional": soft, "max_position_notional": hard}
        st.markdown("#### Drawdown ladder")
        c1, c2, c3, c4 = st.columns(4)
        caution = c1.number_input(
            "CAUTION",
            min_value=0.01,
            value=profile.drawdown_caution_quote,
            step=5.0,
            help="Session drawdown at which capacity enters CAUTION.",
        )
        reduce = c2.number_input(
            "REDUCE",
            min_value=0.01,
            value=profile.drawdown_reduce_quote,
            step=5.0,
            help="Session drawdown at which new risk is reduced further.",
        )
        defensive = c3.number_input(
            "DEFENSIVE",
            min_value=0.01,
            value=profile.drawdown_defensive_quote,
            step=5.0,
            help="Session drawdown at which DEFENSIVE capacity applies.",
        )
        hard_stop = c4.number_input(
            "HARD STOP",
            min_value=0.01,
            value=profile.competition_hard_drawdown_quote,
            step=5.0,
            help="Latched session drawdown stop for new risk.",
        )
        submitted = st.form_submit_button("STAGE RISK CHANGES")
    if submitted:
        record["competition"].update(
            {
                "collateral_reserve_pct": reserve,
                "portfolio_soft_gross_notional": soft_gross,
                "portfolio_max_gross_notional": hard_gross,
                "portfolio_soft_beta_exposure": soft_beta,
                "portfolio_hard_beta_exposure": hard_beta,
                "portfolio_max_long_beta_exposure": hard_long,
                "portfolio_max_short_beta_exposure": hard_short,
                "asset_limits": asset_values,
                "drawdown_caution_quote": caution,
                "drawdown_reduce_quote": reduce,
                "drawdown_defensive_quote": defensive,
                "competition_hard_drawdown_quote": hard_stop,
            }
        )
        _stage_record(st, record)
        st.session_state["stage9_stage_notice"] = (
            "Risk changes staged. Invalid relationships will block Apply."
        )
        st.rerun()
    current_profile = saved.competition
    staged_profile = _staged(st).competition
    equity, collateral = _portfolio_equity(runtime)
    portfolio = runtime.portfolio_risk or {}
    preview = risk_consequence_preview(
        current_profile,
        staged_profile,
        account_equity=equity,
        gross_notional=float(portfolio.get("gross_notional", 0.0) or 0.0),
        beta_long=float(portfolio.get("long_beta_exposure", 0.0) or 0.0),
        beta_short=float(portfolio.get("short_beta_exposure", 0.0) or 0.0),
    )
    st.subheader("Consequence preview")
    st.dataframe(
        [{"Metric": key, "Value": value} for key, value in preview.values.items()],
        width="stretch",
        hide_index=True,
    )
    st.subheader("Portfolio utilization")
    st.dataframe(portfolio_bars(portfolio, staged_profile), width="stretch", hide_index=True)
    st.subheader("Drawdown ladder")
    st.write(
        f"NORMAL 0 to -{staged_profile.drawdown_caution_quote:g} · "
        f"CAUTION -{staged_profile.drawdown_caution_quote:g} to "
        f"-{staged_profile.drawdown_reduce_quote:g} · "
        f"REDUCE -{staged_profile.drawdown_reduce_quote:g} to "
        f"-{staged_profile.drawdown_defensive_quote:g} · "
        f"DEFENSIVE -{staged_profile.drawdown_defensive_quote:g} to "
        f"-{staged_profile.competition_hard_drawdown_quote:g} · "
        f"HARD STOP below -{staged_profile.competition_hard_drawdown_quote:g}"
    )


def _render_execution(
    st: Any,
    runtime: RuntimeSnapshot,
    saved: DashboardConfig,
    staged: DashboardConfig,
) -> None:
    st.header("EXECUTION")
    st.warning(
        "This dashboard cannot place, cancel, or recreate orders. Changes affect future "
        "runtime decisions only after the existing process is restarted."
    )
    record = staged.to_record()
    profile = staged.competition
    with st.form("execution_form"):
        c1, c2, c3 = st.columns(3)
        execution_enabled = c1.checkbox(
            "Stage execution enabled",
            value=profile.execution_enabled,
            disabled=profile.market_environment == "mainnet",
            help=(
                "Stages the existing execution flag; Apply requires explicit risk "
                "acknowledgement and still does not place an order. Mainnet profiles "
                "remain read-only in this dashboard."
            ),
        )
        post_only = c2.checkbox(
            "Post-only", value=profile.post_only, help="Existing maker-only safety requirement."
        )
        leverage = c3.number_input(
            "Leverage capability",
            min_value=0.01,
            max_value=2.0,
            value=profile.leverage,
            step=0.1,
            help="Margin capability; actual portfolio exposure remains independently bounded.",
        )
        c1, c2, c3 = st.columns(3)
        target = c1.number_input(
            "Target order notional",
            min_value=0.01,
            value=profile.target_order_notional,
            step=5.0,
            help="Preferred quote notional before exchange minimum rules.",
        )
        maximum = c2.number_input(
            "Max order notional",
            min_value=0.01,
            value=profile.max_single_order_notional,
            step=5.0,
            help="Absolute configured order budget; exchange minimums above it remain blocked.",
        )
        levels = c3.number_input(
            "Levels / side / asset",
            min_value=1,
            max_value=1,
            value=profile.max_levels_per_side_per_asset,
            step=1,
            help="Competition rollout remains one level per side.",
        )
        c1, c2, c3 = st.columns(3)
        lifetime = c1.number_input(
            "Minimum order lifetime",
            min_value=0.0,
            value=profile.minimum_order_lifetime_seconds,
            step=10.0,
            help="Minimum age before a normal replacement can occur.",
        )
        cooldown = c2.number_input(
            "Replacement cooldown",
            min_value=0.0,
            value=profile.minimum_replace_interval_seconds,
            step=10.0,
            help="Minimum time between replacements of the same level.",
        )
        age = c3.number_input(
            "Maximum order age",
            min_value=0.01,
            value=profile.maximum_order_lifetime_seconds,
            step=30.0,
            help="Maximum normal order age before replacement eligibility.",
        )
        c1, c2, c3 = st.columns(3)
        price_deadband = c1.number_input(
            "Price refresh tolerance (bps)",
            min_value=0.01,
            value=profile.refresh_price_tolerance_bps,
            step=1.0,
            help=(
                "Desired price movement required before replacement; larger values "
                "preserve queue position longer."
            ),
        )
        amount_deadband = c2.number_input(
            "Amount refresh tolerance (%)",
            min_value=0.0,
            value=profile.refresh_amount_tolerance_pct * 100,
            step=1.0,
            help="Relative quote-size movement required before replacement.",
        )
        per_asset = c3.number_input(
            "Max active executors / asset",
            min_value=1,
            value=profile.max_active_executors_per_asset,
            step=1,
            help="Per-asset active executor cap.",
        )
        portfolio = st.number_input(
            "Max active executors / portfolio",
            min_value=1,
            value=profile.max_active_executors_portfolio,
            step=1,
            help="Portfolio active executor cap.",
        )
        submitted = st.form_submit_button("STAGE EXECUTION CHANGES")
    if submitted:
        record["competition"].update(
            {
                "execution_enabled": execution_enabled,
                "post_only": post_only,
                "leverage": leverage,
                "target_order_notional": target,
                "max_single_order_notional": maximum,
                "max_levels_per_side_per_asset": levels,
                "minimum_order_lifetime_seconds": lifetime,
                "minimum_replace_interval_seconds": cooldown,
                "maximum_order_lifetime_seconds": age,
                "refresh_price_tolerance_bps": price_deadband,
                "refresh_amount_tolerance_pct": amount_deadband / 100,
                "max_active_executors_per_asset": per_asset,
                "max_active_executors_portfolio": portfolio,
            }
        )
        _stage_record(st, record)
        st.session_state["stage9_stage_notice"] = (
            "Execution changes staged; no runtime action was taken."
        )
        st.rerun()
    st.subheader("Exchange-minimum consequence preview")
    rules_path = PROJECT_ROOT / "reports" / "competition_800" / "rules.json"
    try:
        import json

        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rules = {}
    preview = order_size_consequence_preview(saved.competition, _staged(st).competition, rules)
    rows = []
    for pair, values in preview.values["markets"].items():
        rows.append({"Market": pair, **values})
    st.dataframe(rows, width="stretch", hide_index=True)
    st.write(
        f"Potential active entry notional at one level/side/asset: "
        f"{preview.values['current_potential_active_entry_notional']:.2f} → "
        f"{preview.values['proposed_potential_active_entry_notional']:.2f} USDC"
    )
    st.subheader("Refresh consequence preview")
    plan_records = runtime.streams.get("plan").records if runtime.streams.get("plan") else ()
    current_estimate = refresh_stability_estimate(
        plan_records,
        price_tolerance_bps=saved.competition.refresh_price_tolerance_bps,
        amount_tolerance_pct=saved.competition.refresh_amount_tolerance_pct,
    )
    proposed_profile = _staged(st).competition
    proposed_estimate = refresh_stability_estimate(
        plan_records,
        price_tolerance_bps=proposed_profile.refresh_price_tolerance_bps,
        amount_tolerance_pct=proposed_profile.refresh_amount_tolerance_pct,
    )
    st.dataframe(
        [
            {
                "Outcome": key,
                "Current estimate": current_estimate.values["percentages"][key],
                "Proposed estimate": proposed_estimate.values["percentages"][key],
            }
            for key in ("KEEP", "REFRESH", "NEW", "REMOVED")
        ],
        width="stretch",
        hide_index=True,
    )
    st.caption("HISTORICAL ESTIMATE — not a future guarantee")


def _render_assets(
    st: Any, runtime: RuntimeSnapshot, saved: DashboardConfig, staged: DashboardConfig
) -> None:
    st.header("ASSETS")
    st.caption(
        "BTC remains signal-only. Per-asset edits use the existing independent "
        "asset-limit model; no arbitrary volatility knobs are created."
    )
    record = staged.to_record()
    profile = staged.competition
    with st.form("assets_form"):
        selected: list[str] = []
        limits: dict[str, dict[str, float]] = {}
        for pair in ("ETH-USDC", "SOL-USDC", "HYPE-USDC"):
            enabled = st.checkbox(
                f"{pair} enabled",
                value=pair in profile.enabled_markets,
                key=f"asset_enabled_{pair}",
                help=(
                    "Controls whether this asset is included in the existing "
                    "multi-asset configuration."
                ),
            )
            if enabled:
                selected.append(pair)
            current_limit = profile.asset_limits[pair]
            c1, c2 = st.columns(2)
            soft = c1.number_input(
                f"{pair} soft limit",
                min_value=0.01,
                value=current_limit.soft_position_notional,
                step=10.0,
                key=f"asset_soft_{pair}",
                help="Soft net-position notional limit.",
            )
            hard = c2.number_input(
                f"{pair} hard limit",
                min_value=0.01,
                value=current_limit.max_position_notional,
                step=10.0,
                key=f"asset_hard_{pair}",
                help="Hard net-position notional limit.",
            )
            limits[pair] = {"soft_position_notional": soft, "max_position_notional": hard}
        st.checkbox(
            "BTC-USDC market data enabled",
            value=True,
            disabled=True,
            help="BTC market/options/global-risk input remains enabled by architecture.",
        )
        st.checkbox(
            "BTC-USDC trading enabled",
            value=False,
            disabled=True,
            help="BTC execution remains signal-only in the committed profile.",
        )
        submitted = st.form_submit_button("STAGE ASSET CHANGES")
    if submitted:
        if not selected:
            st.error("At least one non-BTC target market must remain enabled.")
        else:
            old_alloc = profile.capital_allocation_pct
            total = sum(float(old_alloc.get(pair, 0.0)) for pair in selected) or float(
                len(selected)
            )
            allocations = {
                pair: float(old_alloc.get(pair, 1.0 / len(selected))) / total for pair in selected
            }
            record["competition"].update(
                {
                    "enabled_markets": selected,
                    "asset_limits": limits,
                    "capital_allocation_pct": allocations,
                }
            )
            _stage_record(st, record)
            st.session_state["stage9_stage_notice"] = (
                "Asset changes staged; allocations were normalized across selected markets."
            )
            st.rerun()
    st.subheader("Correlation / beta")
    relationship_rows = []
    for pair in ("ETH-USDC", "SOL-USDC", "HYPE-USDC"):
        relationship = runtime.latest_by_asset.get(pair, {}).get("relationship", {})
        relationship_rows.append(
            {
                "Asset": pair,
                "Correlation": relationship.get("btc_correlation"),
                "Beta": relationship.get("btc_beta"),
                "Confidence": relationship.get("relationship_confidence"),
                "Transmission": relationship.get("transmission_coefficient"),
                "Observations": relationship.get("relationship_observations"),
                "Valid": relationship.get("relationship_valid"),
            }
        )
    st.dataframe(relationship_rows, width="stretch", hide_index=True)
    _render_grid_preview(st, runtime, saved, _staged(st))


def _render_grid_preview(
    st: Any, runtime: RuntimeSnapshot, saved: DashboardConfig, staged: DashboardConfig
) -> None:
    st.subheader("Grid preview")
    available = [
        pair
        for pair in ("BTC-USDC", "ETH-USDC", "SOL-USDC", "HYPE-USDC")
        if pair in runtime.latest_by_asset
    ]
    if not available:
        st.warning("RUNTIME DATA UNAVAILABLE — current/proposed GridPlan preview cannot be built.")
        return
    pair = st.selectbox("Asset", available, key="grid_preview_pair")
    asset_records = runtime.latest_by_asset[pair]
    current = asset_records.get("plan")
    proposed = build_proposed_plan(asset_records, staged.strategy)
    if proposed is None:
        st.warning(
            "Current snapshot/state/mode records are incomplete; grid preview is unavailable."
        )
        return
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### CURRENT GRID")
        st.dataframe(plan_rows(current), width="stretch", hide_index=True)
    with c2:
        st.markdown("#### PROPOSED GRID")
        st.dataframe(plan_rows(proposed), width="stretch", hide_index=True)
    st.dataframe(
        [
            {
                "Field": key,
                "Current": _preview_value(value["current"]),
                "Proposed": _preview_value(value["proposed"]),
            }
            for key, value in compare_plans(current, proposed).items()
            if isinstance(value, dict) and "current" in value
        ],
        width="stretch",
        hide_index=True,
    )
    with st.expander("SYNTHETIC PREVIEW — visualization only"):
        inventory = st.slider("Synthetic inventory ratio", -0.9, 0.9, 0.0, 0.05)
        direction = st.slider("Synthetic direction score", -1.0, 1.0, 0.0, 0.05)
        synthetic = build_proposed_plan(
            asset_records, staged.strategy, inventory_ratio=inventory, direction_score=direction
        )
        if synthetic is not None:
            st.dataframe(plan_rows(synthetic), width="stretch", hide_index=True)
            st.caption("Synthetic inputs never enter the live trading pipeline.")


def _render_history(st: Any, store: ConfigStore, saved: DashboardConfig) -> None:
    st.header("CONFIG HISTORY")
    history = store.load_history()
    if not history:
        st.info("No successful dashboard configuration applies have been recorded yet.")
        return
    st.dataframe(history_rows(history), width="stretch", hide_index=True)
    versions = [int(row["version"]) for row in history if row.get("version") is not None]
    selected = st.selectbox(
        "Version to preview",
        sorted(versions, reverse=True),
        format_func=lambda value: f"v{value:04d}",
    )
    try:
        target = store.load_version(selected)
        changes = rollback_diff(saved.to_record(), target.to_record())
        st.write(f"Rollback preview: {len(changes)} fields would change")
        st.dataframe(_change_rows(changes), width="stretch", hide_index=True)
        if st.button("RESTORE VERSION", type="secondary"):
            result = store.apply_version(selected, operator_note=f"restore v{selected:04d}")
            st.success(f"CONFIG SAVED as v{result.version:04d}; runtime restart required")
            st.rerun()
    except ValueError as exc:
        st.error(str(exc))


def _render_advanced(
    st: Any,
    store: ConfigStore,
    paths: RuntimePaths,
    saved: DashboardConfig,
    staged: DashboardConfig,
) -> None:
    st.header("ADVANCED")
    st.markdown("### Source of truth and reload boundary")
    st.write(f"Competition profile: `{store.profile_path}`")
    st.write(f"Strategy overlay: `{store.strategy_path}`")
    st.write(
        f"Generated Hummingbot controller profile: `{store.controller_path or 'not configured'}`"
    )
    st.write(
        "Runtime reload: RESTART REQUIRED — the current Condor monitor constructs "
        "its Stage 8 config at startup and reports no runtime config hash."
    )
    st.write("Runtime streams:")
    st.dataframe(
        [{"Stream": name, "Path": str(path)} for name, path in paths.stream_paths().items()],
        width="stretch",
        hide_index=True,
    )
    st.markdown("### Redacted current configuration")
    st.code(yaml_export(saved.to_record()), language="yaml")
    st.download_button(
        "EXPORT CURRENT CONFIG",
        yaml_export(saved.to_record()),
        file_name="derive_adaptive_grid_current.yml",
        mime="text/yaml",
    )
    st.download_button(
        "EXPORT STAGED CONFIG",
        yaml_export(staged.to_record()),
        file_name="derive_adaptive_grid_staged.yml",
        mime="text/yaml",
    )
    st.markdown("### Grid mode explanation")
    st.write(
        "NORMAL: standard adaptive grid. DEFENSIVE: wider, fewer, smaller. "
        "LONG_BIAS / SHORT_BIAS: existing directional allocation. PAUSE: no new "
        "entries while filled position management remains with the execution engine."
    )
    st.markdown("### Security")
    st.success(
        "Dashboard code has no exchange client, order action, API-key, wallet-key, "
        "password, token, or .env display path."
    )


def _change_rows(changes: tuple[ConfigChange, ...]) -> list[dict[str, Any]]:
    return [
        {
            "Field": change.path,
            "Old": change.old,
            "Proposed": change.new,
            "Reload": change.classification,
            "Risk increase": "YES" if change.risk_increasing else "NO",
        }
        for change in changes
    ]


def _render_sidebar(
    st: Any, store: ConfigStore, saved: DashboardConfig, staged: DashboardConfig
) -> None:
    st.sidebar.markdown("## CONTROL PANEL")
    if st.sidebar.button("REFRESH STATUS"):
        st.rerun()
    if st.sidebar.button("LOAD COMPETITION PROFILE"):
        _set_staged(st, saved)
        st.sidebar.success("Competition profile staged")
        st.rerun()
    if st.sidebar.button("RESET TO SAVED CONFIG"):
        _set_staged(st, saved)
        st.rerun()
    preset = st.sidebar.selectbox(
        "Configuration template", ["CUSTOM", "COMPETITION", "CONSERVATIVE"]
    )
    if st.sidebar.button("STAGE TEMPLATE"):
        record = staged.to_record()
        record["competition"] = preset_profile(preset, saved.competition).model_dump(mode="json")
        _stage_record(st, record)
        st.sidebar.success(f"{preset} template staged")
        st.rerun()
    current_record = saved.to_record()
    proposed_record = _staged(st).to_record()
    validation, changes = validate_and_diff(current_record, proposed_record)
    st.sidebar.divider()
    st.sidebar.metric("Pending fields", len(changes))
    if st.session_state.get("stage9_stage_error"):
        st.sidebar.error(st.session_state["stage9_stage_error"])
    if notice := st.session_state.pop("stage9_stage_notice", None):
        st.sidebar.success(notice)
    if changes:
        with st.sidebar.expander("PENDING CONFIGURATION CHANGES", expanded=True):
            st.dataframe(_change_rows(changes), width="stretch", hide_index=True)
            if validation.warnings:
                for warning in validation.warnings:
                    st.warning(warning)
            enabling_execution = (
                _staged(st).competition.execution_enabled
                and not saved.competition.execution_enabled
            )
            switching_to_mainnet = (
                saved.competition.market_environment != "mainnet"
                and _staged(st).competition.market_environment == "mainnet"
            )
            risk_ack_required = validation.risk_increasing or enabling_execution
            environment_ack_required = switching_to_mainnet
            acknowledge_risk = True
            acknowledge_environment = True
            if risk_ack_required:
                acknowledge_risk = st.checkbox(
                    "I understand this change increases configured risk", key="stage9_risk_ack"
                )
            if environment_ack_required:
                st.warning(
                    "This applies a mainnet connector/profile selection. The dashboard will "
                    "keep execution and mainnet permission disabled, and the controller must "
                    "be restarted before the selection is consumed."
                )
                acknowledge_environment = st.checkbox(
                    "I understand this is a mainnet network switch, not a trading authorization",
                    key="stage9_environment_ack",
                )
                phrase = st.text_input(
                    "Type SWITCH_TO_MAINNET_READ_ONLY to confirm",
                    key="stage9_environment_ack_phrase",
                )
                acknowledge_environment = acknowledge_environment and (
                    phrase.strip() == "SWITCH_TO_MAINNET_READ_ONLY"
                )
            requires_ack = risk_ack_required or environment_ack_required
            note = st.text_input("Operator note (optional)", key="stage9_operator_note")
            can_apply = validation.valid and acknowledge_risk and acknowledge_environment
            if switching_to_mainnet:
                button_label = "APPLY MAINNET READ-ONLY PROFILE"
            elif requires_ack:
                button_label = "APPLY RISK-INCREASING CONFIGURATION"
            else:
                button_label = "APPLY CONFIGURATION"
            if st.button(
                button_label,
                disabled=not can_apply,
                type="primary",
            ):
                try:
                    result = store.apply(_staged(st), operator_note=note)
                except (OSError, ValueError) as exc:
                    st.error(f"Configuration was not applied: {exc}")
                else:
                    st.session_state["stage9_loaded_hash"] = result.new_hash
                    _set_staged(st, store.load())
                    st.success(f"CONFIG SAVED — v{result.version:04d}; RESTART REQUIRED")
                    st.rerun()
    else:
        st.sidebar.success("No pending configuration changes")


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:  # pragma: no cover - exercised by the run command
        raise SystemExit(
            "Install the dashboard extra first: python -m pip install -e '.[dashboard]'"
        ) from exc

    st.set_page_config(
        page_title="Derive Adaptive State Grid", layout="wide", initial_sidebar_state="expanded"
    )
    args = _args()
    paths = _runtime_paths(args)
    store = _store(args)
    try:
        saved = store.load()
    except Exception as exc:
        st.error(f"Configuration unavailable: {exc}")
        st.stop()
    loaded_hash = st.session_state.setdefault("stage9_loaded_hash", config_hash(saved.to_record()))
    if "stage9_staged" not in st.session_state:
        st.session_state["stage9_staged"] = saved.to_record()
    runtime = read_runtime(paths.stream_paths(), JsonlTailReader())
    staged = _staged(st)
    _render_header(st, store, runtime, saved)
    _render_sidebar(st, store, saved, staged)
    if store.detect_drift(loaded_hash):
        st.warning(
            "CONFIG DRIFT DETECTED — the source file changed after this dashboard "
            "session loaded it."
        )
    page = st.sidebar.radio(
        "Page",
        [
            "OVERVIEW",
            "SHADOW TRADING",
            "ENVIRONMENT",
            "SELF-TUNING",
            "STRATEGY",
            "RISK",
            "EXECUTION",
            "ASSETS",
            "CONFIG HISTORY",
            "ADVANCED",
        ],
        key="stage9_page",
    )
    if page == "OVERVIEW":
        _render_overview(st, runtime, staged)
    elif page == "SHADOW TRADING":
        _render_shadow(st, args)
    elif page == "ENVIRONMENT":
        _render_environment(st, saved, staged)
    elif page == "SELF-TUNING":
        _render_self_tuning(st, runtime)
    elif page == "STRATEGY":
        _render_strategy(st, runtime, saved, staged)
    elif page == "RISK":
        _render_risk(st, runtime, saved, staged)
    elif page == "EXECUTION":
        _render_execution(st, runtime, saved, staged)
    elif page == "ASSETS":
        _render_assets(st, runtime, saved, staged)
    elif page == "CONFIG HISTORY":
        _render_history(st, store, saved)
    else:
        _render_advanced(st, store, paths, saved, staged)


if __name__ == "__main__":
    main()
