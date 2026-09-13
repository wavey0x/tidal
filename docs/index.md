# Tidal

Tidal runs auction observation and managed execution on one host, with one
runtime, one SQLite database and one configuration. The dashboard and API read
that state. Local commands prepare actions under a shared lock, retain each
transaction identity before broadcasting, and reconcile finalized outcomes.

Start with [installation](install.md), the [operator guide](operator-guide.md)
or [backup and recovery](recovery.md). For implementation details, see
[architecture](architecture.md), [configuration](config.md),
[CLI reference](cli-reference.md) and [API reference](api-reference.md).

Auction behavior is described in [pricing](pricing.md) and
[kick selection](kick-selection.md). The runtime consolidation preserves these
policies, including exact historical amounts and no-fill retry delays.
