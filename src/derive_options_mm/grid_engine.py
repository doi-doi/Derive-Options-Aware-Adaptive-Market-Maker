"""Pure Stage 4 adaptive-grid parameter calculations.

Stage 4 consumes the normalized outputs of Stages 1--3 and produces a
theoretical :class:`GridPlan`.  It deliberately has no Hummingbot, Condor,
network, order, or executor dependency.  Prices, percentages, allocations,
and notional amounts are calculated with :class:`~decimal.Decimal`; JSONL
serialization converts the final immutable values to ordinary JSON numbers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .mode_selector import GridMode, GridModeDecision
from .state_engine import MarketState

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TEN_THOUSAND = Decimal("10000")
_TOLERANCE = Decimal("1e-12")


class GridLevelSide(StrEnum):
    """The two theoretical sides of a grid."""

    BUY = "buy"
    SELL = "sell"


class GridParameterConfig(BaseModel):
    """Conservative, explicit Stage 4 parameter defaults.

    These values are illustrative testing defaults, not optimized trading
    parameters.  Stage 5 remains responsible for exchange rules and order
    execution constraints.
    """

    base_grid_width_pct: Decimal = Field(default=Decimal("0.010"), gt=0)
    min_grid_width_pct: Decimal = Field(default=Decimal("0.004"), gt=0)
    max_grid_width_pct: Decimal = Field(default=Decimal("0.030"), gt=0)

    min_volatility_width_multiplier: Decimal = Field(default=Decimal("0.75"), gt=0)
    max_volatility_width_multiplier: Decimal = Field(default=Decimal("2.0"), gt=0)

    normal_width_multiplier: Decimal = Field(default=Decimal("1.0"), gt=0)
    defensive_width_multiplier: Decimal = Field(default=Decimal("1.5"), gt=0)
    long_bias_width_multiplier: Decimal = Field(default=Decimal("1.0"), gt=0)
    short_bias_width_multiplier: Decimal = Field(default=Decimal("1.0"), gt=0)

    configured_min_inner_distance_bps: Decimal = Field(default=Decimal("5"), ge=0)
    maker_safety_buffer_bps: Decimal = Field(default=Decimal("2"), ge=0)
    defensive_inner_distance_multiplier: Decimal = Field(default=Decimal("1.5"), gt=0)

    normal_levels_per_side: int = Field(default=5, ge=1, le=100)
    defensive_levels_per_side: int = Field(default=3, ge=1, le=100)
    bias_levels_per_side: int = Field(default=5, ge=1, le=100)

    base_total_quote_amount: Decimal = Field(default=Decimal("1000"), ge=0)

    normal_size_multiplier: Decimal = Field(default=Decimal("1.0"), ge=0)
    defensive_size_multiplier: Decimal = Field(default=Decimal("0.5"), ge=0)
    long_bias_size_multiplier: Decimal = Field(default=Decimal("1.0"), ge=0)
    short_bias_size_multiplier: Decimal = Field(default=Decimal("1.0"), ge=0)

    max_direction_center_shift_bps: Decimal = Field(default=Decimal("20"), ge=0)
    max_inventory_center_shift_bps: Decimal = Field(default=Decimal("30"), ge=0)
    max_total_center_shift_bps: Decimal = Field(default=Decimal("40"), ge=0)

    directional_allocation_bias: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    inventory_allocation_strength: Decimal = Field(default=Decimal("0.50"), ge=0, le=1)
    max_allocation_bias: Decimal = Field(default=Decimal("0.50"), ge=0, le=1)
    min_side_allocation_pct: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    max_side_allocation_pct: Decimal = Field(default=Decimal("0.90"), ge=0, le=1)

    plan_center_change_threshold_bps: Decimal = Field(default=Decimal("2"), ge=0)
    plan_width_change_threshold_pct: Decimal = Field(default=Decimal("0.001"), ge=0)
    plan_allocation_change_threshold_pct: Decimal = Field(default=Decimal("0.05"), ge=0)

    @model_validator(mode="after")
    def validate_ordered_ranges(self) -> GridParameterConfig:
        if not self.min_grid_width_pct <= self.base_grid_width_pct <= self.max_grid_width_pct:
            raise ValueError(
                "min_grid_width_pct <= base_grid_width_pct <= max_grid_width_pct is required"
            )
        if not (
            self.min_volatility_width_multiplier
            <= Decimal("1")
            <= self.max_volatility_width_multiplier
        ):
            raise ValueError(
                "volatility width multipliers must include the baseline multiplier 1.0"
            )
        if self.max_total_center_shift_bps < self.max_direction_center_shift_bps:
            raise ValueError(
                "max_total_center_shift_bps must cover max_direction_center_shift_bps"
            )
        if self.max_total_center_shift_bps < self.max_inventory_center_shift_bps:
            raise ValueError(
                "max_total_center_shift_bps must cover max_inventory_center_shift_bps"
            )
        if not (
            self.min_side_allocation_pct
            <= Decimal("0.5")
            <= self.max_side_allocation_pct
        ):
            raise ValueError("side allocation limits must contain 50%")
        if self.directional_allocation_bias > self.max_allocation_bias:
            raise ValueError("directional_allocation_bias must not exceed max_allocation_bias")
        return self


class GridLevelPlan(BaseModel):
    """One theoretical level and its equal-notional allocation."""

    side: GridLevelSide
    level_index: int = Field(ge=0)
    theoretical_price: Decimal = Field(gt=0)
    distance_from_center_bps: Decimal = Field(gt=0)
    quote_amount: Decimal = Field(ge=0)
    allocation_weight: Decimal = Field(ge=0)


class GridPlan(BaseModel):
    """Explainable, execution-independent theoretical grid plan."""

    timestamp: str
    trading_pair: str
    market_environment: str = "testnet"
    mode: GridMode
    enabled: bool

    reference_price: Decimal | None = None
    center_price: Decimal | None = None
    center_shift_bps: Decimal = _ZERO

    total_grid_width_pct: Decimal = _ZERO
    half_grid_width_pct: Decimal = _ZERO
    inner_distance_bps: Decimal = _ZERO

    buy_levels_count: int = Field(default=0, ge=0)
    sell_levels_count: int = Field(default=0, ge=0)

    total_quote_amount: Decimal = Field(default=_ZERO, ge=0)
    effective_quote_amount: Decimal = Field(default=_ZERO, ge=0)
    buy_allocation_pct: Decimal = Field(default=_ZERO, ge=0, le=1)
    sell_allocation_pct: Decimal = Field(default=_ZERO, ge=0, le=1)

    volatility_width_multiplier: Decimal = _ZERO
    mode_width_multiplier: Decimal = _ZERO
    mode_size_multiplier: Decimal = _ZERO

    inventory_adjustment: Decimal = _ZERO
    directional_adjustment: Decimal = _ZERO

    buy_levels: list[GridLevelPlan] = Field(default_factory=list)
    sell_levels: list[GridLevelPlan] = Field(default_factory=list)

    valid: bool
    confidence: float = Field(default=0.0, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)

    plan_change_significant: bool = False
    plan_version: int = Field(default=0, ge=0)

    # Stage 13 strategy-regime pause observability.  These fields do not
    # change Stage 4 prices, allocations, widths, or level counts; they let
    # the execution boundary distinguish a pending soft pause from a
    # confirmed safety pause.
    pause_candidate_active: bool = False
    pause_candidate_reason: str | None = None
    pause_candidate_category: str | None = None
    pause_candidate_age_seconds: float | None = None
    pause_confirmation_seconds: float = 0.0
    pause_confirmed: bool = False
    recovery_candidate: GridMode | None = None
    recovery_candidate_age_seconds: float | None = None
    recovery_confirmation_seconds: float = 0.0

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-friendly record without changing internal Decimal use."""

        return _json_value(self.model_dump(mode="python"))


