"""Closed-loop, point-in-time-safe multi-asset Stage 8 replay."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from derive_options_mm.competition_risk import (
    CompetitionProfile,
    CompetitionRiskDecision,
    CompetitionRiskGovernor,
)
from derive_options_mm.multi_asset import (
    MultiAssetConfig,
    MultiAssetCoordinator,
    MultiAssetCycle,
    PortfolioRiskSettings,
    pair_level_id,
)
from derive_options_mm.state_engine import parse_timestamp


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _read(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


@dataclass(frozen=True)
class MultiAssetReplayConfig:
    """Explicit replay assumptions; no values are tuned against PnL."""

    order_scale: float = 0.10
    max_levels_per_side: int = 1
    maker_fee_bps: float = 0.0
    fill_model: str = "conservative_cross_through"


@dataclass
class MultiAssetReplayResult:
    """Replay records and portfolio metrics for one deterministic run."""

    label: str
    cycles: list[MultiAssetCycle] = field(default_factory=list)
    ticks: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    competition_decisions: list[CompetitionRiskDecision] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ticks": len(self.ticks),
            "events": len(self.events),
            "warnings": list(self.warnings),
            "metrics": self.metrics,
            "competition_decisions": len(self.competition_decisions),
        }


def _snapshot_with_inventory(snapshot: Any, position: float) -> dict[str, Any]:
    if isinstance(snapshot, Mapping):
        result = dict(snapshot)
    else:
        result = snapshot.model_dump(mode="python")
    mid = _finite(result.get("mid_price"))
    result["current_position"] = position
    result["position_notional"] = abs(position) * mid if mid is not None else None
    result["account_data_available"] = True
    return result


def _ordered_ticks(ticks: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    decorated: list[tuple[float, int, Mapping[str, Any]]] = []
    for index, tick in enumerate(ticks):
        timestamps = [parse_timestamp(_read(snapshot, "timestamp")) for snapshot in tick.values()]
        timestamp = max((value for value in timestamps if value is not None), default=None)
        if timestamp is None:
            raise ValueError(f"multi-asset tick {index} has no usable timestamp")
        decorated.append((timestamp, index, tick))
    ordered = sorted(decorated, key=lambda item: (item[0], item[1]))
    if [item[0] for item in ordered] != [item[0] for item in decorated]:
        raise ValueError("multi-asset replay input must already be in clock order")
    return [item[2] for item in ordered]


def _level_details(plan: Any, pair: str, max_levels_per_side: int) -> list[dict[str, Any]]:
    levels: list[dict[str, Any]] = []
    for side, rows in (("buy", plan.buy_levels), ("sell", plan.sell_levels)):
        for level in list(rows)[:max_levels_per_side]:
            quote = float(level.quote_amount) * 1.0
            levels.append(
                {
                    "trading_pair": pair,
                    "level_id": pair_level_id(pair, side, level.level_index),
                    "side": side,
                    "price": float(level.theoretical_price),
                    "quote_notional": quote,
                    "level_index": level.level_index,
                }
            )
    return levels


class MultiAssetReplay:
    """Replay one common timeline without reading any future record."""

    def __init__(
        self,
        strategy_config: MultiAssetConfig | None = None,
        replay_config: MultiAssetReplayConfig | None = None,
        *,
        label: str = "shared_btc_iv_with_portfolio_governor",
        competition_profile: CompetitionProfile | None = None,
    ) -> None:
        self.strategy_config = strategy_config or MultiAssetConfig()
        self.replay_config = replay_config or MultiAssetReplayConfig()
        self.label = label
        self.competition_profile = competition_profile

    def run(self, ticks: Sequence[Mapping[str, Any]]) -> MultiAssetReplayResult:
        ordered_ticks = _ordered_ticks(ticks)
        coordinator = MultiAssetCoordinator(self.strategy_config)
        result = MultiAssetReplayResult(self.label)
        competition_governor = (
            CompetitionRiskGovernor(self.competition_profile)
            if self.competition_profile is not None
            else None
        )
        if competition_governor is not None:
            competition_governor.start_session(self.competition_profile.starting_equity_reference)
        positions: dict[str, float] = {pair: 0.0 for pair in self.strategy_config.enabled_markets}
        cost_basis: dict[str, float] = dict(positions)
        pending: dict[str, list[dict[str, Any]]] = {}
        high_water = float("-inf")
        soft_limit_ticks = 0
        hard_limit_ticks = 0
        risk_block_count = 0
        same_direction_block_count = 0
        max_gross = 0.0
        max_beta = 0.0
        max_long_beta = 0.0
        max_short_beta = 0.0
        max_per_asset: dict[str, float] = {}
        competition_block_count = 0
        competition_non_normal_ticks = 0
        competition_hard_stop_ticks = 0
        max_drawdown = 0.0
        last_timestamp: float | None = None

        for tick_index, raw_tick in enumerate(ordered_ticks):
            snapshots = {
                pair: _snapshot_with_inventory(snapshot, positions.get(pair, 0.0))
                for pair, snapshot in raw_tick.items()
                if pair in self.strategy_config.enabled_markets
            }
            timestamps = [
                parse_timestamp(_read(snapshot, "timestamp")) for snapshot in snapshots.values()
            ]
            timestamp_seconds = max(value for value in timestamps if value is not None)
            if last_timestamp is not None and timestamp_seconds <= last_timestamp:
                raise ValueError("multi-asset replay timestamps must be strictly increasing")
            last_timestamp = timestamp_seconds

            # Fill only orders created on an earlier tick.  This ordering is the
            # explicit look-ahead guard for both local and global inputs.
            for pair, orders in list(pending.items()):
                snapshot = snapshots.get(pair)
                if snapshot is None:
                    continue
                best_bid = _finite(_read(snapshot, "best_bid"))
                best_ask = _finite(_read(snapshot, "best_ask"))
                remaining: list[dict[str, Any]] = []
                for order in orders:
                    if order["created_at"] >= timestamp_seconds:
                        remaining.append(order)
                        continue
                    filled = (
                        order["side"] == "buy"
                        and best_ask is not None
                        and order["price"] >= best_ask
                    ) or (
                        order["side"] == "sell"
                        and best_bid is not None
                        and order["price"] <= best_bid
                    )
                    if not filled:
                        remaining.append(order)
                        continue
                    amount = order["quote_notional"] / order["price"]
                    signed_amount = amount if order["side"] == "buy" else -amount
                    signed_quote = (
                        order["quote_notional"]
                        if order["side"] == "buy"
                        else -order["quote_notional"]
                    )
                    positions[pair] = positions.get(pair, 0.0) + signed_amount
                    cost_basis[pair] = cost_basis.get(pair, 0.0) + signed_quote
                    fee = order["quote_notional"] * self.replay_config.maker_fee_bps / 10_000.0
                    result.events.append(
                        {
                            "timestamp": _read(snapshot, "timestamp"),
                            "event": "ENTRY_FILLED",
                            "trading_pair": pair,
                            "level_id": order["level_id"],
                            "side": order["side"],
                            "price": order["price"],
                            "quote_notional": order["quote_notional"],
                            "fee": fee,
                        }
                    )
                pending[pair] = remaining

            cycle_snapshots = {
                pair: snapshots[pair]
                for pair in self.strategy_config.enabled_markets
                if pair in snapshots
            }
            cycle = coordinator.update(
                cycle_snapshots,
                positions={
                    pair: positions.get(pair, 0.0)
                    * (_finite(_read(snapshots.get(pair), "mid_price")) or 0.0)
                    for pair in positions
                },
                pending_entries={
                    pair: {
                        "buy": sum(
                            order["quote_notional"] for order in orders if order["side"] == "buy"
                        ),
                        "sell": sum(
                            order["quote_notional"] for order in orders if order["side"] == "sell"
                        ),
                        "count": len(orders),
                    }
                    for pair, orders in pending.items()
                },
            )
            result.cycles.append(cycle)
            if cycle.portfolio_risk.soft_limit_triggered:
                soft_limit_ticks += 1
            if cycle.portfolio_risk.hard_limit_triggered:
                hard_limit_ticks += 1
            risk_block_count += sum(
                len(value) for value in cycle.portfolio_risk.blocked_level_ids.values()
            )
            same_direction_block_count += sum(
                len(value) for value in cycle.portfolio_risk.blocked_sides.values()
            )

            competition_routes: dict[str, tuple[str, ...]] | None = None
            competition_decision: CompetitionRiskDecision | None = None
            if competition_governor is not None:
                current_equity = self.competition_profile.starting_equity_reference + sum(
                    positions.get(pair, 0.0)
                    * (_finite(_read(snapshots.get(pair), "mid_price")) or 0.0)
                    - cost_basis.get(pair, 0.0)
                    for pair in positions
                )
                beta_values = {
                    pair: cycle.states[pair].btc_beta
                    if cycle.states[pair].btc_beta is not None
                    else 1.0
                    for pair in cycle.states
                }
                competition_decision, competition_routes = competition_governor.route_plans(
                    cycle.plans,
                    positions={
                        pair: positions.get(pair, 0.0)
                        * (_finite(_read(snapshots.get(pair), "mid_price")) or 0.0)
                        for pair in positions
                    },
                    pending_entries={
                        pair: {
                            "buy": sum(
                                order["quote_notional"]
                                for order in orders
                                if order["side"] == "buy"
                            ),
                            "sell": sum(
                                order["quote_notional"]
                                for order in orders
                                if order["side"] == "sell"
                            ),
                            "count": len(orders),
                        }
                        for pair, orders in pending.items()
                    },
                    betas=beta_values,
                    available_collateral=self.competition_profile.starting_equity_reference,
                    current_equity=current_equity,
                    quote_scale=self.replay_config.order_scale,
                )
                result.competition_decisions.append(competition_decision)
                competition_block_count += sum(
                    len(levels) for levels in competition_decision.blocked_level_ids.values()
                )
                if competition_decision.state.risk_stage.value != "NORMAL":
                    competition_non_normal_ticks += 1
                if competition_decision.state.hard_stop_latched:
                    competition_hard_stop_ticks += 1
                for pair, value in competition_decision.exposure.per_asset_exposure.items():
                    max_per_asset[pair] = max(max_per_asset.get(pair, 0.0), value)
                max_long_beta = max(max_long_beta, competition_decision.exposure.long_beta_exposure)
                max_short_beta = max(
                    max_short_beta, competition_decision.exposure.short_beta_exposure
                )

            pending = {}
            for pair, plan in cycle.plans.items():
                allowed = set(
                    competition_routes.get(pair, ())
                    if competition_routes is not None
                    else cycle.routes[pair].allowed_level_ids
                )
                for level in _level_details(plan, pair, self.replay_config.max_levels_per_side):
                    if not plan.enabled or not plan.valid:
                        continue
                    if level["level_id"] not in allowed:
                        result.events.append(
                            {
                                "timestamp": cycle.timestamp,
                                "event": "ENTRY_BLOCKED",
                                "trading_pair": pair,
                                "level_id": level["level_id"],
                                "side": level["side"],
                                "reason": (
                                    competition_decision.blocked_reasons.get(level["level_id"])
                                    if competition_decision is not None
                                    else "portfolio risk governor"
                                ),
                            }
                        )
                        continue
                    pending.setdefault(pair, []).append(
                        {
                            **level,
                            "price": level["price"] * 1.0,
                            "quote_notional": level["quote_notional"]
                            * self.replay_config.order_scale,
                            "created_at": timestamp_seconds,
                        }
                    )
                    result.events.append(
                        {
                            "timestamp": cycle.timestamp,
                            "event": "ENTRY_CREATED",
                            **pending[pair][-1],
                            "dry_run": True,
                        }
                    )

            gross = sum(
                abs(positions.get(pair, 0.0))
                * (_finite(_read(snapshots.get(pair), "mid_price")) or 0.0)
                for pair in positions
            )
            beta = abs(cycle.portfolio_risk.btc_beta_equivalent_exposure)
            max_gross = max(max_gross, gross)
            max_beta = max(max_beta, beta)
            mark_to_market = sum(
                positions.get(pair, 0.0) * (_finite(_read(snapshots.get(pair), "mid_price")) or 0.0)
                - cost_basis.get(pair, 0.0)
                for pair in positions
            )
            high_water = max(high_water, mark_to_market)
            drawdown = high_water - mark_to_market
            max_drawdown = max(max_drawdown, drawdown)
            result.ticks.append(
                {
                    "tick_index": tick_index,
                    "timestamp": cycle.timestamp,
                    "global_risk_score": cycle.global_risk.global_risk_score,
                    "global_risk_regime": cycle.global_risk.global_risk_regime.value,
                    "plans": {
                        pair: {
                            "trading_pair": pair,
                            "mode": plan.mode.value,
                            "plan_version": plan.plan_version,
                            "enabled": plan.enabled,
                            "valid": plan.valid,
                            "grid_width_pct": float(plan.total_grid_width_pct),
                            "allowed_level_ids": list(
                                competition_routes.get(pair, ())
                                if competition_routes is not None
                                else cycle.routes[pair].allowed_level_ids
                            ),
                        }
                        for pair, plan in cycle.plans.items()
                    },
                    "portfolio": (
                        competition_decision.model_dump(mode="json")
                        if competition_decision is not None
                        else cycle.portfolio_risk.model_dump(mode="json")
                    ),
                    "positions": dict(positions),
                    "pending_exposure": {
                        pair: {
                            "buy": sum(
                                order["quote_notional"]
                                for order in orders
                                if order["side"] == "buy"
                            ),
                            "sell": sum(
                                order["quote_notional"]
                                for order in orders
                                if order["side"] == "sell"
                            ),
                            "count": len(orders),
                        }
                        for pair, orders in pending.items()
                    },
                    "gross_notional": gross,
                    "mark_to_market_pnl": mark_to_market,
                    "drawdown": drawdown,
                }
            )
        if not result.ticks:
            result.warnings.append("no replay ticks were available")
        final_pnl = result.ticks[-1]["mark_to_market_pnl"] if result.ticks else 0.0
        result.metrics = {
            "ticks": len(result.ticks),
            "gross_notional": result.ticks[-1]["gross_notional"] if result.ticks else 0.0,
            "max_gross_notional": max_gross,
            "net_notional": result.cycles[-1].portfolio_risk.net_notional if result.cycles else 0.0,
            "btc_beta_equivalent_exposure": (
                result.cycles[-1].portfolio_risk.btc_beta_equivalent_exposure
                if result.cycles
                else 0.0
            ),
            "max_beta_equivalent_exposure": max_beta,
            "max_long_beta_exposure": max_long_beta
            or max(
                (cycle.portfolio_risk.long_beta_exposure for cycle in result.cycles),
                default=0.0,
            ),
            "max_short_beta_exposure": max_short_beta
            or max(
                (cycle.portfolio_risk.short_beta_exposure for cycle in result.cycles),
                default=0.0,
            ),
            "max_per_asset_exposure": max_per_asset,
            "per_asset_inventory": dict(positions),
            "portfolio_pnl": final_pnl,
            "portfolio_drawdown": max_drawdown,
            "risk_blocks": risk_block_count,
            "portfolio_soft_limit_ticks": soft_limit_ticks,
            "portfolio_hard_limit_attempts": hard_limit_ticks,
            "same_direction_block_count": same_direction_block_count,
            "competition_risk_blocks": competition_block_count,
            "competition_non_normal_ticks": competition_non_normal_ticks,
            "competition_hard_stop_ticks": competition_hard_stop_ticks,
            "competition_profile": (
                self.competition_profile.profile_name
                if self.competition_profile is not None
                else None
            ),
            "lookahead_violation": False,
            "fill_model": self.replay_config.fill_model,
            "maker_fee_bps": self.replay_config.maker_fee_bps,
            "relationship_window_sensitivity": {
                pair: coordinator.state_engine.relationship_engine.window_sensitivity(pair)
                for pair in self.strategy_config.enabled_markets
            },
        }
        return result


def run_multi_asset_replay(
    ticks: Sequence[Mapping[str, Any]],
    *,
    strategy_config: MultiAssetConfig | None = None,
    replay_config: MultiAssetReplayConfig | None = None,
    label: str = "shared_btc_iv_with_portfolio_governor",
    competition_profile: CompetitionProfile | None = None,
) -> MultiAssetReplayResult:
    return MultiAssetReplay(
        strategy_config,
        replay_config,
        label=label,
        competition_profile=competition_profile,
    ).run(ticks)


def _independent_config(config: MultiAssetConfig) -> MultiAssetConfig:
    large = PortfolioRiskSettings(
        portfolio_max_gross_notional=1_000_000_000.0,
        portfolio_soft_beta_exposure=1_000_000_000.0,
        portfolio_hard_beta_exposure=2_000_000_000.0,
        portfolio_max_long_beta_exposure=1_000_000_000.0,
        portfolio_max_short_beta_exposure=1_000_000_000.0,
        per_asset_max_position_notional=1_000_000_000.0,
        max_active_executors_per_asset=10_000,
        max_active_executors_portfolio=10_000,
    )
    return config.model_copy(update={"portfolio_risk": large})


def run_stage8_ablations(
    ticks: Sequence[Mapping[str, Any]],
    *,
    strategy_config: MultiAssetConfig | None = None,
    replay_config: MultiAssetReplayConfig | None = None,
) -> list[MultiAssetReplayResult]:
    """Run governor and shared-IV ablations under identical replay assumptions."""

    config = strategy_config or MultiAssetConfig()
    replay = replay_config or MultiAssetReplayConfig()
    results = [
        run_multi_asset_replay(
            ticks,
            strategy_config=_independent_config(config),
            replay_config=replay,
            label="independent_per_asset_grids",
        ),
        run_multi_asset_replay(
            ticks,
            strategy_config=config,
            replay_config=replay,
            label="shared_btc_iv_with_portfolio_governor",
        ),
        run_multi_asset_replay(
            ticks,
            strategy_config=config.model_copy(
                update={"transmitted_btc_iv_weight": 0.0, "local_rv_weight": 1.0}
            ),
            replay_config=replay,
            label="local_rv_only_with_portfolio_governor",
        ),
    ]
    return results


__all__ = [
    "MultiAssetReplay",
    "MultiAssetReplayConfig",
    "MultiAssetReplayResult",
    "run_multi_asset_replay",
    "run_stage8_ablations",
]
