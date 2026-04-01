"""Recovery planning for stale empty auction lots revived by global param changes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from eth_utils import to_checksum_address

from tidal.chain.contracts.abis import AUCTION_ABI
from tidal.normalizers import normalize_address
from tidal.transaction_service.types import KickRecoveryPlan

WAD = 10**18
RAY = 10**27
_RAY_BPS_SCALE = 10**23


@dataclass(frozen=True, slots=True)
class AuctionGlobals:
    starting_price: int
    minimum_price: int
    step_decay_rate: int
    step_duration: int
    auction_length: int
    want_scaler: int


@dataclass(frozen=True, slots=True)
class AuctionTokenState:
    token_address: str
    kicked_at: int
    scaler: int
    initial_available: int
    auction_balance: int


def _wdiv(x: int, y: int) -> int:
    return (x * WAD + y // 2) // y


def _rmul(x: int, y: int) -> int:
    return (x * y + RAY // 2) // RAY


def _rpow(x: int, n: int) -> int:
    z = x if n % 2 else RAY
    n //= 2
    while n:
        x = _rmul(x, x)
        if n % 2:
            z = _rmul(z, x)
        n //= 2
    return z


def _public_price(token_state: AuctionTokenState, globals_state: AuctionGlobals, *, timestamp: int) -> int:
    available_scaled = token_state.initial_available * token_state.scaler
    if available_scaled == 0 or token_state.kicked_at == 0:
        return 0
    if timestamp < token_state.kicked_at:
        return 0

    seconds_elapsed = timestamp - token_state.kicked_at
    if seconds_elapsed > globals_state.auction_length:
        return 0

    steps = seconds_elapsed // globals_state.step_duration
    ray_multiplier = RAY - (globals_state.step_decay_rate * _RAY_BPS_SCALE)
    decay_multiplier = _rpow(ray_multiplier, steps)
    initial_price = _wdiv(globals_state.starting_price * WAD, available_scaled)
    current_price = _rmul(initial_price, decay_multiplier)
    if current_price < globals_state.minimum_price:
        return 0
    return current_price // globals_state.want_scaler


def _would_be_active(token_state: AuctionTokenState, globals_state: AuctionGlobals, *, timestamp: int) -> bool:
    return _public_price(token_state, globals_state, timestamp=timestamp) > 0


def build_recovery_plan(
    token_states: list[AuctionTokenState],
    current_globals: AuctionGlobals,
    *,
    timestamp: int,
    proposed_starting_price: int,
    proposed_minimum_price: int,
    proposed_step_decay_rate: int,
) -> KickRecoveryPlan:
    remaining = sorted(token_states, key=lambda item: item.token_address.lower())

    start_globals = replace(current_globals, starting_price=proposed_starting_price)
    settle_after_start = tuple(
        token.token_address
        for token in remaining
        if _would_be_active(token, start_globals, timestamp=timestamp)
    )
    remaining = [token for token in remaining if token.token_address not in settle_after_start]

    min_globals = replace(start_globals, minimum_price=proposed_minimum_price)
    settle_after_min = tuple(
        token.token_address
        for token in remaining
        if _would_be_active(token, min_globals, timestamp=timestamp)
    )
    remaining = [token for token in remaining if token.token_address not in settle_after_min]

    decay_globals = replace(min_globals, step_decay_rate=proposed_step_decay_rate)
    settle_after_decay = tuple(
        token.token_address
        for token in remaining
        if _would_be_active(token, decay_globals, timestamp=timestamp)
    )

    return KickRecoveryPlan(
        settle_after_start=settle_after_start,
        settle_after_min=settle_after_min,
        settle_after_decay=settle_after_decay,
    )


async def plan_prepared_kick_recovery(
    *,
    prepared_kick,
    web3_client,
    erc20_reader,
) -> KickRecoveryPlan | None:
    auction_address = normalize_address(prepared_kick.candidate.auction_address)
    auction_contract = web3_client.contract(auction_address, AUCTION_ABI)

    enabled_tokens_raw, auction_length, starting_price, minimum_price, step_decay_rate, step_duration, want_address = await asyncio.gather(
        web3_client.call(auction_contract.functions.getAllEnabledAuctions()),
        web3_client.call(auction_contract.functions.auctionLength()),
        web3_client.call(auction_contract.functions.startingPrice()),
        web3_client.call(auction_contract.functions.minimumPrice()),
        web3_client.call(auction_contract.functions.stepDecayRate()),
        web3_client.call(auction_contract.functions.stepDuration()),
        web3_client.call(auction_contract.functions.want()),
    )

    enabled_tokens = [normalize_address(token) for token in enabled_tokens_raw]
    if not enabled_tokens:
        return None

    want_decimals = await erc20_reader.read_decimals(normalize_address(str(want_address)))
    globals_state = AuctionGlobals(
        starting_price=int(starting_price),
        minimum_price=int(minimum_price),
        step_decay_rate=int(step_decay_rate),
        step_duration=int(step_duration),
        auction_length=int(auction_length),
        want_scaler=10 ** (18 - int(want_decimals)),
    )
    timestamp = await web3_client.get_latest_block_timestamp()

    async def _read_token_state(token_address: str) -> AuctionTokenState | None:
        kicked_at, scaler, initial_available = await web3_client.call(
            auction_contract.functions.auctions(to_checksum_address(token_address))
        )
        kicked_at = int(kicked_at)
        scaler = int(scaler)
        initial_available = int(initial_available)
        if kicked_at == 0 or scaler == 0 or initial_available == 0:
            return None
        if timestamp - kicked_at > globals_state.auction_length:
            return None
        if prepared_kick.settle_token and normalize_address(prepared_kick.settle_token) == token_address:
            return None

        auction_balance = await erc20_reader.read_balance(token_address, auction_address)
        if int(auction_balance) != 0:
            return None

        return AuctionTokenState(
            token_address=token_address,
            kicked_at=kicked_at,
            scaler=scaler,
            initial_available=initial_available,
            auction_balance=int(auction_balance),
        )

    token_states = [
        token_state
        for token_state in await asyncio.gather(*(_read_token_state(token) for token in enabled_tokens))
        if token_state is not None
    ]
    if not token_states:
        return None

    plan = build_recovery_plan(
        token_states,
        globals_state,
        timestamp=timestamp,
        proposed_starting_price=int(prepared_kick.starting_price_unscaled),
        proposed_minimum_price=int(prepared_kick.minimum_price_scaled_1e18),
        proposed_step_decay_rate=int(prepared_kick.step_decay_rate_bps),
    )
    return None if plan.is_empty else plan