@dataclass(frozen=True)
class ModeProfile:
    """Mode-specific controls used by the pure parameter engine."""

    width_multiplier: Decimal
    size_multiplier: Decimal
    levels_per_side: int
    allocation_bias: Decimal
    enabled: bool


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _read(value: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(field_name, default)
    return getattr(value, field_name, default)


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(upper, value))


def _iso_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mode(value: Any) -> GridMode:
    return value if isinstance(value, GridMode) else GridMode(str(value))


def _safe_confidence(*values: Any) -> float:
    parsed: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            parsed.append(max(0.0, min(1.0, number)))
    return min(parsed) if parsed else 0.0


def _signed(value: Decimal | None, places: str = ".4g") -> str:
    return "unavailable" if value is None else format(value, f"+{places}")


def _dedupe(reasons: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(reason) for reason in reasons if str(reason)))


def _reference_with_source(snapshot: Any) -> tuple[Decimal | None, str]:
    explicit = _decimal(_read(snapshot, "microprice"))
    if explicit is not None and explicit > 0:
        return explicit, "microprice"

    best_bid = _decimal(_read(snapshot, "best_bid"))
    best_ask = _decimal(_read(snapshot, "best_ask"))
    bid_size = _decimal(_read(snapshot, "best_bid_size"))
    ask_size = _decimal(_read(snapshot, "best_ask_size"))
    if (
        best_bid is not None
        and best_ask is not None
        and best_bid > 0
        and best_ask > best_bid
        and bid_size is not None
        and ask_size is not None
        and bid_size > 0
        and ask_size > 0
    ):
        total_size = bid_size + ask_size
        microprice = (best_ask * bid_size + best_bid * ask_size) / total_size
        if microprice > 0:
            return microprice, "derived microprice"

    mid_price = _decimal(_read(snapshot, "mid_price"))
    if mid_price is not None and mid_price > 0:
        return mid_price, "mid price"
    return None, "unavailable"


def calculate_base_reference(snapshot: Any) -> Decimal | None:
    """Prefer an explicit/derived microprice, then fall back to mid price."""

    return _reference_with_source(snapshot)[0]


