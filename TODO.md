# Kivyfn Store Refactor TODO

## Core Architecture

### Transactions

* [ ] Make **every Store write a transaction**.
* [ ] A transaction contains one or more actions.
* [ ] A normal mutation is an implicit transaction with one action.
* [ ] An explicit transaction groups multiple actions into one atomic Store write.

Examples:

```text
state.age = 30

Transaction
└── SetAction(age=30)
```

```text
with manager.transaction():
    state.age = 30
    settings.enabled = True

Transaction
├── SetAction(age=30)
└── SetAction(enabled=True)
```

* [ ] The Store should have one write execution path:
  `Transaction -> validate -> execute atomically -> events -> response`.
* [ ] Remove separate Store-level mutation paths where possible.
* [ ] Keep State-facing methods such as `request_set()`,
  `request_insert()`, etc. for clarity, but normalize them into a
  transaction internally.
* [ ] A transaction must belong to exactly one `StateManager`.
* [ ] Therefore, a transaction also belongs to exactly one `StoreClient`.
* [ ] Raise an error if a transaction attempts to include a State or
  CollectionState owned by another manager/client.
* [ ] Do not limit an application to one StoreClient globally. Only enforce
  transaction ownership.

---

## Remove StateOperation

* [ ] Remove `StateOperation`.
* [ ] Remove `operation_id`.
* [ ] Replace the write identity with `transaction_id`.
* [ ] Move the awaitable lifecycle currently provided by `StateOperation`
  into `StateTransaction`.
* [ ] `StateTransaction` should be awaitable.
* [ ] `StateTransaction` should expose:

```text
id
done
success
failed
failure

on_success
on_failed
on_done
```

* [ ] A single mutation such as `state.set()` should return a transaction.
* [ ] An explicit transaction context should expose the same transaction
  object.

Expected API:

```python
await state.set(age=30)
```

and:

```python
with manager.transaction() as transaction:
    state.set(age=30)
    settings.set(enabled=True)

await transaction
```

* [ ] `State.set()` remains a synchronous method.
* [ ] It returns an awaitable `StateTransaction`.
* [ ] Mutation creation/staging is synchronous.
* [ ] Transaction completion is asynchronous.
* [ ] Do not allow awaiting an explicit transaction while its context is
  still open.

---

## StateTransaction Actions

Keep local transaction actions for building transactions and tracking local
participants.

Current action concepts:

```text
SetAction
SetRowAction
InsertAction
RemoveAction
SignalAction
```

* [ ] Review action fields and separate **local bookkeeping** from
  **wire/protocol data**.
* [ ] Local actions may contain references to `State` and
  `CollectionState`.
* [ ] Protocol transaction actions must never contain Python State objects.
* [ ] Convert local actions into protocol actions before encoding.

Example local action:

```text
SetAction
├── state
├── old
└── new
```

Example protocol action:

```text
SetAction
├── schema
└── values
```

* [ ] Reevaluate whether `old` and `new` are both still required locally.
* [ ] Do not use transaction responses to update canonical State data.
* [ ] Optimistic rollback should primarily work by removing the overlay and
  exposing canonical data again.

---

## TransactionContext

Create manager-side transaction bookkeeping.

```python
@dataclass(slots=True)
class TransactionContext:
    transaction: StateTransaction
    states: set[State[Any]]
    collections: set[CollectionState[Any]]
```

* [ ] `StateManager` maintains:

```text
transaction_id -> TransactionContext
```

* [ ] Add a State to `context.states` when the transaction directly mutates
  that State.
* [ ] Add a CollectionState to `context.collections` when the transaction
  directly mutates collection membership.
* [ ] Query result States are not transaction participants merely because
  the transaction indirectly changes their query result.
* [ ] Use `TransactionContext` to determine which local reactive objects
  receive transaction lifecycle completion.
* [ ] Remove the context after the transaction reaches success or failure.

---

## State / Collection Pending Tracking

* [ ] Rename operation tracking to transaction tracking.

Change concepts such as:

```text
_pending_operations
```

to:

```text
_pending_transactions
```

* [ ] State and CollectionState should store transaction IDs, not transaction
  objects.
* [ ] They only need the IDs to maintain:

```text
is_pending
pending_count
```

Example:

```text
_pending_transactions = {
    "TX-1",
    "TX-2",
}
```

