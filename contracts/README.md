# AuctionKicker (Foundry)

This project contains a minimal Yearn `AuctionKicker` mech contract that can be
allowlisted in TradeHandler and then atomically:

1. transfer a sell token from a source contract to the Auction
2. set Auction starting price
3. set Auction minimum price
4. kick the Auction

The mech builds a fixed 4-step Weiroll command program from typed inputs and
does not accept arbitrary command/state payloads. `kick(...)` receives an
explicit `source`, `auction`, `sellToken`, and `wantToken`, then validates
`auction.receiver() == source` and `auction.want() == wantToken`. The
TradeHandler is hardcoded to
`0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b`.

## Requirements

- Foundry installed
- `MAINNET_URL` set to a mainnet RPC endpoint

## Run tests

```bash
MAINNET_URL=https://your-mainnet-rpc forge test -vvv
```

## Deploy

```bash
cd contracts
forge script script/DeployAuctionKicker.s.sol:DeployAuctionKicker \
  --account wavey3 \
  --rpc-url $MAINNET_URL \
  --broadcast
```

The deploy script uses Forge's native signer flow with `vm.startBroadcast()`.
Because `~/.foundry/keystores/wavey3` is in the default keystore directory, the
simplest invocation is `--account wavey3`. `--account` expects the keystore
name, not the deployer address. If you prefer an explicit path, use
`--keystore ~/.foundry/keystores/wavey3` instead.

## Notes

- The mech must be allowlisted in `TradeHandler.mechs`.
- Command packing uses TradeHandler VM's short command format:
  `selector(4) | flags(1) | arg slots(6) | out slot(1) | target(20)`.
- Short-command packing is isolated in `src/utils/WeiRollCommandLib.sol`.
- Tests run on a mainnet fork and use:
  - governance impersonation + `TradeHandler.addMech(mech)` to allowlist the mech
  - `deal` to fund source token balances deterministically
  - `stdstore` to set sell-token allowance (`source -> tradeHandler`)
