"""Append a validation-only, passive near-touch GridPlan for Stage 5E.

The source plan is read-only. The output target must be an isolated Stage 5
validation file. The plan keeps one geometric level per side, puts BUY at the
observed best bid, and puts SELL one observed spread above the best ask so the
opposite side remains safely passive.
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
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict):
                return record
    raise ValueError(f"no GridPlan record found in {path}")


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
    sell_spread_multiple: int = 1,
) -> dict[str, Any]:
    if best_bid >= best_ask:
        raise ValueError("best_bid must be strictly below best_ask")
    if sell_spread_multiple < 1:
        raise ValueError("sell_spread_multiple must be positive")
    spread = best_ask - best_bid
    buy_price = best_bid
    sell_price = best_ask + spread * sell_spread_multiple
    center = (buy_price + sell_price) / Decimal("2")
    source = _latest(source_path)
    buy_level = _level(source, "buy", buy_price, center)
    sell_level = _level(source, "sell", sell_price, center)
    buy_quote = _decimal(buy_level["quote_amount"], "buy quote_amount")
    sell_quote = _decimal(sell_level["quote_amount"], "sell quote_amount")
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    plan_version = int(source.get("plan_version", 0)) + 1
    total_width = (sell_price - buy_price) / center
    record = dict(source)
    record.update(
        {
            "timestamp": now,
            "plan_version": plan_version,
            "mode": str(source.get("mode", "normal")).lower(),
            "enabled": True,
            "valid": True,
            "plan_change_significant": True,
            "validation_only": True,
            "validation_reason": "Stage 5E passive near-touch maker-fill observation",
            "center_price": float(center),
            "reference_price": float(center),
            "half_grid_width_pct": float(total_width / Decimal("2")),
            "total_grid_width_pct": float(total_width),
            "inner_distance_bps": float(abs(buy_price - center) / center * Decimal("10000")),
            "buy_levels": [buy_level],
            "sell_levels": [sell_level],
            "buy_levels_count": 1,
            "sell_levels_count": 1,
            "buy_allocation_pct": 0.5,
            "sell_allocation_pct": 0.5,
            "total_quote_amount": float(buy_quote + sell_quote),
            "effective_quote_amount": float(buy_quote + sell_quote),
        }
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
    return {
        "target": str(target_path),
        "plan_version": plan_version,
        "mode": record["mode"],
        "validation_only": True,
        "center_price": float(center),
        "buy_price": float(buy_price),
        "sell_price": float(sell_price),
        "best_bid": float(best_bid),
        "best_ask": float(best_ask),
        "spread": float(spread),
        "sell_spread_multiple": sell_spread_multiple,
        "buy_quote_amount": float(buy_quote),
        "sell_quote_amount": float(sell_quote),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--best-bid", required=True, type=Decimal)
    parser.add_argument("--best-ask", required=True, type=Decimal)
    parser.add_argument("--sell-spread-multiple", type=int, default=1)
    args = parser.parse_args()
    print(
        json.dumps(
            emit(
                args.source,
                args.target,
                args.best_bid,
                args.best_ask,
                args.sell_spread_multiple,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
