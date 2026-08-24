"""Append one controlled plan to an isolated Stage 5 target JSONL.

The Stage 4 source file is read-only here.  This utility exists only for a
live testnet mode check: it widens the current grid, keeps three theoretical
levels per side, halves the quote allocation, and marks the record defensive.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _widen_level(level: dict[str, Any], center: Decimal, multiplier: Decimal) -> dict[str, Any]:
    side = str(level.get("side", "")).lower()
    price = Decimal(str(level["theoretical_price"]))
    widened = (
        center - (center - price) * multiplier
        if side == "buy"
        else center + (price - center) * multiplier
    )
    result = dict(level)
    result["theoretical_price"] = float(widened)
    result["quote_amount"] = float(Decimal("100") / Decimal("1.2"))
    return result


def emit(source: Path, target: Path) -> dict[str, Any]:
    source_record = json.loads(source.read_text(encoding="utf-8").splitlines()[-1])
    center = Decimal(str(source_record["center_price"]))
    record = dict(source_record)
    record.update(
        {
            "timestamp": (
                datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            "plan_version": int(source_record.get("plan_version", 0)) + 1,
            "mode": "defensive",
            "plan_change_significant": True,
            "total_grid_width_pct": float(
                Decimal(str(source_record.get("total_grid_width_pct", "0.01")))
                * Decimal("1.5")
            ),
            "enabled": True,
            "valid": True,
            "buy_levels": [
                _widen_level(level, center, Decimal("1.5"))
                for level in source_record["buy_levels"][:3]
            ],
            "sell_levels": [
                _widen_level(level, center, Decimal("1.5"))
                for level in source_record["sell_levels"][:3]
            ],
        }
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
    return {
        "target": str(target),
        "plan_version": record["plan_version"],
        "mode": record["mode"],
        "buy_levels": len(record["buy_levels"]),
        "sell_levels": len(record["sell_levels"]),
        "quote_amount": record["buy_levels"][0]["quote_amount"],
        "total_grid_width_pct": record["total_grid_width_pct"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(emit(args.source, args.target), sort_keys=True))


if __name__ == "__main__":
    main()
