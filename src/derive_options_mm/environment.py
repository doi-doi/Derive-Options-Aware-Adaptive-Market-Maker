"""Canonical Derive environment profiles shared by config and execution code.

The selected environment is a configuration choice, not a second set of
strategy calculations.  Connector/domain names and the four data boundaries
are kept here so callers do not each maintain their own testnet/mainnet map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAINNET_CONNECTOR_NAME = "derive_perpetual"
TESTNET_CONNECTOR_NAME = "derive_perpetual_testnet"
MAINNET_DOMAIN = "derive_perpetual"
TESTNET_DOMAIN = "derive_perpetual_testnet"
MAINNET_OPTIONS_API_BASE_URL = "https://api.lyra.finance"
TESTNET_OPTIONS_API_BASE_URL = "https://api-demo.lyra.finance"


@dataclass(frozen=True)
class DeriveEnvironmentProfile:
    """All network identifiers required for one Derive environment."""

    name: str
    connector_name: str
    domain: str
    options_environment: str
    options_api_base_url: str
    account_environment: str
    execution_environment: str

    @property
    def is_mainnet(self) -> bool:
        return self.name == "mainnet"


def normalize_environment(value: Any) -> str:
    """Normalize the labels accepted by dashboard and controller configs."""

    normalized = str(value or "").strip().lower()
    if normalized in {"mainnet", "production", "prod", "live"}:
        return "mainnet"
    if normalized in {"testnet", "demo", "sandbox"}:
        return "testnet"
    return "unknown"


_ENVIRONMENT_PROFILES = {
    "testnet": DeriveEnvironmentProfile(
        name="testnet",
        connector_name=TESTNET_CONNECTOR_NAME,
        domain=TESTNET_DOMAIN,
        options_environment="testnet",
        options_api_base_url=TESTNET_OPTIONS_API_BASE_URL,
        account_environment="testnet",
        execution_environment="testnet",
    ),
    "mainnet": DeriveEnvironmentProfile(
        name="mainnet",
        connector_name=MAINNET_CONNECTOR_NAME,
        domain=MAINNET_DOMAIN,
        options_environment="mainnet",
        options_api_base_url=MAINNET_OPTIONS_API_BASE_URL,
        account_environment="mainnet",
        execution_environment="mainnet",
    ),
}


def environment_profile(value: Any) -> DeriveEnvironmentProfile:
    """Return the canonical profile for an environment label."""

    normalized = normalize_environment(value)
    try:
        return _ENVIRONMENT_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported Derive environment: {value!r}") from exc


def environment_for_connector(connector_name: Any, domain: Any = None) -> str:
    """Return the environment implied by a connector/domain pair."""

    connector = str(connector_name or "").strip().lower()
    connector_domain = str(domain or "").strip().lower()
    for profile in _ENVIRONMENT_PROFILES.values():
        if connector == profile.connector_name and connector_domain in {"", profile.domain}:
            return profile.name
    return "unknown"


__all__ = [
    "DeriveEnvironmentProfile",
    "MAINNET_CONNECTOR_NAME",
    "MAINNET_DOMAIN",
    "MAINNET_OPTIONS_API_BASE_URL",
    "TESTNET_CONNECTOR_NAME",
    "TESTNET_DOMAIN",
    "TESTNET_OPTIONS_API_BASE_URL",
    "environment_for_connector",
    "environment_profile",
    "normalize_environment",
]
