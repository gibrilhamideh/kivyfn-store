# KivyFN Store Refactor Plan

This document records the architectural decisions for the next
`kivyfn-store` refactor.

The goal is to keep the public API extremely small while making State powerful
enough for local and remote reactive UI applications.

This is a design contract first. Implementation details may change as long as
the public behavior and invariants below remain intact.

---

## 1. Core Architecture

`Store` remains the synchronous data engine.

It owns:

- schemas
- fields
- validation
- indexes
- foreign keys
- triggers
- policies
- transactions
- persistence
- revisions
- commit history / replay data

`State` is the application-facing reactive layer.

It owns:

- reactive entities
- Kivy properties and events
- optimistic state
- strict operations
- subscriptions
- queries
- pending state
- synchronization
- batching
- reconnect / resume
- local and remote backend abstraction

Application code should primarily interact with `State`, not directly with
`Store`.

```python
store = Store(...)
state = State(store)

fans = state.create(FanSchema)
```

Conceptually:

```text
Application / UI
       |
       v
     State
       |
       v
     Store
```

### Runtime rule

`Store` stays synchronous and event-loop independent.

The `State` layer requires an asyncio runtime.

Reads remain synchronous:

```python
fan = fans["fan-1"]
speed = fan.speed
```

Operations are awaitable when the caller cares about completion:

```python
await fan.set(speed=70)
```

Local and remote State should expose the same public API.

---

## 2. Entity State

A single-schema State exposes typed reactive properties.

Example:

```python
fan.speed
fan.enabled
fan.mode
```

Normal Kivy binding remains available:

```python
fan.bind(
    speed=self.on_speed,
)
```

Direct assignment is optimistic:

```python
fan.speed = 70
```

Strict mutation uses `set()`:

```python
operation = fan.set(
    speed=70,
)
```

and may be awaited:

```python
await operation
```

---

## 3. Optimistic vs Strict Mutation

### Optimistic mutation

Python assignment is optimistic:

```python
fan.speed = 70
```

Conceptually:

```text
canonical = 50
optimistic = 70
visible = 70
```

The optimistic value must never overwrite the canonical value internally.

If the Store accepts the mutation:

```text
canonical = 70
optimistic = removed
visible = 70
```

If the Store rejects the mutation:

```text
optimistic = removed
visible = latest canonical value
```

The rollback target is the latest canonical value, not necessarily the value
that was canonical when the request started.

Every optimistic mutation still receives an internal operation ID.

### Strict mutation

Explicit mutation methods are strict:

```python
fan.set(...)
fans.insert(...)
fans.replace(...)
fans.remove(...)
```

Strict operations do not alter visible State until the Store has successfully
committed them.

They return a `StateOperation`.

```python
operation = fan.set(
    speed=70,
)
```

The caller may ignore it:

```python
fan.set(
    speed=70,
)
```

observe it:

```python
operation.bind(
    on_success=self.on_success,
    on_failed=self.on_failed,
    on_done=self.on_done,
)
```

or await it:

```python
await operation
```

---

## 4. StateOperation

A `StateOperation` represents one requested Store operation.

Minimum public state:

```python
operation.id
operation.done
operation.success
operation.failed
operation.failure
```

Events:

```python
operation.bind(
    on_success=self.on_success,
    on_failed=self.on_failed,
    on_done=self.on_done,
)
```

The operation object is used for both local and remote State.

Local Store execution may complete synchronously internally, but the public
operation contract must remain identical to the remote case.

An optimistic property assignment also creates an internal `StateOperation`,
even though Python assignment cannot return it.

---

## 5. Failure

Failure information should remain intentionally small.

Conceptually:

```python
FailureType = Literal[
    "network",
    "field",
    "foreign_key",
    "trigger",
    "policy",
    "conflict",
    "not_found",
    "timeout",
    "internal",
]
```

A failure only needs:

```python
failure.type
failure.message
```

Example:

