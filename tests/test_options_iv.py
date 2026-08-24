from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from derive_options_mm.options_iv import (
    DAY_SECONDS,
    DeriveOptionsProvider,
    OptionContract,
    OptionsDataError,
    build_options_snapshot,
    parse_active_option_contracts,
    parse_option_ticker,
    select_atm_strike,
    select_expiry,
    unavailable_options_snapshot,
)

NOW = 1_700_000_000.0


def _contract(
    name: str,
    *,
    days: float = 7.0,
    strike: float = 100.0,
    option_type: str = "C",
) -> OptionContract:
    return OptionContract(
        instrument_name=name,
        underlying="BTC",
        expiry_ts=NOW + days * DAY_SECONDS,
        strike=strike,
        option_type=option_type,
    )


def _metadata(contract: OptionContract, *, active: bool = True) -> dict:
    return {
        "instrument_name": contract.instrument_name,
        "instrument_type": "option",
        "is_active": active,
        "option_details": {
            "expiry": contract.expiry_ts * 1_000,
            "strike": str(contract.strike),
            "option_type": contract.option_type,
        },
    }


def _ticker(iv: float | None = 0.5, *, timestamp: float = NOW) -> dict:
    pricing = {"i": str(iv)} if iv is not None else {}
    return {"option_pricing": pricing, "t": timestamp * 1_000, "I": "100"}


def test_active_contract_parser_excludes_inactive_and_expired_rows() -> None:
    future = _contract("BTC-FUTURE-C")
    expired = _contract("BTC-EXPIRED-C", days=-1)
    rows = [_metadata(future), _metadata(future, active=False), _metadata(expired)]

    contracts = parse_active_option_contracts(rows, now=NOW)

    assert [contract.instrument_name for contract in contracts] == ["BTC-FUTURE-C"]


def test_expiry_selection_respects_dte_bounds_target_and_call_put_pair() -> None:
    near_call = _contract("near-c", days=1, option_type="C")
    near_put = _contract("near-p", days=1, option_type="P")
    target_call = _contract("target-c", days=6, option_type="C")
    target_put = _contract("target-p", days=6, option_type="P")
    too_far_call = _contract("far-c", days=20, option_type="C")
    too_far_put = _contract("far-p", days=20, option_type="P")

    expiry, selected = select_expiry(
        [near_call, near_put, target_call, target_put, too_far_call, too_far_put],
        now=NOW,
        min_days_to_expiry=2,
        target_days_to_expiry=7,
        max_days_to_expiry=14,
    )

    assert expiry == target_call.expiry_ts
    assert {contract.instrument_name for contract in selected} == {"target-c", "target-p"}


def test_expiry_selection_rejects_invalid_range_and_missing_surface() -> None:
    with pytest.raises(ValueError, match="target_days_to_expiry"):
        select_expiry(
            [],
            now=NOW,
            min_days_to_expiry=8,
            target_days_to_expiry=7,
            max_days_to_expiry=14,
        )
    with pytest.raises(OptionsDataError, match="no active"):
        select_expiry(
            [_contract("call-only", days=7)],
            now=NOW,
            min_days_to_expiry=2,
            target_days_to_expiry=7,
            max_days_to_expiry=14,
        )


def test_atm_selection_uses_nearest_strike_and_distance_guard() -> None:
    calls = [_contract("c-100", strike=100, option_type="C")]
    puts = [_contract("p-100", strike=100, option_type="P")]
    selection = select_atm_strike(
        calls + puts,
        expiry_ts=calls[0].expiry_ts,
        reference_price=101.0,
        max_atm_distance_pct=0.05,
    )

    assert selection.atm_strike == 100
    assert selection.atm_distance_pct == pytest.approx(1 / 101)
    assert selection.call is calls[0]
    assert selection.put is puts[0]
    with pytest.raises(OptionsDataError, match="nearest ATM strike"):
        select_atm_strike(
            calls + puts,
            expiry_ts=calls[0].expiry_ts,
            reference_price=120.0,
            max_atm_distance_pct=0.05,
        )


def test_ticker_parser_prefers_mark_iv_then_bid_ask_midpoint_then_one_side() -> None:
    contract = _contract("BTC-OPTION-C")
    mark = parse_option_ticker(
        {"option_pricing": {"i": "0.50", "bi": "0.40", "ai": "0.60"}, "t": NOW},
        contract,
        max_iv=10,
    )
    midpoint = parse_option_ticker(
        {"option_pricing": {"bi": "0.40", "ai": "0.60"}, "t": NOW},
        contract,
        max_iv=10,
    )
    one_side = parse_option_ticker(
        {"option_pricing": {"bi": "0.40"}, "t": NOW},
        contract,
        max_iv=10,
    )

    assert (mark.iv, mark.iv_source) == (0.5, "mark_iv")
    assert (midpoint.iv, midpoint.iv_source) == (0.5, "bid_ask_iv_midpoint")
    assert (one_side.iv, one_side.iv_source) == (0.4, "bid_iv_only")


def test_options_snapshot_averages_sides_and_scores_distance_confidence() -> None:
    call = _contract("BTC-C", strike=100, option_type="C")
    put = _contract("BTC-P", strike=100, option_type="P")
    selection = select_atm_strike(
        [call, put],
        expiry_ts=call.expiry_ts,
        reference_price=101,
        max_atm_distance_pct=0.05,
    )

    snapshot = build_options_snapshot(
        selection,
        {"BTC-C": _ticker(0.4), "BTC-P": _ticker(0.6)},
        reference_price=101,
        now=NOW,
        max_option_data_age_seconds=15,
        max_iv=10,
        max_atm_distance_pct=0.05,
    )

    assert snapshot.data_available is True
    assert snapshot.atm_call_iv == 0.4
    assert snapshot.atm_put_iv == 0.6
    assert snapshot.atm_iv == pytest.approx(0.5)
    assert snapshot.confidence == pytest.approx(0.95, abs=1e-3)
    assert snapshot.errors == ()


