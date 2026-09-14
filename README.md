
# kivyfn-store

`kivyfn-store` is a reactive state and data-store framework for Python and Kivy applications.

It provides a schema-driven Store with persistence, transactions, live subscriptions, queries, reactive Kivy State objects, and transport-independent client/server communication.

The project is designed so application code works primarily with **State**, while the Store remains responsible for canonical data, validation, persistence, queries, constraints, and atomic execution.

---

## Goals

`kivyfn-store` is built around a few core principles:

* One canonical source of truth in the Store.
* Reactive application-facing State objects.
* Optimistic and strict mutations.
* Atomic transactions.
* Schema-based subscriptions.
* Live query subscriptions.
* Transport-independent clients and hosts.
* Signals as ephemeral events rather than stored values.
* Per-schema revisions for detecting synchronization gaps.
* Minimal duplication between local and remote behavior.
* Strong typing and predictable APIs.

---

## Architecture

The main data flow is:

```text
Application
    ↓
State / CollectionState / QueryCollectionState
    ↓
StateManager
    ↓
StoreClient
    ↓
Transport
    ↓
StoreHost
    ↓
Store
    ↓
Engine
    ↓
Node / Schema / Persistence
```

Changes flow back through the same stack:

```text
StoreCommit
    ↓
StoreHost
    ↓
SubscriptionEvent
    ↓
StoreClient
    ↓
StateManager
    ↓
Reactive State
    ↓
Kivy bindings
```

The application does not need separate local and remote State APIs.

The same State API is used regardless of transport.

---

# Core Concepts

## Schema

Schemas define canonical Store data.

There are two primary schema types:

```python
SingleSchema
ManySchema
```

A `SingleSchema` represents one record.

A `ManySchema` represents a keyed collection of records.

Example:

```python
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.fields import BooleanField, StringField


class FanSchema(ManySchema):
    name = StringField()
    enabled = BooleanField(default=False)
```

Schema fields are Store metadata and validation objects.

They are not directly used as Kivy properties. Reactive Kivy properties are generated in the State layer from schema metadata.

---

## State

State is the application-facing representation of Store data.

For a single schema:

```python
controller = state.create(ControllerSchema)
```

For a collection:

```python
fans = state.create(FanSchema)
```

A collection contains row State objects:

```python
fan = fans["fan-1"]

print(fan.name)
print(fan.enabled)
```

State objects expose generated properties matching their schema fields.

---

# Mutations

## Optimistic assignment

Normal property assignment is optimistic:

```python
fan.enabled = True
```

The visible State changes immediately.

The mutation is sent to the Store and later confirmed by the canonical Store event.

If the transaction fails, the optimistic overlay is rolled back.

---

## Strict mutation

Use `.set()` when the visible value should remain canonical until confirmation:

```python
operation = fan.set(enabled=True)

await operation
```

Strict changes become visible when the Store confirms them.

---

## Collection mutations

Insert:

```python
operation = fans.insert(
    "fan-1",
    name="Tunnel Fan",
    enabled=False,
)

await operation
```

Remove:

```python
operation = fans.remove("fan-1")

await operation
```

Collection operations participate in the same transaction and failure model as normal State mutations.

---

# Transactions

State mutations can be grouped into one atomic transaction:

```python
with state.transaction() as transaction:
    fan.enabled = True
    fan.speed = 75

await transaction
```

A transaction is validated and executed atomically by the Store.

Either all canonical mutations succeed or none do.

The transaction itself is awaitable.

---

## Transaction lifecycle

Reactive objects expose transaction lifecycle events:

```text
on_pending
on_success
on_failed
on_done
```

State also exposes:

```text
is_pending
pending_count
```

Transactions can contain mutations affecting multiple State objects.

---

# Canonical Store Events

Canonical data changes are represented using Store events such as:

```text
SetEvent
SetRowEvent
InsertEvent
RemoveEvent
```

A successful Store execution produces a `StoreCommit`.

Example conceptual structure:

```python
StoreCommit(
    events=(...),
    signals=(...),
    transaction_id="...",
    server_instance_id="...",
    schema_revisions={
        "FanSchema": 12,
    },
)
```

State-changing events advance schema revisions.

Signals do not.

---

# Revisions

`kivyfn-store` uses **per-schema integer revisions**.

Example:

```text
FanSchema: 14
HouseSchema: 8
AlarmSchema: 31
```

A schema advances once per successful transaction that changes that schema.

Multiple field changes inside one transaction still advance the schema only once.

Revisions are used to detect missing or out-of-order subscription events.

They are not used as a replay journal.

Reconnect synchronization uses a fresh subscription snapshot when required.

---

# Subscriptions

Subscriptions are schema-based rather than field-based.

There are three primary subscription forms:

```text
Single schema
Collection schema
Query
```

