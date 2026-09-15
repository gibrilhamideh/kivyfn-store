# kivyfn-store

A schema-driven Store and reactive Kivy State framework for Python 3.11+.
Applications use the same State API over an in-process Direct connection or
Socket.IO. The Store owns canonical data, constraints, transactions, queries
and persistence; State owns reactive properties and optimistic changes.

## Getting started

For a release archive, install the repository and its development tools:

```bash
uv sync --group dev
```

`kivyfn` supplies the shared `kivyfn.logging.get_logger` integration and is a
required dependency. `kivy-stubs` is a development dependency. The uv sources
in `pyproject.toml` use editable sibling checkouts:

```toml
[tool.uv.sources]
kivyfn = { path = "../kivyfn", editable = true }
kivy-stubs = { path = "../kivy-stubs", editable = true }
```

Keep those sibling repositories beside `kivyfn-store` when using this local
configuration. These source paths control development installs and are not
included in the published wheel's dependency metadata. Release archives include
compiled devtools assets. A source checkout needs its frontend built before a
Python installation; see [release builds](docs/release.md).

This complete example creates a persistent collection, connects a reactive
client, and performs strict mutations. Run it with `uv run python example.py`.

```python
# example.py
import asyncio

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder, HostConfig
from kivyfn.store.schema import (
    ManySchema,
    NumericField,
    SingleSchema,
    StringField,
)
from kivyfn.store.state.manager import StateManager


class RootSchema(SingleSchema):
    pass


class FanSchema(ManySchema):
    persistent = True

    label = StringField(default="Fan")
    speed = NumericField(default=0, ge=0, le=100)


async def main() -> None:
    tree = Branch(RootSchema).child("fans", Branch(FanSchema))
    endpoint = "direct://fans"
    config = (
        Builder()
        .tree(tree)
        .storage_path("./fan-store")
        .host(HostConfig(endpoint=endpoint))
        .build()
    )
    store = Store(config)
    store.boot()
    host = StoreHost(store)
    client = StoreClient()
    state = StateManager(client)
    fans = state.create(FanSchema)

    try:
        await host.start()
        await client.connect(endpoint)
        assert fans.synced

        if "fan-1" not in fans:
            inserted = await fans.insert("fan-1", label="Tunnel fan")
            if not inserted.success:
                print(inserted.failure)
                return

        changed = await fans["fan-1"].set(speed=45)
        if changed.success:
            print(fans["fan-1"].get("speed"))
        else:
            print(changed.failure)
    finally:
        state.close()
        await client.disconnect()
        await host.stop()
        store.shutdown()


asyncio.run(main())
```

Use an endpoint such as `http://127.0.0.1:8765` for Socket.IO. A Host listens
on HTTP; an HTTPS client can connect through a TLS-terminating proxy. Direct
connections also pass through the protocol codecs.

`StoreClient()` creates no transport and requires no running event loop.
The Client and its StateManagers can be module globals in each application
process, with desired State targets registered before the endpoint is known.
Pass the endpoint to the first `await client.connect(endpoint)` after the
Store process reports its listening address. For a listener bound to port
zero, use that reported address rather than the original port-zero endpoint.

`StateManager.create()` registers a desired subscription. Create targets
before connecting to have them synchronized when `connect()` returns.
For targets added while connected, await `client.connect()` again to wait
for the new subscription handoff. `client.online` means connected;
`client.synced` and each target's `synced` describe subscription readiness.
The Client remembers its endpoint: `connect()` reuses it after a disconnect.
To switch endpoints, disconnect first and then call `connect(new_endpoint)`.
Desired subscriptions survive disconnects and synchronize on the next
connection.

A rejected subscription raises `StoreSubscriptionError` from `connect()`
while leaving the Client online and healthy subscriptions synchronized.
The error's `failure` contains the Host's structured rejection. Correct the
cause and call `connect()` again to retry inactive subscriptions, or release
the unwanted source. Mutations and queries on the connection remain usable;
this retry does not replay previous transactions.

## Configuration and startup

Builder returns one `StoreConfig` containing the `Branch` tree declaration,
storage/version settings, migrations, optional initializer, optional history,
and optional hosting settings. Every `Store(config)` constructs its own Nodes,
values and indexes. Reusing a configuration does not share canonical state.
Constructing or booting Store alone opens no listeners.

```python
# app/store/config.py
from kivyfn.store.builder import Builder, HistoryConfig, HostConfig

config = (
    Builder()
    .tree(tree)
    .storage_path("./storage")
    .store_version(2)
    .migrations(migrations)
    .initializer(initializer)
    .history(HistoryConfig())
    .host(HostConfig(endpoint="http://127.0.0.1:8765", devtools=True))
    .build()
)
```

Here `tree`, `migrations`, and `initializer` are application declarations.
Omit `.initializer()`, `.history()`, or `.host()` when they are not needed.
`StoreHost(store)` uses `config.host`; an explicit `HostConfig` may instead
be passed to Host. The endpoint selects Direct or Socket.IO. Client recovery
and process-manager timeouts stay with their respective callers.

