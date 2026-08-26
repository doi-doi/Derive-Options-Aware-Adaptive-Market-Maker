"""Current/proposed grid previews that call the existing Stage 4 planner."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from derive_options_mm.grid_engine import GridPlan, build_grid_plan

from .config_schema import Stage9StrategySettings


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def build_proposed_plan(
    asset_records: dict[str, dict[str, Any]],
    strategy: Stage9StrategySettings,
    *,
    inventory_ratio: float | None = None,
    direction_score: float | None = None,
) -> GridPlan | None:
    """Build a preview plan through ``build_grid_plan``; never writes runtime state."""

    snapshot = asset_records.get("snapshot")
    state = asset_records.get("state")
    decision = asset_records.get("mode")
    current_raw = asset_records.get("plan")
    if not snapshot or not state or not decision:
        return None
    staged_state = deepcopy(state)
    staged_decision = deepcopy(decision)
    if inventory_ratio is not None:
        staged_state["inventory_ratio"] = inventory_ratio
        staged_decision["inventory_ratio"] = inventory_ratio
    if direction_score is not None:
        staged_state["direction_score"] = direction_score
        staged_decision["direction_score"] = direction_score
    current_plan = GridPlan.model_validate(current_raw) if current_raw else None
    return build_grid_plan(
        snapshot,
        staged_state,
        staged_decision,
        strategy.to_multi_asset_config().grid,
        previous_plan=current_plan,
        current_plan_version=current_plan.plan_version if current_plan else 0,
    )


def plan_rows(plan: GridPlan | dict[str, Any] | None) -> list[dict[str, Any]]:
    if plan is None:
        return []
    record = plan.to_record() if isinstance(plan, GridPlan) else plan
    rows: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        for level in record.get(f"{side}_levels", []) or []:
            rows.append(
                {
                    "side": side.upper(),
                    "level": level.get("level_index"),
                    "price": level.get("theoretical_price"),
                    "distance_bps": level.get("distance_from_center_bps"),
                    "quote_notional": level.get("quote_amount"),
                }
            )
    return rows


def plan_summary(plan: GridPlan | dict[str, Any] | None) -> dict[str, Any]:
    if plan is None:
        return {}
    record = plan.to_record() if isinstance(plan, GridPlan) else plan
    return {
        key: record.get(key)
        for key in (
            "trading_pair",
            "mode",
            "reference_price",
            "center_price",
            "center_shift_bps",
            "total_grid_width_pct",
            "inner_distance_bps",
            "buy_levels_count",
            "sell_levels_count",
            "buy_allocation_pct",
            "sell_allocation_pct",
            "effective_quote_amount",
            "plan_version",
            "valid",
        )
    }


def compare_plans(current: Any, proposed: Any) -> dict[str, Any]:
    old = plan_summary(current)
    new = plan_summary(proposed)
    result: dict[str, Any] = {}
    for key in (
        "center_price",
        "total_grid_width_pct",
        "inner_distance_bps",
        "buy_levels_count",
        "sell_levels_count",
        "buy_allocation_pct",
        "sell_allocation_pct",
        "effective_quote_amount",
    ):
        result[key] = {"current": old.get(key), "proposed": new.get(key)}
    result["level_count_change"] = {
        "current": (old.get("buy_levels_count", 0), old.get("sell_levels_count", 0)),
        "proposed": (new.get("buy_levels_count", 0), new.get("sell_levels_count", 0)),
    }
    return result


__all__ = [
    "build_proposed_plan",
    "compare_plans",
    "plan_rows",
    "plan_summary",
]
