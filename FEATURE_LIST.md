# Feature List

## Future Implementation

### Action Authorization Hardening

Current v1 assumption:

- Any valid Tidal API key is a fully trusted operator key.
- Authenticated action routes may read or mutate any prepared action and its audit history.
- The stored action `sender` is Ethereum transaction metadata, not API-client identity.

Future implementation:

- Scope `GET /actions` to the authenticated operator by default.
- Require `api_actions.operator_id == current_operator.operator_id` for action detail, broadcast, and receipt routes.
- Validate the reported broadcast `sender` against the prepared action sender.
- Reject invalid `txIndex` values and reject updates to finalized transaction rows instead of allowing silent overwrite behavior.