```python
if operation.failed:
    print(operation.failure.type)
    print(operation.failure.message)
```

Raw backend exceptions should not leak through State.

Local and remote failures should be normalized into the same failure model.

---

## 6. State Lifecycle Events

State lifecycle events always receive the operation for consistency.

### Pending

```python
def on_pending(
    self,
    state,
    operation,
    key,
    old,
    new,
):
    ...
```

### Success

```python
def on_success(
    self,
    state,
    operation,
    key,
    old,
    new,
):
    ...
```

### Failed

```python
def on_failed(
    self,
    state,
    operation,
    key,
    old,
    new,
    failure,
):
    ...
```

### Done

```python
def on_done(
    self,
    state,
    operation,
    key,
    old,
    new,
    failure,
):
    ...
```

For `on_done`:

```python
failure is None
```

means success.

For an entity State:

```text
key = field name
```

For a collection State:

```text
key = row ID
```

The failure object explains why the mutation failed.

---

## 7. Pending State

Pending belongs to State, not to the operation.

State exposes:

```python
state.pending
state.pending_count
```

`pending` means:

> At least one unfinished mutation belongs to this State.

`pending_count` counts unfinished operations, not changed fields.

A transaction containing many writes is still one pending operation.

There is no need for `operation.pending`.

If:

```python
operation.done is False
```

the operation is unfinished.

---

## 8. ManyState

A schema-backed many State behaves like a keyed collection of reactive
`EntityState` objects.

Example:

```python
fans["fan-1"]
fans.get("fan-1")
fans.keys()
fans.values()
fans.items()
len(fans)
```

The collection owns membership.

Each row entity owns its fields.

```text
ManyState
    membership

EntityState
    field values
```

Changing:

```python
fans["fan-1"].speed = 70
```

does not mean collection membership changed.

---

## 9. Reactive `rows`

`ManyState` and `QueryState` should expose a Kivy-observable membership map:

```python
state.rows
```

Its values are the live `EntityState` / query-row State objects:

```python
{
    "fan-1": FanState(...),
    "fan-2": FanState(...),
}
```

The purpose of `rows` is:

> Provide a reactive, KV-friendly `id -> State entity` mapping.

Example in KV:

```kv
runtime: fan_state.rows.get(root.fan_id)
```

If the row does not exist:

```python
fan_state.rows.get("fan-1")
```

returns `None`.

If it later enters the State, `rows` dispatches and KV re-evaluates.

If it later leaves the State, `rows` dispatches and KV receives `None`.

### `rows` is read-only

Application code must not mutate membership through `rows`.

These should fail:

```python
state.rows["fan-1"] = {}
del state.rows["fan-1"]
state.rows.clear()
state.rows.update(...)
state.rows.pop("fan-1")
```

Mutation occurs through `ManyState` itself.

Optimistic:

```python
state["fan-1"] = {
    "type": "side_fan",
    "index": 0,
}
```

```python
del state["fan-1"]
```

Strict:

```python
state.insert(...)
state.replace(...)
state.remove(...)
```

The exact implementation may use a custom read-only observable mapping rather
than exposing a raw mutable Kivy `DictProperty`.

### `rows` only describes membership

This:

```python
state["fan-1"].speed = 70
```

must not dispatch `rows`.

The mapping still points from `"fan-1"` to the same `FanState`.

Membership changes such as enter/leave do dispatch `rows`.

A full row replacement may avoid dispatching `rows` when the existing
`EntityState` object is preserved.

---

## 10. ManyState Optimistic Syntax

### Insert

If an ID does not exist:

```python
state["fan-1"] = {
    "type": "side_fan",
    "index": 0,
}
```

means optimistic insert.

### Replace

If the ID already exists:

```python
state["fan-1"] = {
    "type": "tunnel_fan",
    "index": 1,
}
```

means optimistic replace.

### Remove

```python
del state["fan-1"]
```

means optimistic remove.

Strict equivalents:

```python
state.insert(...)
state.replace(...)
state.remove(...)
```