def _calculate_spread_bps(snapshot: Any) -> Decimal | None:
    spread = _decimal(_read(snapshot, "spread_bps"))
    if spread is not None and spread >= 0:
        return spread

    best_bid = _decimal(_read(snapshot, "best_bid"))
    best_ask = _decimal(_read(snapshot, "best_ask"))
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask < best_bid:
        return None
    mid = (best_bid + best_ask) / Decimal(2)
    if mid <= 0:
        return None
    return (best_ask - best_bid) / mid * _TEN_THOUSAND


def calculate_direction_center_shift(
    direction_score: Decimal | float | None,
    mode: GridMode | str,
    config: GridParameterConfig | None = None,
) -> Decimal:
    """Apply a modest continuous directional shift only in bias modes."""

    cfg = config or GridParameterConfig()
    selected_mode = _mode(mode)
    if selected_mode not in (GridMode.LONG_BIAS, GridMode.SHORT_BIAS):
        return _ZERO
    score = _decimal(direction_score)
    if score is None:
        raise ValueError("direction score unavailable for a bias mode")
    return _clamp(score, Decimal("-1"), Decimal("1")) * cfg.max_direction_center_shift_bps


def calculate_inventory_center_shift(
    inventory_ratio: Decimal | float | None,
    config: GridParameterConfig | None = None,
) -> Decimal:
    """Shift the center against existing signed inventory exposure."""

    cfg = config or GridParameterConfig()
    ratio = _decimal(inventory_ratio)
    if ratio is None:
        raise ValueError("inventory ratio unavailable")
    ratio = _clamp(ratio, Decimal("-1"), Decimal("1"))
    return -ratio * cfg.max_inventory_center_shift_bps


def calculate_final_center(
    base_reference_price: Decimal,
    directional_shift_bps: Decimal,
    inventory_shift_bps: Decimal,
    config: GridParameterConfig | None = None,
) -> tuple[Decimal, Decimal]:
    """Return ``(center_price, clamped_total_shift_bps)``."""

    cfg = config or GridParameterConfig()
    if base_reference_price <= 0:
        raise ValueError("base reference price must be positive")
    total_shift = _clamp(
        directional_shift_bps + inventory_shift_bps,
        -cfg.max_total_center_shift_bps,
        cfg.max_total_center_shift_bps,
    )
    center = base_reference_price * (_ONE + total_shift / _TEN_THOUSAND)
    if center <= 0:
        raise ValueError("center price must be positive")
    return center, total_shift


def calculate_volatility_width_multiplier(
    volatility_score: Decimal | float | None,
    config: GridParameterConfig | None = None,
) -> Decimal:
    """Clamp the continuous Stage 2 volatility score into a width multiplier."""

    cfg = config or GridParameterConfig()
    score = _decimal(volatility_score)
    if score is None or score < 0:
        raise ValueError("volatility score must be finite and non-negative")
    return _clamp(
        score,
        cfg.min_volatility_width_multiplier,
        cfg.max_volatility_width_multiplier,
    )


def calculate_mode_profile(
    mode: GridMode | str,
    config: GridParameterConfig | None = None,
) -> ModeProfile:
    """Return the compact mode-specific width, size, levels, and bias profile."""

    cfg = config or GridParameterConfig()
    selected_mode = _mode(mode)
    if selected_mode is GridMode.NORMAL:
        return ModeProfile(
            cfg.normal_width_multiplier,
            cfg.normal_size_multiplier,
            cfg.normal_levels_per_side,
            _ZERO,
            True,
        )
    if selected_mode is GridMode.DEFENSIVE:
        return ModeProfile(
            cfg.defensive_width_multiplier,
            cfg.defensive_size_multiplier,
            cfg.defensive_levels_per_side,
            _ZERO,
            True,
        )
    if selected_mode is GridMode.LONG_BIAS:
        return ModeProfile(
            cfg.long_bias_width_multiplier,
            cfg.long_bias_size_multiplier,
            cfg.bias_levels_per_side,
            cfg.directional_allocation_bias,
            True,
        )
    if selected_mode is GridMode.SHORT_BIAS:
        return ModeProfile(
            cfg.short_bias_width_multiplier,
            cfg.short_bias_size_multiplier,
            cfg.bias_levels_per_side,
            -cfg.directional_allocation_bias,
            True,
        )
    if selected_mode is GridMode.PAUSE:
        return ModeProfile(_ZERO, _ZERO, 0, _ZERO, False)
    raise ValueError(f"unsupported grid mode: {selected_mode}")


