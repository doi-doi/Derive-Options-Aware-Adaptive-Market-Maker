"""Small read-only client for the public Derive REST API.

The allowlist is deliberate: Phase 1 must never reach a private or trading endpoint.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

ALLOWED_PUBLIC_METHODS = frozenset(
    {
        "public/get_all_instruments",
        "public/get_instruments",
        "public/get_funding_rate_history",
        "public/get_index_chart_data",
        "public/get_interest_rate_history",
        "public/get_liquidation_history",
        "public/get_maker_programs",
        "public/get_option_settlement_history",
        "public/get_spot_feed_history",
        "public/get_spot_feed_history_candles",
        "public/get_trade_history",
        "public/get_tickers",
        "public/get_time",
        "public/get_tradingview_chart_data",
    }
)


class DeriveAPIError(RuntimeError):
    """Raised when Derive returns an RPC error or an unusable response."""


@dataclass(frozen=True)
class DerivePublicClient:
    """Read-only HTTP client with bounded retries for public observations."""

    base_url: str = "https://api.lyra.finance"
    timeout_seconds: float = 60.0
    max_attempts: int = 3

    def post(self, method: str, params: dict[str, Any]) -> Any:
        if method not in ALLOWED_PUBLIC_METHODS:
            raise ValueError(f"Method is not approved for the Phase 1 read-only audit: {method}")

        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "derive-options-mm-phase1-audit/0.1",
            },
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.load(response)
                if payload.get("error"):
                    raise DeriveAPIError(str(payload["error"]))
                if "result" not in payload:
                    raise DeriveAPIError(f"Response has no result for {method}")
                return payload["result"]
            except (TimeoutError, urllib.error.URLError, DeriveAPIError) as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(0.5 * attempt)

        raise DeriveAPIError(f"{method} failed after {self.max_attempts} attempts: {last_error}")