---

## 11. Replace Semantics

Replace means full replacement, not merge.

Suppose the current row is:

```python
{
    "name": "John",
    "age": 40,
    "enabled": False,
}
```

and replacement input is:

```python
{
    "name": "Jack",
}
```

The final row is reconstructed using normal schema creation semantics:

```text
name = "Jack"
age = schema default
enabled = schema default
```

Missing required fields without defaults cause failure.

The existing reactive `EntityState` object should be preserved.

```python
row = people["john"]

await people.replace(
    "john",
    name="Jack",
)

assert people["john"] is row
```

This preserves bindings and references.

---

## 12. Collection Events

Schema-backed `ManyState` exposes structural Store events:

```python
on_insert
on_remove
on_replace
```

Example insert callback:

```python
def on_insert(
    self,
    state,
    row_id,
    row,
):
    ...
```

Example remove callback:

```python
def on_remove(
    self,
    state,
    row_id,
    row,
):
    ...
```

Replace preserves the reactive row object while applying the replacement data.

Collection States also have membership events:

```python
on_enter
on_leave
```

These describe whether an ID is currently visible in the State.

For schema-backed `ManyState`, an actual Store insert can result in both:

```text
on_insert
on_enter
```

A Store remove can result in:

```text
on_remove
on_leave
```

Query-backed collections use membership events without pretending a Store row
was inserted or removed.

---

## 13. Transactions

Transactions are atomic.

Example:

```python
with state.transaction():
    state.name = "John"
    state.age = 500
    state.enabled = True
```

If `age` causes the transaction to fail:

```text
name     -> reverted
age      -> reverted
enabled  -> reverted
```

All optimistic transaction changes must be reverted before failure callbacks
run.

Only the actual failure cause needs to be reported.

For example:

```text
key = "age"
failure.type = "field"
```

The cancelled fields do not each receive fake failure causes.

A transport-level failure may have no specific key:

```text
key = None
failure.type = "network"
```

A transaction is one `StateOperation` and counts as one pending operation.

---

## 14. Atomic Apply Before Dispatch

For a successful multi-field Store commit:

```text
a -> 10
b -> 20
c -> 30
```

State must apply all canonical values before dispatching observable callbacks.

When the callback for `a` runs:

```python
state.b == 20
state.c == 30
```

must already be true.

The same rule applies to rollback:

1. restore all canonical visible values;
2. update collection membership;
3. settle pending state;
4. then dispatch failure / data events.

Observers must never see a partially applied Store transaction.

---

## 15. Query

`Query` should be:

```text
immutable
hashable
comparable by semantics
```

Equivalent queries must compare and hash equally:

```python
assert q1 == q2
assert hash(q1) == hash(q2)
```

This allows StateManager to deduplicate subscriptions.

Two screens creating equivalent queries should share one backend subscription.

Query expressions and query values must therefore normalize into immutable,
hashable internal structures.

---

## 16. QueryState Purpose

A live `QueryState` is designed for reactive UI, not unrestricted SQL result-set
semantics.

The defining invariant is:

> Every live QueryState row corresponds to exactly one root-schema row and keeps
> that root row's ID for its lifetime.

This gives the UI:

- stable identity
- stable EventDispatcher objects
- O(1) lookup
- predictable bindings
- efficient keyed reconciliation
- predictable reconnect behavior
- simpler Pyright typing

A live Query may enrich a root row, but it must never multiply that root row.

---

## 17. Query Membership

Query membership is derived and read-only.

A QueryState does not expose collection CRUD:

```text
no insert()
no remove()
no replace()

no query_state[id] = ...
no del query_state[id]
```

It exposes membership events:

```python
on_enter
on_leave
```

Example:

```python
active_fans = state.create(
    Query(FanSchema).where(
        field("enabled") == True,
    )
)
```

If a fan becomes enabled:

```text
on_enter
```

If it becomes disabled:

```text
on_leave
```

