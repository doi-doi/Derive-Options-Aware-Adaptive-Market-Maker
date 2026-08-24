import json
from decimal import Decimal

from tools.emit_stage5f_touch_plan import (
    derive_wire_price,
    emit,
    hummingbot_quantized_price,
)


def test_connector_precision_loss_is_reproducible_at_btc_price() -> None:
    assert hummingbot_quantized_price(Decimal("77549")) == Decimal("77549")
    assert hummingbot_quantized_price(Decimal("77551")) == Decimal("77551")
    assert derive_wire_price(Decimal("77549")) == Decimal("77550")
    assert derive_wire_price(Decimal("77551")) == Decimal("77550")


def test_touch_plan_selects_closest_representable_passive_prices(tmp_path) -> None:
    source = tmp_path / "source.jsonl"
    target = tmp_path / "target.jsonl"
    source.write_text(
        json.dumps(
            {
                "plan_version": 7,
                "mode": "normal",
                "buy_levels": [{"side": "buy", "level_index": 0, "quote_amount": 100}],
                "sell_levels": [{"side": "sell", "level_index": 0, "quote_amount": 100}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = emit(source, target, Decimal("77549"), Decimal("77550"))
    record = json.loads(target.read_text(encoding="utf-8").splitlines()[-1])

    assert result["validation_only"] is True
    assert result["plan_version"] == 8
    assert result["buy_wire_price"] == 77540.0
    assert result["sell_wire_price"] == 77550.0
    assert record["validation_stage"] == "stage5f"
    assert record["buy_levels"][0]["quote_amount"] == 100
    assert record["sell_levels"][0]["quote_amount"] == 100
