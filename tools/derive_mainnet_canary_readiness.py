#!/usr/bin/env python3
"""Run a bounded, public-data-only Derive mainnet canary readiness audit.

The command deliberately has no private endpoint, Hummingbot account client,
order, cancel, leverage, or position-mode call.  It can prove public market
and options connectivity and calculate the smallest rule-valid canary size;
authenticated account state and human approval remain separate gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
INTEGRATION_ROOT = PROJECT_ROOT / "integrations" / "hummingbot"
for path in (SRC_ROOT, INTEGRATION_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from derive_adaptive_grid.mainnet_canary import (  # noqa: E402
    MAINNET_CHAIN_ID,
    MAINNET_CONNECTOR_NAME,
    MAINNET_DOMAIN,
    MAINNET_REST_URL,
    MAINNET_WS_URL,
    CanaryRiskLimits,
    calculate_minimum_canary_size,
    check_environment_consistency,
    existing_account_blockers,
    maker_price_is_passive,
)

from derive_options_mm.options_iv import DeriveOptionsProvider  # noqa: E402

DEFAULT_PLAN = Path(
    os.environ.get("CONDOR_DATA_DIR", "/Users/wilfred/Documents/Hummingbot/condor/data")
) / "derive_grid_plans.jsonl"
PUBLIC_METHODS = frozenset(
    {
        "public/get_all_instruments",
        "public/get_ticker",
        "public/get_time",
    }
)


class ReadOnlyAuditError(RuntimeError):
    """Raised for a public response that cannot support a safe audit."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _request_public(method: str, *, base_url: str, params: dict[str, Any] | None = None) -> Any:
    if method not in PUBLIC_METHODS:
        raise ReadOnlyAuditError(f"method not in public audit allowlist: {method}")
    path = f"/{method}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "derive-adaptive-state-grid-mainnet-read-only/1.0",
        },
        method="GET" if method == "public/get_time" else "POST",
    )
    if params is not None:
        request.data = json.dumps(params).encode("utf-8")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise ReadOnlyAuditError(f"{method} request failed: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
        raise ReadOnlyAuditError(f"{method} returned an unusable public response")
    return payload["result"]


def _latest_plan(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                latest = row
    return latest


def _plan_level(plan: dict[str, Any] | None, side: str) -> tuple[Decimal, Decimal] | None:
    if plan is None:
        return None
    levels = plan.get(f"{side}_levels")
    if not isinstance(levels, list) or not levels:
        return None
    level = levels[0]
    if not isinstance(level, dict):
        return None
    quote = _decimal(level.get("quote_amount"))
    price = _decimal(level.get("theoretical_price"))
    if quote is None or price is None or quote <= 0 or price <= 0:
        return None
    return quote, price


def _hummingbot_wire_price(value: Decimal) -> Decimal:
    """Mirror the installed Derive connector's five-significant-digit price rule."""

    return Decimal(str(round(float(f"{value:.5g}"), 6)))


def _maker_wire_price(
    side: str,
    theoretical_price: Decimal,
    *,
    best_bid: Decimal,
    best_ask: Decimal,
    tick_size: Decimal,
) -> tuple[Decimal, bool]:
    price = _hummingbot_wire_price(theoretical_price)
    adjusted = False
    if side == "buy" and price >= best_ask:
        adjusted = True
        candidate = _hummingbot_wire_price(best_ask - tick_size)
        price = (
            candidate
            if candidate > 0 and candidate < best_ask
            else _hummingbot_wire_price(best_bid)
        )
    if side == "sell" and price <= best_bid:
        adjusted = True
        candidate = _hummingbot_wire_price(best_bid + tick_size)
        price = candidate if candidate > best_bid else _hummingbot_wire_price(best_ask)
    return price, adjusted


def _public_market_snapshot(base_url: str) -> dict[str, Any]:
    server_time = _request_public("public/get_time", base_url=base_url)
    result = _request_public(
        "public/get_all_instruments",
        base_url=base_url,
        params={"expired": False, "instrument_type": "perp", "page": 1, "page_size": 1000},
    )
    rows = result.get("instruments", []) if isinstance(result, dict) else []
    btc_rule = next(
        (row for row in rows if isinstance(row, dict) and row.get("instrument_name") == "BTC-PERP"),
        None,
    )
    if btc_rule is None:
        raise ReadOnlyAuditError("BTC-PERP was not present in the mainnet public instrument list")
    ticker = _request_public(
        "public/get_ticker", base_url=base_url, params={"instrument_name": "BTC-PERP"}
    )
    if not isinstance(ticker, dict):
        raise ReadOnlyAuditError("BTC-PERP ticker response was not an object")
    best_bid = _decimal(ticker.get("best_bid_price"))
    best_ask = _decimal(ticker.get("best_ask_price"))
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= best_bid:
        raise ReadOnlyAuditError("mainnet BTC-PERP ticker did not contain a valid two-sided book")
    return {
        "server_time": server_time,
        "rule": btc_rule,
        "ticker": ticker,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": (best_bid + best_ask) / Decimal("2"),
    }


def _options_snapshot(mid: Decimal, base_url: str) -> Any:
    provider = DeriveOptionsProvider(
        base_url=base_url,
        currency="BTC",
        environment="production",
        request_timeout_seconds=15.0,
    )
    return asyncio.run(provider.snapshot(float(mid)))


def _fmt(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _audit_size(
    side: str,
    plan: dict[str, Any] | None,
    *,
    best_bid: Decimal,
    best_ask: Decimal,
    rule: dict[str, Any],
    max_order_notional: Decimal | None,
) -> dict[str, Any]:
    level = _plan_level(plan, side)
    if level is None:
        return {"side": side, "status": "NOT_CALCULATED", "reason": "Stage 4 level unavailable"}
    quote, theoretical_price = level
    tick_size = _decimal(rule.get("tick_size")) or Decimal("0")
    wire_price, adjusted = _maker_wire_price(
        side,
        theoretical_price,
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=tick_size,
    )
    passive = maker_price_is_passive(side, wire_price, best_bid, best_ask)
    minimum_amount = _decimal(rule.get("minimum_amount"))
    amount_step = _decimal(rule.get("amount_step"))
    if minimum_amount is None or amount_step is None:
        return {
            "side": side,
            "status": "BLOCKED",
            "reason": "public minimum_amount or amount_step unavailable",
            "wire_price": wire_price,
            "maker_passive": passive,
        }
    try:
        size = calculate_minimum_canary_size(
            theoretical_quote=quote,
            reference_price=wire_price,
            minimum_order_size=minimum_amount,
            amount_increment=amount_step,
            minimum_notional_size=_decimal(rule.get("min_notional_size")) or Decimal("0"),
            max_order_notional=max_order_notional,
        )
    except ValueError as exc:
        return {
            "side": side,
            "status": "BLOCKED",
            "reason": str(exc),
            "wire_price": wire_price,
            "maker_passive": passive,
            "theoretical_quote": quote,
        }
    return {
        "side": side,
        "status": "PASS" if passive else "BLOCKED",
        "reason": "" if passive else "wire price is not strictly passive",
        "theoretical_quote": quote,
        "theoretical_price": theoretical_price,
        "wire_price": wire_price,
        "wire_price_adjusted": adjusted,
        "amount": size.amount,
        "notional": size.notional,
        "required_scale": size.required_scale,
        "maker_passive": passive,
    }


def audit(
    *,
    plan_path: Path,
    base_url: str,
    limits: CanaryRiskLimits,
    stop_loss_pct: Decimal | None = None,
) -> dict[str, Any]:
    """Collect public evidence and return a JSON-safe readiness summary."""

    if base_url.rstrip("/") != MAINNET_REST_URL:
        raise ReadOnlyAuditError("mainnet readiness accepts only the installed mainnet REST URL")
    market = _public_market_snapshot(base_url)
    options = _options_snapshot(market["mid"], base_url)
    plan = _latest_plan(plan_path)
    rule = market["rule"]
    buy = _audit_size(
        "buy",
        plan,
        best_bid=market["best_bid"],
        best_ask=market["best_ask"],
        rule=rule,
        max_order_notional=limits.max_order_notional,
    )
    sell = _audit_size(
        "sell",
        plan,
        best_bid=market["best_bid"],
        best_ask=market["best_ask"],
        rule=rule,
        max_order_notional=limits.max_order_notional,
    )
    environments = check_environment_consistency(
        required_environment="mainnet",
        market_connector=MAINNET_CONNECTOR_NAME,
        market_domain=MAINNET_DOMAIN,
        options_environment="production",
        account_environment="unknown",
        execution_environment="mainnet",
    )
    account_blockers = existing_account_blockers(
        account_read_available=False,
        position_notional=None,
        open_order_count=None,
    )
    blockers = list(limits.blockers()) + list(environments.reasons) + list(account_blockers)
    if stop_loss_pct is None:
        blockers.append("mainnet_loss_control_not_configured")
    elif stop_loss_pct <= 0:
        blockers.append("mainnet_stop_loss_pct_must_be_positive")
    elif (
        limits.max_loss_quote is not None
        and limits.max_total_position_notional is not None
        and limits.max_total_position_notional * stop_loss_pct > limits.max_loss_quote
    ):
        blockers.append("configured_stop_loss_exceeds_mainnet_loss_budget")
    if plan is None:
        blockers.append("Stage 4 GridPlan unavailable")
    for row in (buy, sell):
        if row["status"] != "PASS":
            blockers.append(f"{row['side']}_canary_size_or_passivity_blocked")
    return {
        "timestamp": _iso_now(),
        "connector": MAINNET_CONNECTOR_NAME,
        "domain": MAINNET_DOMAIN,
        "chain_id": MAINNET_CHAIN_ID,
        "rest_url": base_url,
        "websocket_url": MAINNET_WS_URL,
        "trading_pair": "BTC-USDC",
        "exchange_instrument": "BTC-PERP",
        "mapping_verified": True,
        "server_time": market["server_time"],
        "best_bid": market["best_bid"],
        "best_ask": market["best_ask"],
        "mid": market["mid"],
        "spread": market["best_ask"] - market["best_bid"],
        "rule": {
            key: rule.get(key)
            for key in (
                "minimum_amount",
                "amount_step",
                "tick_size",
                "maker_fee_rate",
                "taker_fee_rate",
                "maximum_amount",
            )
        },
        "maker_fee_rate": market["ticker"].get("maker_fee_rate") or "UNKNOWN",
        "options": {
            "environment": getattr(options, "environment", None),
            "data_available": getattr(options, "data_available", False),
            "atm_iv": getattr(options, "atm_iv", None),
            "atm_strike": getattr(options, "atm_strike", None),
            "expiry": getattr(options, "expiry", None),
            "source": getattr(options, "source", None),
            "errors": list(getattr(options, "errors", ()) or ()),
        },
        "environments": environments,
        "plan_path": str(plan_path),
        "plan_version": plan.get("plan_version") if plan else None,
        "buy": buy,
        "sell": sell,
        "account_blockers": account_blockers,
        "risk_blockers": limits.blockers(),
        "stop_loss_pct": stop_loss_pct,
        "blockers": tuple(dict.fromkeys(blockers)),
        "actions_sent": 0,
        "approval_token_used": False,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {key: _json_safe(item) for key, item in value.__dict__.items()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--base-url", default=MAINNET_REST_URL)
    parser.add_argument("--max-order-notional", type=Decimal, default=None)
    parser.add_argument("--max-total-position-notional", type=Decimal, default=None)
    parser.add_argument("--max-loss-quote", type=Decimal, default=None)
    parser.add_argument(
        "--stop-loss-pct",
        type=Decimal,
        default=None,
        help="explicit loss-control percentage; omitted means canary is blocked",
    )
    parser.add_argument("--json", action="store_true", help="print the sanitized summary as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limits = CanaryRiskLimits(
        max_order_notional=args.max_order_notional,
        max_total_position_notional=args.max_total_position_notional,
        max_loss_quote=args.max_loss_quote,
    )
    try:
        result = audit(
            plan_path=args.plan.expanduser(),
            base_url=args.base_url,
            limits=limits,
            stop_loss_pct=args.stop_loss_pct,
        )
    except ReadOnlyAuditError as exc:
        print(f"[DERIVE MAINNET READ ONLY] BLOCKED: {exc}")
        return 2
    if args.json:
        print(json.dumps(_json_safe(result), indent=2, sort_keys=True))
        return 0
    print("[DERIVE MAINNET READ ONLY]")
    print(f"Timestamp: {result['timestamp']}")
    print(
        f"Connector/domain: {result['connector']} / {result['domain']} | "
        f"chain {result['chain_id']}"
    )
    print(f"Mapping: {result['exchange_instrument']} -> {result['trading_pair']} (verified)")
    print(
        f"Book: bid {_fmt(result['best_bid'])} / ask {_fmt(result['best_ask'])} / "
        f"mid {_fmt(result['mid'])} / spread {_fmt(result['spread'])}"
    )
    print(
        "Rules: minimum_amount "
        f"{result['rule']['minimum_amount']} | amount_step {result['rule']['amount_step']} | "
        f"tick_size {result['rule']['tick_size']} | maker_fee {result['maker_fee_rate']}"
    )
    options = result["options"]
    print(
        f"Options: env {options['environment']} | available {options['data_available']} | "
        f"ATM IV {options['atm_iv']} | expiry {options['expiry']} | errors {options['errors']}"
    )
    for side in ("buy", "sell"):
        row = result[side]
        print(
            f"{side.upper()} dry-run: status {row['status']} | theoretical quote "
            f"{row.get('theoretical_quote', 'UNKNOWN')} | "
            f"wire price {row.get('wire_price', 'UNKNOWN')} | "
            f"amount {row.get('amount', 'UNKNOWN')} | notional {row.get('notional', 'UNKNOWN')} | "
            f"required scale {row.get('required_scale', 'UNKNOWN')} | "
            f"maker passive {row.get('maker_passive', False)}"
        )
    print("Authenticated account reads: NOT PERFORMED by this public-only command")
    print(f"Loss control stop_loss_pct: {_fmt(result['stop_loss_pct'])}")
    print("Order/cancel/leverage/position-mode calls: 0")
    print("Approval token used: false")
    print("MAINNET CANARY NOT READY — STOP")
    for blocker in result["blockers"]:
        print(f"  BLOCKER: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
