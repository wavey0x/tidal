# New Auction Integration Plan

## Goal

Update the scanner and dashboard so auction detection uses only the new auction factory, enforces that an auction is bound to the strategy via `auction.receiver()`, persists `auction.version()`, and shows that version in the dashboard UI without any UI-side chain calls.

## Final Policy

- Auction discovery uses exactly one configured factory: the new factory.
- A strategy-to-auction match is valid only when all of the following hold:
  - the auction was returned by `getAllAuctions()` from the configured factory
  - `auction.governance()` equals the required governance address
  - `auction.want()` equals `strategy.want()`
  - `auction.receiver()` equals the strategy address
- `auction.version()` is read during scan, persisted to the database, exposed by the API, and rendered by the UI.
- The UI does not make any on-chain or extra backend calls for auction version data.

## Assumptions

- The new auction factory uses the same governance address (`0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b` in `factory_dashboard/constants.py`). If this changes, `YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS` must be updated as well.
- The on-chain `version()` return value is a short string (e.g. `"1.0.1.cc"`). Verify the actual format before finalizing badge width.

## Current State

The current scanner logic lives in `factory_dashboard/scanner/auction_mapper.py` and works like this:

- fetch all auctions from one factory
- read each auction's `governance()` and `want()`
- keep the last auction per `want`
- read each strategy's `want()`
- map `strategy.want()` to `auction.want()`

This is not sufficient for the new policy because `receiver == strategy` cannot be enforced with a global `want -> auction` map alone.

## Scope

In scope:

- scanner auction matching logic
- auction ABI updates
- database schema for persisted auction version
- API payload extension for `auctionVersion`
- UI rendering of a subtle version badge
- tests and docs

Out of scope:

- multi-factory support
- UI-side RPC or direct contract calls
- transaction service matching logic changes

## Recommended UI Treatment

Use a very small, subtle badge next to the auction listing, not a `" | 1.0.1.cc"` string suffix.

Reasoning:

- the auction column is already tight
- a badge is easier to scan and less likely to collide with the copy affordance
- badge styling can stay muted in both themes while remaining legible

## Implementation Plan

### 1. Keep single-factory configuration

Do not add multi-factory logic.

Use the existing setting:

- `auction_factory_address` in `factory_dashboard/config.py`

Use a YAML config file for scanner deployment instead of ad hoc env-only overrides. There is already support for `--config`, but there is no checked-in YAML file today.

Recommended config shape:

```yaml
rpc_url: https://...
db_path: ./factory_dashboard.db
auction_factory_address: 0xNEW_FACTORY
multicall_enabled: true
multicall_auction_batch_calls: 500
```

Implementation note:

- if a checked-in example is desired, add a non-secret sample such as `scanner.mainnet.example.yaml`
- if deployment config is private, keep the real file out of git and document the expected keys in `README.md`

### 2. Extend the auction ABI

Update `factory_dashboard/chain/contracts/abis.py` so `AUCTION_ABI` includes:

- `receiver() -> address`
- `version() -> string`

`version()` is informational and persisted for display. It is not part of the match predicate.

### 3. Change the auction metadata shape

The current mapper stores auction metadata in a loose dict with `governance` and `want` only. Extend this to include:

- `governance: str | None`
- `want: str | None`
- `receiver: str | None`
- `version: str | None`

Recommended implementation:

- replace the anonymous nested dict with a small `@dataclass(slots=True)` `AuctionMetadata` class in `factory_dashboard/scanner/auction_mapper.py`, consistent with the existing `AuctionMappingRefreshResult` pattern

Reasoning:

- mixed address and string fields will otherwise make decoding and type handling brittle
- `version()` decoding is different from address decoding

### 4. Update multicall and direct-read logic

In `factory_dashboard/scanner/auction_mapper.py`, update `_read_auction_metadata_many()` to read:

- `governance()`
- `want()`
- `receiver()`
- `version()`

This affects both code paths:

- direct per-contract calls when multicall is disabled
- batched multicall when multicall is enabled

Implementation details:

- keep `governance`, `want`, and `receiver` normalized with `normalize_address`
- decode `version` as `string`, not `address`
- if an address read fails, keep that field as `None`
- if `version()` fails, keep `version` as `None`

Critical refactor — multicall decoding:

The current multicall result loop (`auction_mapper.py:148-157`) uniformly decodes every field as `abi_decode(["address"], ...)` and applies `normalize_address`. This must be branched by field type:

- for `logical_key[1]` in `("governance", "want", "receiver")`: decode as `["address"]`, apply `normalize_address`
- for `logical_key[1] == "version"`: decode as `["string"]`, store raw string, skip `normalize_address`

Recommended approach: branch on `logical_key[1]` inside the result loop, or extract a small helper that maps field name to ABI type and post-processing.

Batch size note:

Going from 2 calls per auction (`governance`, `want`) to 4 (`governance`, `want`, `receiver`, `version`) halves the effective auctions per batch at the same `multicall_auction_batch_calls` setting. At 500 calls: 250 auctions/batch becomes 125. This is fine for current auction counts but operators should be aware if the number of auctions grows significantly.