The Host owns active client subscriptions.

The Client retains desired subscriptions across reconnects.

The StateManager maps subscription IDs back to reactive State objects.

---

## Single schema subscription

```python
controller = state.create(ControllerSchema)
```

The State receives canonical updates for the schema.

---

## Collection subscription

```python
fans = state.create(FanSchema)
```

The collection receives:

* row inserts
* row removals
* row field changes
* schema signals

The StateManager performs fine-grained Kivy dispatch after receiving the canonical subscription event.

---

# Queries

Queries provide live, read-oriented projections over Store data.

Example:

```python
from kivyfn.store.query import Query, field


active_fans = state.create(
    Query(FanSchema).where(
        field("enabled") == True
    )
)
```

The query behaves as a reactive collection.

Rows automatically enter or leave as Store data changes.

---

## Query joins

Joins are explicit.

```python
users = state.create(
    Query(UserSettingsSchema).join(
        "user_id",
        as_="user",
    )
)
```

Joined output is flat and alias-prefixed.

For example:

```text
username
theme
user_username
user_email
```

Joined fields retain metadata describing their canonical origin.

That allows joined projected fields to remain writable.

Example:

```python
row.user_username = "updated"
```

The StateManager resolves this back to the canonical target:

```text
UserSchema
    row ID
    username
```

and sends a normal `SetRowRequestAction`.

The Store never needs to understand projected query field names such as `user_username`.

---

## Query shape metadata

Query fields carry source metadata similar to:

```python
QueryFieldShape(
    name="user_username",
    schema="UserSchema",
    field="username",
    relation_path=("user",),
    nullable=False,
)
```

This allows the State layer to map projected fields back to canonical Store fields.

---

# Signals

Signals represent ephemeral occurrences.

They are not stored State.

Example schema:

```python
from kivyfn.store.schema.signal import Signal


class ScaleSchema(ManySchema):
    tare = Signal()
```

The generated row State exposes:

```python
scale.tare()
```

or:

```python
scale.tare(
    {
        "source": "button",
    }
)
```

---

## Signal listeners

A row can listen to its own signal:

```python
scale.bind(
    on_tare=lambda state, data: ...
)
```

The collection can listen to signals from every row:

```python
scales.bind(
    on_tare=lambda collection, row_id, data: ...
)
```

There is intentionally no:

```python
scales.tare()
```

Signals belonging to a `ManySchema` are emitted by individual rows.

---

## Signal delivery order

For a row signal:

```text
row State callback
    ↓
collection callback
```

For example:

```text
scale.on_tare
    ↓
scales.on_tare
```

---

## Signal semantics

Signals:

* have no canonical value;
* have no optimistic overlay;
* have no snapshot;
* are not persisted;
* are not rolled back as values;
* never coalesce;
* never advance schema revisions.

Two calls:

```python
scale.tare()
scale.tare()
```

represent two distinct events.

---

## Signals and transactions

Signals participate in State transactions.

Example:

```python
with state.transaction():
    fan.speed = 50
    fan.started(
        {
            "reason": "automatic",
        }
    )
```

Signal dispatch occurs only after canonical mutations succeed.

Conceptually:

```text
validate transaction
    ↓
apply canonical mutations
    ↓
mutation failure?
    ├── yes → rollback, publish no signals
    └── no
         ↓
finalize canonical State
         ↓
publish queued signals
         ↓
return transaction response
```

Therefore signal callbacks always observe the final committed State.

Signals preserve ordering relative to other signals:

```python
fan.starting()
fan.started()
```

dispatches:

```text
starting
started
```

after the transaction's retained State mutations have completed.

---

# Signal-Only Commits

A signal can produce a successful commit containing no canonical State events:

```python
StoreCommit(
    events=(),
    signals=(...),
    schema_revisions={},
)
```

This is intentional.

Signals are still delivered to subscribers even though no revision advances.

---

# Query Signals

Root-schema signals can be delivered to live query rows.

A signal is sent to a query only if the row belongs to the query after the transaction's canonical mutations have been captured.

This preserves the rule:

> Signal callbacks observe final committed State.

Joined-schema signal aliases are a separate extension and are not required for the basic root-query signal path.

---

# Store

`Store` owns canonical data behavior.

Its responsibilities include:

* booting Store data;
* canonical snapshots;
* query execution;
* transaction execution;
* persistence;
* schema revisions;
* commit publication;
* schema lookup.

The Store does not own UI State.

---

# Engine

The Engine remains synchronous and focused on canonical retained data mutations.

It processes instructions such as:

```text
SetInstruction
SetRowInstruction
InsertInstruction
RemoveInstruction
```

Signals intentionally do not require a core Engine instruction.

The Store resolves signals separately and attaches them to a successful commit only after the mutation Engine succeeds.

