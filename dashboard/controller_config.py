"""Build the fail-closed Hummingbot controller artifact for the dashboard profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from derive_options_mm.competition_risk import CompetitionProfile
from derive_options_mm.environment import environment_profile

_CONTROLLER_DIR = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "hummingbot"
    / "derive_adaptive_grid"
)


def build_controller_config(
    profile: CompetitionProfile, *, template_dir: str | Path = _CONTROLLER_DIR
) -> dict[str, Any]:
    """Return a controller config for ``profile`` with execution fail-closed.

    The dashboard controls only the network selection here.  The environment
    template supplies controller-specific defaults, and every generated
    artifact keeps the execution and mainnet permission switches disabled.
    """

    environment = environment_profile(profile.market_environment)
    template_path = (
        Path(template_dir)
        / f"derive_adaptive_grid_{'mainnet' if environment.is_mainnet else 'testnet'}.example.yml"
    )
    raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"controller template must contain a mapping: {template_path}")
    raw.update(
        {
            "id": f"derive_adaptive_grid_dashboard_{environment.name}",
            "connector_name": environment.connector_name,
            "environment": environment.name,
            "market_environment": environment.name,
            "options_environment": environment.options_environment,
            "account_environment": environment.account_environment,
            "execution_environment": environment.execution_environment,
            "allow_mainnet_trading": False,
            "execution_enabled": False,
            "execution_max_levels_per_side": 1,
            "post_only": True,
        }
    )
    if environment.is_mainnet:
        raw.update(
            {
                "testnet_order_scale": None,
                "mainnet_canary_order_scale": None,
                "mainnet_canary_max_order_notional": None,
                "mainnet_canary_max_total_position_notional": None,
                "mainnet_canary_max_loss_quote": None,
                "mainnet_environment_verified": False,
                "mainnet_account_state_verified": False,
                "mainnet_canary_ack": None,
            }
        )
    else:
        raw["testnet_order_scale"] = raw.get("testnet_order_scale") or 0.05
        raw.pop("mainnet_canary_order_scale", None)
        raw.pop("mainnet_canary_max_order_notional", None)
        raw.pop("mainnet_canary_max_total_position_notional", None)
        raw.pop("mainnet_canary_max_loss_quote", None)
        raw.pop("mainnet_environment_verified", None)
        raw.pop("mainnet_account_state_verified", None)
        raw.pop("mainnet_canary_ack", None)
    return raw


def controller_yaml(profile: CompetitionProfile) -> str:
    """Serialize a generated controller config without any secret fields."""

    return yaml.safe_dump(build_controller_config(profile), sort_keys=False, allow_unicode=True)


__all__ = ["build_controller_config", "controller_yaml"]