@pytest.mark.parametrize("available_type", ["C", "P"])
def test_options_snapshot_uses_one_side_fallback_and_lower_confidence(
    available_type: str,
) -> None:
    call = _contract("BTC-C", option_type="C")
    put = _contract("BTC-P", option_type="P")
    selection = select_atm_strike(
        [call, put],
        expiry_ts=call.expiry_ts,
        reference_price=100,
        max_atm_distance_pct=0.05,
    )

    ticker_map = (
        {"BTC-C": _ticker(0.4)}
        if available_type == "C"
        else {"BTC-P": _ticker(0.4)}
    )
    snapshot = build_options_snapshot(
        selection,
        ticker_map,
        reference_price=100,
        now=NOW,
        max_option_data_age_seconds=15,
        max_iv=10,
        max_atm_distance_pct=0.05,
    )

    assert snapshot.data_available is True
    assert snapshot.atm_iv == 0.4
    assert (snapshot.atm_call_iv is not None) is (available_type == "C")
    assert (snapshot.atm_put_iv is not None) is (available_type == "P")
    assert snapshot.confidence == pytest.approx(0.75)
    assert any("ticker missing" in error for error in snapshot.errors)


def test_options_snapshot_fails_closed_on_stale_or_invalid_tickers() -> None:
    call = _contract("BTC-C", option_type="C")
    put = _contract("BTC-P", option_type="P")
    selection = select_atm_strike(
        [call, put],
        expiry_ts=call.expiry_ts,
        reference_price=100,
        max_atm_distance_pct=0.05,
    )

    snapshot = build_options_snapshot(
        selection,
        {"BTC-C": _ticker(0.4, timestamp=NOW - 16), "BTC-P": _ticker(None)},
        reference_price=100,
        now=NOW,
        max_option_data_age_seconds=15,
        max_iv=10,
    )

    assert snapshot.data_available is False
    assert snapshot.atm_iv is None
    assert any("stale" in error for error in snapshot.errors)
    assert any("no valid" in error for error in snapshot.errors)


def test_ticker_parser_rejects_non_positive_nan_and_over_bound_iv() -> None:
    contract = _contract("BTC-OPTION-C")
    for invalid in ("0", "-0.1", "NaN", "11"):
        ticker = parse_option_ticker(
            {"option_pricing": {"i": invalid}, "t": NOW},
            contract,
            max_iv=10,
        )
        assert ticker.iv is None
        assert "no valid" in ticker.errors[0]


def test_unavailable_snapshot_is_explicit_and_does_not_fabricate_iv() -> None:
    snapshot = unavailable_options_snapshot(
        now=NOW,
        reference_price=100,
        errors=("options disabled by configuration",),
    )

    assert snapshot.data_available is False
    assert snapshot.atm_iv is None
    assert snapshot.confidence == 0
    assert snapshot.errors == ("options disabled by configuration",)


def test_provider_uses_official_public_methods_and_caches_metadata() -> None:
    call = _contract("BTC-C", days=6, strike=100, option_type="C")
    put = _contract("BTC-P", days=6, strike=100, option_type="P")
    calls: list[tuple[str, dict]] = []

    def fake_post(method: str, params: dict):
        calls.append((method, params))
        if method == "public/get_instruments":
            return [_metadata(call), _metadata(put)]
        if method == "public/get_tickers":
            return {"tickers": {"BTC-C": _ticker(0.4), "BTC-P": _ticker(0.6)}}
        raise AssertionError(method)

    provider = DeriveOptionsProvider(
        base_url="https://example.invalid",
        metadata_refresh_interval_seconds=900,
    )
    provider._post = fake_post  # type: ignore[method-assign]

    first = asyncio.run(provider.snapshot(100, now=NOW))
    second = asyncio.run(provider.snapshot(100, now=NOW + 5))

    assert first.data_available is True
    assert second.atm_iv == pytest.approx(0.5)
    assert [method for method, _ in calls] == [
        "public/get_instruments",
        "public/get_tickers",
        "public/get_tickers",
    ]
    assert all(method in {"public/get_instruments", "public/get_tickers"} for method, _ in calls)
    ticker_params = calls[1][1]
    assert ticker_params["currency"] == "BTC"
    assert ticker_params["instrument_type"] == "option"
    assert ticker_params["expiry_date"] == call.expiry_date


def test_provider_converts_api_failures_to_unavailable_snapshot() -> None:
    provider = DeriveOptionsProvider()

    def failed_post(method: str, params: dict):
        raise OptionsDataError("synthetic public API failure")

    provider._post = failed_post  # type: ignore[method-assign]
    snapshot = asyncio.run(provider.snapshot(100, now=NOW))

    assert snapshot.data_available is False
    assert snapshot.atm_iv is None
    assert snapshot.errors == ("synthetic public API failure",)


def test_options_adapter_has_no_mutating_or_private_api_surface() -> None:
    source = Path(__file__).parents[1] / "src" / "derive_options_mm" / "options_iv.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in (
        "place_order",
        "cancel_order",
        "set_leverage",
        "set_position_mode",
        "private/",
    ):
        assert forbidden not in text