For a managed child, construct `StoreProcess(processes, config)` and call
`start()`. It constructs the Store and Host in the child, then reports the
bound `endpoint` and optional `devtools_url` in `ready_payload`. Define schema,
migration and initializer classes at module scope and keep configured instance
data serializable. Direct transport is for callers in the same process.

An `Initializer` subclass implements `run(data)` over a detached dictionary
keyed by schema name. Boot runs it after migration and before schema defaults,
field/relationship validation and indexing. It may seed singleton or collection
data, including nonpersistent schemas. The validated result is the startup
baseline: initialization runs no triggers, cascades, signals or history actions.
An initializer exception fails startup without invoking storage erasure. See
[configuration and initialization](docs/architecture.md#configuration-and-startup)
for the class contract and recovery behavior.

## Mutations and transactions

| API | Visibility before confirmation | Result |
| --- | --- | --- |
| `fan.speed = 60` | Optimistic field overlay | Implicit transaction |
| `fan.set(speed=60)` | Canonical value retained | `StateTransaction` |
| `fans["new"] = {"label": "Fan"}` | Optimistic membership and values | Implicit transaction |
| `del fans["new"]` | Optimistic removal | Implicit transaction |
| `fans.insert("new", label="Fan")` | Waits for canonical insertion | `StateTransaction` |
| `fans.remove("new")` | Waits for canonical removal | `StateTransaction` |

Group mutations with `with state.transaction() as transaction`, then await
the transaction **after** leaving the context. Assignment stays optimistic
and `.set()` stays strict inside a transaction. The transaction records the
caller task; nested transactions and sharing an open transaction across
asyncio tasks or StateManagers are rejected.

```python
# Inside an async function with an existing state and fan.
with state.transaction() as transaction:
    fan.speed = 60
    fan.set(label="Updated fan")

await transaction
if transaction.failed:
    print(transaction.failure.type, transaction.failure.message)
```

Awaiting returns the transaction, including on an ordinary rejection; inspect
`success`, `failed` and `failure`. Local API misuse or local field validation
can raise before submission. State and collection objects expose
`pending_count`, `is_pending`, and `on_pending`, `on_success`, `on_failed`,
`on_done` events. Callback failures are logged and do not strand completion.
A throwing Kivy binding may stop later bindings on that same native event;
the framework still continues later notifications and transaction completion.
Cancelling a task awaiting a transaction does not cancel the submitted work.

Canonical fields and membership are installed for all affected subscribed
views before mutation and signal notifications. A successful transaction
acknowledgement follows delivery of its canonical messages to that client.
Application callbacks therefore read a coherent committed State view.
Optimistic changes belonging to other transactions may still be visible;
rollback removes only the failed transaction's contribution.

## Queries and signals

Use an immutable Query for a live read-only collection:

```python
# With FanSchema and an existing StateManager.
from kivyfn.store.query import Query, field

running_query = Query(FanSchema).where(field("speed") > 0)
running_fans = state.create(running_query)
```

Query membership follows canonical data. Individual projected fields remain
writable when their canonical target can be resolved. Explicit foreign-key
joins use `.join("owner_id", as_="owner")` or `.left_join(...)`; projected
fields are named, for example, `owner_label`. A missing nullable target
cannot be written through. Aliases and output names must not collide.
See [queries and State ownership](docs/architecture.md#queries-and-state).

Declare `tare = Signal()` on a schema to generate `row.tare(data)` and an
`on_tare` binding. Signals are ephemeral, ordered occurrences: no retained
value, persistence, optimistic value or revision increment. A row signal
requires a row that exists in the final transaction. Failed transactions
emit no signals; repeated identical calls remain separate occurrences.
Row callbacks run before the corresponding collection callback.

## History and browser devtools

History is optional, bounded, and owned by Store. Set `track_history = True`
on editable schemas and call `Builder.history(HistoryConfig())`. A selected
transaction retains all its effects, including trigger and cascade changes.
`await client.undo()` and `await client.redo()` navigate the current history
head; explicit expected positions protect controls based on displayed history.
Gesture transactions can share `history_group` and `history_label` metadata.

Undo/redo restores recorded effects, including previous trigger results.
It does not recompute those results against current untracked inputs.
Declared `Signal(undo="inverse_name")` occurrences can be compensated. A
compensation handler's new tracked work is a separate transaction and can
clear redo. See [history, grouping, and signals](docs/history.md) for the
restoration, conflict, and retry contracts.

Enable the independent browser interface through Builder with
`.host(HostConfig(endpoint=endpoint, devtools=True))`. Its default browser URL is
`http://127.0.0.1:3000`. After `await host.start()`, `host.devtools_url` reports
its actual address. State, History, and Activity views support guarded edits,
signals, queries, head navigation, and isolated historical previews. The
listener binds to loopback and serves bundled assets without a CDN.
`DevToolsConfig` controls the browser port, recording and session limits.
Selecting a schema loads and follows its live State automatically. Activity
recording is a separate control and does not enable history. Inspection remains
available when history retention is disabled; see [devtools](docs/devtools.md).

The wire protocol is version 3. Upgrade Hosts and application Clients together;
incompatible versions are rejected at connection time. Existing application
State APIs retain their mutation and subscription behavior.

## Correctness contracts

- Transactions stage logical values and final indexes before persistence or
  canonical publication. Unique-value swaps are valid; failures cannot leave
  a prefix of staged changes installed.
- Triggers expand in operation order. A delete's cascade boundary runs after
  its trigger commands and before the next caller instruction. Reparenting
  before deletion survives; deleting then reinserting a parent removes old
  cascade dependents before insertion triggers recreate them.
- Foreign keys reference `ManySchema` rows. A singleton foreign key supports
  `RESTRICT`; `CASCADE` requires a collection source.
- Persistent commits use one complete generation and an atomic manifest
  switch, including boot upgrades. A failure after manifest replacement but
  during directory `fsync` means the commit is visible with uncertain crash
  durability; it is logged and canonical memory follows the visible commit.
- Revisions are per schema and per Store instance. They detect stream gaps;
  there is no replay journal. Reconnection refreshes snapshots as needed.
- Interrupted requests are not automatically retried. Their outcome may be
  unknown. Host transaction-ID deduplication is bounded and in memory, not a
  durable exactly-once guarantee. Reconnect and reconcile before retrying.
- A Store with an active Host accepts mutations only on that Host's event
  loop. Workers must schedule the complete operation there. Standalone Store
  use retains synchronous locking.
- Commit and history-status callbacks share one publication guard, including
  history-only notifications. They may read, subscribe, and unsubscribe;
  schedule subsequent mutations after delivery returns. Callback exceptions
  are isolated.
- Mutable inputs, snapshots, events and State reads are detached. Change a
  nested value by assigning a replacement or calling `.set()`; mutating a
  returned list or dictionary in place does not submit a Store operation.

`state.release(source)` removes one desired subscription. `state.close()`
releases all of that manager's subscriptions and finishes outstanding local
operations. Neither disconnects the shared client. Disconnect the client and
stop the Host separately when their application lifetime ends.

## Connection recovery and keyed collections

Use `StoreClient(reconnect_interval=10.0)` to retry an unexpectedly lost
connection every ten seconds between failed attempts. The default `None` keeps
manual reconnection. Initial `connect()` remains a bounded attempt; explicit
`disconnect()` stops retries. Recovery resubscribes and obtains fresh snapshots,
without replaying interrupted transactions or past signals.

A restarted process must be reachable at the known endpoint. When using port
zero, get its new endpoint from process readiness; the Client does not discover
new ports automatically. Host and Client require the same protocol version (3).
There is no compatibility negotiation across releases.

Collections and queries are keyed by row ID. Undo/redo restores IDs and values,
not dictionary iteration positions. Bind to `rows` for visible membership
changes and to individual State properties for field changes. The internal
`_row_version` notification dependency never enters Store data or the protocol.

## Typing and ownership

Use `StateType`, `CollectionStateType[Row]`, and `QueryStateType[Row]` to
add typed field declarations, methods, local Kivy properties, and event
handlers. Define a reusable template with `state_template(source,
state_type=RowClass, collection_type=ContainerClass)`, then call
`manager.create(template)` to obtain the correctly typed managed instance.
Customization arguments belong on the template; `create()` still accepts
plain schemas and Queries.

Schemas remain shared data contracts. Custom State classes describe
client-side behavior; their local properties are not persisted or sent over
transports. Choose those classes before first creating the source in each
manager. Conflicting types are rejected to preserve stable row identity.

See [custom states and typed templates](docs/state-templates.md) for the
application layout, complete examples, query shapes, identity rules, and
typing limits. Generated fields need explicit application annotations;
`State[FanSchema]` alone does not infer their names.

Node and Engine are internal implementation interfaces. Direct Node mutation
bypasses Store constraints, persistence, revisions and event delivery.
Application data changes belong in State or `Store.execute()`.

## Development

```bash
uv run pytest
uv run pyright
uv run ruff check .
uv run ruff format --check .
```

Tests cover core invariants, persistence fault injection, queries, reactive
State and protocol behavior. Transport-dependent integration tests exercise
both Direct and a real local Socket.IO server. Timing-sensitive tests use
explicit synchronization; no external service credentials are required.

Read [architecture](docs/architecture.md), [error handling](docs/errors.md),
and the [documentation index](docs/index.md) for the detailed contracts.
Frontend checks, browser tests, and installed wheel/sdist checks are described
in [release verification](docs/release.md).

Updating an existing application? See [the Builder startup migration guide](docs/upgrading-builder.md).
