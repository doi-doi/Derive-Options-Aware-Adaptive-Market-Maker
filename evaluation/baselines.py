"""Fair comparison variants for Stage 6 replay."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from derive_options_mm.grid_engine import (
    GridMode,
    GridParameterConfig,
    GridPlan,
    allocate_quote_per_level,
    calculate_base_reference,
    calculate_inner_distance,
    generate_buy_levels,
    generate_geometric_distances,
    generate_sell_levels,
)


class StrategyVariant(StrEnum):
    """The three required, non-optimized comparison strategies."""

    STATIC = "static_geometric_grid"
    RV_ONLY = "rv_only_adaptive_grid"
    IV_ADAPTIVE = "iv_adaptive_grid"


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _disabled_plan(snapshot: Mapping[str, Any], reason: str) -> dict[str, Any]:
    timestamp = str(snapshot.get("timestamp", ""))
    return {
        "timestamp": timestamp,
        "trading_pair": str(snapshot.get("trading_pair", "BTC-USDC")),
        "mode": GridMode.PAUSE.value,
        "enabled": False,
        "valid": False,
        "reference_price": None,
        "center_price": None,
        "center_shift_bps": 0.0,
        "total_grid_width_pct": 0.0,
        "half_grid_width_pct": 0.0,
        "inner_distance_bps": 0.0,
        "buy_levels_count": 0,
        "sell_levels_count": 0,
        "total_quote_amount": 0.0,
        "effective_quote_amount": 0.0,
        "buy_allocation_pct": 0.0,
        "sell_allocation_pct": 0.0,
        "volatility_width_multiplier": 0.0,
        "mode_width_multiplier": 0.0,
        "mode_size_multiplier": 0.0,
        "inventory_adjustment": 0.0,
        "directional_adjustment": 0.0,
        "buy_levels": [],
        "sell_levels": [],
        "confidence": 0.0,
        "reasons": [reason],
        "plan_change_significant": True,
        "plan_version": 0,
    }


def static_geometric_plan(
    snapshot: Mapping[str, Any],
    *,
    config: GridParameterConfig | None = None,
    plan_version: int = 0,
) -> dict[str, Any]:
    """Build the frozen-width, five-level, 50/50 baseline.

    The baseline shares Stage 4's reference source, spacing function, maker
    safety distance, quote budget, and exchange-rule assumptions.  It does not
    consume IV, volatility score, direction, inventory, or mode switching.
    """

    cfg = config or GridParameterConfig()
    reference = calculate_base_reference(snapshot)
    spread = _decimal(snapshot.get("spread_bps"))
    if reference is None or reference <= 0:
        return _disabled_plan(snapshot, "static baseline reference unavailable")
    if spread is None or spread < 0:
        return _disabled_plan(snapshot, "static baseline spread unavailable")
    try:
        inner_bps = calculate_inner_distance(spread, GridMode.NORMAL, cfg)
        half_width = cfg.base_grid_width_pct / Decimal(2)
        inner_pct = inner_bps / Decimal(10_000)
        distances = generate_geometric_distances(inner_pct, half_width, 5)
        quote_per_level = allocate_quote_per_level(
            cfg.base_total_quote_amount,
            Decimal("0.5"),
            5,
        )
        buy_levels = generate_buy_levels(reference, distances, quote_per_level)
        sell_levels = generate_sell_levels(reference, distances, quote_per_level)
        plan = GridPlan(
            timestamp=str(snapshot.get("timestamp", "")),
            trading_pair=str(snapshot.get("trading_pair", "BTC-USDC")),
            mode=GridMode.NORMAL,
            enabled=True,
            reference_price=reference,
            center_price=reference,
            center_shift_bps=Decimal(0),
            total_grid_width_pct=cfg.base_grid_width_pct,
            half_grid_width_pct=half_width,
            inner_distance_bps=inner_bps,
            buy_levels_count=5,
            sell_levels_count=5,
            total_quote_amount=cfg.base_total_quote_amount,
            effective_quote_amount=cfg.base_total_quote_amount,
            buy_allocation_pct=Decimal("0.5"),
            sell_allocation_pct=Decimal("0.5"),
            volatility_width_multiplier=Decimal(1),
            mode_width_multiplier=Decimal(1),
            mode_size_multiplier=Decimal(1),
            inventory_adjustment=Decimal(0),
            directional_adjustment=Decimal(0),
            buy_levels=buy_levels,
            sell_levels=sell_levels,
            valid=True,
            confidence=1.0,
            reasons=[
                "static baseline: fixed Stage 4 base width",
                "static baseline: five geometric levels per side",
                "static baseline: 50/50 fixed allocation",
            ],
            plan_change_significant=True,
            plan_version=plan_version,
        )
        return plan.to_record()
    except (ArithmeticError, ValueError):
        return _disabled_plan(snapshot, "static baseline geometry unavailable")


def strategy_description(variant: StrategyVariant | str) -> str:
    """Return a report-friendly description without implying superiority."""

    selected = StrategyVariant(str(variant))
    return {
        StrategyVariant.STATIC: "fixed Stage 4 base width, five levels, 50/50 allocation",
        StrategyVariant.RV_ONLY: (
            "existing adaptive State -> Mode -> GridPlan with IV weight removed"
        ),
        StrategyVariant.IV_ADAPTIVE: "existing full options-aware State -> Mode -> GridPlan",
    }[selected]


__all__ = [
    "StrategyVariant",
    "static_geometric_plan",
    "strategy_description",
]