Direct-read path note:

The non-multicall path (`auction_mapper.py:105-120`) will also double its per-auction RPC calls (2 to 4). This path is only used when `multicall_enabled: false`, which is not the default.

### 5. Replace the current matching algorithm

The current algorithm builds a global `want_to_auction` map. That needs to change.

Recommended algorithm:

1. Read all auction metadata from the configured factory.
2. Build a lookup keyed by `(want_address, receiver_address)`.
3. Iterate auction addresses in factory order and overwrite on each valid candidate so the latest auction for the same `(want, receiver)` wins.
4. Read all strategy `want()` values.
5. For each strategy, look up `(strategy_want, strategy_address)` in the auction map.
6. Persist the selected auction address and version if a match exists; otherwise persist `NULL`s.

Validity rules for an auction candidate before it enters the lookup:

- `governance == required_governance`
- `want` is not `None`
- `want != ZERO_ADDRESS`
- `receiver` is not `None`
- `receiver != ZERO_ADDRESS`

Diagnostic counters:

- rename `governance_allowed_auction_count` to `valid_auction_count` (or add a separate counter) since this now reflects auctions passing governance + want + receiver checks, not just governance. Update the corresponding log field in `service.py` (`governance_allowed_auctions`) to match.
- consider adding a `receiver_filtered_count` counter tracking auctions that passed governance + want but failed receiver validation — useful for debugging mapping gaps after the switch.

Why this algorithm:

- it enforces the receiver binding exactly
- it preserves the existing "latest by factory order wins" rule
- it stays O(number of auctions + number of strategies)

### 6. Extend mapper results to include version

Extend `AuctionMappingRefreshResult` in `factory_dashboard/scanner/auction_mapper.py` to include:

- `strategy_to_auction_version: dict[str, str | None]`

Existing fields should remain:

- `strategy_to_auction`
- `strategy_to_want`
- counts and source metadata

The scan stage should receive both the chosen auction address and chosen auction version from the mapper in one result object.

### 7. Persist auction version in the database

Add a new nullable column to `strategies`:

- `auction_version`

Files:

- `factory_dashboard/persistence/models.py`
- new Alembic migration, likely `alembic/versions/0009_add_auction_version_to_strategies.py`

Migration requirements:

- nullable string/text column is sufficient
- no backfill required

Repository changes:

- extend `StrategyRepository.set_auction_mappings()` in `factory_dashboard/persistence/repositories.py` to accept `strategy_to_auction_version`
- on successful refresh, write `auction_address`, `want_address`, `auction_version`, `auction_updated_at`, and clear `auction_error_message`
- when a strategy has no matched auction in a successful refresh, explicitly write `auction_address = NULL` and `auction_version = NULL`

Failure behavior:

- keep the current cache-on-refresh-failure behavior
- on refresh failure, do not overwrite previously stored `auction_address` or `auction_version`
- only set `auction_updated_at` and `auction_error_message`, as today
- `mark_auction_refresh_failed()` in `repositories.py` needs no changes — it already only writes `auction_updated_at` and `auction_error_message`, so `auction_version` is preserved by omission

### 8. Wire the scanner to store version data

Update `factory_dashboard/scanner/service.py` so the mapping stage passes the new version map into `StrategyRepository.set_auction_mappings()`.

No changes are needed in the transaction service because it already consumes the persisted strategy mapping from the database.

### 9. Expose `auctionVersion` from the API

The UI must receive version data from the existing dashboard API response. The UI must not make any extra call for this.

Required API change:

- extend the row payload to include `auctionVersion`

Expected row shape addition:

```json
{
  "auctionAddress": "0x...",
  "auctionVersion": "1.0.1.cc"
}
```

Important repository note:

- this repo does not appear to contain the read-only API implementation
- the API work must be done in the service that reads SQLite and serves `GET /factory-dashboard`

API behavior requirements:

- if `strategies.auction_version` is `NULL`, return `null`
- do not derive version in the API layer from on-chain calls
- just select the persisted DB field and serialize it

### 10. Render the version badge in the UI

Update the auction cell renderer in `ui/src/App.jsx`.

Current component:

- `AuctionAddressCell({ address, kicks, nowMs, isExpanded, onToggleExpand })`

Planned component shape:

- add a `version` prop
- render a small badge adjacent to the address only when `version` is present

Recommended markup approach:

- keep `AddressCopy` unchanged
- wrap the top auction line in a compact inline row
- render the badge immediately after the address/copy affordance

Recommended styling in `ui/src/styles.css`:

- muted border
- muted text
- small padding
- rounded pill or squared micro-badge
- slightly smaller than body monospace text

Constraints:

- maintain contrast in both themes
- do not break the copy affordance styling
- keep the badge subtle enough that the address remains primary
- keep the column width stable; only widen if necessary after visual check

The UI should simply read `row.auctionVersion` from the API payload and pass it to `AuctionAddressCell`.