No fake `on_insert` / `on_remove` is emitted because the underlying Store row
may still exist.

QueryState also exposes the reactive read-only:

```python
query_state.rows
```

mapping keyed by the root schema ID.

---

## 18. Query Row Editing

Query collection membership is read-only.

Fields on a Query row may be writable when the field maps directly and
unambiguously to one underlying Store field.

Example:

```python
row.enabled = False
```

may route to:

```text
FanSchema.enabled
```

A joined field may also be writable when its source is unambiguous.

Computed, aggregate, or otherwise non-direct fields are read-only.

If this rule creates excessive edge cases during implementation, joined-field
writes should be restricted before adding more complexity.

Schema State remains the canonical full CRUD interface.

---

## 19. Foreign-Key Joins

Live Query joins are based on `ForeignKeyField`.

They are not arbitrary SQL joins.

Conceptually:

```python
Query(IoSchema).join(
    IoSchema.component_id,
)
```

is valid because:

```text
IoSchema.component_id -> ComponentSchema.id
```

The foreign-key metadata already tells Query:

- source schema
- target schema
- relationship
- field ownership
- typing information

### Root identity rule

A join may enrich a root row but must never multiply it.

Valid:

```text
many -> one
one -> one
```

Valid chain:

```text
Io
 |
 v
Component
 |
 v
Controller
 |
 v
Farm
```

as long as every root IO row still resolves to at most one final query row.

Multiple independent foreign keys are also valid:

```text
Program
 |-- primary_sensor -> Sensor
 `-- backup_sensor  -> Sensor
```

The result is still keyed by `Program.id`.

### Reverse one-to-many joins

These are deliberately excluded from live QueryState.

Given:

```text
Component
    ^
    |
Io.component_id
```

this direction is valid:

```text
Io -> Component
```

This expanding direction is not a live join:

```text
Component -> many Io rows
```

Instead make `IoSchema` the query root:

```python
Query(IoSchema).where(
    field("component_id") == component_id,
)
```

Now the result is naturally keyed by IO IDs.

---

## 20. Join Types

Initially only useful UI-safe join semantics should be supported.

### `join()`

Equivalent to an inner join for the relationship.

The root row remains only when the foreign key resolves.

### `left_join()`

The root row remains even when the optional foreign key does not resolve.

Joined fields are `None` when no joined row exists.

Not initially supported:

```text
right join
full outer join
cross join
arbitrary expression joins
row-multiplying reverse joins
```

These do not fit the root identity invariant of live QueryState.

---

## 21. Join Result Shape

Joined Query rows may be flat.

Example:

```python
row.name
row.enabled
row.component_type
```

Query metadata tracks which Store field each result field originates from.

If multiple selected/joined schemas expose the same output name, the collision
must be resolved explicitly with an alias.

No silent field overwriting.

The query result shape should be machine-readable so Pyright / Kivy LSP can
provide useful autocomplete.

---

## 22. Batching

StateManager should micro-batch using asyncio.

Do not rely on Kivy `Clock` for the State runtime.

The first queued operation can schedule a flush on the next asyncio loop turn.

Conceptually:

```text
loop.call_soon(flush)
```

Things created during the same synchronous turn may share one batch.

Batch:

- subscription changes
- initial hydration requests
- outgoing mutations
- incoming Store commits

### Batching is not transaction semantics

A transport batch may contain:

```text
operation A
operation B
transaction C
operation D
```

A failure in A must not fail B or D.

Only the contents of transaction C are atomic together.

---

## 23. State Reads

Reading State is synchronous memory access.

Do not introduce APIs such as:

```python
await fan.speed
```

or network reads hidden behind property access.

Once State is hydrated:

```python
fan.speed
fans["fan-1"]
fans.rows.get("fan-1")
```

are immediate local reads.

State owns synchronization in the background.

---

## 24. Subscription Deduplication

Equivalent Query objects share one backend subscription.

Conceptually:

```text
Query
   |
   v