means:

```text
is_pending = True
pending_count = 2
```

* [ ] State/CollectionState do not own transaction lifecycle.
* [ ] `StateManager` owns transaction lifecycle.
* [ ] State/CollectionState are transaction participants.

---

## Kivy Lifecycle Events

Keep:

```text
on_pending
on_success
on_failed
on_done
```

* [ ] Keep the boolean property named `is_pending` so it does not conflict
  with the custom Kivy `on_pending` event.
* [ ] A transaction should be added to a State/Collection pending set once,
  even if it changes multiple fields.
* [ ] Do not dispatch operation lifecycle events once per field.
* [ ] Individual field changes are already observable through Kivy property
  events.

Conceptually:

```text
Property events
    = what data changed

Transaction lifecycle events
    = whether the logical write is pending/successful/failed/done
```

* [ ] For a transaction touching multiple States, each directly
  participating State gets its lifecycle event once.
* [ ] The transaction object itself gets its lifecycle event once.

---

## Transaction Response

A successful transaction response should be minimal.

Conceptually:

```text
TransactionResponse
└── transaction_id
```

A failure response:

```text
TransactionFailureResponse
├── transaction_id
└── failure
```

* [ ] Do not return transaction actions from a successful response.
* [ ] Do not return canonical values from a successful transaction response.
* [ ] Do not update State data from a transaction response.
* [ ] The response exists to **finish the transaction lifecycle**.
* [ ] Failure information may include schema/field/action information where
  useful for diagnostics.

---

## Events vs Responses

Lock in this invariant:

> Events update canonical reactive data. Responses finish transaction
> lifecycle.

Successful flow:

```text
Store commits transaction
        ↓
Store/Host produces subscription events
        ↓
Host sends events to requesting client
        ↓
Client applies canonical State/Collection updates
        ↓
Host sends TransactionResponse
        ↓
Client routes response to manager
        ↓
Manager completes transaction
        ↓
State / Collection:
    on_success
    on_done
        ↓
Transaction:
    on_success
    on_done
```

Failure flow:

```text
Store rejects transaction
        ↓
No successful canonical events
        ↓
TransactionFailureResponse
        ↓
Manager removes optimistic state
        ↓
State / Collection:
    on_failed
    on_done
        ↓
Transaction:
    on_failed
    on_done
```

* [ ] Guarantee that transaction-generated events for the requesting client
  are emitted before the successful transaction response.
* [ ] Do not dispatch transaction success from the event handler.
* [ ] Do not update canonical data from the response handler.

---

## Subscription Model

A subscription represents one **logical reactive object**.

Rule:

```text
one reactive object = one subscription
```

Examples:

```text
SUB-1 -> State

SUB-2 -> CollectionState

SUB-3 -> QueryCollectionState
```

* [ ] Do not create a separate subscription for every State inside a
  CollectionState.
* [ ] Do not create a subscription per schema dependency.
* [ ] A subscription may depend on multiple schemas.
* [ ] The same schema may be a dependency of many subscriptions.
* [ ] Identical logical queries should reuse the same QueryCollectionState
  through the existing hashable Query/registry behavior.
* [ ] Therefore identical logical reactive objects naturally reuse one
  subscription.

---

## Client-Side Subscription Routing

Decoded events should be simple.

Conceptually:

```python
Event(
    subscription_id="SUB-1",
    actions=(...),
)
```

* [ ] Events do not need `transaction_id`.
* [ ] Events do not need `request_id`.
* [ ] Events do not need `client_id`.
* [ ] `subscription_id` identifies which local subscription receives the
  canonical update.

Client flow:

```text
decoded Event
    ↓
subscription_id
    ↓
client subscription lookup
    ↓
StateManager
    ↓
reactive object
    ↓
apply canonical event
```

* [ ] Client may pass `subscription_id` and the decoded event directly to
  `StateManager.on_event(...)`.
* [ ] StateManager should maintain enough subscription routing information
  to locate the reactive object.

---

## Event Actions

Transaction actions and event actions are different concepts.

Transaction actions mean:

> What the client asked the Store to do.

Event actions mean:

> What canonical data changed for this subscription.

Possible event actions:

```text
SetEvent
SetRowEvent
InsertEvent
RemoveEvent
SignalEvent
```

Exact naming still needs to be decided.

