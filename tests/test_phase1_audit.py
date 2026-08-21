from __future__ import annotations

import unittest

from derive_options_mm.derive_public import ALLOWED_PUBLIC_METHODS, DerivePublicClient
from derive_options_mm.phase1_audit import iso_utc, summarize_trade_candles


class Phase1AuditTests(unittest.TestCase):
    def test_iso_utc_accepts_seconds_and_milliseconds(self) -> None:
        self.assertEqual(iso_utc(1_700_000_000), "2023-11-14T22:13:20Z")
        self.assertEqual(iso_utc(1_700_000_000_000), "2023-11-14T22:13:20Z")

    def test_trade_candle_summary_excludes_zero_volume_backfill(self) -> None:
        rows = [
            {
                "timestamp_bucket": 1_700_000_000,
                "volume_contracts": "0",
                "volume_usd": "0",
            },
            {
                "timestamp_bucket": 1_700_086_400,
                "volume_contracts": "0.01",
                "volume_usd": "700",
            },
        ]
        summary = summarize_trade_candles(rows)
        self.assertEqual(summary["returned_rows"], 2)
        self.assertEqual(summary["nonzero_volume_rows"], 1)
        self.assertEqual(summary["usable_range"]["earliest"], "2023-11-15T22:13:20Z")

    def test_phase1_client_rejects_private_and_order_methods(self) -> None:
        client = DerivePublicClient(max_attempts=1)
        with self.assertRaises(ValueError):
            client.post("private/order", {})
        self.assertFalse(any(method.startswith("private/") for method in ALLOWED_PUBLIC_METHODS))
        self.assertFalse(
            any(
                "order" in method and method.endswith("/order")
                for method in ALLOWED_PUBLIC_METHODS
            )
        )


if __name__ == "__main__":
    unittest.main()