SubscriptionEntry
   |
   +-- consumer State A
   +-- consumer State B
   `-- consumer State C
```

When another equivalent Query is created, StateManager reuses the existing
subscription.

When the final consumer disappears, StateManager may unsubscribe.

Schema-backed State should use the same centralized subscription machinery where
possible.

---

## 25. Reconnect / Resume

Individual State objects must not independently resynchronize themselves.

`StateManager` owns reconnect centrally.

On reconnect, send one resume handshake containing information such as:

```text
server instance ID
last applied revision
unique active subscriptions
unique active queries
```

### Replay when possible

If:

```text
same server instance
+
server still has all missing revisions
```

the server sends only the missing commits.

Example:

```text
client last revision = 18420
server current revision = 18465

replay:
    18421
    ...
    18465
```

### Snapshot fallback

Request a new batched snapshot when replay is impossible.

Examples:

- server restarted
- client was offline longer than replay history
- revision gap cannot be recovered
- synchronization mismatch was detected

The server should keep a bounded recent commit/revision journal to make short
disconnects cheap.

One reconnect should not cause every State object to independently request a
snapshot.

---

## 26. Local and Remote Backend Symmetry

State behavior should not depend on whether the Store is local or remote.

Conceptually:

```text
                State
                  |
                  v
             StateBackend
              /       \
             /         \
    LocalBackend     RemoteBackend
         |                |
       Store            network
                          |
                        Store
```

Local Store execution remains synchronous.

Remote execution is asynchronous because of transport.

Both expose the same State behavior:

```python
fan.speed = 70
await fan.set(speed=70)
```

---

## 27. Deferred Ideas

The following ideas were discussed but are intentionally not part of the current
refactor contract.

### `StateRef`

A reactive reference wrapper may still be useful later:

```python
state.ref(id)
```

or:

```python
state.reference(id)
```

However, the current preferred KV solution is the reactive read-only membership
mapping:

```kv
runtime: state.rows.get(root.id)
```

Do not add `StateRef` unless `rows` proves insufficient.

### FnWidget identity

KivyFN may later give every `FnWidget`:

```text
immutable framework UID
optional logical/data key
O(1) child lookup maps
```

This fits keyed State-driven rendering well, but it is outside this Store
refactor.

### Unrestricted SQL-style queries

A future one-shot reporting/read API may support:

- expanding joins
- aggregates
- grouping
- arbitrary projections
- non-identity-preserving result sets

Such an API should remain separate from live `QueryState`.

Do not weaken live QueryState's stable root identity just to support reporting
queries.

---

## 28. Core Invariants

The refactor should preserve these rules above all else.

1. Store is the synchronous data engine.
2. State is the reactive application API.
3. State uses asyncio; Store does not require it.
4. State reads are synchronous.
5. Direct property/mapping mutation is optimistic.
6. Explicit mutation methods are strict and return `StateOperation`.
7. Canonical State is never overwritten by optimistic State.
8. Operation IDs are tracked for every mutation.
9. `StateFailure` stays small and normalized.
10. State exposes `pending` and `pending_count`.
11. Transactions are atomic.
12. Apply or rollback the whole transaction before dispatching callbacks.
13. ManyState owns membership; EntityState owns field values.
14. `rows` is a reactive, read-only `id -> State entity` membership map.
15. Writes happen through State, never through `rows`.
16. Query is immutable and hashable.
17. Equivalent queries share backend subscriptions.
18. Live QueryState preserves root-schema identity.
19. Query membership is derived and read-only.
20. Live joins follow foreign keys and may never multiply the root row.
21. Query joins may be chained while the root identity remains stable.
22. StateManager owns batching and reconnect.
23. Batching never changes operation/transaction semantics.
24. Reconnect prefers revision replay over full snapshots.
25. Local and remote State expose the same public contract.

---

## 29. Small End-to-End Examples

### Entity

```python
fans = state.create(FanSchema)

fan = fans["fan-1"]

fan.speed = 70

operation = fan.set(
    speed=80,
)

await operation

if operation.failed:
    print(operation.failure.message)
```

