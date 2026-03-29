# KICK_PLAN

## Problem

`TIDAL_API_REQUEST_TIMEOUT_SECONDS` only changes how long the operator CLI waits for the control-plane response. It does not fix the underlying issue when `/api/v1/tidal/kick/prepare` eagerly walks an entire shortlist and performs live prepare work for every candidate before returning anything.

That is the wrong place to spend latency.

Cached token prices are already good enough to rank kick opportunities. The expensive work is the live pre-transaction quote and pricing calculation, and that data should be fetched only when a candidate is actually about to be shown to the operator and sent on chain.

## Current Diagnosis

`build_shortlist()` already ranks candidates from cached balances and cached token prices in SQLite. The core ordering logic was not the main inefficiency.

The expensive path was the API-backed operator flow:

1. shortlist candidates
2. inspect the entire ready set
3. call `prepare_kick()` across the full selected set
4. quote every candidate up front
5. only then return a preview to the CLI

That design creates one large synchronous request whose cost scales with shortlist size.

## Target Design

The operator kick flow should be two-stage.

1. `inspect` or `list` candidates using cached prices only
2. sort by cached USD value
3. present candidates in that order
4. prepare exactly one candidate at a time
5. fetch the live quote only for that candidate
6. confirm and optionally broadcast
7. move to the next candidate

The ordering decision stays cheap and stable. The on-chain pricing inputs stay fresh.

## Implemented Now

This repo now moves the operator flow in that direction.

1. `tidal kick run` performs an inspect step first and disables live inspection for that run path with `includeLiveInspection=false`.
2. The API shortlist/inspect/prepare path now supports exact `tokenAddress` targeting, so a single `(source, auction, token)` candidate can be prepared without re-processing the whole shortlist.
3. Operator broadcast mode now prepares candidates one-by-one in cached order and only quotes the candidate currently being confirmed.
4. Operator dry-run no longer eagerly prepares the whole shortlist. It shows the ranked ready set and makes the just-in-time quote behavior explicit.

## Remaining Work

1. Add a dedicated lightweight `kick/list` API that never performs live auction RPC reads. `kick/inspect` should remain the richer explainability endpoint.
2. Refill from the next cached candidate when an exact candidate falls out during prepare because of a race, stale balance, or an auction state change.
3. Consider a very short-lived quote cache only for the same exact candidate in the same operator session. This is an optimization, not a correctness dependency.
4. Add timing metrics for shortlist, inspect, prepare, quote, gas estimation, and broadcast so regressions are obvious.
5. Decide whether the daemon path should keep eager batch preparation for throughput or adopt the same just-in-time model behind a configurable mode flag.

## Guardrails

1. Never use live quote data to rank the shortlist.
2. Never block the operator on quotes for candidates they may never approve.
3. Keep exact-candidate prepare idempotent and cheap enough that retrying the next candidate is safe.
4. Treat stale shortlist data as acceptable for ordering, but not for transaction pricing.
