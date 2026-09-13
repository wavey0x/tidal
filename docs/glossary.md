# Glossary

## Strategy

A Yearn strategy that may hold sellable reward or inventory tokens and may be associated with an auction through its receiver address.

## Fee burner

A configured non-strategy source that accumulates tokens and maps to an auction via `(receiver, want)`.

## Source

A generic sell-side origin in Tidal. A source is either a strategy or a fee burner.

## Auction

A Yearn auction contract that can receive a sell token lot, price that lot in terms of a want token, and decay over time.

## Want token

The token the auction expects in exchange for the sell token lot.

## Sell token

The token currently being considered for an auction kick from a given source.

## Kick

The action that starts a new auction lot with a computed sell amount, starting price, and minimum price.

## Prepare

The stateless step that validates a candidate and computes unsigned transaction inputs and a confirmation summary. Managed commands prepare locally under the shared execution lock.

## Broadcast

The act of sending a signed Ethereum transaction to the network.

## Receipt

The observed transaction result after broadcast, including status, block number, and gas used.

## Action

An intended operation such as kick, enablement or settlement. Preparation creates no durable action job. A managed submission records one transaction identity linked to all affected business operations.

## Activation

Local permission to execute, bound to the database UUID, chain, declared signers and checked account nonce. It is excluded from backups and recreated only by explicit native resume.

## Finality

Canonical finalized chain evidence matching a retained transaction's exact identity and intent. Receipt inclusion alone is provisional and continues to block that signer.

## Shortlist

The cached, ranked set of kick candidates produced from scanner data before cooldown checks and just-in-time preparation.

## Deferred same-auction candidate

A candidate that passes threshold checks but is not selected because another token on the same auction has a higher cached USD value.

## Cooldown

The guardrail that skips a `(source, token)` pair if it was kicked too recently.

## Pricing profile

The named configuration that defines start-price buffer, minimum-price buffer, and decay rate for a given `auction + sell token` combination.
