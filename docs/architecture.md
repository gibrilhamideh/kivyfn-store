# Architecture

## Ownership map

| Layer | Owns | Does not own |
| --- | --- | --- |
| Schema and fields | Declarations, defaults, scalar validation, relationship metadata | Runtime values |
| Node and indexes | Internal canonical records and indexed lookup | Application mutation lifecycle |
| Compiler, Delta, ConstraintGuard | Logical operations, trigger expansion, structural and relational checks | Network requests or Kivy events |
| Engine | Prepared canonical execution, snapshots, queries, boot and persistence | Reactive overlays or transports |
| Store | Domain action resolution, signals, commit revisions and publication | Client subscriptions |
| StoreHost | Request handling, live queries, subscriptions and delivery | Optimistic State |
| StoreClient | Batches, connection lifecycle, desired subscriptions and stream validation | Field routing or membership overlays |
| StateManager | State identity, transaction routing, canonical staging and notifications | Durable data |
| State and collections | Kivy properties, visible values, membership and local lifecycle | Authoritative Store constraints |

External commands identify schemas by their unique class names. `Address`
remains an immutable internal node-tree path; `Address()` identifies the
root. Builder attaches each schema once and resolves relationship metadata
before boot.

The protocol action records are also the Store's domain command vocabulary.
`Store.execute(actions, transaction_id=...)` accepts a tuple of set, set-row,
insert, remove or signal actions. The Host passes the decoded actions to this
same entry point. Direct and Socket.IO transport share the codecs.

## Schemas and fields

A `SingleSchema` owns one record. A `ManySchema` owns records keyed by
nonempty string identifiers. Fields declare defaults and scalar validation;
unique keys and indexes can cover one or several fields. `None` values do
not occupy unique-index entries. Indexed values must be hashable. Unknown
insert fields and unknown set fields are rejected rather than silently
accepted.

`ForeignKeyField("OtherSchema")` stores a nullable reference to a
`ManySchema` row. `RESTRICT` prevents deleting a still-referenced row, using
the transaction's logical future values. Collection sources also support
`CASCADE`. A singleton cannot be deleted as a dependent row, so declaring a
singleton CASCADE or a singleton foreign-key target is a construction error.

Wire codecs validate envelope structure and primitive payloads at the trust
boundary. Fields validate their own scalar values; Compiler and Delta own
operation shape and row existence; ConstraintGuard owns relationships; final
index preparation owns uniqueness. State's local field checks provide
immediate UI feedback but never replace the Store's authoritative checks.

## Atomic execution

One transaction follows these steps:

1. Store resolves schema actions into typed instructions and separate signals.
2. Compiler applies instructions to a private Delta. Missing rows and invalid
   operations are rejected before their trigger callbacks run.
3. Each operation's trigger commands expand before the next caller
   instruction. A deletion evaluates its cascade boundary after its trigger
   commands. Generated policy deletions are deduplicated across paths and
   cycles; they can satisfy a later explicit removal of the same row.
4. ConstraintGuard checks the logical relationships. Nodes prepare indexes
   from the final values, so valid unique-value swaps do not fail on an
   intermediate collision. Store validates signal targets in that same view.
5. If persistent data changed, Engine commits a complete persistent generation.
6. Engine installs the prepared values and indexes. Store advances each
   changed schema revision once, then publishes a `StoreCommit`.

Compilation, validation, index preparation and pre-commit persistence errors
leave canonical values and revisions unchanged. This is staged execution;
there is no sequence of canonical writes followed by best-effort rollback.
Preparation currently copies and reindexes each touched schema once. This
keeps publication simple; large collections should be profiled before adding
more elaborate incremental staging.

Same-value field assignments produce no operation. An empty/no-op commit
publishes nothing. A transaction that changes a value and later restores it
may still contain real operations and advance its schema once. A low-level
insert followed by remove can similarly retain its ordered event history.
StateTransaction may coalesce application actions before they reach Store.

### Trigger and delete order

`@trigger(OpCode.SET, field="speed")` matches one field; omitting `field`
matches that operation kind. A trigger receives an address-based
`Transaction` command builder and a `TriggerContext`. This builder is distinct
from the application's awaitable `StateTransaction`.

`context.current()` sees the logical record after set/insert, or immediately
before remove. `context.row(...)`, `context.single(...)`, and foreign-key
resolution include preceding compiled changes. Returned values are detached.
Future caller instructions are not visible to an earlier trigger.

Consequences of the ordered boundary:

- Reparent a child before deleting its old parent to preserve it.
- A delete trigger can reparent a child before that deletion's cascade runs.
- Insertion triggers create dependents before the next caller instruction,
  which can then edit or remove those dependents.
- Removing and reinserting the same parent ID removes its old CASCADE
  dependents before insertion triggers create replacements.
- RESTRICT and final foreign-key existence still use the completed logical
  transaction, so clearing a reference and removing its parent can be atomic.

Compiler limits expansion to 10,000 operations to reject runaway trigger
loops without mutating canonical data.

### Commit callbacks