### State subscription

State primarily needs:

```text
Set
```

### CollectionState subscription

CollectionState may receive:

```text
Insert
Remove
SetRow
```

Routing:

```text
Insert
    -> CollectionState creates/adds State

Remove
    -> CollectionState removes State

SetRow
    -> CollectionState finds row State
    -> row State applies canonical data
```

### QueryCollectionState subscription

QueryCollectionState behaves similarly to a read-only collection:

```text
Insert
Remove
SetRow
```

* [ ] Query subscriptions cannot request insert/remove themselves.
* [ ] Query result events may still contain insert/remove because membership
  of the query result can change.

---

## Store-Side Subscription Architecture

A Store subscription has a context.

Conceptually:

```python
@dataclass(slots=True)
class SubscriptionContext:
    id: str
    client_id: str
    source: SubscriptionSource
    dependencies: frozenset[str]
```

* [ ] Store generates the `subscription_id`.
* [ ] Store tracks:

```text
subscription_id -> SubscriptionContext
```

* [ ] Store also maintains a reverse dependency index:

```text
schema -> set[subscription_id]
```

Example:

```text
subscriptions:

SUB-1
    source = UserSchema
    dependencies = {UserSchema}

SUB-2
    source = Query(...)
    dependencies = {
        UserSchema,
        HouseSchema,
    }


dependency index:

UserSchema
    -> SUB-1
    -> SUB-2

HouseSchema
    -> SUB-2
```

* [ ] A committed transaction reports which schemas were touched.
* [ ] Touched schemas are used only to find subscriptions that may need
  processing.
* [ ] Do not treat "schema touched" as the actual subscription event.
* [ ] Each affected subscription determines what canonical change occurred
  for its own result.
* [ ] Query subscriptions may need reevaluation when any dependency schema
  changes.

---

## Subscription Events

Lock in this distinction:

```text
Transaction action
    = what Store should do

Touched schema
    = which subscriptions may be affected

Subscription event
    = what actually changed for one subscription
```

Example:

```text
Transaction:
    SetRow UserSchema row=5
```

may cause:

```text
SUB-1 State:
    Set(...)

SUB-2 Collection:
    SetRow(...)

SUB-3 Query:
    Insert(...)

SUB-4 Query:
    no event
```

The same Store mutation can produce different event actions for different
subscriptions.

---

## Client-Side Subscription Context

Design a client-side subscription structure.

At minimum it needs to support:

```text
subscription_id
    ↓
reactive object / manager routing
```

Possible structure:

```python
@dataclass(slots=True)
class ClientSubscriptionContext:
    subscription_id: str
    target: SubscriptionTarget
```

or keep manager ownership and let the manager maintain target routing.

Exact ownership still needs to be finalized.

Important invariant:

```text
subscription_id
    -> exactly one logical reactive object
```

That object may itself contain many State rows.

---

## Request ID

Do not refactor this yet until transaction and subscription flow are stable.

Current intended meaning:

```text
request_id
    = correlation between one protocol request and its response
```

* [ ] Revisit whether `request_id` remains necessary once the final protocol
  batching and strict response ordering are established.
* [ ] Do not mix `request_id` with transaction identity.
* [ ] Do not use `request_id` for subscription event routing.
* [ ] Do not use `request_id` for State transaction lifecycle.

---

## StateManager Responsibilities

After the refactor, StateManager should primarily coordinate:

```text
reactive object creation
transaction creation
transaction contexts
optimistic staging/rollback
transaction lifecycle completion
subscription event routing to reactive objects
```

* [ ] Remove networking/protocol-specific logic where it belongs in
  StoreClient.
* [ ] Keep State unaware of protocol responses.
* [ ] Keep StoreClient unaware of State implementation details.
* [ ] StateManager remains the bridge between those layers.

Desired separation:

```text
State / CollectionState
    reactive data

StateManager
    coordination

StoreClient
    protocol/client communication

Host
    client routing

Store
    canonical data + atomic transactions
```

---

## StoreClient Responsibilities

* [ ] Send transactions.
* [ ] Receive/decode transaction responses.
* [ ] Receive/decode subscription events.
* [ ] Maintain client-side subscription registrations.
* [ ] Route transaction responses to the owning StateManager.
* [ ] Route subscription events to the owning StateManager.
* [ ] Do not modify State data itself.
* [ ] Do not dispatch State transaction lifecycle events itself.

