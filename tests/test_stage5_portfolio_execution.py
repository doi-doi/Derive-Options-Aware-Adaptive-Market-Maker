from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

INTEGRATION_ROOT = Path(__file__).parents[1] / "integrations" / "hummingbot"
if str(INTEGRATION_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT))

from derive_adaptive_grid.execution_logic import (  # noqa: E402
    DesiredLevel,
    ExecutionSide,
    ReconciliationResult,
)
from derive_adaptive_grid.portfolio_config import (  # noqa: E402
    DEFAULT_TRADING_PAIRS,
    validate_trading_pairs,
)
from derive_adaptive_grid.portfolio_execution import (  # noqa: E402
    PortfolioExecutionPolicy,
    apply_portfolio_risk,
    evaluate_portfolio_risk,
)


def _desired(level_id: str, side: ExecutionSide, quote: str = "50") -> DesiredLevel:
    price = Decimal("100")
    return DesiredLevel(
        level_id=level_id,
        side=side,
        level_index=0,
        theoretical_price=price,
        price=price,
        amount=Decimal(quote) / price,
        quote_amount=Decimal(quote),
        quote_notional=Decimal(quote),
        take_profit_pct=Decimal("0.001"),
        maker_price_adjusted=False,
        plan_version=1,
        mode="normal",
    )


def _policy(**overrides: object) -> PortfolioExecutionPolicy:
    values: dict[str, object] = {
        "portfolio_max_gross_notional": Decimal("700"),
        "portfolio_soft_beta_exposure": Decimal("450"),
        "portfolio_hard_beta_exposure": Decimal("650"),
        "portfolio_max_long_beta_exposure": Decimal("650"),
        "portfolio_max_short_beta_exposure": Decimal("650"),
        "per_asset_max_position_notional": Decimal("400"),
        "max_active_executors_per_asset": 2,
        "max_active_executors_portfolio": 4,
        "collateral_safety_buffer_pct": Decimal("0"),
        "available_collateral": Decimal("1000"),
        "betas": {"BTC-USDC": Decimal("1"), "ETH-USDC": Decimal("1")},
    }
    values.update(overrides)
    values.pop("available_collateral", None)
    return PortfolioExecutionPolicy(**values)


def test_portfolio_scope_is_configurable_and_validated() -> None:
    assert validate_trading_pairs(DEFAULT_TRADING_PAIRS) == ("BTC-USDC", "ETH-USDC")
    assert validate_trading_pairs(("BTC-USDC", "SOL-USDC")) == ("BTC-USDC", "SOL-USDC")
    with pytest.raises(ValueError, match="must be unique"):
        validate_trading_pairs(("BTC-USDC", "BTC-USDC"))
    with pytest.raises(ValueError, match="BASE-USDC format"):
        validate_trading_pairs(("BTC-USDC", "ETH-PERP"))


def test_portfolio_gate_applies_pair_and_portfolio_executor_caps() -> None:
    decision = evaluate_portfolio_risk(
        {
            "BTC-USDC": [_desired("buy_0", ExecutionSide.BUY)],
            "ETH-USDC": [_desired("buy_0", ExecutionSide.BUY)],
        },
        active_executors={"BTC-USDC": 1, "ETH-USDC": 2},
        available_collateral=Decimal("1000"),
        policy=_policy(),
    )

    assert decision.allowed_level_ids["BTC-USDC"] == ("buy_0",)
    assert decision.blocked_level_ids["ETH-USDC"] == ("buy_0",)
    assert "executor limit" in decision.blocked_reasons["ETH-USDC"]["buy_0"]


def test_risk_reducing_exit_remains_allowed_when_gross_limit_is_reached() -> None:
    decision = evaluate_portfolio_risk(
        {
            "BTC-USDC": [
                _desired("buy_0", ExecutionSide.BUY),
                _desired("sell_0", ExecutionSide.SELL),
            ],
            "ETH-USDC": [],
        },
        positions={"BTC-USDC": Decimal("680")},
        available_collateral=Decimal("1000"),
        policy=_policy(
            portfolio_max_gross_notional=Decimal("700"),
            per_asset_max_position_notional=Decimal("1000"),
        ),
    )

    assert decision.blocked_level_ids["BTC-USDC"] == ("buy_0",)
    assert decision.allowed_level_ids["BTC-USDC"] == ("sell_0",)


def test_apply_portfolio_risk_removes_only_blocked_creates() -> None:
    result = ReconciliationResult(
        creates=[
            _desired("buy_0", ExecutionSide.BUY),
            _desired("sell_0", ExecutionSide.SELL),
        ]
    )
    decision = evaluate_portfolio_risk(
        {"BTC-USDC": result.creates, "ETH-USDC": []},
        active_executors={"BTC-USDC": 2, "ETH-USDC": 0},
        available_collateral=Decimal("1000"),
        policy=_policy(),
    )
    apply_portfolio_risk({"BTC-USDC": result}, decision)

    assert [item.level_id for item in result.creates] == []
    assert {item.level_id for item in result.blocked} == {"buy_0", "sell_0"}
