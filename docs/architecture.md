# Architecture

## Ownership map

| Layer | Owns | Does not own |
| --- | --- | --- |
| Schema and fields | Declarations, defaults, scalar validation, relationship metadata | Runtime values |
| Node and indexes | Internal canonical records and indexed lookup | Application mutation lifecycle |
| Compiler, Delta, ConstraintGuard | Logical operations, trigger expansion, structural and relational checks | Network requests or Kivy events |
| Engine | Prepared canonical execution, snapshots, queries, boot and persistence | Reactive overlays or transports |
| Store | Domain actions, signals, history, revisions and publication | Client subscriptions |
| StoreHost | Event-loop ownership, requests, live queries, subscriptions and per-connection delivery | Optimistic State |
| StoreClient | Batches, connection lifecycle, desired subscriptions and stream validation | Field routing or membership overlays |
| StateManager | State identity, transaction routing, canonical staging and notifications | Durable data |
| Devtools | Bounded diagnostic capture, browser sessions and isolated preview | Canonical mutation or undo execution |
| State and collections | Kivy properties, visible values, membership and local lifecycle | Authoritative Store constraints |

External commands identify schemas by their unique class names. `Address`
remains an immutable internal node-tree path; `Address()` identifies the
root. Configuration retains the `Branch` declaration. Each Store constructs
its own Node tree and resolves relationship metadata before boot.

The protocol action records are also the Store's domain command vocabulary.
`Store.execute(actions, transaction_id=...)` accepts a tuple of set, set-row,
insert, remove or signal actions. The Host passes the decoded actions to this
same entry point. Direct and Socket.IO transport share the codecs.

## Configuration and startup

`Builder.build()` returns one `StoreConfig`. Its `root` is a detached `Branch`
declaration, not a live Node tree. `Store(config)` builds privately owned Nodes,
indexes and revisions for that Store; reusing configuration never shares those
mutable runtime objects.

Builder centralizes `.tree()`, `.storage_path()`, `.store_version()`,
`.boot_policy()`, `.migrations()`, `.initializer()`, `.history()` and `.host()`.
`HistoryConfig` owns history limits. Optional `HostConfig` owns its endpoint and
`devtools=False`, `True`, or a `DevToolsConfig`. There is no duplicate transport
selector: the endpoint chooses Direct or Socket.IO and the transport parses it.

`StoreHost(store)` uses `store.config.host`; callers may explicitly supply a
`HostConfig` instead. A standalone Store needs no hosting configuration and
opens no listeners. `StoreProcess(processes, config)` passes that single
configuration to its child. Process names/timeouts remain process-runner options,
and reconnect settings remain per Client. Module-level schema, migration and
initializer classes with serializable instance data support process spawning.

An optional `Initializer` instance transforms detached raw data after the
migration stage and before schema/default/relationship validation. It receives
`dict[str, Any]` keyed by schema name and returns the resulting dictionary.
Singletons contain field dictionaries; collections contain dictionaries keyed
by row ID. No Store, Node, transport, or live observer is passed to the callback.
The class has one `run(data)` method; application subclasses may use ordinary
helper methods without a registration or dependency-ordering framework.

```python
# app/store/initializer.py
from typing import Any

from kivyfn.store.builder import Initializer as BaseInitializer


class Initializer(BaseInitializer):
    def run(self, data: dict[str, Any]) -> dict[str, Any]:
        data["StatusSchema"] = {"ready": False}
        data["RuntimeSchema"] = self._runtime_rows(data)
        return data

    def _runtime_rows(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            row_id: {"component_id": row_id}
            for row_id in data.get("ComponentSchema", {})
        }
```

This application example assumes those three schemas are declared in its tree.
It seeds a singleton and collection using the application's migrated component
format. The callback reads raw data, not defaulted or validated State: handle
only incoming formats the application supports, or fail startup explicitly.
Configure it with `.initializer(Initializer())`; no version number is needed.

Initialization is a data-preparation step, not a transaction. Defaults,
field/relationship checks and index construction run afterward through the
existing boot machinery. Initialization emits no triggers, cascades or signals,
and adds no undo entry. The validated result is the startup baseline. Ordinary
triggers and cascade relationships can maintain subsequent application changes.
Nonpersistent schemas may be seeded, but only persistent schemas reach disk.

An exception or non-dictionary result from the initializer aborts startup and
does not invoke ERASE. Invalid returned values go through the configured boot
validation/recovery policy. If ERASE must rebuild an empty candidate after boot
validation fails, initialization runs again on that empty candidate before
validation. There is no retry loop; initializer code should prepare data without
external side effects. Clients and reconnects never invoke it.

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
6. Engine installs the prepared values and indexes. Store installs its prepared
   history state, advances each changed schema revision once, then publishes
   a `StoreCommit` and any history-status change under one guard.

History selection happens before compaction. Restoration applies normalized
operation-owned records to a private Delta and reuses final constraints,
indexes, persistence, installation, and publication. It does not rerun the
compiler, triggers, defaults, or generated IDs. See [history](history.md).

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

