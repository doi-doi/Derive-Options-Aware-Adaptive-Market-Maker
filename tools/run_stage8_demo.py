"""Generate the offline Stage 8 four-asset dry-run and machine outputs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from derive_options_mm.grid_engine import GridParameterEngine  # noqa: E402
from derive_options_mm.mode_selector import ModeSelector  # noqa: E402
from derive_options_mm.multi_asset import (  # noqa: E402
    SUPPORTED_TRADING_PAIRS,
    MultiAssetConfig,
    MultiAssetCoordinator,
)  # noqa: E402
from derive_options_mm.state_engine import StateEngine  # noqa: E402
from evaluation.multi_asset_replay import (  # noqa: E402
    MultiAssetReplayConfig,
    run_stage8_ablations,
)  # noqa: E402


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(_json_safe(row), sort_keys=True, allow_nan=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(_json_safe(rows))


def _stage8_config() -> MultiAssetConfig:
    # These are transparent demo settings, not a calibration or optimization run.
    return MultiAssetConfig(
        state={
            "minimum_history_samples": 20,
            "realized_vol_window_seconds": 60,
            "realized_vol_baseline_seconds": 300,
            "direction_return_window_seconds": 30,
            "direction_price_scale": 0.01,
            "direction_confirmation_samples": 1,
        },
        mode={
            "mode_confirmation_samples": 1,
            "pause_recovery_samples": 1,
            "minimum_mode_duration_seconds": 0,
        },
        global_options={
            "minimum_history_samples": 5,
            "history_window_seconds": 900,
        },
        relationship={
            "window_seconds": 3600,
            "short_window_seconds": 900,
            "medium_window_seconds": 1800,
            "sensitivity_windows_seconds": (900, 1800, 3600),
            "minimum_observations": 15,
        },
        portfolio_risk={
            "portfolio_max_gross_notional": 10_000,
            "portfolio_soft_beta_exposure": 2_000,
            "portfolio_hard_beta_exposure": 4_000,
            "portfolio_max_long_beta_exposure": 3_000,
            "portfolio_max_short_beta_exposure": 3_000,
            "per_asset_max_position_notional": 2_000,
        },
    )


def build_demo_ticks(count: int = 140) -> list[dict[str, dict[str, Any]]]:
    """Create a deterministic common clock with distinct local asset paths."""

    levels = {"BTC-USDC": 80_000.0, "ETH-USDC": 3_000.0, "SOL-USDC": 150.0, "HYPE-USDC": 35.0}
    prices = dict(levels)
    ticks: list[dict[str, dict[str, Any]]] = []
    for index in range(count):
        btc_return = (
            0.0006 * math.sin(index * 0.31)
            + 0.00035 * math.cos(index * 0.17)
            + (0.002 if 82 <= index <= 86 else 0.0)
        )
        local_returns = {
            "BTC-USDC": btc_return,
            "ETH-USDC": 0.72 * btc_return + 0.00045 * math.sin(index * 0.53),
            "SOL-USDC": 1.25 * btc_return + 0.0012 * math.cos(index * 0.91),
            "HYPE-USDC": -0.18 * btc_return + 0.0022 * math.sin(index * 1.17),
        }
        for pair, value in local_returns.items():
            prices[pair] *= math.exp(value)
        timestamp = 1_700_000_000.0 + index * 5.0
        iv = 0.48 * (1.0 + 0.08 * math.sin(index * 0.12))
        if 82 <= index <= 86:
            iv *= 1.35
        tick: dict[str, dict[str, Any]] = {}
        for pair in SUPPORTED_TRADING_PAIRS:
            price = prices[pair]
            book = 0.06 * math.sin(index * 0.19 + len(pair))
            flow = 0.05 * math.cos(index * 0.23 + len(pair))
            tick[pair] = {
                "timestamp": timestamp,
                "trading_pair": pair,
                "market_environment": "testnet",
                "data_valid": True,
                "best_bid": price * (1.0 - 0.0001),
                "best_ask": price * (1.0 + 0.0001),
                "mid_price": price,
                "spread_bps": 2.0,
                "best_bid_size": 2.0,
                "best_ask_size": 1.8,
                "depth_imbalance": book,
                "order_flow_imbalance": flow,
                "trade_data_available": True,
                "current_position": 0.0,
                "position_notional": 0.0,
                "available_balance": 100_000.0,
                "account_data_available": True,
            }
        tick["BTC-USDC"].update(
            {
                "atm_iv": iv,
                "atm_call_iv": iv * 1.01,
                "atm_put_iv": iv * 0.99,
                "iv_data_available": True,
                "iv_confidence": 1.0,
                "option_data_age_seconds": 1.0,
                "option_data_timestamp": timestamp - 1.0,
                "option_environment": "production",
            }
        )
        for pair in SUPPORTED_TRADING_PAIRS[1:]:
            tick[pair]["iv_data_available"] = False
            tick[pair]["iv_confidence"] = 0.0
        ticks.append(tick)
    return ticks


def _btc_regression(
    ticks: list[dict[str, dict[str, Any]]], config: MultiAssetConfig
) -> dict[str, Any]:
    old_state_engine = StateEngine(config.state)
    old_selector = ModeSelector(config.mode)
    old_plan_engine = GridParameterEngine(config.grid)
    coordinator = MultiAssetCoordinator(config)
    state_mismatches = 0
    mode_mismatches = 0
    plan_mismatches = 0
    comparable = 0
    for tick in ticks:
        btc = tick["BTC-USDC"]
        old_state = old_state_engine.update(btc)
        old_mode = old_selector.update(old_state)
        old_plan = old_plan_engine.build(btc, old_state, old_mode)
        new_cycle = coordinator.update(tick)
        new_state = new_cycle.states["BTC-USDC"]
        new_mode = new_cycle.decisions["BTC-USDC"]
        new_plan = new_cycle.plans["BTC-USDC"]
        if not old_state.state_valid or not new_state.state_valid:
            continue
        comparable += 1
        for field in (
            "volatility_score",
            "direction_score",
            "inventory_ratio",
            "confidence",
        ):
            left = getattr(old_state, field)
            right = getattr(new_state, field)
            if left is None or right is None:
                if left != right:
                    state_mismatches += 1
            elif abs(left - right) > 1e-12:
                state_mismatches += 1
        if (
            old_state.volatility_state != new_state.volatility_state
            or old_state.direction_state != new_state.direction_state
        ):
            state_mismatches += 1
        if old_mode.mode != new_mode.mode or old_mode.valid != new_mode.valid:
            mode_mismatches += 1
        if (
            old_plan.mode != new_plan.mode
            or old_plan.enabled != new_plan.enabled
            or old_plan.valid != new_plan.valid
            or abs(old_plan.total_grid_width_pct - new_plan.total_grid_width_pct) > 1e-12
            or old_plan.buy_levels_count != new_plan.buy_levels_count
            or old_plan.sell_levels_count != new_plan.sell_levels_count
        ):
            plan_mismatches += 1
    return {
        "comparable_valid_frames": comparable,
        "state_mismatches": state_mismatches,
        "mode_mismatches": mode_mismatches,
        "plan_mismatches": plan_mismatches,
        "within_tolerance": state_mismatches == mode_mismatches == plan_mismatches == 0,
        "tolerance": 1e-12,
    }


def _asset_rows(results: list[Any]) -> list[dict[str, Any]]:
    shared = next(
        result
        for result in results
        if result.label == "shared_btc_iv_with_portfolio_governor"
    )
    rows: list[dict[str, Any]] = []
    for pair in SUPPORTED_TRADING_PAIRS:
        states = [cycle.states[pair] for cycle in shared.cycles]
        modes = Counter(cycle.decisions[pair].mode.value for cycle in shared.cycles)
        widths = [float(cycle.plans[pair].total_grid_width_pct) for cycle in shared.cycles]
        scores = [state.volatility_score for state in states if state.volatility_score is not None]
        local = [
            state.local_realized_volatility_ratio
            for state in states
            if state.local_realized_volatility_ratio is not None
        ]
        transmitted = [
            state.transmitted_btc_iv_component
            for state in states
            if state.transmitted_btc_iv_component is not None
        ]
        rows.append(
            {
                "trading_pair": pair,
                "frames": len(states),
                "mean_local_rv_ratio": fmean(local) if local else None,
                "mean_transmitted_btc_iv": fmean(transmitted) if transmitted else None,
                "mean_volatility_score": fmean(scores) if scores else None,
                "mean_grid_width_pct": fmean(widths) if widths else None,
                "normal_count": modes.get("normal", 0),
                "defensive_count": modes.get("defensive", 0),
                "pause_count": modes.get("pause", 0),
                "long_bias_count": modes.get("long_bias", 0),
                "short_bias_count": modes.get("short_bias", 0),
            }
        )
    return rows


def _relationship_rows(shared: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pair in SUPPORTED_TRADING_PAIRS:
        relationships = [cycle.relationships[pair] for cycle in shared.cycles]
        correlations = [
            item.btc_correlation
            for item in relationships
            if item.btc_correlation is not None
        ]
        betas = [item.btc_beta for item in relationships if item.btc_beta is not None]
        transmissions = [item.transmission_coefficient for item in relationships]
        residuals = [
            item.residual_volatility
            for item in relationships
            if item.residual_volatility is not None
        ]
        last = relationships[-1]
        rows.append(
            {
                "trading_pair": pair,
                "last_correlation": last.btc_correlation,
                "last_beta": last.btc_beta,
                "last_transmission": last.transmission_coefficient,
                "mean_correlation": fmean(correlations) if correlations else None,
                "mean_beta": fmean(betas) if betas else None,
                "mean_transmission": fmean(transmissions) if transmissions else None,
                "mean_residual_volatility": fmean(residuals) if residuals else None,
                "observations": last.relationship_observations,
                "relationship_valid": last.relationship_valid,
            }
        )
    return rows


def _relationship_sensitivity_rows(shared: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sensitivity = shared.metrics.get("relationship_window_sensitivity", {})
    for pair, windows in sensitivity.items():
        for window, values in windows.items():
            rows.append(
                {
                    "trading_pair": pair,
                    "window_seconds": float(window),
                    "correlation": values.get("correlation"),
                    "beta": values.get("beta"),
                    "observations": values.get("observations"),
                }
            )
    return rows


def _ablation_rows(results: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        for pair in SUPPORTED_TRADING_PAIRS:
            states = [cycle.states[pair] for cycle in result.cycles]
            scores = [
                state.volatility_score
                for state in states
                if state.volatility_score is not None
            ]
            widths = [float(cycle.plans[pair].total_grid_width_pct) for cycle in result.cycles]
            rows.append(
                {
                    "scenario": result.label,
                    "trading_pair": pair,
                    "mean_volatility_score": fmean(scores) if scores else None,
                    "mean_grid_width_pct": fmean(widths) if widths else None,
                    "portfolio_pnl": result.metrics.get("portfolio_pnl"),
                    "portfolio_drawdown": result.metrics.get("portfolio_drawdown"),
                    "risk_blocks": result.metrics.get("risk_blocks"),
                }
            )
    return rows


def _report(
    path: Path,
    *,
    config: MultiAssetConfig,
    regression: dict[str, Any],
    relationships: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    results: list[Any],
) -> None:
    shared = next(
        result
        for result in results
        if result.label == "shared_btc_iv_with_portfolio_governor"
    )
    latest = shared.cycles[-1]
    comparison = "\n".join(
        f"| {result.label} | {result.metrics.get('portfolio_pnl', 0):.6f} | "
        f"{result.metrics.get('portfolio_drawdown', 0):.6f} | "
        f"{result.metrics.get('risk_blocks', 0)} |"
        for result in results
    )
    relationship_table = "\n".join(
        f"| {row['trading_pair']} | {row['last_correlation']} | {row['last_beta']} | "
        f"{row['last_transmission']} | {row['observations']} |"
        for row in relationships
    )
    asset_table = "\n".join(
        f"| {row['trading_pair']} | {row['mean_local_rv_ratio']} | "
        f"{row['mean_transmitted_btc_iv']} | {row['mean_grid_width_pct']} | "
        f"{row['defensive_count']} |"
        for row in assets
    )
    text = f"""# Stage 8 — Multi-Asset Shared BTC-Options Risk and Portfolio Grid Risk

