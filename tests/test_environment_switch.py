"""Regression tests for the shared Derive environment selector."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dashboard.config_schema import environment_preset  # noqa: E402
from derive_options_mm.competition_risk import CompetitionProfile  # noqa: E402
from derive_options_mm.environment import (  # noqa: E402
    MAINNET_CONNECTOR_NAME,
    MAINNET_OPTIONS_API_BASE_URL,
    TESTNET_CONNECTOR_NAME,
    TESTNET_OPTIONS_API_BASE_URL,
    environment_for_connector,
    environment_profile,
)


def test_environment_profiles_are_the_single_connector_mapping() -> None:
    testnet = environment_profile("demo")
    mainnet = environment_profile("production")

    assert testnet.connector_name == TESTNET_CONNECTOR_NAME
    assert testnet.domain == TESTNET_CONNECTOR_NAME
    assert testnet.options_api_base_url == TESTNET_OPTIONS_API_BASE_URL
    assert mainnet.connector_name == MAINNET_CONNECTOR_NAME
    assert mainnet.domain == MAINNET_CONNECTOR_NAME
    assert mainnet.options_api_base_url == MAINNET_OPTIONS_API_BASE_URL
    assert environment_for_connector(TESTNET_CONNECTOR_NAME) == "testnet"
    assert environment_for_connector(MAINNET_CONNECTOR_NAME) == "mainnet"
    assert environment_for_connector(MAINNET_CONNECTOR_NAME, TESTNET_CONNECTOR_NAME) == "unknown"


def test_dashboard_environment_switch_preserves_stage4_allocations_and_disables_execution() -> None:
    current = CompetitionProfile()
    switched = environment_preset(current, "mainnet")

    assert switched.market_environment == "mainnet"
    assert switched.connector_name == MAINNET_CONNECTOR_NAME
    assert switched.execution_enabled is False
    assert switched.allow_mainnet_trading is False
    assert switched.post_only is True
    assert switched.leverage == 1
    assert switched.enabled_markets == current.enabled_markets
    assert switched.capital_allocation_pct == current.capital_allocation_pct
    assert switched.asset_limits == current.asset_limits


def test_dashboard_can_switch_back_to_testnet_without_inheriting_permission() -> None:
    mainnet = environment_preset(CompetitionProfile(), "mainnet")
    testnet = environment_preset(mainnet, "testnet")

    assert testnet.market_environment == "testnet"
    assert testnet.connector_name == TESTNET_CONNECTOR_NAME
    assert testnet.execution_enabled is False
    assert testnet.allow_mainnet_trading is False
    assert testnet.enabled_markets == mainnet.enabled_markets
    assert testnet.capital_allocation_pct == mainnet.capital_allocation_pct


def test_mainnet_competition_profile_is_read_only_only() -> None:
    profile = environment_preset(CompetitionProfile(), "mainnet")
    assert CompetitionProfile.model_validate(profile.model_dump(mode="python")) == profile

    with pytest.raises(ValueError, match="read-only"):
        CompetitionProfile.model_validate(
            profile.model_copy(update={"execution_enabled": True}).model_dump(mode="python")
        )


def test_profile_rejects_connector_environment_mismatch() -> None:
    values = CompetitionProfile().model_dump(mode="python")
    values.update({"market_environment": "mainnet", "connector_name": TESTNET_CONNECTOR_NAME})

    with pytest.raises(ValueError, match="requires derive_perpetual"):
        CompetitionProfile.model_validate(values)
