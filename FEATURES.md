# Features Added (2026-03-25 to 2026-03-27)

This summary is based on commits dated March 25 through March 27, 2026. Docs-only and plan-only commits are intentionally omitted.

## Transaction Engine And Workflows

- Added fee burner sell-source support across scanning, persistence, transaction evaluation, and the UI, so fee burner inventories can be discovered and processed alongside strategy sources. (`1aa5cb6`)

- Added an auction deploy helper and fixed fee burner approval handling for deploy flows. (`faaf967`)
- Added strategy auction deploy actions in the UI, with wallet-driven deployment from the dashboard. (`2a6251f`)
- Added automatic settlement for stale auctions inside the scanner. (`a92527b`)
- Added explicit auction pricing and settle workflows in the transaction service, plus a `sweep_and_settle.py` helper script. (`e2e4dbe`)
- Added token-level USD kick sizing through the pricing policy layer, so kick sizing can vary by token instead of relying on a single global rule. (`57d23ca`, `d379055`)
- Added a transaction source-type filter so operators can target specific source classes such as fee burners. (`f692c5d`)

## Safety And Operator Guardrails

- Prevented kicking want tokens, avoiding invalid sell-side candidates. (`ef4698d`)
- Limited transaction runs to one candidate per auction and made deferred same-auction candidates explicit in the UX. (`9777b95`, `4f8122b`)
- Skipped kicks when the resolved sell token symbol matches the want token, adding another guardrail against invalid candidates. (`78d4fbb`)
- Kept transaction processing moving after a declined confirmation instead of aborting the whole run. (`03e386b`)
- Preserved attempted counts for skipped aborts, improving run accounting. (`24430c6`)
- Parallelized active auction state reads to speed up candidate evaluation. (`c52507c`)
- Improved revert decoding and estimate error messages for failed transaction attempts. (`5f5bb49`, `3de4e8c`)

## Transaction CLI And Confirmation UX

- Improved the interactive transaction CLI with richer confirmation output and clearer operator-facing summaries. (`8b8e0fc`)
- Clarified transaction candidate ordering so shortlist behavior is easier to understand. (`948887f`)
- Added quote-detail confirmation output that distinguishes cached valuations from live quote results, including clearer start and floor pricing presentation. (`db08409`, `a90dffe`, `96abd81`)
- Improved the fee-burner transaction confirmation layout and same-auction defer messaging so operators can see why only one candidate is actionable at a time. (`4f8122b`, `5895ceb`)

## Dashboard And UI

- Renamed the package and UI from `factory-dashboard` to `tidal`. (`f236f67`)
- Added AuctionScan links to the logs page, then refined the behavior to hide failed or empty placeholders and surface kick actions in status badges. (`03534f5`, `d9911f9`, `7770413`, `3996faf`, `c73b9f3`)
- Renamed the kick log tab to `Logs`. (`a56a64c`)
- Added auction enabled-token status to the dashboard so sell-token availability is visible in the UI. (`3a0e319`)
- Hid zero-balance and tiny-value token entries from the dashboard for a cleaner balance view. (`20332c4`)
- Improved deploy UX with a clearer CTA, fixed table layout, and a confirmation modal. (`e5ce5ca`)
- Preferred Rabby for deploy wallet actions when available. (`7fd15ce`)
- Aligned the fee burner page more closely with the strategy layout, including header cleanup, right-side token balances, and integrated total display. (`716362c`, `5895ceb`, `518bd91`)
- Refined the logs page layout by reordering columns, removing the header AuctionScan icon, widening the time column, and truncating long source labels more cleanly. (`5895ceb`)