### 11. Update tests

#### Unit tests

Update `tests/unit/test_strategy_auction_mapper.py` to cover:

- matching requires `receiver == strategy`
- receiver mismatch results in no mapping
- latest valid auction still wins when two auctions share the same `(want, receiver)`
- governance filter still applies
- version is returned for the chosen auction
- version `None` when `version()` call fails — auction still matches, just with `None` version
- multicall path decodes both address fields and string version correctly

Recommended additions to the fake clients:

- `FakeAuctionFunctions`: add `receiver()` and `version()` methods
- `FakeWeb3Client`: add `auction_receivers: dict[str, str]` and `auction_versions: dict[str, str]` constructor params, wire them into `call()`
- `FakeMulticallClient`: support multicall return data for string fields (use `abi_encode(["string"], [...])` for version responses)

#### Integration tests

Update `tests/integration/test_scanner_service.py` to cover:

- `auction_version` is persisted on matched strategies
- `auction_version` is `NULL` for unmatched strategies
- on mapping refresh failure, previously persisted `auction_version` is retained

Required fake update:

- `FakeStrategyAuctionMapper.refresh_for_strategies()` must populate the new `strategy_to_auction_version` field in `AuctionMappingRefreshResult` — without this, existing integration tests will break at dataclass construction

#### UI verification

This repo does not currently appear to have frontend component tests. Use build plus manual verification:

- run `npm run build` in `ui/`
- verify light theme
- verify dark theme
- verify badge contrast and spacing
- verify auction rows without version render unchanged

### 12. Update docs

Update `README.md`:

- scanner uses one configured factory only
- matching requires `governance`, `want`, and `receiver == strategy`
- version is scanned and persisted
- dashboard reads version from API payload

If a YAML config example is added, document it there as well.

## Files Expected To Change

In this repo:

- `factory_dashboard/chain/contracts/abis.py`
- `factory_dashboard/scanner/auction_mapper.py`
- `factory_dashboard/scanner/service.py`
- `factory_dashboard/persistence/models.py`
- `factory_dashboard/persistence/repositories.py`
- `alembic/versions/0009_add_auction_version_to_strategies.py`
- `tests/unit/test_strategy_auction_mapper.py`
- `tests/integration/test_scanner_service.py`
- `ui/src/App.jsx`
- `ui/src/styles.css`
- `README.md`

Outside this repo:

- the read-only dashboard API service that selects rows from SQLite and returns `GET /factory-dashboard`

## Suggested Implementation Order

1. Add ABI entries for `receiver()` and `version()`.
2. Add `auction_version` schema support via model and Alembic migration.
3. Refactor `StrategyAuctionMapper` metadata reading and matching algorithm.
4. Extend `AuctionMappingRefreshResult` and repository write path to persist version.
5. Update unit tests for the mapper.
6. Update integration scanner tests.
7. Update the external API payload to include `auctionVersion`.
8. Update `ui/src/App.jsx` and `ui/src/styles.css` to render the badge.
9. Update `README.md`.
10. Run verification.

## Deployment Order

This work spans the scanner repo, an external API service, and the UI. Deploy in this order:

1. **Database migration** — run `factory-dashboard db migrate` to add `auction_version` column
2. **Scanner deploy** — deploy updated scanner code; it begins writing `auction_version` to the DB
3. **API deploy** — deploy the external API service that reads `auction_version` from SQLite and serializes it as `auctionVersion`; safe to deploy before scanner (the column exists but is `NULL`, API returns `null`)
4. **UI deploy** — deploy the updated UI that reads `row.auctionVersion` and renders the badge; must deploy after the API, otherwise the UI reads `undefined`

Steps 1-2 can be combined. Steps 3-4 are independent of scanner timing but must be ordered relative to each other.

## Verification Checklist

Backend:

- `pytest tests/unit/test_strategy_auction_mapper.py tests/integration/test_scanner_service.py`
- `factory-dashboard db migrate`
- `factory-dashboard scan once --config <new-factory-config>`

Expected backend outcomes:

- strategies only map to auctions returned by the configured new factory
- a strategy with matching `want` but mismatched `receiver` does not get an auction
- matched strategies persist `auction_address`, `want_address`, and `auction_version`
- unmatched strategies persist `auction_address = NULL` and `auction_version = NULL`

API:

- `GET /factory-dashboard` rows include `auctionVersion`
- `auctionVersion` is `null` when absent in SQLite

UI:

- `cd ui && npm run build`
- visually verify the auction version badge in light and dark themes
- verify no extra network requests are introduced for version lookup

## Acceptance Criteria

- Scanner uses only the configured new factory for auction discovery.
- Strategy-to-auction matches require `governance`, `want`, and `receiver == strategy`.
- Auction version is read from chain during scan and persisted in `strategies.auction_version`.
- API returns `auctionVersion` from persisted scan data.
- Dashboard shows a subtle auction version badge next to the auction listing.
- UI performs no on-chain calls and no follow-up fetches for version data.
