"""Derive testnet action-signing compatibility for the pinned Hummingbot image.

The installed connector uses the newer trade-module address when signing the
legacy ``api-demo.lyra.finance`` testnet order endpoint.  Read-only requests
can still succeed, but authenticated order submissions are rejected with
``Signature does not match data``.  Keep this narrow runtime adapter beside
the Stage 5 controller so the base Hummingbot image remains unchanged.
"""

from __future__ import annotations

from decimal import Decimal

LEGACY_TESTNET_DOMAIN_SEPARATOR = (
    "0x9bcf4dc06df5d8bf23af818d5716491b995020f377d3b7b64c29ed14e3dd1105"
)
LEGACY_TESTNET_TRADE_MODULE_ADDRESS = "0x87F2863866D85E3192a35A73b388BD625D83f2be"


def _is_testnet_domain(domain: object) -> bool:
    """Keep the legacy adapter limited to the installed testnet domain."""

    return str(domain or "").strip().lower() == "derive_perpetual_testnet"


def install_derive_testnet_signing_compatibility() -> None:
    """Use the legacy testnet trade module for authenticated order actions."""

    from hummingbot.connector.derivative.derive_perpetual import derive_perpetual_constants
    from hummingbot.connector.derivative.derive_perpetual.derive_perpetual_auth import (
        DerivePerpetualAuth,
    )
    from hummingbot.connector.derivative.derive_perpetual.derive_perpetual_web_utils import (
        MAX_INT_32,
        get_action_nonce,
    )
    from hummingbot.connector.other.derive_common_utils import SignedAction, TradeModuleData

    if getattr(DerivePerpetualAuth, "_codex_testnet_signing_patch_applied", False):
        return

    original_sign = DerivePerpetualAuth.sign

    def _canonical_wire_decimal(value: Decimal) -> str:
        return format(Decimal(value).quantize(Decimal("1e-18")), "f")

    def _patched_sign(self: DerivePerpetualAuth, params):
        if not _is_testnet_domain(self._domain):
            return original_sign(self, params)

        action = SignedAction(
            subaccount_id=int(self._sub_id),
            owner=self._api_key,
            signer=self.session_key_wallet.address,
            signature_expiry_sec=MAX_INT_32,
            nonce=get_action_nonce(),
            module_address=LEGACY_TESTNET_TRADE_MODULE_ADDRESS,
            module_data=TradeModuleData(
                asset_address=params["asset_address"],
                sub_id=int(params["sub_id"]),
                limit_price=Decimal(params["limit_price"]),
                amount=Decimal(params["amount"]),
                max_fee=Decimal(params["max_fee"]),
                recipient_id=int(params["recipient_id"]),
                is_bid=params["is_bid"],
            ),
            DOMAIN_SEPARATOR=LEGACY_TESTNET_DOMAIN_SEPARATOR,
            ACTION_TYPEHASH=derive_perpetual_constants.TESTNET_ACTION_TYPEHASH,
        )
        action.sign(self.session_key_wallet.key)
        signed = action.to_json()
        signed["limit_price"] = _canonical_wire_decimal(action.module_data.limit_price)
        signed["amount"] = _canonical_wire_decimal(action.module_data.amount)
        signed["max_fee"] = _canonical_wire_decimal(action.module_data.max_fee)
        return signed

    DerivePerpetualAuth.sign = _patched_sign
    DerivePerpetualAuth._codex_testnet_signing_patch_applied = True


def install_derive_testnet_post_only_compatibility() -> None:
    """Map Hummingbot LIMIT_MAKER requests to Derive's post-only wire flag.

    The pinned connector labels LIMIT_MAKER orders correctly for Hummingbot,
    but sends ``time_in_force=gtc`` to Derive.  Derive exposes post-only as a
    distinct wire value; leaving this as GTC would make the Stage 5 safety
    assertion depend on a connector implementation detail.  Keep the adapter
    limited to testnet limit orders and leave mainnet behavior untouched.
    """

    from hummingbot.connector.derivative.derive_perpetual import derive_perpetual_constants
    from hummingbot.connector.derivative.derive_perpetual.derive_perpetual_derivative import (
        DerivePerpetualDerivative,
    )

    if getattr(DerivePerpetualDerivative, "_codex_testnet_post_only_patch_applied", False):
        return

    original_api_post = DerivePerpetualDerivative._api_post

    async def _patched_api_post(self, *args, **kwargs):
        path_url = kwargs.get("path_url")
        if path_url is None and args:
            path_url = args[0]
        data = kwargs.get("data")
        if (
            _is_testnet_domain(self.domain)
            and path_url == derive_perpetual_constants.CREATE_ORDER_URL
            and isinstance(data, dict)
            and data.get("type") == "order"
            and data.get("order_type") == "limit"
            and data.get("time_in_force") == "gtc"
        ):
            patched_data = dict(data)
            patched_data["time_in_force"] = "post_only"
            kwargs["data"] = patched_data
        return await original_api_post(self, *args, **kwargs)

    DerivePerpetualDerivative._api_post = _patched_api_post
    DerivePerpetualDerivative._codex_testnet_post_only_patch_applied = True
