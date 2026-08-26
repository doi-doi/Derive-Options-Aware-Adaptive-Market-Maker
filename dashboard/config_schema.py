"""Typed dashboard configuration assembled from the existing Stage 8 models.

The dashboard deliberately keeps the competition profile and the Stage 8
strategy overlay separate.  ``CompetitionProfile`` remains the source of
truth for risk and execution settings; the strategy overlay exists because
the current Condor monitor constructs ``MultiAssetConfig`` in-process and has
no reloadable Stage 8 configuration file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from derive_options_mm.competition_risk import CompetitionProfile
from derive_options_mm.environment import DeriveEnvironmentProfile, environment_profile
from derive_options_mm.multi_asset import MultiAssetConfig


class Stage9StrategySettings(BaseModel):
    """Small, explicit set of existing Stage 8 controls exposed by the UI."""

    model_config = ConfigDict(extra="forbid")

    btc_iv_weight: float = Field(default=0.25, ge=0, le=1)
    iv_stale_timeout_seconds: float = Field(default=15.0, gt=0)
    iv_missing_behavior: Literal["rv_only", "defensive", "pause"] = "rv_only"
    relationship_lookback_seconds: float = Field(default=3600.0, gt=0)
    rv_weight: float = Field(default=0.75, ge=0, le=1)
    transmitted_btc_iv_weight: float = Field(default=0.25, ge=0, le=1)
    direction_threshold: float = Field(default=0.25, ge=0, le=1)
    defensive_volatility_score: float = Field(default=1.50, gt=0)
    base_grid_width_pct: float = Field(default=0.010, gt=0)
    normal_levels_per_side: int = Field(default=5, ge=1, le=100)
    defensive_levels_per_side: int = Field(default=3, ge=1, le=100)
    defensive_width_multiplier: float = Field(default=1.50, gt=0)
    max_inventory_center_shift_bps: float = Field(default=30.0, ge=0)

    @model_validator(mode="after")
    def validate_weights(self) -> Stage9StrategySettings:
        if self.rv_weight == 0 and self.transmitted_btc_iv_weight == 0:
            raise ValueError("at least one volatility weight must be positive")
        return self

    @classmethod
    def from_multi_asset_config(cls, config: MultiAssetConfig) -> Stage9StrategySettings:
        return cls(
            btc_iv_weight=config.global_options.iv_weight,
            iv_stale_timeout_seconds=config.global_options.stale_seconds,
            iv_missing_behavior=config.global_options.missing_behavior,
            relationship_lookback_seconds=config.relationship.window_seconds,
            rv_weight=config.local_rv_weight,
            transmitted_btc_iv_weight=config.transmitted_btc_iv_weight,
            direction_threshold=config.mode.bias_direction_score_threshold,
            defensive_volatility_score=config.mode.defensive_volatility_score,
            base_grid_width_pct=float(config.grid.base_grid_width_pct),
            normal_levels_per_side=config.grid.normal_levels_per_side,
            defensive_levels_per_side=config.grid.defensive_levels_per_side,
            defensive_width_multiplier=float(config.grid.defensive_width_multiplier),
            max_inventory_center_shift_bps=float(config.grid.max_inventory_center_shift_bps),
        )

    def to_multi_asset_config(self, base: MultiAssetConfig | None = None) -> MultiAssetConfig:
        """Apply staged values to the real Stage 8 model and revalidate it."""

        current = base or MultiAssetConfig()
        raw = current.model_dump(mode="python")
        raw["global_options"].update(
            {
                "iv_weight": self.btc_iv_weight,
                "stale_seconds": self.iv_stale_timeout_seconds,
                "missing_behavior": self.iv_missing_behavior,
            }
        )
        raw["relationship"]["window_seconds"] = self.relationship_lookback_seconds
        raw["local_rv_weight"] = self.rv_weight
        raw["transmitted_btc_iv_weight"] = self.transmitted_btc_iv_weight
        raw["mode"].update(
            {
                "bias_direction_score_threshold": self.direction_threshold,
                "defensive_volatility_score": self.defensive_volatility_score,
            }
        )
        raw["grid"].update(
            {
                "base_grid_width_pct": self.base_grid_width_pct,
                "normal_levels_per_side": self.normal_levels_per_side,
                "defensive_levels_per_side": self.defensive_levels_per_side,
                "defensive_width_multiplier": self.defensive_width_multiplier,
                "max_inventory_center_shift_bps": self.max_inventory_center_shift_bps,
            }
        )
        return MultiAssetConfig.model_validate(raw)


class RuntimePaths(BaseModel):
    """Local status-file locations; these are not exchange credentials."""

    data_dir: Path
    snapshot: str = "derive_market_snapshots.jsonl"
    state: str = "derive_market_states.jsonl"
    mode: str = "derive_grid_modes.jsonl"
    plan: str = "derive_grid_plans.jsonl"
    relationship: str = "derive_btc_relationship_states.jsonl"
    portfolio_risk: str = "derive_portfolio_risk_states.jsonl"
    execution_journal: str = "derive_execution_events.jsonl"

    def stream_paths(self) -> dict[str, Path]:
        return {
            name: self.data_dir / filename
            for name, filename in {
                "snapshot": self.snapshot,
                "state": self.state,
                "mode": self.mode,
                "plan": self.plan,
                "relationship": self.relationship,
                "portfolio_risk": self.portfolio_risk,
                "execution_journal": self.execution_journal,
            }.items()
        }


class DashboardConfig(BaseModel):
    """In-memory bundle used by staging, previews, and history snapshots."""

    competition: CompetitionProfile
    strategy: Stage9StrategySettings

    def to_record(self) -> dict:
        return {
            "competition": self.competition.model_dump(mode="json"),
            "strategy": self.strategy.model_dump(mode="json"),
        }


def default_strategy_settings() -> Stage9StrategySettings:
    return Stage9StrategySettings.from_multi_asset_config(MultiAssetConfig())


def environment_preset(
    current: CompetitionProfile, target: str
) -> CompetitionProfile:
    """Switch the shared Derive profile without changing Stage 4 allocations.

    A network switch always stages execution off.  This keeps a mainnet
    selection useful for read-only validation while preventing a connector
    change from inheriting an execution permission from the other network.
    """

    profile: DeriveEnvironmentProfile = environment_profile(target)
    updates = {
        "market_environment": profile.name,
        "connector_name": profile.connector_name,
        "allow_mainnet_trading": False,
        "execution_enabled": False,
        "post_only": True,
    }
    if profile.is_mainnet:
        updates["leverage"] = 1.0
    return CompetitionProfile.model_validate(
        current.model_copy(update=updates).model_dump(mode="python")
    )


def preset_profile(name: str, current: CompetitionProfile) -> CompetitionProfile:
    """Return a visible configuration template; presets never apply changes."""

    normalized = name.strip().lower()
    if normalized in {"competition", "custom"}:
        return current
    if normalized == "conservative":
        return CompetitionProfile.model_validate(
            current.model_copy(
                update={
                    "target_order_notional": 50.0,
                    "max_single_order_notional": 70.0,
                    "portfolio_soft_gross_notional": 700.0,
                    "portfolio_max_gross_notional": 900.0,
                    "portfolio_soft_beta_exposure": 450.0,
                    "portfolio_hard_beta_exposure": 650.0,
                    "portfolio_max_long_beta_exposure": 650.0,
                    "portfolio_max_short_beta_exposure": 650.0,
                    "leverage": 1.0,
                    "minimum_order_lifetime_seconds": 180.0,
                    "minimum_replace_interval_seconds": 90.0,
                }
            ).model_dump(mode="python")
        )
    raise ValueError(f"unknown configuration template: {name}")


__all__ = [
    "DashboardConfig",
    "RuntimePaths",
    "Stage9StrategySettings",
    "default_strategy_settings",
    "environment_preset",
    "preset_profile",
]
