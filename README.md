# kivyfn-store

A schema-driven Store and reactive Kivy State framework for Python 3.11+.
Applications use the same State API over an in-process Direct connection or
Socket.IO. The Store owns canonical data, constraints, transactions, queries
and persistence; State owns reactive properties and optimistic changes.

## Getting started

Install the repository and its development tools:

```bash
uv sync --group dev
```

This complete example creates a persistent collection, connects a reactive
client, and performs strict mutations. Run it with `uv run python example.py`.

```python
# example.py
import asyncio

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
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
    root, config = Builder().tree(tree).storage_path("./fan-store").build()
    store = Store(root, config)
    store.boot()

    endpoint = "direct://fans"
    host = StoreHost(store, endpoint)
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
- Synchronous `Store.subscribe_commits()` callbacks may read, subscribe and
  unsubscribe. They cannot call `Store.execute()` reentrantly; schedule a
  subsequent mutation after delivery returns.
- Mutable inputs, snapshots, events and State reads are detached. Change a
  nested value by assigning a replacement or calling `.set()`; mutating a
  returned list or dictionary in place does not submit a Store operation.

`state.release(source)` removes one desired subscription. `state.close()`
releases all of that manager's subscriptions and finishes outstanding local
operations. Neither disconnects the shared client. Disconnect the client and
stop the Host separately when their application lifetime ends.

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