---

## Remove Old / Duplicate Refactor Code

* [ ] Delete old duplicate `StateOperation` implementation.
* [ ] Delete old duplicate failure implementation if superseded by
  `state/mutation/failure.py`.
* [ ] Update all imports to canonical paths.
* [ ] Remove protocol request/response classes superseded by the unified
  transaction protocol.
* [ ] Remove separate write response types if all writes become
  `TransactionResponse`.
* [ ] Remove stale manager code that expects canonical values in write
  responses.
* [ ] Remove stale mutation code that relies on `operation_id`.
* [ ] Rename operation-related variables/maps/events where they now mean
  transactions.

Examples:

```text
operation_id
    -> transaction_id

_pending_operations
    -> _pending_transactions

_SetContext / mutation contexts
    -> TransactionContext
```

---

## Typing

* [ ] Keep truthful generics:

```python
State[UserSchema]
CollectionState[UserSchema]
QueryCollectionState[UserSchema]
```

* [ ] Do not pretend a `State[UserSchema]` is actually a `UserSchema` merely
  to obtain autocomplete.
* [ ] Keep schema generic information available for future `kivyfn-lsp`
  integration.
* [ ] Future Kivyfn-aware tooling can provide dynamic schema-field completion
  for:

```python
state: State[UserSchema]

state.age
```

without lying about the Python runtime type.

---

## Tests During Refactor

Add tests incrementally for the new contracts.

### Transaction

* [ ] Single Set becomes one-action transaction.
* [ ] SetRow becomes one-action transaction.
* [ ] Insert becomes one-action transaction.
* [ ] Remove becomes one-action transaction.
* [ ] Signal becomes one-action transaction.
* [ ] Explicit transaction groups multiple actions.
* [ ] Same-field updates merge correctly.
* [ ] Transaction cannot cross StateManagers.
* [ ] Transaction cannot cross StoreClients.
* [ ] Transaction is awaitable after submission.
* [ ] Explicit transaction cannot be awaited while still open.
* [ ] Successful response completes transaction.
* [ ] Failure rolls optimistic state back.

### Lifecycle

* [ ] `is_pending` becomes true when transaction begins.
* [ ] `pending_count` counts unique transactions.
* [ ] Multiple field edits in one transaction count once.
* [ ] `on_pending` dispatches once per participating reactive object.
* [ ] `on_success` dispatches only after canonical event updates.
* [ ] `on_failed` dispatches on transaction failure.
* [ ] `on_done` dispatches after success/failure.
* [ ] Transaction-level events dispatch once.

### Subscription

* [ ] Single State subscription routing.
* [ ] Collection subscription routing.
* [ ] Query subscription routing.
* [ ] Collection SetRow routes to correct row State.
* [ ] Collection Insert creates State.
* [ ] Collection Remove removes State.
* [ ] Query membership Insert/Remove works.
* [ ] One subscription can have multiple schema dependencies.
* [ ] One schema can map to multiple subscriptions.
* [ ] Identical Query objects reuse the same reactive object/subscription.
* [ ] Different queries sharing a schema remain separate subscriptions.

### Ordering Contract

* [ ] Successful subscription events are emitted before transaction response.
* [ ] State canonical data is updated before `on_success`.
* [ ] Failed transaction produces no successful canonical event.
* [ ] Transaction response never directly modifies canonical State data.

---

## Core Invariants

Keep these rules visible while refactoring:

1. **Every Store write is a transaction.**
2. **One transaction has one `transaction_id`.**
3. **A transaction contains one or more actions.**
4. **StateManager owns transaction lifecycle.**
5. **State/CollectionState only track transaction participation.**
6. **Events update canonical reactive data.**
7. **Transaction responses finish transaction lifecycle.**
8. **Successful events are delivered before the successful transaction
   response.**
9. **One logical reactive object has one subscription.**
10. **One subscription may depend on many schemas.**
11. **One schema may affect many subscriptions.**
12. **`subscription_id` routes canonical events.**
13. **Touched schemas select subscriptions; they are not themselves events.**
14. **Queries/collections route their own row-level events to contained
    States.**
15. **Keep Python types truthful; future Kivyfn LSP handles dynamic schema
    field completion.**

