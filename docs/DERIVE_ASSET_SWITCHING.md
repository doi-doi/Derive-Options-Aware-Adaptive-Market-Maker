# Switching the Derive execution asset

The portfolio controller accepts a configurable Derive perpetual universe in
`BASE-USDC` Hummingbot format. It no longer hard-codes BTC and HYPE. The
current testnet rollout uses:

```yaml
trading_pairs:
  - BTC-USDC
  - ETH-USDC
```

Each configured pair must also have explicit values in:

- `testnet_order_scales_by_pair`
- `pair_max_total_position_notional`
- `pair_max_side_position_notional`
- `portfolio_betas`

This is intentional: a new asset must not silently inherit an unverified order
size or risk limit. The Stage 4 plan stream must also publish a valid plan for
the selected pair.

## Safe live switch

1. Pause or stop the bot from the Hummingbot dashboard and wait for its
   unfilled entry orders to be cancelled.
2. Edit the portfolio controller YAML and replace the pair in `trading_pairs`
   and all four pair-specific maps. If BTC is removed entirely, also change
   the compatibility `trading_pair` field to one of the remaining pairs.
3. Confirm `connector_name: derive_perpetual_testnet`,
   `execution_enabled: true`, `post_only: true`,
   `execution_max_levels_per_side: 1`, and `allow_mainnet_trading: false`.
4. Make sure the read-only Stage 4 monitor is publishing the new pair and its
   plan is `valid: true` before execution resumes.
5. Restart the bot. A restart is required because Hummingbot initializes
   connector market subscriptions at strategy startup; changing the YAML
   while the bot is running does not subscribe a new market safely.
6. Verify `testnet_verified: true`, no duplicate level IDs, and one active
   maker entry per side for the selected pair.

The current live configuration is BTC-USDC plus ETH-USDC. Mainnet is not a
valid target for this rollout.