## 1. Executive summary

This offline dry run routes BTC-USDC, ETH-USDC, SOL-USDC, and HYPE-USDC through
one shared BTC ATM IV state, local asset state engines, independent grid plans,
and a portfolio risk governor. Execution remains disabled and no exchange order
endpoint is contacted.

## 2. Architecture

`GlobalRiskState(BTC options)` -> `BTCTransmissionState(asset)` -> local
`MarketState` -> local `GridModeDecision` -> local `GridPlan` -> portfolio
`PortfolioRiskDecision` -> pair-scoped dry-run routing.

## 3. Shared BTC options risk

BTC ATM IV is fetched/processed once per common clock tick. Non-BTC assets do
not receive copied absolute IV; they receive the relative BTC IV ratio scaled by
their measured BTC relationship.

## 4. Asset-local state

Each asset retains its own book imbalance, OFI, returns, inventory, data quality,
direction, mode, and grid geometry. Direction does not use BTC IV or BTC
direction.

## 5. BTC correlation/beta

| Pair | Last correlation | Last beta | Transmission | Observations |
| --- | ---: | ---: | ---: | ---: |
{relationship_table}

The relationship engine uses synchronized log returns, minimum observations,
zero-variance guards, staleness checks, and a documented beta clip of
`+/-{config.relationship.beta_clip}`. The 15m/30m/60m sensitivity windows are
reported by the engine and are not selected by PnL.

