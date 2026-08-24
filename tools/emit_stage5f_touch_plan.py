"""Append a Stage 5F validation-only plan using Derive's wire-safe touch prices.

The installed Derive connector first quantizes through Hummingbot and then
serializes a limit price with four significant digits.  At BTC prices around
77,000 this makes the exchange wire price move in ten-dollar buckets even
though Derive's live price increment is 0.1.  This helper mirrors both steps
and chooses the closest representable passive price without changing the
Stage 4 source plan or production execution logic.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return parsed


def _latest(path: Path) -> dict[str, Any]:
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict):
                return record
    raise ValueError(f"no GridPlan record found in {path}")


def hummingbot_quantized_price(price: Decimal) -> Decimal:
    """Mirror DerivePerpetualDerivative.quantize_order_price()."""

    return Decimal(round(float(f"{price:.5g}"), 6))


def derive_wire_price(price: Decimal) -> Decimal:
    """Mirror DerivePerpetualDerivative._place_order() serialization."""

    return Decimal(str(float(f"{price:.4g}")))


def _safe_touch_price(
    start: Decimal,
    *,
    side: str,
    best_bid: Decimal,
    best_ask: Decimal,
    tick: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    candidate = start
    for _ in range(100_000):
        quantized = hummingbot_quantized_price(candidate)
        wire = derive_wire_price(quantized)
        if side == "buy":
            safe = wire <= best_bid and wire < best_ask
            candidate -= tick if not safe else Decimal("0")
        else:
            safe = wire >= best_ask and wire > best_bid
            candidate += tick if not safe else Decimal("0")
        if safe:
            return candidate, quantized, wire
        if candidate <= 0:
            break
    raise ValueError(f"could not find a wire-safe {side} touch price")


def _level(source: dict[str, Any], side: str, price: Decimal, center: Decimal) -> dict[str, Any]:
    levels = source.get(f"{side}_levels") or []
    if not levels or not isinstance(levels[0], dict):
        raise ValueError(f"source plan has no {side}_levels[0]")
    result = dict(levels[0])
    result.update(
        {
            "side": side,
            "level_index": 0,
            "theoretical_price": float(price),
            "distance_from_center_bps": float(
                abs(price - center) / center * Decimal("10000")
            ),
            "allocation_weight": 1.0,
        }
    )
    return result


def emit(
    source_path: Path,
    target_path: Path,
    best_bid: Decimal,
    best_ask: Decimal,
    min_price_increment: Decimal = Decimal("0.1"),
) -> dict[str, Any]:
    if best_bid >= best_ask:
        raise ValueError("best_bid must be strictly below best_ask")
    tick = _decimal(min_price_increment, "min_price_increment")
    source = _latest(source_path)
    buy_theoretical, buy_quantized, buy_wire = _safe_touch_price(
        best_bid,
        side="buy",
        best_bid=best_bid,
        best_ask=best_ask,
        tick=tick,
    )
    sell_theoretical, sell_quantized, sell_wire = _safe_touch_price(
        best_ask,
        side="sell",
        best_bid=best_bid,
        best_ask=best_ask,
        tick=tick,
    )
    center = (buy_theoretical + sell_theoretical) / Decimal("2")
    buy_level = _level(source, "buy", buy_theoretical, center)
    sell_level = _level(source, "sell", sell_theoretical, center)
    buy_quote = _decimal(buy_level["quote_amount"], "buy quote_amount")
    sell_quote = _decimal(sell_level["quote_amount"], "sell quote_amount")
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    record = dict(source)
    record.update(
        {
            "timestamp": now,
            "plan_version": int(source.get("plan_version", 0)) + 1,
            "mode": str(source.get("mode", "normal")).lower(),
            "enabled": True,
            "valid": True,
            "plan_change_significant": True,
            "validation_only": True,
            "validation_stage": "stage5f",
            "validation_reason": "closest representable passive Derive touch",
            "center_price": float(center),
            "reference_price": float(center),
            "buy_levels": [buy_level],
            "sell_levels": [sell_level],
            "buy_levels_count": 1,
            "sell_levels_count": 1,
            "buy_allocation_pct": 0.5,
            "sell_allocation_pct": 0.5,
            "total_quote_amount": float(buy_quote + sell_quote),
            "effective_quote_amount": float(buy_quote + sell_quote),
            "observed_best_bid": float(best_bid),
            "observed_best_ask": float(best_ask),
            "min_price_increment": str(tick),
            "wire_price_transform": "quantize .5g then serialize .4g",
            "buy_quantized_price": float(buy_quantized),
            "sell_quantized_price": float(sell_quantized),
            "buy_wire_price": float(buy_wire),
            "sell_wire_price": float(sell_wire),
        }
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
    return {
        "target": str(target_path),
        "plan_version": record["plan_version"],
        "validation_only": True,
        "observed_best_bid": float(best_bid),
        "observed_best_ask": float(best_ask),
        "buy_theoretical_price": float(buy_theoretical),
        "buy_quantized_price": float(buy_quantized),
        "buy_wire_price": float(buy_wire),
        "sell_theoretical_price": float(sell_theoretical),
        "sell_quantized_price": float(sell_quantized),
        "sell_wire_price": float(sell_wire),
        "buy_quote_amount": float(buy_quote),
        "sell_quote_amount": float(sell_quote),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--best-bid", required=True, type=Decimal)
    parser.add_argument("--best-ask", required=True, type=Decimal)
    parser.add_argument("--min-price-increment", type=Decimal, default=Decimal("0.1"))
    args = parser.parse_args()
    print(
        json.dumps(
            emit(
                args.source,
                args.target,
                args.best_bid,
                args.best_ask,
                args.min_price_increment,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
