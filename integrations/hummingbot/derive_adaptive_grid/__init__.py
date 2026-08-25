"""Stage 5 controller package.

Hummingbot discovers controller configuration classes by inspecting the
controller package itself.  Re-export the adapter eagerly in the bot runtime
so that discovery can see :class:`DeriveAdaptiveGridConfig`; retain the lazy
fallback for the host-only reconciliation tests where Hummingbot is absent.
"""

__all__ = [
    "DeriveAdaptiveGrid",
    "DeriveAdaptiveGridConfig",
    "DeriveAdaptiveGridPortfolio",
    "DeriveAdaptiveGridPortfolioConfig",
]

try:
    from .derive_perpetual_signing_compat import (
        install_derive_testnet_post_only_compatibility,
        install_derive_testnet_signing_compatibility,
    )
    from .orderbook_snapshot_compat import install_derive_orderbook_snapshot_compatibility

    install_derive_testnet_signing_compatibility()
    install_derive_testnet_post_only_compatibility()
    install_derive_orderbook_snapshot_compatibility()
    from .derive_adaptive_grid import DeriveAdaptiveGrid, DeriveAdaptiveGridConfig
    from .derive_adaptive_grid_portfolio import (
        DeriveAdaptiveGridPortfolio,
        DeriveAdaptiveGridPortfolioConfig,
    )
except ModuleNotFoundError as exc:
    if exc.name != "hummingbot":
        raise

    def __getattr__(name: str):
        if name in __all__:
            from .derive_adaptive_grid import DeriveAdaptiveGrid, DeriveAdaptiveGridConfig
            from .derive_adaptive_grid_portfolio import (
                DeriveAdaptiveGridPortfolio,
                DeriveAdaptiveGridPortfolioConfig,
            )

            return {
                "DeriveAdaptiveGrid": DeriveAdaptiveGrid,
                "DeriveAdaptiveGridConfig": DeriveAdaptiveGridConfig,
                "DeriveAdaptiveGridPortfolio": DeriveAdaptiveGridPortfolio,
                "DeriveAdaptiveGridPortfolioConfig": DeriveAdaptiveGridPortfolioConfig,
            }[name]
        raise AttributeError(name)