## 6. Transmission formula

For non-BTC assets:

`transmission = min(transmission_max, confidence * abs(correlation) * abs(clipped_beta))`.

The coefficient is bounded and correlation sign is diagnostic only; negative
correlation does not invert volatility. The transmitted component is
`btc_iv_ratio * transmission`.

## 7. Portfolio risk governor

The governor includes filled signed positions, pending entry notional, gross
notional, net notional, beta-equivalent exposure, long/short beta exposure, and
per-asset limits. It blocks exposure-increasing sides while allowing
risk-reducing sides and filled-position management.

Latest dry-run portfolio state: gross `{latest.portfolio_risk.gross_notional:.6f}`;
beta-equivalent `{latest.portfolio_risk.btc_beta_equivalent_exposure:.6f}`;
blocked pairs `{', '.join(latest.portfolio_risk.blocked_pairs) or 'none'}`.

## 8. Hierarchical PAUSE behavior

Missing asset snapshots disable that asset only. Missing or stale BTC IV defaults
to local-RV-only with reduced confidence. A configured `pause` fallback can make
the affected asset invalid. Portfolio limits block worsening sides without
force-liquidating filled positions.

## 9. Per-asset GridPlans

| Pair | Mean local RV ratio | Mean transmitted BTC IV | Mean width | Defensive frames |
| --- | ---: | ---: | ---: | ---: |
{asset_table}

