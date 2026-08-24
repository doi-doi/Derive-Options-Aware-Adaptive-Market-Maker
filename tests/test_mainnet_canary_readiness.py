from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

INTEGRATION_ROOT = Path(__file__).parents[1] / "integrations" / "hummingbot"
if str(INTEGRATION_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT))

from derive_adaptive_grid.derive_perpetual_signing_compat import (  # noqa: E402
    _is_testnet_domain,
)
from derive_adaptive_grid.execution_logic import ExecutionPolicy, RuntimeHealth  # noqa: E402
from derive_adaptive_grid.mainnet_canary import (  # noqa: E402
    MAINNET_CANARY_ACK,
    CanaryRiskLimits,
    calculate_minimum_canary_size,
    check_environment_consistency,
    existing_account_blockers,
    mainnet_canary_authorized,
    maker_price_is_passive,
)
from derive_adaptive_grid.orderbook_snapshot_compat import (  # noqa: E402
    is_supported_derive_domain,
)


def test_mainnet_environment_requires_all_four_boundaries_to_match() -> None:
    consistent = check_environment_consistency(
        required_environment="mainnet",
        market_connector="derive_perpetual",
        market_domain="derive_perpetual",
        options_environment="production",
        account_environment="mainnet",
        execution_environment="mainnet",
    )
    mismatch = check_environment_consistency(
        required_environment="mainnet",
        market_connector="derive_perpetual",
        market_domain="derive_perpetual",
        options_environment="testnet",
        account_environment="mainnet",
        execution_environment="mainnet",
    )

    assert consistent.consistent is True
    assert mismatch.consistent is False
    assert "options_environment_mismatch" in mismatch.reasons


def test_minimum_canary_size_uses_amount_increment_and_stage4_quote() -> None:
    size = calculate_minimum_canary_size(
        theoretical_quote=Decimal("1000"),
        reference_price=Decimal("79400"),
        minimum_order_size=Decimal("0.01"),
        amount_increment=Decimal("0.0001"),
    )

    assert size.amount == Decimal("0.0100")
    assert size.notional == Decimal("794.0000")
    assert size.required_scale == Decimal("0.794")


def test_minimum_canary_size_stops_when_live_minimum_exceeds_explicit_budget() -> None:
    with pytest.raises(ValueError, match="exceeds mainnet_canary_max_order_notional"):
        calculate_minimum_canary_size(
            theoretical_quote=Decimal("1000"),
            reference_price=Decimal("79400"),
            minimum_order_size=Decimal("0.01"),
            amount_increment=Decimal("0.0001"),
            max_order_notional=Decimal("793.99"),
        )


def test_wire_prices_must_be_strictly_passive() -> None:
    assert maker_price_is_passive("buy", Decimal("79357"), Decimal("79357.1"), Decimal("79462.5"))
    assert maker_price_is_passive("sell", Decimal("79463"), Decimal("79357.1"), Decimal("79462.5"))
    assert not maker_price_is_passive(
        "buy", Decimal("79462.5"), Decimal("79357.1"), Decimal("79462.5")
    )
    assert not maker_price_is_passive(
        "sell", Decimal("79357.1"), Decimal("79357.1"), Decimal("79462.5")
    )


def test_mainnet_authorization_requires_three_switches_ack_and_budgets() -> None:
    limits = CanaryRiskLimits(
        max_order_notional=Decimal("800"),
        max_total_position_notional=Decimal("1600"),
        max_loss_quote=Decimal("20"),
    )
    common = {
        "mainnet_environment_verified": True,
        "environment_consistent": True,
        "allow_mainnet_trading": True,
        "execution_enabled": True,
        "acknowledgement": MAINNET_CANARY_ACK,
        "risk_limits": limits,
        "order_scale": Decimal("0.8"),
        "account_state_verified": True,
        "stop_loss_pct": Decimal("0.01"),
    }

    assert mainnet_canary_authorized(**common) is True
    assert mainnet_canary_authorized(**{**common, "acknowledgement": "yes"}) is False
    assert mainnet_canary_authorized(**{**common, "allow_mainnet_trading": False}) is False
    assert mainnet_canary_authorized(**{**common, "execution_enabled": False}) is False
    assert mainnet_canary_authorized(**{**common, "stop_loss_pct": None}) is False


def test_existing_account_state_is_a_hard_gate() -> None:
    blockers = existing_account_blockers(
        account_read_available=True,
        position_notional=Decimal("1"),
        open_order_count=1,
    )

    assert "existing_btc_position_requires_explicit_acknowledgement" in blockers
    assert "existing_btc_orders_require_explicit_acknowledgement" in blockers


def test_mainnet_health_requires_environment_consistency_even_with_other_checks() -> None:
    health = RuntimeHealth(
        testnet_verified=False,
        connector_ready=True,
        market_data_ready=True,
        trading_rules_available=True,
        balance_verified=True,
        position_verified=True,
        best_bid=Decimal("79357.1"),
        best_ask=Decimal("79462.5"),
        trading_rules=None,
        environment="mainnet",
        environment_verified=True,
        environment_consistent=False,
        mainnet_canary_authorized=True,
    )

    assert health.ready_for_new_entries is False


def test_mainnet_execution_policy_does_not_invent_missing_canary_values() -> None:
    policy = ExecutionPolicy(
        environment="mainnet",
        testnet_order_scale=None,
        max_total_position_notional=None,
        max_side_position_notional=None,
    )

    assert policy.testnet_order_scale is None
    assert policy.max_total_position_notional is None
    assert policy.max_side_position_notional is None


def test_testnet_signing_and_snapshot_compatibility_do_not_activate_for_mainnet() -> None:
    assert _is_testnet_domain("derive_perpetual_testnet") is True
    assert _is_testnet_domain("derive_perpetual") is False
    assert is_supported_derive_domain("derive_perpetual_testnet") is True
    assert is_supported_derive_domain("derive_perpetual") is True
    assert is_supported_derive_domain("unexpected-domain") is False
    signing_source = (
        Path(__file__).parents[1]
        / "integrations"
        / "hummingbot"
        / "derive_adaptive_grid"
        / "derive_perpetual_signing_compat.py"
    ).read_text(encoding="utf-8")
    assert "if not _is_testnet_domain(self._domain):" in signing_source
    assert "derive_perpetual_constants.TESTNET_DOMAIN_SEPARATOR =" not in signing_source


def test_mainnet_template_is_disabled_and_separate_from_testnet() -> None:
    root = Path(__file__).parents[1] / "integrations" / "hummingbot" / "derive_adaptive_grid"
    mainnet = (root / "derive_adaptive_grid_mainnet.example.yml").read_text(encoding="utf-8")
    testnet = (root / "derive_adaptive_grid_testnet.example.yml").read_text(encoding="utf-8")

    assert "connector_name: derive_perpetual\n" in mainnet
    assert "connector_name: derive_perpetual_testnet\n" in testnet
    assert "allow_mainnet_trading: false" in mainnet
    assert "execution_enabled: false" in mainnet
    assert "execution_max_levels_per_side: 1" in mainnet
    assert "mainnet_canary_ack: null" in mainnet
    assert "mainnet_account_state_verified: false" in mainnet
    assert "testnet_order_scale: null" in mainnet
