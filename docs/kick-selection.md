# Kick Selection

## Goal

The kick engine should answer:

1. which candidates are worth considering right now?
2. which single candidate per auction should be acted on first?
3. what should be prepared just in time before sending a transaction?

The system deliberately separates cached ranking from live transaction pricing.

Manual ignore rules and cooldown rules live under `kick:` in `config/server.yaml`.

## Shortlist Inputs

The shortlist is built from cached scanner data in SQLite:

- source balances
- sell-token USD prices
- auction mappings
- want token addresses
- enabled-token cache per auction

The shortlist currently includes both:

- strategies
- fee burners

## Freshness Requirements

Candidates only enter the shortlist when:

- the source has an auction address
- the source has a want token
- the sell token has `price_status == SUCCESS`
- the sell token has a cached `price_usd`
- the cached balance scan is fresh enough
- the cached sell-token price is fresh enough

Freshness is controlled by:

- `txn_max_data_age_seconds`

## Exclusions Before Ranking

A candidate is excluded early if:

- sell token address equals want token address
- sell token symbol matches want token symbol
- cached USD value is below `txn_usd_threshold`
- enabled-token scan for that auction succeeded and the token is not currently enabled

Optional filters are then applied:

- `source_type`
- `source_address`
- `auction_address`
- `token_address`

## Ranking

The shortlist sorts candidates by:

1. highest USD value first
2. auction address
3. source address
4. token address

The USD value here is:

```text
cached normalized balance * cached sell-token USD price
```

No live quote is involved in this ranking step.

## One Candidate Per Auction

After sorting, Tidal keeps only the best candidate per auction for the actionable set.

Why:

- one auction can only carry one active lot at a time
- preparing multiple same-auction candidates in one evaluation cycle is misleading

Any additional above-threshold tokens on the same auction are tracked as:

- deferred same-auction candidates

This is why `kick inspect --show-all` may show more interesting tokens than `kick run` can act on immediately.

## Ignore Rules

Before same-auction ranking, Tidal applies any manual `ignore` rules from the server `kick:` policy.

An ignore rule can target:

- one source
- one auction
- one specific `(auction, token)` combination

Ignored candidates are tracked as:

- ignored

This is intentional. An ignored high-USD token should not block a lower-USD token from the same auction from becoming actionable.

## Cooldown Check

After ignore rules, Tidal checks recent kick history for the same `(auction, token)` pair.

If the pair was kicked too recently, it is marked as:

- cooldown

Cooldown is controlled by:

- `cooldown_minutes`
- optional per-`(auction, token)` rules in `cooldown`

## Just-In-Time Preparation

Once a candidate is selected for preparation, Tidal does the expensive work only for that exact candidate:

1. read the live source balance
2. apply token-specific USD kick cap if configured
3. skip if the live value falls below threshold
4. fetch a live quote for the exact sell amount
5. derive start price and minimum price from the live quote
6. estimate gas and show confirmation

This keeps CLI client latency proportional to the candidate being acted on, not to the whole shortlist.

## CLI Client Flow Versus Daemon Flow

The CLI client API-backed flow is intentionally one-by-one:

- inspect using cached ordering
- prepare one candidate
- confirm and optionally broadcast
- move to the next candidate

That means cached prices are used for ranking, while quote freshness is preserved for the actual transaction.

## Why A Candidate Can Fall Out During Prepare

A candidate that looked ready during shortlist time may still be skipped later because:

- live balance is lower than cached balance
- token sizing cap pushes it below threshold
- quote API fails
- Curve quote is missing while strict mode is enabled
- auction state changed
- the lot should be settled instead of kicked

This is expected behavior. Cached shortlist data is advisory for ordering, not a guarantee that the candidate is still actionable.

## Mental Model

Use this rule:

```text
cached data decides order
live data decides transaction contents
```