Plan versions are maintained by one `GridParameterEngine` per pair.

## 10. Execution routing

Dry-run level keys are pair-qualified, for example `BTC-USDC::buy_0` and
`ETH-USDC::buy_0`. The existing Hummingbot adapter still uses one controller
instance per pair and rejects unsupported symbols; it remains the execution
boundary and is not started by this demo.

## 11. BTC regression compatibility

`{regression['comparable_valid_frames']}` valid frames compared; state mismatches
`{regression['state_mismatches']}`, mode mismatches `{regression['mode_mismatches']}`,
plan mismatches `{regression['plan_mismatches']}` at tolerance
`{regression['tolerance']}`. Result: **{'PASS' if regression['within_tolerance'] else 'FAIL'}**.

## 12–14. BTC + ETH, BTC + ETH + SOL, and full four-asset dry run

The same common-clock coordinator supports staged enablement through
`MultiAssetConfig.enabled_markets`. This run used all four configured markets;
unavailable markets would be disabled independently.

## 15–17. Multi-asset evaluation, IV ablation, and portfolio-governor ablation

| Scenario | Portfolio PnL | Max drawdown | Risk blocks |
| --- | ---: | ---: | ---: |
{comparison}

These metrics are deterministic replay diagnostics, not performance claims.
The ablation changes only the BTC-IV input or governor limits; it does not tune
parameters to maximize PnL.

## 18. Risk metrics

Machine-readable outputs include global risk, relationship statistics, relationship
window sensitivity, per-asset
state statistics, portfolio risk summaries, ablation rows, and portfolio risk
events under `reports/stage8/`. The point-in-time JSONL ledgers are
`derive_btc_relationship_states.jsonl` and `derive_portfolio_risk_states.jsonl`.

## 19. Tests

Focused Stage 8 tests cover shared-state reuse, relationship bounds and
zero-variance handling, stale-IV fallback, local direction, plan-version
isolation, side-specific portfolio blocking, and pair-scoped IDs. Full project
verification is reported separately after execution.