### KV lookup

```kv
<FanWidget>:
    runtime: fan_state.rows.get(root.fan_id)
```

### ManyState optimistic insert

```python
fans["fan-10"] = {
    "type": "side_fan",
    "index": 10,
}
```

### ManyState strict insert

```python
operation = fans.insert(
    "fan-10",
    type="side_fan",
    index=10,
)

await operation
```

### Replace

```python
await fans.replace(
    "fan-10",
    type="tunnel_fan",
)
```

Unspecified fields use normal schema defaults.

### Optimistic remove

```python
del fans["fan-10"]
```

### Strict remove

```python
await fans.remove("fan-10")
```

### Query

```python
active_fans = state.create(
    Query(FanSchema).where(
        field("enabled") == True,
    )
)
```

Membership is read-only:

```python
fan = active_fans.rows.get("fan-1")
```

### Foreign-key join

```python
ios = state.create(
    Query(IoSchema).join(
        IoSchema.component_id,
    )
)
```

The query remains keyed by `IoSchema.id`.

### Reverse one-to-many relationship

Do not use a row-multiplying join.

Use the referencing schema as the root:

```python
component_io = state.create(
    Query(IoSchema).where(
        field("component_id") == component_id,
    )
)
```

Now each query result remains keyed by an IO ID.

---

## 30. Refactor Principle

When an implementation choice adds a large number of edge cases for relatively
little application value, do not add the feature.

The framework should be:

```text
simple for normal use
powerful when needed
predictable under failure
stable for long-lived UI bindings
```

The public State API should hide networking, subscription management, batching,
reconnect, Store routing, optimistic overlays, and synchronization whenever the
application does not explicitly need to care about them.

---

## 31. Direct Store Query Execution

`Query` itself is not restricted to the live UI identity rules.

The restriction applies only when a Query is converted into live reactive State.

This means the same Query system can support two execution models.

### One-shot Store query

```python
rows = store.query(
    Query(ComponentSchema)
    .join(...)
    .where(...)
)
```

`Store.query()` may support the full relational query model because its result is
a one-shot snapshot.

It does not need to preserve long-lived UI identity.

This execution path may therefore support features such as:

- row-multiplying joins
- reverse one-to-many joins
- arbitrary relational result cardinality
- projections
- composite result rows
- reporting queries
- exports
- analytics
- diagnostics
- future grouping / aggregation features

A Store query result is not required to behave like `ManyState`.

It may return ordinary result rows whose lifetime ends with that query result.

### Live QueryState

```python
live = state.create(
    Query(IoSchema)
    .join(
        IoSchema.component_id,
    )
)
```

A Query used with `State.create()` must satisfy the stronger live-State
invariants.

In particular:

```text
one root Store row
        |
        v
one stable QueryState row
        |
        v
one stable root ID
```

Joins may enrich the root row but may not multiply it.

If a Query does not preserve root identity, it is valid for:

```python
store.query(query)
```

but invalid for:

```python
state.create(query)
```

State should reject such a Query with a clear error explaining that the result
does not preserve live root-row identity.

Example:

```text
ComponentSchema -> many IoSchema rows
```

may be valid for a one-shot Store query:

```python
rows = store.query(query)
```

because several result rows for one component are acceptable.

The same Query is not valid as live State because one component ID would no
longer identify exactly one reactive query row.

By contrast:

```text
IoSchema.component_id -> ComponentSchema
```

preserves one result per root IO row and is therefore valid for both:

```python
store.query(query)
```

and:

```python
state.create(query)
```

### Architectural rule

Use:

```python
state.create(...)
```

for long-lived reactive UI data.

Use:

```python
store.query(...)
```

for one-shot relational reads where stable reactive identity is unnecessary.

This is the intentional direct-Store use case for application code.

The framework should use one `Query` language for both execution models rather
than creating separate UI-query and relational-query DSLs.