def calculate_grid_width(
    volatility_score: Decimal | float | None,
    mode: GridMode | str,
    config: GridParameterConfig | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(total_width_pct, volatility_multiplier, mode_multiplier)``."""

    cfg = config or GridParameterConfig()
    profile = calculate_mode_profile(mode, cfg)
    volatility_multiplier = calculate_volatility_width_multiplier(volatility_score, cfg)
    raw_width = (
        cfg.base_grid_width_pct * volatility_multiplier * profile.width_multiplier
    )
    width = _clamp(raw_width, cfg.min_grid_width_pct, cfg.max_grid_width_pct)
    return width, volatility_multiplier, profile.width_multiplier


def calculate_inner_distance(
    spread_bps: Decimal | float | None,
    mode: GridMode | str,
    config: GridParameterConfig | None = None,
) -> Decimal:
    """Return the minimum center-to-first-level distance in basis points."""

    cfg = config or GridParameterConfig()
    selected_mode = _mode(mode)
    if selected_mode is GridMode.PAUSE:
        return _ZERO
    spread = _decimal(spread_bps)
    if spread is None or spread < 0:
        raise ValueError("spread bps must be finite and non-negative")
    distance = max(
        cfg.configured_min_inner_distance_bps,
        spread / Decimal(2) + cfg.maker_safety_buffer_bps,
    )
    if selected_mode is GridMode.DEFENSIVE:
        distance *= cfg.defensive_inner_distance_multiplier
    return distance


def calculate_allocation_bias(
    mode: GridMode | str,
    inventory_ratio: Decimal | float | None,
    config: GridParameterConfig | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(net_bias, buy_pct, sell_pct)`` with inventory risk overlay."""

    cfg = config or GridParameterConfig()
    profile = calculate_mode_profile(mode, cfg)
    if not profile.enabled:
        return _ZERO, _ZERO, _ZERO
    ratio = _decimal(inventory_ratio)
    if ratio is None:
        raise ValueError("inventory ratio unavailable")
    net_bias = _clamp(
        profile.allocation_bias
        - _clamp(ratio, Decimal("-1"), Decimal("1")) * cfg.inventory_allocation_strength,
        -cfg.max_allocation_bias,
        cfg.max_allocation_bias,
    )
    buy_pct = _clamp(
        Decimal("0.5") + net_bias / Decimal(2),
        cfg.min_side_allocation_pct,
        cfg.max_side_allocation_pct,
    )
    sell_pct = _ONE - buy_pct
    if sell_pct < cfg.min_side_allocation_pct:
        sell_pct = cfg.min_side_allocation_pct
        buy_pct = _ONE - sell_pct
    elif sell_pct > cfg.max_side_allocation_pct:
        sell_pct = cfg.max_side_allocation_pct
        buy_pct = _ONE - sell_pct
    return net_bias, buy_pct, sell_pct


def generate_geometric_distances(
    inner_distance_pct: Decimal,
    outer_distance_pct: Decimal,
    level_count: int,
) -> list[Decimal]:
    """Generate ascending percentage distances from inner to outer boundary.

    For one level, the level is placed at the outer boundary because that is
    the only possible level that can satisfy both the inner and outer-boundary
    requirements.  For multiple levels, the ratio is
    ``(outer / inner) ** (1 / (N - 1))`` and every distance is multiplied by
    that ratio.
    """

    if level_count < 1:
        raise ValueError("level_count must be positive")
    if inner_distance_pct <= 0 or outer_distance_pct <= inner_distance_pct:
        raise ValueError("outer distance must be greater than inner distance")
    if level_count == 1:
        return [outer_distance_pct]
    ratio = (outer_distance_pct / inner_distance_pct) ** (
        Decimal(1) / Decimal(level_count - 1)
    )
    return [inner_distance_pct * (ratio**index) for index in range(level_count)]


def allocate_quote_per_level(
    effective_quote_amount: Decimal,
    side_allocation_pct: Decimal,
    level_count: int,
) -> Decimal:
    """Allocate equal theoretical quote notional to each level on one side."""

    if effective_quote_amount < 0 or side_allocation_pct < 0:
        raise ValueError("quote amount and allocation must be non-negative")
    if level_count < 1:
        raise ValueError("level_count must be positive")
    return effective_quote_amount * side_allocation_pct / Decimal(level_count)


def generate_buy_levels(
    center_price: Decimal,
    distances: Sequence[Decimal],
    quote_amount_per_level: Decimal,
) -> list[GridLevelPlan]:
    """Generate inner-to-outer buy levels below the center."""

    return [
        GridLevelPlan(
            side=GridLevelSide.BUY,
            level_index=index,
            theoretical_price=center_price * (_ONE - distance),
            distance_from_center_bps=distance * _TEN_THOUSAND,
            quote_amount=quote_amount_per_level,
            allocation_weight=Decimal(1) / Decimal(len(distances)),
        )
        for index, distance in enumerate(distances)
    ]


def generate_sell_levels(
    center_price: Decimal,
    distances: Sequence[Decimal],
    quote_amount_per_level: Decimal,
) -> list[GridLevelPlan]:
    """Generate inner-to-outer sell levels above the center."""

    return [
        GridLevelPlan(
            side=GridLevelSide.SELL,
            level_index=index,
            theoretical_price=center_price * (_ONE + distance),
            distance_from_center_bps=distance * _TEN_THOUSAND,
            quote_amount=quote_amount_per_level,
            allocation_weight=Decimal(1) / Decimal(len(distances)),
        )
        for index, distance in enumerate(distances)
    ]


def _pairwise(values: Sequence[Decimal]) -> Sequence[tuple[Decimal, Decimal]]:
    return list(zip(values, values[1:], strict=False))


def validate_grid_plan(plan: GridPlan) -> tuple[str, ...]:
    """Validate level ordering, allocations, and boundary invariants."""

    errors: list[str] = []
    if not plan.enabled:
        if plan.buy_levels or plan.sell_levels:
            errors.append("disabled plan must not contain theoretical levels")
        if plan.effective_quote_amount != 0:
            errors.append("disabled plan must have zero effective quote amount")
        if plan.mode is not GridMode.PAUSE and plan.valid:
            errors.append("a disabled valid plan must be PAUSE")
        return tuple(_dedupe(errors))

    if plan.reference_price is None or plan.reference_price <= 0:
        errors.append("reference price must be positive")
    if plan.center_price is None or plan.center_price <= 0:
        errors.append("center price must be positive")
    if plan.total_grid_width_pct <= 0:
        errors.append("grid width must be positive")
    if plan.half_grid_width_pct <= plan.inner_distance_bps / _TEN_THOUSAND:
        errors.append("grid width must be larger than inner distance")
    if len(plan.buy_levels) != plan.buy_levels_count:
        errors.append("buy level count does not match generated levels")
    if len(plan.sell_levels) != plan.sell_levels_count:
        errors.append("sell level count does not match generated levels")
    if plan.buy_allocation_pct + plan.sell_allocation_pct != 1:
        if abs(plan.buy_allocation_pct + plan.sell_allocation_pct - _ONE) > _TOLERANCE:
            errors.append("buy and sell allocations must sum to one")

    buy_prices = [level.theoretical_price for level in plan.buy_levels]
    sell_prices = [level.theoretical_price for level in plan.sell_levels]
    all_prices = buy_prices + sell_prices
    if len(all_prices) != len(set(all_prices)):
        errors.append("theoretical levels must not duplicate prices")
    if plan.center_price is not None:
        if any(price >= plan.center_price for price in buy_prices):
            errors.append("all buy prices must be below center")
        if any(price <= plan.center_price for price in sell_prices):
            errors.append("all sell prices must be above center")
    if any(left <= right for left, right in _pairwise(buy_prices)):
        errors.append("buy prices must decrease from inner to outer")
    if any(left >= right for left, right in _pairwise(sell_prices)):
        errors.append("sell prices must increase from inner to outer")
    if any(level.quote_amount < 0 for level in plan.buy_levels + plan.sell_levels):
        errors.append("quote amounts must be non-negative")
    if any(level.distance_from_center_bps <= 0 for level in plan.buy_levels + plan.sell_levels):
        errors.append("level distances must be positive")
    if plan.center_price is not None and plan.half_grid_width_pct > 0:
        lower = plan.center_price * (_ONE - plan.half_grid_width_pct)
        upper = plan.center_price * (_ONE + plan.half_grid_width_pct)
        if any(price < lower for price in buy_prices):
            errors.append("buy level exceeds outer grid boundary")
        if any(price > upper for price in sell_prices):
            errors.append("sell level exceeds outer grid boundary")
    total_levels_quote = sum(
        (level.quote_amount for level in plan.buy_levels + plan.sell_levels),
        start=_ZERO,
    )
    if abs(total_levels_quote - plan.effective_quote_amount) > _TOLERANCE:
        errors.append("level quote amounts do not equal effective quote amount")
    return tuple(_dedupe(errors))


def plan_change_significant(
    plan: GridPlan,
    previous_plan: GridPlan | None,
    config: GridParameterConfig | None = None,
) -> bool:
    """Compare the material plan dimensions used by Stage 5 later."""

    if previous_plan is None:
        return True
    cfg = config or GridParameterConfig()
    if plan.mode is not previous_plan.mode:
        return True
    if plan.enabled != previous_plan.enabled or plan.valid != previous_plan.valid:
        return True
    if plan.buy_levels_count != previous_plan.buy_levels_count:
        return True
    if plan.sell_levels_count != previous_plan.sell_levels_count:
        return True
    if plan.center_price is None or previous_plan.center_price is None:
        return plan.center_price != previous_plan.center_price
    center_change_bps = abs(plan.center_price / previous_plan.center_price - _ONE) * _TEN_THOUSAND
    if center_change_bps > cfg.plan_center_change_threshold_bps:
        return True
    if (
        abs(plan.total_grid_width_pct - previous_plan.total_grid_width_pct)
        > cfg.plan_width_change_threshold_pct
    ):
        return True
    if (
        abs(plan.buy_allocation_pct - previous_plan.buy_allocation_pct)
        > cfg.plan_allocation_change_threshold_pct
    ):
        return True
    return False


def _with_plan_metadata(
    plan: GridPlan,
    previous_plan: GridPlan | None,
    current_plan_version: int,
    config: GridParameterConfig,
) -> GridPlan:
    significant = plan_change_significant(plan, previous_plan, config)
    base_version = current_plan_version
    if base_version <= 0 and previous_plan is not None:
        base_version = previous_plan.plan_version
    version = base_version + 1 if significant else base_version
    return plan.model_copy(
        update={
            "plan_change_significant": significant,
            "plan_version": version,
        }
    )


def _invalid_plan(
    *,
    timestamp: str,
    trading_pair: str,
    mode: GridMode,
    confidence: float,
    config: GridParameterConfig,
    reasons: Sequence[str],
    reference_price: Decimal | None = None,
    center_price: Decimal | None = None,
    center_shift_bps: Decimal = _ZERO,
    total_grid_width_pct: Decimal = _ZERO,
    half_grid_width_pct: Decimal = _ZERO,
    inner_distance_bps: Decimal = _ZERO,
    volatility_width_multiplier: Decimal = _ZERO,
    mode_width_multiplier: Decimal = _ZERO,
    mode_size_multiplier: Decimal = _ZERO,
    inventory_adjustment: Decimal = _ZERO,
    directional_adjustment: Decimal = _ZERO,
    buy_levels_count: int = 0,
    sell_levels_count: int = 0,
    buy_allocation_pct: Decimal = _ZERO,
    sell_allocation_pct: Decimal = _ZERO,
) -> GridPlan:
    return GridPlan(
        timestamp=timestamp,
        trading_pair=trading_pair,
        mode=mode,
        enabled=False,
        reference_price=reference_price,
        center_price=center_price,
        center_shift_bps=center_shift_bps,
        total_grid_width_pct=total_grid_width_pct,
        half_grid_width_pct=half_grid_width_pct,
        inner_distance_bps=inner_distance_bps,
        buy_levels_count=buy_levels_count,
        sell_levels_count=sell_levels_count,
        total_quote_amount=config.base_total_quote_amount,
        effective_quote_amount=_ZERO,
        buy_allocation_pct=buy_allocation_pct,
        sell_allocation_pct=sell_allocation_pct,
        volatility_width_multiplier=volatility_width_multiplier,
        mode_width_multiplier=mode_width_multiplier,
        mode_size_multiplier=mode_size_multiplier,
        inventory_adjustment=inventory_adjustment,
        directional_adjustment=directional_adjustment,
        valid=False,
        confidence=confidence,
        reasons=_dedupe(reasons),
    )


def _coerce_inputs(
    state: MarketState | Mapping[str, Any],
    decision: GridModeDecision | Mapping[str, Any],
) -> tuple[MarketState | None, GridModeDecision | None, list[str]]:
    reasons: list[str] = []
    try:
        market_state = (
            state if isinstance(state, MarketState) else MarketState.model_validate(state)
        )
    except (TypeError, ValueError):
        market_state = None
        reasons.append("MarketState could not be validated")
    try:
        mode_decision = (
            decision
            if isinstance(decision, GridModeDecision)
            else GridModeDecision.model_validate(decision)
        )
    except (TypeError, ValueError):
        mode_decision = None
        reasons.append("GridModeDecision could not be validated")
    return market_state, mode_decision, reasons


def build_grid_plan(
    snapshot: Any,
    state: MarketState | Mapping[str, Any],
    decision: GridModeDecision | Mapping[str, Any],
    config: GridParameterConfig | None = None,
    *,
    previous_plan: GridPlan | None = None,
    current_plan_version: int = 0,
) -> GridPlan:
    """Build one deterministic theoretical plan, failing closed on bad inputs."""

    cfg = config or GridParameterConfig()
    market_state, mode_decision, coercion_reasons = _coerce_inputs(state, decision)
    raw_mode = _read(decision, "mode", GridMode.PAUSE)
    try:
        selected_mode = _mode(raw_mode)
    except ValueError:
        selected_mode = GridMode.PAUSE
        coercion_reasons.append("grid mode is invalid")
    timestamp = str(
        _read(decision, "timestamp", None)
        or _read(state, "timestamp", None)
        or _read(snapshot, "timestamp", "")
    )
    trading_pair = str(
        _read(decision, "trading_pair", None)
        or _read(state, "trading_pair", None)
        or _read(snapshot, "trading_pair", "unknown")
    )
    confidence = _safe_confidence(
        _read(mode_decision, "confidence") if mode_decision else None,
        _read(market_state, "confidence") if market_state else None,
    )

    if coercion_reasons or market_state is None or mode_decision is None:
        plan = _invalid_plan(
            timestamp=timestamp,
            trading_pair=trading_pair,
            mode=selected_mode,
            confidence=confidence,
            config=cfg,
            reasons=coercion_reasons,
        )
        return _with_plan_metadata(plan, previous_plan, current_plan_version, cfg)

    if not market_state.state_valid:
        plan = _invalid_plan(
            timestamp=timestamp,
            trading_pair=trading_pair,
            mode=selected_mode,
            confidence=confidence,
            config=cfg,
            reasons=["MarketState invalid", *market_state.reasons],
        )
        return _with_plan_metadata(plan, previous_plan, current_plan_version, cfg)
    if not mode_decision.valid:
        plan = _invalid_plan(
            timestamp=timestamp,
            trading_pair=trading_pair,
            mode=selected_mode,
            confidence=confidence,
            config=cfg,
            reasons=["GridModeDecision invalid", *mode_decision.reasons],
        )
        return _with_plan_metadata(plan, previous_plan, current_plan_version, cfg)
    if selected_mode is GridMode.PAUSE:
        plan = _invalid_plan(
            timestamp=timestamp,
            trading_pair=trading_pair,
            mode=selected_mode,
            confidence=confidence,
            config=cfg,
            reasons=["mode PAUSE disables theoretical grid", *mode_decision.reasons],
        )
        return _with_plan_metadata(plan, previous_plan, current_plan_version, cfg)

    snapshot_valid = _read(snapshot, "data_valid", None)
    if snapshot_valid is not True:
        plan = _invalid_plan(
            timestamp=timestamp,
            trading_pair=trading_pair,
            mode=selected_mode,
            confidence=confidence,
            config=cfg,
            reasons=["MarketSnapshot invalid or data_valid is not true"],
        )
        return _with_plan_metadata(plan, previous_plan, current_plan_version, cfg)
    snapshot_pair = str(_read(snapshot, "trading_pair", trading_pair))
    if snapshot_pair != trading_pair or market_state.trading_pair != trading_pair:
        plan = _invalid_plan(
            timestamp=timestamp,
            trading_pair=trading_pair,
            mode=selected_mode,
            confidence=confidence,
            config=cfg,
            reasons=["trading pair mismatch across Stage 1--3 inputs"],
        )
        return _with_plan_metadata(plan, previous_plan, current_plan_version, cfg)

    try:
        base_reference, reference_source = _reference_with_source(snapshot)
        if base_reference is None:
            raise ValueError("reference price unavailable")
        spread_bps = _calculate_spread_bps(snapshot)
        if spread_bps is None:
            raise ValueError("spread bps unavailable")
        direction_shift = calculate_direction_center_shift(
            mode_decision.direction_score,
            selected_mode,
            cfg,
        )
        inventory_shift = calculate_inventory_center_shift(
            mode_decision.inventory_ratio,
            cfg,
        )
        center_price, total_shift = calculate_final_center(
            base_reference,
            direction_shift,
            inventory_shift,
            cfg,
        )
        width, volatility_multiplier, mode_width_multiplier = calculate_grid_width(
            market_state.volatility_score,
            selected_mode,
            cfg,
        )
        inner_distance_bps = calculate_inner_distance(spread_bps, selected_mode, cfg)
        profile = calculate_mode_profile(selected_mode, cfg)
        half_width = width / Decimal(2)
        inner_distance_pct = inner_distance_bps / _TEN_THOUSAND
        if half_width <= inner_distance_pct:
            raise ValueError("grid width is smaller than inner distance")
        distances = generate_geometric_distances(
            inner_distance_pct,
            half_width,
            profile.levels_per_side,
        )
        net_bias, buy_allocation, sell_allocation = calculate_allocation_bias(
            selected_mode,
            mode_decision.inventory_ratio,
            cfg,
        )
        effective_quote = cfg.base_total_quote_amount * profile.size_multiplier
        buy_per_level = allocate_quote_per_level(
            effective_quote,
            buy_allocation,
            profile.levels_per_side,
        )
        sell_per_level = allocate_quote_per_level(
            effective_quote,
            sell_allocation,
            profile.levels_per_side,
        )
        buy_levels = generate_buy_levels(center_price, distances, buy_per_level)
        sell_levels = generate_sell_levels(center_price, distances, sell_per_level)
        reasons = [
            f"base reference {base_reference} from {reference_source}",
            f"direction score {_signed(_decimal(mode_decision.direction_score))} produced "
            f"{_signed(direction_shift, '.4f')} bps center adjustment",
            f"inventory ratio {_signed(_decimal(mode_decision.inventory_ratio))} produced "
            f"{_signed(inventory_shift, '.4f')} bps inventory adjustment",
            f"volatility score {_decimal(market_state.volatility_score)} produced "
            f"{volatility_multiplier}x width",
            f"{selected_mode.name.lower()} profile allocated {buy_allocation:.2%} buy / "
            f"{sell_allocation:.2%} sell (net bias {net_bias:+.4f})",
            f"{profile.levels_per_side} geometric levels generated per side",
        ]
        candidate = GridPlan(
            timestamp=timestamp or _iso_utc(),
            trading_pair=trading_pair,
            mode=selected_mode,
            enabled=True,
            reference_price=base_reference,
            center_price=center_price,
            center_shift_bps=total_shift,
            total_grid_width_pct=width,
            half_grid_width_pct=half_width,
            inner_distance_bps=inner_distance_bps,
            buy_levels_count=profile.levels_per_side,
            sell_levels_count=profile.levels_per_side,
            total_quote_amount=cfg.base_total_quote_amount,
            effective_quote_amount=effective_quote,
            buy_allocation_pct=buy_allocation,
            sell_allocation_pct=sell_allocation,
            volatility_width_multiplier=volatility_multiplier,
            mode_width_multiplier=mode_width_multiplier,
            mode_size_multiplier=profile.size_multiplier,
            inventory_adjustment=inventory_shift,
            directional_adjustment=direction_shift,
            buy_levels=buy_levels,
            sell_levels=sell_levels,
            valid=True,
            confidence=confidence,
            reasons=reasons,
            pause_candidate_active=bool(
                _read(mode_decision, "pause_candidate_active", False)
            ),
            pause_candidate_reason=_read(mode_decision, "pause_candidate_reason"),
            pause_candidate_category=_read(mode_decision, "pause_candidate_category"),
            pause_candidate_age_seconds=_read(
                mode_decision, "pause_candidate_age_seconds"
            ),
            pause_confirmation_seconds=float(
                _read(mode_decision, "pause_confirmation_seconds", 0.0) or 0.0
            ),
            pause_confirmed=bool(_read(mode_decision, "pause_confirmed", False)),
            recovery_candidate=_read(mode_decision, "recovery_candidate"),
            recovery_candidate_age_seconds=_read(
                mode_decision, "recovery_candidate_age_seconds"
            ),
            recovery_confirmation_seconds=float(
                _read(mode_decision, "recovery_confirmation_seconds", 0.0) or 0.0
            ),
        )
        validation_errors = validate_grid_plan(candidate)
        if validation_errors:
            raise ValueError("; ".join(validation_errors))
        plan = candidate
    except (ValueError, ArithmeticError) as exc:
        plan = _invalid_plan(
            timestamp=timestamp,
            trading_pair=trading_pair,
            mode=selected_mode,
            confidence=confidence,
            config=cfg,
            reasons=[f"grid plan failed closed: {exc}"],
        )
    return _with_plan_metadata(plan, previous_plan, current_plan_version, cfg)


class GridParameterEngine:
    """Stateful plan/version wrapper around the pure Stage 4 calculations."""

    def __init__(self, config: GridParameterConfig | None = None) -> None:
        self.config = config or GridParameterConfig()
        self._previous_plan: GridPlan | None = None
        self._plan_version = 0

    @property
    def previous_plan(self) -> GridPlan | None:
        return self._previous_plan

    @property
    def plan_version(self) -> int:
        return self._plan_version

    def build(
        self,
        snapshot: Any,
        state: MarketState | Mapping[str, Any],
        decision: GridModeDecision | Mapping[str, Any],
    ) -> GridPlan:
        plan = build_grid_plan(
            snapshot,
            state,
            decision,
            self.config,
            previous_plan=self._previous_plan,
            current_plan_version=self._plan_version,
        )
        self._previous_plan = plan
        self._plan_version = plan.plan_version
        return plan


def format_grid_plan_summary(plan: GridPlan) -> str:
    """Render the Stage 4 plan in a concise human-readable form."""

    def value(item: Decimal | None, fmt: str = ".8g") -> str:
        return "unavailable" if item is None else format(item, fmt)

    lines = [
        "[ADAPTIVE GRID PLAN]",
        f"Pair: {plan.trading_pair}",
        f"Mode: {plan.mode.name}",
        f"Enabled: {str(plan.enabled).lower()}",
        "Plan version: "
        f"{plan.plan_version} (significant={str(plan.plan_change_significant).lower()})",
        "Reference:",
        f"  Base: {value(plan.reference_price)}",
        f"  Direction shift: {format(plan.directional_adjustment, '+.4f')} bps",
        f"  Inventory shift: {format(plan.inventory_adjustment, '+.4f')} bps",
        f"  Center: {value(plan.center_price)}",
        "Volatility:",
        f"  Width multiplier: {value(plan.volatility_width_multiplier, '.6g')}x",
        "Grid:",
        f"  Total width: {format(plan.total_grid_width_pct, '.3%')}",
        f"  Inner distance: {format(plan.inner_distance_bps, '.4f')} bps",
        f"  Buy levels: {plan.buy_levels_count}",
        f"  Sell levels: {plan.sell_levels_count}",
        "Capital:",
        f"  Total: {value(plan.total_quote_amount, '.8g')}",
        f"  Effective: {value(plan.effective_quote_amount, '.8g')}",
        f"  Buy allocation: {format(plan.buy_allocation_pct, '.2%')}",
        f"  Sell allocation: {format(plan.sell_allocation_pct, '.2%')}",
        "BUY",
    ]
    lines.extend(
        f"L{level.level_index}  price={value(level.theoretical_price)} "
        f"quote={value(level.quote_amount)}"
        for level in plan.buy_levels
    )
    lines.append("SELL")
    lines.extend(
        f"L{level.level_index}  price={value(level.theoretical_price)} "
        f"quote={value(level.quote_amount)}"
        for level in plan.sell_levels
    )
    lines.extend(
        [
            f"Valid: {str(plan.valid).lower()}",
            "Reasons:",
            *[f"  {reason}" for reason in plan.reasons],
        ]
    )
    return "\n".join(lines)


__all__ = [
    "GridLevelPlan",
    "GridLevelSide",
    "GridParameterConfig",
    "GridParameterEngine",
    "GridPlan",
    "ModeProfile",
    "allocate_quote_per_level",
    "build_grid_plan",
    "calculate_allocation_bias",
    "calculate_base_reference",
    "calculate_direction_center_shift",
    "calculate_final_center",
    "calculate_grid_width",
    "calculate_inner_distance",
    "calculate_inventory_center_shift",
    "calculate_mode_profile",
    "calculate_volatility_width_multiplier",
    "format_grid_plan_summary",
    "generate_buy_levels",
    "generate_geometric_distances",
    "generate_sell_levels",
    "plan_change_significant",
    "validate_grid_plan",
]
