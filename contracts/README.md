# AuctionKicker (Foundry)

This project contains a minimal Yearn `AuctionKicker` mech contract that can be
allowlisted in TradeHandler and then atomically:

1. transfer CRV from a strategy to the Auction
2. set Auction starting price
3. kick the Auction

The mech builds a fixed 3-step Weiroll command program from typed inputs and
does not accept arbitrary command/state payloads. `kick(...)` receives an
auction parameter and validates `auction.want() == strategy.want()`.

## Requirements

- Foundry installed
- `MAINNET_RPC_URL` set to a mainnet RPC endpoint

## Run tests

```bash
MAINNET_RPC_URL=https://your-mainnet-rpc forge test -vvv
```

## Notes

- The mech must be allowlisted in `TradeHandler.mechs`.
- Command packing uses TradeHandler VM's short command format:
  `selector(4) | flags(1) | arg slots(6) | out slot(1) | target(20)`.
- Tests run on a mainnet fork and use:
  - governance impersonation + `TradeHandler.addMech(mech)` to allowlist the mech
  - `deal` to fund strategy CRV balance deterministically
  - `stdstore` to set CRV allowance (`strategy -> tradeHandler`)