`Store.subscribe_commits(callback)` returns an unsubscribe callable. A
callback sees final values and stamped revisions. Each listener receives
its own detached commit payload. Exceptions are logged; later listeners
continue. Unsubscribing a listener before its turn prevents that invocation.
Listeners added during delivery start with the next commit.

Callbacks are synchronous read-only observers. Reentrant `Store.execute()`
raises `StoreStateError`; schedule a follow-up mutation after delivery returns.
This keeps live queries from observing newer values with an older commit's
revision. Node lookups are internal borrowed objects, not a supported way to
bypass this boundary. Public Store reads observe a complete recorded commit;
separate read calls do not form a read transaction. `snapshot_if_changed()`
returns one schema snapshot and its revision together. Async Client and Host
objects still belong to their event loop.

## Host, Client and synchronization

StoreHost owns active subscriptions and cached live query computations.
StoreClient keeps the application's desired subscriptions across disconnects.
`StoreClient()` is independent of the endpoint and creates no transport.
Applications may create a global Client and its StateManagers before a
Store process reports a dynamically allocated listening port. The first
`connect(endpoint)` creates the transport; subsequent `connect()` calls
reuse the remembered endpoint. Disconnect before supplying a different
endpoint. Each application process owns its own Client and reactive objects.
A new connection binds snapshots and drains messages that arrived during
handoff before reporting synchronized. Releasing a subscription during that
handoff also releases any Host subscription created by its late response.

Each Store instance has an identifier and each schema has an integer
revision, initially zero. A successful transaction advances a changed schema
once. Signals never advance revisions. A query subscription tracks all of
its dependency revisions, including a changed dependency that leaves the
projected rows unchanged. A signal-only event legitimately has no revisions.

The Client checks the entire received envelope, instance identity and
revisions before advancing its subscription cursors. Unknown released
subscription IDs can be ignored. Gaps, duplicate/stale revisions and malformed
messages are protocol failures; the connection is invalidated rather than
silently marking stale State synchronized. Reconnect restores desired
subscriptions and uses fresh snapshots whenever cached revisions and Store
identity do not match. There is no retained event replay journal.

`await client.connect(endpoint)` waits for current desired subscriptions to
synchronize. Calling `connect()` while connected also waits for incremental
subscriptions. `online` is connection status; `synced` is synchronization
status. A row's synchronization ownership follows its live subscribed views.

A valid subscription rejection retains its structured failure on the desired
subscription and raises `StoreSubscriptionError` to synchronization waiters.
It does not invalidate the transport or successful bindings. The affected
target stays unsynchronized; healthy targets keep receiving events, and
transactions and queries continue. Calling `connect()` again retries inactive
sources without reconnecting or replaying transactions. Late failures apply
only to the exact desired subscription that requested them, so replacing a
source cannot inherit an obsolete rejection.

### Canonical delivery and acknowledgement

A commit is captured into per-client messages. The Host flushes that client's
canonical messages before returning the transaction acknowledgement. The
Client binds pending snapshots before draining messages that overtook their
responses. StateManager first installs all affected canonical values and
membership, then publishes retained callbacks across managers before signals
in global occurrence order. Transaction completion
occurs after its successful acknowledgement is processed.

A mutation response is `TransactionResponse(transaction_id)`, with no
replacement copy of the committed fields. Canonical data belongs to
subscription messages. Failed transactions return a structured
`TransactionFailureResponse`; queries and subscription requests have their
own response variants.

A failed query computation or client delivery is isolated to the affected
client. The Host can continue serving unrelated subscribers. Internal Store
failure still makes that Store unavailable; isolation does not claim to repair
corrupt application code or failed infrastructure.

### Interrupted requests and deduplication

StoreClient never automatically resends interrupted transaction batches.
A lost acknowledgement can follow a successful commit; disconnecting or
cancelling a waiter does not undo server work. Reconnect, inspect canonical
State and decide what the application should do next.

The Host remembers a bounded number of completed transaction IDs and their
exact actions. Repeating the same ID and actions returns the cached response;
reusing an ID with different actions is rejected. The cache is shared across
connections to that Host and remains in memory only. Eviction or Host
replacement removes this protection. It is not a durable exactly-once
execution facility, and a replay does not redeliver ephemeral signals.

## Queries and State

`Query` is an immutable, hashable description over a collection schema.
Use `where(...)` with comparisons, membership predicates and expression
`&`, `|`, `~` operators. Python `and`, `or` and truth-testing a query
expression are not query combinators. `Store.query(query)` returns a tuple
of detached `QueryRow` mappings; `StoreClient.query(query)` resolves to
`QueryResponse` or `FailureResponse`.

Joins follow declared foreign-key fields. Declare each parent join before a
nested join, for example `join("owner_id", as_="owner")`, then
`left_join("owner.team_id", as_="team")`. Inner joins omit absent targets;
left joins keep the root and expose absent fields as `None`. Flat names use
the alias path, such as `owner_label` or `owner_team_label`. Aliases cannot
shadow scalar fields and projected names cannot collide.

