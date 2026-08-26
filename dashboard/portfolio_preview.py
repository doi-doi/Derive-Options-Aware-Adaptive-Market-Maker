"""Read-only portfolio bars and utilization consequences."""

from __future__ import annotations

from typing import Any

from derive_options_mm.competition_risk import CompetitionProfile


def _value(record: dict[str, Any] | None, key: str, default: float = 0.0) -> float:
    if not record:
        return default
    try:
        return float(record.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def portfolio_bars(
    portfolio: dict[str, Any] | None,
    profile: CompetitionProfile,
) -> list[dict[str, Any]]:
    """Return progress-bar rows for gross, beta, and per-asset exposure."""

    rows = [
        {
            "label": "GROSS EXPOSURE",
            "value": _value(portfolio, "gross_notional"),
            "limit": profile.portfolio_max_gross_notional,
        },
        {
            "label": "BTC-BETA LONG",
            "value": _value(portfolio, "long_beta_exposure"),
            "limit": profile.portfolio_max_long_beta_exposure,
        },
        {
            "label": "BTC-BETA SHORT",
            "value": _value(portfolio, "short_beta_exposure"),
            "limit": profile.portfolio_max_short_beta_exposure,
        },
    ]
    exposures = (portfolio or {}).get("per_asset_exposure", {})
    if not isinstance(exposures, dict):
        exposures = {}
    for pair, limit in profile.asset_limits.items():
        rows.append(
            {
                "label": pair,
                "value": float(exposures.get(pair, 0.0) or 0.0),
                "limit": limit.max_position_notional,
            }
        )
    for row in rows:
        limit = float(row["limit"])
        row["utilization"] = row["value"] / limit if limit > 0 else None
    return rows


def collateral_summary(
    profile: CompetitionProfile,
    *,
    equity: float | None,
    available_collateral: float | None,
) -> dict[str, Any]:
    equity_value = float(equity) if equity is not None else None
    collateral_value = float(available_collateral) if available_collateral is not None else None
    reserve = profile.collateral_reserve_pct * (equity_value or profile.starting_equity_reference)
    return {
        "equity": equity_value,
        "available_collateral": collateral_value,
        "reserve": reserve,
        "reserve_pct": profile.collateral_reserve_pct,
        "usable_collateral": (
            max(0.0, collateral_value - reserve) if collateral_value is not None else None
        ),
    }


__all__ = ["collateral_summary", "portfolio_bars"]