## 20. Limitations

Correlation is time-varying; beta is estimated rather than guaranteed; BTC IV
may not help every asset; HYPE can have substantial idiosyncratic risk; BTC
options are a systematic volatility signal rather than direct ETH/SOL/HYPE IV;
historical correlation does not imply future correlation; beta-equivalent risk
is an approximation; and BBO replay remains simulated without raw queue trades.

## 21. Proposed testnet rollout

Do not enable multi-asset live execution from this Stage 8 demo. If separately
authorized later, validate BTC + ETH one level per side on testnet first, then
review before adding SOL or HYPE. Mainnet remains disabled.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run(output_dir: Path, report_path: Path, count: int = 140) -> dict[str, Any]:
    config = _stage8_config()
    ticks = build_demo_ticks(count)
    results = run_stage8_ablations(
        ticks,
        strategy_config=config,
        replay_config=MultiAssetReplayConfig(order_scale=0.10, max_levels_per_side=1),
    )
    shared = next(
        result
        for result in results
        if result.label == "shared_btc_iv_with_portfolio_governor"
    )
    regression = _btc_regression(ticks, config)
    relationships = _relationship_rows(shared)
    relationship_sensitivity = _relationship_sensitivity_rows(shared)
    assets = _asset_rows(results)
    ablations = _ablation_rows(results)
    latest = shared.cycles[-1]
    _write_json(
        output_dir / "global_risk_summary.json",
        {
            "latest": latest.global_risk.model_dump(mode="json"),
            "options_processed_once_per_tick": True,
            "options_update_count": len(ticks),
            "ticks": len(ticks),
        },
    )
    _write_csv(output_dir / "relationship_statistics.csv", relationships)
    _write_csv(
        output_dir / "relationship_window_sensitivity.csv",
        relationship_sensitivity,
    )
    _write_jsonl(
        output_dir / "derive_btc_relationship_states.jsonl",
        [
            {
                "timestamp": cycle.timestamp,
                "global_risk_regime": cycle.global_risk.global_risk_regime.value,
                **relationship.model_dump(mode="json"),
            }
            for cycle in shared.cycles
            for relationship in cycle.relationships.values()
        ],
    )
    _write_csv(output_dir / "asset_state_statistics.csv", assets)
    _write_csv(
        output_dir / "btc_iv_ablation_by_asset.csv",
        [row for row in ablations if row["scenario"] in {
            "shared_btc_iv_with_portfolio_governor",
            "local_rv_only_with_portfolio_governor",
        }],
    )
    _write_json(
        output_dir / "portfolio_risk_summary.json",
        {
            "latest": latest.portfolio_risk.model_dump(mode="json"),
            "metrics": shared.metrics,
        },
    )
    _write_jsonl(
        output_dir / "derive_portfolio_risk_states.jsonl",
        [
            {
                "timestamp": cycle.timestamp,
                "positions": tick["positions"],
                "pending_exposure": tick["pending_exposure"],
                **cycle.portfolio_risk.model_dump(mode="json"),
            }
            for cycle, tick in zip(shared.cycles, shared.ticks, strict=True)
        ],
    )
    _write_csv(
        output_dir / "portfolio_replay_comparison.csv",
        [
            {
                "scenario": result.label,
                **result.metrics,
            }
            for result in results
        ],
    )
    _write_jsonl(
        output_dir / "portfolio_risk_events.jsonl",
        [
            event
            for result in results
            for event in result.events
            if event.get("event") == "ENTRY_BLOCKED"
        ],
    )
    _write_json(output_dir / "btc_regression_summary.json", regression)
    _report(
        report_path,
        config=config,
        regression=regression,
        relationships=relationships,
        assets=assets,
        results=results,
    )
    return {
        "output_dir": str(output_dir.resolve()),
        "report": str(report_path.resolve()),
        "ticks": len(ticks),
        "scenarios": [result.label for result in results],
        "btc_regression": regression,
        "portfolio_metrics": shared.metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/stage8"))
    parser.add_argument("--report", type=Path, default=Path("reports/stage8_multi_asset.md"))
    parser.add_argument("--count", type=int, default=140)
    args = parser.parse_args()
    print(json.dumps(run(args.output, args.report, args.count), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