`StateManager.create(Query(...))` creates a QueryCollectionState with
read-only membership and editable projected fields. Canonical schema, field
and relationship-path metadata identify each projected write. StateManager
captures the canonical target when the action is created, including preceding
relationship changes in the same transaction; unrelated optimistic overlays
do not retarget the action later. A nullable path without a target is rejected.
For a nested path retargeted to a different row, subscribe to the intermediate
schema so the new relationship can be resolved from synchronized data.

The registry preserves ordinary row identity within one StateManager and
query row identity for equivalent output shapes. Different projected shapes
can have separate State objects for the same canonical row. Canonical
messages update all affected subscribed views before notifications.

StateData owns canonical values plus overlays keyed by transaction ID.
CollectionState similarly owns canonical membership plus per-transaction
membership overlays. Rolling back one transaction removes only its own
contribution; it does not restore a stale whole-collection snapshot.

State reads return detached values. In-place mutation of a returned nested
list/dictionary is a local edit, not a submitted operation. Assign the changed
value back or use `.set()`. Generated Kivy attributes and signal methods are
runtime APIs; generic State typing does not synthesize every property for
Pyright. `.get(name)` and `.set(**values)` expose explicit dynamic access.

`state.release(source)` removes a desired subscription and its synchronization
ownership. `state.close()` releases all targets and completes pending local
transactions. Those methods do not shut down a shared Client or Host.

## Signals

Signals have names and optional payloads but no retained field value.
Schema kind, declaration and final row existence are validated before commit.
An inserted row can signal in the same transaction; a removed final target
cannot. Failed mutations emit no signals. Repeated identical signals remain
separate occurrences and keep their relative order.

All canonical retained changes are installed before signal dispatch.
For a collection row, the row callback runs before the collection callback.
A root-row signal reaches a query only when that row belongs to the query
following the commit. Joined-schema signals are not synthesized as projected
signal aliases. Reconnect snapshots restore retained data, not past signals.

## Persistence and boot

With `persistent = True`, a schema participates in the Store's complete
durable generation. Nonpersistent schemas start from defaults on boot;
reactive overlays, revisions and signals are not persisted.

Under the configured storage directory, `.kivyfn-store/` contains
`generation-<uuid>.json` files and `current.json`. A generation includes one
Store version and every persistent schema. The writer flushes and fsyncs the
generation, then atomically replaces the manifest naming it. That manifest
replacement is the commit point. Transactions and boot normalization/upgrades
use the same mechanism, so a restart never deliberately combines schema
snapshots from different new generations.

Pre-switch failures leave the prior manifest authoritative. Orphan
generations and an uncommitted manifest temporary file are ignored. After a
durable switch, cleanup retains the current and previous generations.
Cleanup failures are logged without reversing a committed transaction.

If directory fsync fails **after** manifest replacement, the new commit is
already visible. The writer logs uncertain crash durability and returns
without undoing it; Engine installs the matching canonical data. Previous
generations and legacy originals are retained and cleanup is skipped. A
power loss at that point may affect which complete manifest is durable; the
API does not pretend this is a rejected, safely retryable transaction.

Legacy per-schema JSON files are imported on first boot. A valid live file
is preferred over its temporary file; a valid temporary file can recover a
missing/corrupt live file. Envelopes are validated at this storage boundary.
The highest legacy Store version is selected, preserving the existing
version-selection contract.

| Boot policy | Behavior |
| --- | --- |
| `raise` | Propagates malformed storage, invalid persisted fields and relationship failures; preserves failed input |
| `recover` | Recovers defaults/rows where possible; corrupt shared generation metadata requires whole-Store recovery |
| `erase` | Attempts recovery, then can rebuild defaults when boot validation still fails |

Disk write failures do not trigger an ERASE fallback. Invalid schema defaults
remain configuration errors when even an empty Store cannot validate.

Configure `Builder.store_version(n)` and migration instances with
`Builder.migrations([...])`. A migration declares `version = 2` for v1 to v2
and implements `run(data)` over a dictionary keyed by schema name. A complete
forward chain is checked before transformations run on a private copy.

Missing, invalid or failed migrations, and attempted downgrades, retain the
existing policy: log the migration problem, then validate/recover the
original selected data against the configured schemas. They do not
unconditionally abort under `raise` if that original data still validates.
After successful validation it is saved at the configured Store version.
Applications requiring a mandatory transformation should enforce that at
their deployment boundary.

## Tooling

Python 3.11 is the declared minimum. The package gets loggers from
`kivyfn.logging.get_logger`; `kivyfn` is a required runtime dependency.
Configure shared logging in the hosting application. Kivy remains the
reactive runtime dependency, and integration tests exercise actual Direct
and Socket.IO transports.

```bash
uv run pytest
uv run pyright
uv run ruff check .
uv run ruff format --check .
```

Testing uses deterministic synchronization for ordering and explicit storage
fault injection for crash boundaries. Coverage should be evaluated by the
invariants the tests protect, rather than a fixed test count.