Commit and history-status callbacks are synchronous read-only observers.
One guard covers both publication phases, including history-only changes.
Reentrant mutations, including undo/redo and group closure, raise
`StoreStateError`; schedule follow-up work after delivery returns.
This keeps live queries from observing newer values with an older commit's
revision. Node lookups are internal borrowed objects, not a supported way to
bypass this boundary. Public Store reads observe a complete recorded commit;
separate read calls do not form a read transaction. `snapshot_if_changed()`
returns one schema snapshot and its revision together. Async Client and Host
objects belong to their event loop. An active Host claims its Store centrally:
all Store mutations must execute on that Host loop. A worker schedules the
complete operation there, never just publication. Standalone Store use retains
synchronous locking.

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

Each registered Host client owns its delivery and close callbacks. Direct,
Socket.IO, and browser connections can coexist without a global sender.
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

The Host remembers a bounded number of completed mutation command IDs and
exact request contents, including history positions and grouping metadata.
Repeating the same ID and contents returns the cached response; reusing an ID
with different contents is rejected. The cache is shared across
connections to that Host and remains in memory only. Eviction or Host
replacement removes this protection. It is not a durable exactly-once
execution facility, and a replay does not redeliver ephemeral signals.
Direct Store navigation has expected-position guards and no second cache;
a repeated applied command can return stale. Protocol version 3 is required
at both ends, including the Direct connection handshake.

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
Collections are keyed by row ID. Dictionary iteration positions are incidental
and do not produce commits, revisions or collection notifications. State and
query collections preserve surviving row objects and their local Kivy properties.
Their uncached `rows` AliasProperty depends on private `_row_version`, advanced
only when publishing visible membership changes after final data installation.
Row-field edits notify the row's properties without refreshing the whole map.

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

A declaration may name an inverse signal on the same schema. Selected history
retains both payload directions. Undo emits inverses in reverse occurrence
order and redo emits originals in forward order after the entire restored
state is installed. Unpaired or unsupported selected signals create a history
barrier. Compensation handlers' new tracked work is a separate transaction
and can clear redo; there is no automatic suppression or causality tracking.

## Persistence and boot

With `persistent = True`, a schema participates in the Store's complete
durable generation. Nonpersistent schemas start from defaults unless the
configured initializer supplies their startup data. Reactive overlays, revisions
and signals are not persisted.

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
JSON parser depth and integer-digit failures follow the same corruption policy.
Empty row IDs are invalid at canonical construction and use row recovery.
The highest legacy Store version is selected, preserving the existing
version-selection contract.

| Boot policy | Behavior |
| --- | --- |
| `raise` | Propagates malformed storage, invalid persisted fields and relationship failures; preserves failed input |
| `recover` | Recovers defaults/rows where possible; corrupt shared generation metadata requires whole-Store recovery |
| `erase` | Attempts recovery, then can rebuild defaults when boot validation still fails |

Disk write and initializer failures do not trigger an ERASE fallback. Invalid
schema defaults remain configuration errors when even an initialized empty
candidate cannot validate.

Configure `Builder.store_version(n)` and migration instances with
`Builder.migrations([...])`. A migration declares `version = 2` for v1 to v2
and implements `run(data)` over a dictionary keyed by schema name. A complete
forward chain is checked before transformations run on a private copy.

Missing, invalid or failed migrations, and attempted downgrades, retain the
existing policy: log the migration problem, then validate/recover the
original selected data against the configured schemas. They do not
unconditionally abort under `raise` if that original data still validates.
The initializer receives the migration result or that selected fallback data.
After initialization and successful validation, persistent data is saved at the
configured Store version. Applications requiring a mandatory transformation
should enforce that at their deployment boundary.

## Browser integration

A disabled devtools option creates no listener or recorder. When enabled,
StoreHost owns a separate loopback aiohttp listener on its existing event
loop. A facade captures requests/responses and lifecycle events centrally;
canonical commits enter once through Store observation. The recorder bounds
Activity and checkpoint/delta preview separately from undo retention.
The default devtools address is `http://127.0.0.1:3000`; an occupied configured
port fails startup rather than selecting a different port. Explicit `port=0`
requests a dynamically allocated listener. The browser uses a Tokyo Midnight
inspired dark theme and loads live data when the user selects a schema; Activity
recording controls are independent of that browser-local inspection scope.

Browser commands use the normal RequestBatch codec and Host dispatcher.
The diagnostic WebSocket carries a scoped checkpoint followed by ordered
commits, with epochs, sequence numbers, and explicit gaps/resynchronization.
HTTP acknowledgements do not establish stream synchronization. Browser sessions
own their Host delivery connection and do not create ordinary application
subscriptions. Detached schema catalogs and atomic observation checkpoints
keep devtools independent of Node internals and Kivy State.

Preview reconstructs an isolated value cache from captured chronological
checkpoints and commits; it never moves the live undo cursor. Copy-as-new
uses original caller actions retained in Activity only. Expanded trigger
or cascade effects and normalized history are never executable drafts.
A valid draft referencing additional known schemas extends the browser's live
inspection scope automatically. Their loaded revisions enter its guarded
command only after the user chooses **Use current revisions**; incoming
snapshots never silently retarget an existing draft. Invalid draft text or
unknown schema names do not create live watches.
See [browser devtools](devtools.md) for scope and lifecycle behavior.

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
