"""Print a read-only Stage 7 demo card from local JSONL and report artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path(
    os.environ.get("CONDOR_DATA_DIR", "/Users/wilfred/Documents/Hummingbot/condor/data")
)
DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[1] / "reports" / "stage6_5"
DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "hummingbot"
    / "derive_adaptive_grid"
    / "derive_adaptive_grid_testnet.example.yml"
)


def _latest_json_object(path: Path) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSON in {path} at line {line_number}: {exc}") from exc
            if isinstance(candidate, dict):
                latest = candidate
    if latest is None:
        raise RuntimeError(f"no JSON object found in {path}")
    return latest


def _scalar_config_value(text: str, key: str) -> str:
    pattern = rf"(?m)^\s*{re.escape(key)}\s*:\s*([^#\n]+)"
    match = re.search(pattern, text)
    if match is None:
        return "missing"
    return match.group(1).strip().strip("'\"")


def _as_bool(value: str) -> bool | None:
    normalized = value.lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    return None


def _number(value: Any, places: int = 5) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def _fraction_percent(value: Any, places: int = 3) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.{places}f}%"
    except (TypeError, ValueError):
        return str(value)


def _iv_percent(value: Any) -> str:
    return _fraction_percent(value, places=2)


def _reason_text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value) or "none recorded"
    return str(value) if value not in (None, "") else "none recorded"


def _plan_level_count(plan: dict[str, Any], side: str) -> int:
    count = plan.get(f"{side}_levels_count")
    if count is not None:
        return int(count)
    levels = plan.get(f"{side}_levels", [])
    return len(levels) if isinstance(levels, list) else 0


def _load_replay_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in audit.get("base_replay_summaries", []):
        if row.get("fill_model") == "conservative_cross_through":
            rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("strategy", "")))


def _print_snapshot(snapshot: dict[str, Any]) -> None:
    print("MARKET SNAPSHOT")
    print(
        f"  {snapshot.get('timestamp', 'n/a')} | {snapshot.get('trading_pair', 'n/a')} | "
        f"mid {_number(snapshot.get('mid_price'), 2)} | "
        f"spread {_number(snapshot.get('spread_bps'), 3)} bps"
    )
    print(
        f"  ATM IV {_iv_percent(snapshot.get('atm_iv'))} | "
        f"IV source {snapshot.get('option_data_source', 'n/a')} | "
        f"data valid {snapshot.get('data_valid', 'n/a')}"
    )


def _print_state(state: dict[str, Any]) -> None:
    print("MARKET STATE")
    print(
        f"  volatility {state.get('volatility_state', 'n/a')} "
        f"(score {_number(state.get('volatility_score'), 3)}) | "
        f"IV ratio {_number(state.get('iv_ratio'), 3)}"
    )
    print(
        f"  direction {state.get('direction_state', 'n/a')} "
        f"(score {_number(state.get('direction_score'), 3)}) | "
        f"inventory {state.get('inventory_state', 'n/a')} "
        f"(ratio {_number(state.get('inventory_ratio'), 4)})"
    )
    print(f"  reasons: {_reason_text(state.get('reasons'))}")


def _print_mode(mode: dict[str, Any]) -> None:
    print("GRID MODE")
    print(
        f"  {str(mode.get('mode', 'n/a')).upper()} | confidence "
        f"{_number(mode.get('confidence'), 3)} | transition "
        f"{mode.get('transition_occurred', 'n/a')}"
    )
    print(f"  reasons: {_reason_text(mode.get('reasons'))}")


def _print_plan(plan: dict[str, Any]) -> None:
    print("GRID PLAN")
    print(
        f"  v{plan.get('plan_version', 'n/a')} | {str(plan.get('mode', 'n/a')).upper()} | "
        f"center {_number(plan.get('center_price'), 2)} | "
        f"width {_fraction_percent(plan.get('total_grid_width_pct'), 3)}"
    )
    print(
        f"  levels buy/sell {_plan_level_count(plan, 'buy')}/{_plan_level_count(plan, 'sell')} | "
        f"allocation {_fraction_percent(plan.get('buy_allocation_pct'), 1)} / "
        f"{_fraction_percent(plan.get('sell_allocation_pct'), 1)} | "
        f"enabled {plan.get('enabled', 'n/a')}"
    )
    print(f"  reasons: {_reason_text(plan.get('reasons'))}")


def _print_safety(config_path: Path) -> None:
    config_text = config_path.read_text(encoding="utf-8")
    values = {
        key: _scalar_config_value(config_text, key)
        for key in (
            "connector_name",
            "trading_pair",
            "allow_mainnet_trading",
            "execution_enabled",
            "execution_max_levels_per_side",
            "post_only",
            "leverage",
        )
    }
    execution_enabled = _as_bool(values["execution_enabled"])
    posture = (
        "DRY RUN (execution_enabled=false)"
        if execution_enabled is False
        else "CONFIG IS NOT DRY RUN; this helper still makes no exchange calls"
    )
    print("EXECUTION SAFETY POSTURE")
    print(f"  {posture}")
    print(
        f"  connector {values['connector_name']} | pair {values['trading_pair']} | "
        f"leverage {values['leverage']} | post-only {values['post_only']}"
    )
    print(
        f"  mainnet allowed {values['allow_mainnet_trading']} | "
        f"max live levels/side {values['execution_max_levels_per_side']}"
    )


def _print_replay(audit: dict[str, Any]) -> None:
    print("STAGE 6.5 OFFLINE REPLAY")
    verdict = audit.get("audit_verdict", {})
    window = audit.get("common_window", {})
    print(
        f"  status {verdict.get('status', 'n/a')} | canonical frames "
        f"{audit.get('canonical_frame_count', 'n/a')} | "
        f"window {window.get('start', 'n/a')} -> {window.get('end', 'n/a')}"
    )
    labels = {
        "static_geometric_grid": "static",
        "rv_only_adaptive_grid": "rv-only",
        "iv_adaptive_grid": "iv-aware",
    }
    for row in _load_replay_rows(audit):
        strategy = labels.get(str(row.get("strategy")), str(row.get("strategy")))
        print(
            f"  {strategy:8} total {_number(row.get('total_pnl'), 5)} | "
            f"realized {_number(row.get('net_realized_pnl'), 5)} | "
            f"drawdown {_number(row.get('maximum_drawdown'), 5)} | "
            f"fills {row.get('entry_fills', 'n/a')}"
        )
    print("  classification: OFFLINE REPLAY; not a live fill or profitability result")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    report_dir = args.report_dir.expanduser().resolve()
    config_path = args.config.expanduser().resolve()

    snapshot = _latest_json_object(data_dir / "derive_market_snapshots.jsonl")
    state = _latest_json_object(data_dir / "derive_market_states.jsonl")
    mode = _latest_json_object(data_dir / "derive_grid_modes.jsonl")
    plan = _latest_json_object(data_dir / "derive_grid_plans.jsonl")
    audit = json.loads((report_dir / "audit_summary.json").read_text(encoding="utf-8"))

    print("=" * 78)
    print("DERIVE ADAPTIVE STATE GRID | SAFE READ-ONLY DEMO")
    print(f"data: {data_dir}")
    print("No exchange/API calls are made by this helper.")
    print("=" * 78)
    _print_snapshot(snapshot)
    _print_state(state)
    _print_mode(mode)
    _print_plan(plan)
    _print_safety(config_path)
    _print_replay(audit)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