This keeps ephemeral events out of the canonical mutation engine.

---

# StoreHost

`StoreHost` exposes one Store through a transport.

Its responsibilities include:

* handling client connections;
* handling request batches;
* managing subscriptions;
* routing Store commits;
* managing live queries;
* delivering subscription events.

The Host is transport-independent.

---

# StoreClient

`StoreClient` owns the client-side connection and protocol layer.

Responsibilities include:

* connection lifecycle;
* request batching;
* transaction requests;
* one-shot queries;
* desired subscriptions;
* reconnect/resubscription;
* revision tracking;
* incoming event delivery.

The Client does not interpret individual State fields or signals.

That belongs to `StateManager`.

---

# StateManager

`StateManager` connects reactive State to `StoreClient`.

It is responsible for:

* creating State transactions;
* converting State actions into protocol actions;
* optimistic overlays;
* rollback;
* canonical event application;
* query-field canonical routing;
* incoming signal dispatch;
* transaction lifecycle updates.

Typical flow:

```text
State mutation
    ↓
StateManager
    ↓
StateTransaction
    ↓
RequestAction
    ↓
StoreClient
```

And on the way back:

```text
SubscriptionEvent
    ↓
StateManager
    ↓
State / CollectionState / QueryCollectionState
```

---

# Protocol

Requests are sent in ordered batches.

Important request actions include:

```text
SetRequestAction
SetRowRequestAction
InsertRequestAction
RemoveRequestAction
SignalRequestAction
SignalRowRequestAction
```

Transactions contain one ordered tuple of request actions.

Responses primarily acknowledge success or return structured failure information.

Canonical data arrives through subscription events rather than being duplicated in mutation responses.

---

# Transport

The architecture is transport-independent.

Current transport support includes:

```text
Direct transport
Socket.IO transport
```

The Direct transport is useful when the Store and client run in the same process.

Socket.IO supports remote Store access.

The Store, StateManager, and protocol do not depend on Socket.IO-specific behavior.

---

# Persistence

Store data can be persisted and restored across restarts.

Persistence is controlled by schema metadata.

The persistence layer stores canonical data rather than reactive State overlays.

Deletion and retained State changes are persisted through normal Store commits.

---

# Store Versioning and Migration

Store persistence is versioned at the Store level.

Migration policy supports sequential migrations:

```text
v1 → v2 → v3
```

Migration steps must be contiguous.

If a required migration is unavailable, Store boot follows the configured recovery policy rather than silently interpreting incompatible persisted data.

After successful migration, obsolete Store versions can be discarded.

---

# Error Model

Store errors are structured by:

```text
code
category
status
stage
context
```

Examples include:

```text
field validation failure
foreign-key failure
unique constraint failure
row not found
conflict
Store state failure
internal failure
network failure
timeout
```

State transactions convert Store failures into application-facing transaction failures while preserving rollback behavior.

---

# Testing

Run the complete test suite:

```bash
pytest tests
```

Run with live logging:

```bash
pytest tests -o log_cli=true -o log_cli_level=DEBUG
```

Run static type checking:

```bash
pyright
```

Current test coverage includes:

* optimistic mutations;
* strict mutations;
* inserts and removes;
* transaction rollback;
* transaction lifecycle events;
* foreign-key triggers;
* query membership;
* joined query projection;
* joined-field writes;
* reconnect synchronization;
* persistence;
* schema revision tracking;
* partial query dependency revision updates;
* signal codec round trips;
* single-schema signal events;
* row signal events;
* collection signal aggregation;
* signal callback ordering.

Current baseline:

```text
47 tests passed
```

---

# Project Status

The current refactor has established the primary architecture:

```text
Schema
    ↓
Store
    ↓
StoreHost
    ↓
StoreClient
    ↓
StateManager
    ↓
Reactive State
```

Implemented major features include:

* State and CollectionState;
* QueryCollectionState;
* optimistic and strict mutations;
* atomic StateTransaction;
* schema subscriptions;
* live queries;
* explicit joins;
* writable joined fields;
* per-schema revisions;
* reconnect synchronization;
* Store persistence;
* Direct and Socket.IO transports;
* State-generated signals;
* signal transport and codec support;
* signal subscription routing.

The project is intentionally moving away from separate local/remote application APIs and toward one State-centered application interface.

---

# Design Direction

`kivyfn-store` should remain split into clear layers:

```text
Store
    canonical data
    validation
    queries
    persistence
    transactions

Host / Client
    protocol
    subscriptions
    transport
    synchronization

State
    reactive application API
    optimistic UI behavior
    transactions
    signal bindings
    Kivy events
```

The Store should remain usable without Kivy-specific application logic.

The State layer should remain the primary interface for applications that need live reactive data.

---

## License

Add the project license here when finalized.

