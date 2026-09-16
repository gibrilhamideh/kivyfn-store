# Reactive state ownership

This refactor keeps host subscriptions and host live-query evaluation. It
changes how the client-side objects divide their work.

| Object | Responsibility |
| --- | --- |
| `BaseData` | Abstract contract for reads, canonical patches, and transaction overlays. |
| `StateData` | Confirmed and optimistic field values, with defensive copying. |
| `CollectionData` | Confirmed and optimistic row references, preserving State identity. |
| `TransactionStatus` | Shared pending transaction tracking and participant event delivery. |
| `State` | Field reads, canonical values, optimistic values, field notifications. |
| `BaseCollection` | Shared row access, row creation, host event routing, and notifications. |
| `CollectionState` | Owns `CollectionData` for canonical membership and optimistic inserts/removals. |
| `QueryCollectionState` | Query metadata and a plain dictionary of host-confirmed rows. |
| `StoreClient` | Connection lifecycle, request batches, and ordered response/event delivery. |
| `ClientSubscriptions` | Desired sources, server subscription IDs, and revision bookkeeping. |
| `RequestFutures` | Request waiters, reply completion, and disconnect failure. |
| `StateTransaction` | Ordered action recording, removal decisions, and transaction completion. |
| `StateManager` | Source-to-target routing, transaction recording/submission/completion, projected write resolution. |
| `NotificationBatch` | Collect callbacks until all affected data has been installed. |

Both collection classes inherit `BaseCollection` for row access and host
updates. It has no transaction lifecycle or overlay methods. `State` and
`CollectionState` each own a `TransactionStatus` helper; there is no shared
`BaseState` superclass. `MembershipDelta` is defined in `data.py` and re-exported
from `base.py`, keeping the data layer independent of Kivy.

A collection tracks transactions that insert or remove its rows. Editing a row
marks the row as pending; it does not automatically mark the collection pending.
A query container has no pending properties or transaction events. Its rows
still follow committed host results, and its individual projected States retain
their field editing and transaction lifecycle behavior.

## Data storage and the common contract

Both data implementations inherit `BaseData`, an `ABC`. Required methods use
`@abstractmethod`; an incomplete implementation cannot be instantiated. Shared
methods are `canonical`, `visible`, `set_canonical`, `set_overlay`,
`discard_overlay`, `canonical_snapshot`, and `visible_snapshot`.

`StateData` stores field values and returns changed field names. It deep-copies
mutable field data. `CollectionData` stores row references and returns a
`MembershipDelta`. It never copies, creates, or modifies a row object.

Collection data updates use a row object for presence and `None` for absence:

```python
data.set_canonical({"a": row_a, "b": row_b})
data.set_overlay("tx", {"a": None, "c": row_c})
# Visible: b and c. Confirmed: a and b.
data.discard_overlay("tx")
# Visible: a and b, assuming no intervening host update.
```

The reactive collection uses the same payload: `{row_id: state}` adds a row,
and `{row_id: None}` removes it. There is no boolean-to-State conversion.
Insert and snapshot handling resolve the State objects first; the setters
validate their IDs and store their references without calling a row factory.

Only `CollectionState` owns `CollectionData` and exposes collection overlay
methods. `QueryCollectionState` keeps a plain row dictionary because all its
row additions and removals come from the host. Its individual rows are still
`State` objects with their own `StateData`. `BaseCollection` receives a read-only
row view from either subclass and contains no data object or overlay storage.

Inside `CollectionData`, `_canonical` holds confirmed rows, `_overlays` holds
transaction decisions, and `_visible` holds their merged result. `visible_view`
is a stable read-only mapping onto `_visible`. It is not an overlay or a copy.
Snapshots copy the dictionary while preserving the contained row references.

The collection's `_rows_view` and observable public `rows` property expose that
same read-only mapping. They do not introduce another row store. KV bindings
continue to use `collection.rows`; its entries are the actual reactive States.

Deleting a collection entry hides a reference; it does not erase the State's
field data. If an optimistic delete fails, discarding that overlay reveals the
same State with any host field updates received while it was hidden. An
optimistic insert applies two changes in one notification batch: the collection
gets the State reference, and that State gets its own field overlay.

`set_canonical` patches selected entries. `replace_canonical` is a
collection-specific operation for complete membership snapshots; it removes
unspecified rows while preserving optimistic overlays and surviving row order.

Reactive wrappers consume the returned changes and arrange notification.
There is no callback dispatch, sync ownership, row factory, or schema validation
inside the data layer.

## Following an optimistic insert

1. Application code assigns `rows[row_id] = values`.
2. `CollectionState` validates and forwards the intent to the manager.
3. The manager records the insert in the current transaction, or creates one.
4. The manager resolves the State for the row ID, then
   `collection.apply_insert_overlay(...)` installs the reference and row values.
5. Its notification batch publishes fields and then membership callbacks.
6. The manager submits the recorded transaction when its context closes.

The manager no longer collects changed fields or calls `state.publish()` after
applying an overlay.

## Transaction decisions and startup

`StateTransaction.add_remove()` owns the interpretation of the action sequence.
It returns `RemovalOutcome`, defined alongside the local actions in
`state/transaction/action.py`:

| Outcome | Recorded instructions | Manager's collection overlay action |
| --- | --- | --- |
| `RECORDED` | Append the removal, preserving preceding instructions. | Hide the row for an optimistic removal; otherwise retain its current overlay. |
| `CANCELLED_INSERT` | Drop a trailing insert. | Discard this transaction's row entry. |
| `CANCELLED_REPLACEMENT` | Drop a trailing replacement insert, retaining the earlier removal. | Keep the row hidden. |

The manager also clears this transaction's field overlay on the removed row.
It does not search the action list to reconstruct which removal outcome applies.
An intervening action prevents insert cancellation because triggers can observe
that row before removal. Adjacent singleton sets still merge, using the last
recorded action directly instead of a separate `_sets` dictionary.

At submission, the manager records the lifecycle participants and starts them
one at a time. `pending_count`, `is_pending`, and `on_pending` callbacks may
close the manager and abort that transaction. Startup checks completion before
each next participant and before sending the request. If a pending-property
callback aborts, the participant also suppresses its subsequent `on_pending`.
Already started participants finish through the normal failure path; remaining
participants never start, and the client receives no mutation request.

This guarantee applies before submission. Closing a manager after a request
has already been submitted cannot cancel work at the host.

## Following a committed message

1. The client resolves server subscription IDs to sources and checks revisions
   for the entire message. It groups `SubscriptionUpdate(source, actions)`
   values by manager.
2. The manager opens a notification collection and routes retained actions to
   `target.apply_event(action)`.
3. Collections choose a handler from their action-type map. States install
   singleton fields. Each target owns its storage and notifications.
4. The manager removes only the matching transaction's optimistic overlays.
5. Each manager returns a `SubscriptionPublication` without firing callbacks.
6. Once every manager is prepared, the client publishes retained notifications,
   then signals in occurrence order.

Data changes during preparation. Property and membership callbacks wait until
publication. This distinction prevents a callback from reading a partial commit.
Signals retain their existing row deduplication and collection delivery rules.

The response future belongs to the client request. The `StateTransaction` has
its own completion future, which settles through the existing completion flow.
For successful submissions, canonical delivery still precedes awaited
transaction completion.

## Client files and response ordering

| File | Responsibility |
| --- | --- |
| `client.py` | Coordinate connection, subscriptions, batches, and delivery order. |
| `validation.py` | Match reply types and IDs to requests, and check batch length. |
| `future.py` | Create, resolve, and fail outgoing transaction/query/history waiters. |
| `subscription.py` | Keep subscription identity and revisions; define the reactive delivery contract. |
| `queue.py` | Keep outgoing requests in order. |

`StoreClient._handle_response_batch()` reads in delivery order:

1. Validate every request/reply pair before applying any reply.
2. Install subscription replies and snapshots.
3. Deliver buffered messages that arrived before their subscription replies.
4. Resolve the transaction, query, and history futures.

Validation uses a request-type-to-allowed-response-types table. It also checks
transaction and unsubscribe IDs. These are correlation checks at the client
boundary, separate from decoding message fields and checking subscription
revisions. A malformed later reply must not let an earlier request report
success. The manager trusts this boundary and no longer repeats response type
and transaction ID validation.

`RequestFutures` exposes `create_transaction`, `create_query`, `create_history`,
`resolve`, and `fail_all`. Its handler map selects the correct waiter registry.
Mutations use transaction IDs. Queries and history use request object identity,
so identical requests have independent waiters. The client queue or active
batch retains each request until its reply is handled.

Cancelling a request future does not cancel a mutation at the host. Its registry
entry remains pending until a reply or connection failure, keeping undo/redo
blocked while that mutation might still commit. Disconnect fails unfinished
waiters and clears all registries. Connection readiness and StateTransaction
completion remain separate lifecycle futures.

The client owns server subscription IDs. Managers only keep a source-to-target
map and implement three `SubscriptionManager` callbacks:

- `on_subscription_snapshot(source, response)` installs received rows/fields.
- `prepare_subscription_updates(updates, transaction_id=...)` stages data and
  returns a `SubscriptionPublication` without running its callbacks.
- `on_subscription_status(source, synced)` updates readiness.

The client prepares every affected manager before publishing any of them.
Retained changes publish before signals, with signals ordered by occurrence ID
across managers. The protocol, host subscriptions, and live-query evaluation
are unchanged.

## Internal integration changes

Application mutation APIs remain the same. Code that directly called the
synchronization helpers must use their new contracts:

```python
state.set_canonical(values)
state.set_overlay(transaction_id, values)
state.discard_overlay(transaction_id)

collection.set_canonical({row_id: state})
collection.set_overlay(transaction_id, {row_id: None})
collection.discard_overlay(transaction_id, (row_id,))
collection.replace_canonical({row_id: state})
```

These methods now own notification and return `None`. Do not pass their result
to `publish()`. The old collection names `set_membership_overlay` and
`discard_membership_overlay` have been replaced by `set_overlay` and
`discard_overlay`.

Collections now use `set_canonical({row_id: state_or_none})` for patches and
`replace_canonical({row_id: state})` for complete row sets. These methods, plus
the `enter` and `leave` conveniences, return `None` and own their notifications.
Do not follow them with a separate publish call. `set_membership`,
`publish_membership`, and `_dispatch_membership` have been removed.

`publish` is the common name for the internal callback delivery method. The
manager continues to use `apply_event` and `apply_snapshot`.

At the data layer, `StateData.remove_overlay` is now `discard_overlay`, matching
`CollectionData`. The data methods still return changes to their owning wrapper.

Row synchronization ownership now uses `_synced_owners`, a `WeakSet`. A row
remains synced while any confirmed owning collection is synced. Optimistic
membership alone does not establish synchronization ownership.

Use `notification_batch()` for one immediate compound update. Nested operations
join the current batch. `collect_notifications()` captures notifications without
publishing them on exit; the manager uses it to prepare client delivery.
Neither context manager rolls back storage on exceptions.

The request and snapshot handler maps live in the manager's constructor. The
row event handler map lives in `BaseCollection`. Conditional guards remain for
validation, transaction state, and optimistic cancellation rules.

Custom reactive consumers now implement the single `SubscriptionManager`
protocol in `client/subscription.py`. `ClientSubscriptionManager`, manager
`bind_subscription`/`unbind_subscription`, `on_subscription_event`,
`on_subscription_message`, and `prepare_subscription_message` are removed.
Custom delivery code passes resolved sources through `SubscriptionUpdate` to
`prepare_subscription_updates`. Client transports continue delivering the
existing `EventMessage` wire protocol to `StoreClient.on_message`.

Internal callers of `StateTransaction.add_remove()` must now inspect its
`RemovalOutcome` instead of interpreting the result as a boolean.

## Validation of this update

- Ruff lint and format checks pass for all changed Python files.
- Python compilation checks pass.
- Supplemental runtime checks: 650 cases pass across regression and focused
  runs with actual Kivy 2.3.1. They cover
  client, state, query, state integration, and history behavior on direct and
  Socket.IO transports, including row identity,
  direct State-reference updates, independent query-row transactions,
  overlapping writes, rollback, callback ordering, projected writes, and
  Python/KV row bindings. Client checks include cancelled waiters, independent
  equal query requests, disconnect cleanup, malformed response batches,
  reconnects, and staging multiple managers before callbacks and replies.
  Transaction cases cover startup aborted by pending callbacks, cancellation
  of inserts and replacements, strict and optimistic removals, and retained
  trigger ordering. Startup aborts are checked at the first and last participant
  and through both real transports.
- These supplemental checks used a process-local standard-library logging
  substitute for the unavailable `kivyfn.logging` module. That substitute is
  not included in this update and does not qualify as release validation.
- One subprocess import check failed because its fresh interpreter could not
  import `kivyfn.logging`. The normal pytest command is blocked during collection
  by the same missing dependency.
- Pyright reports four missing `kivyfn.logging` imports in the checked client
  and state code, with no other errors in that code or the checked tests.
  The required genuine `kivy-stubs` checkout is also absent, so the full typing
  gate is still open.

After unpacking, use the real sibling dependencies configured in `pyproject.toml`
and run:

```bash
uv sync --group dev
uv run pytest tests/client tests/state tests/query \
    tests/integration/state tests/integration/test_history.py -q
uv run pyright
```

The ZIP contains all changed files relative to the original uploaded archive,
rooted at `src/`, `tests/`, and `docs/`. It includes the previous manager and data
refactors, separate query storage and lifecycle changes, client delivery, and
transaction startup/removal changes, so it works with or without the earlier ZIP.
Extract it from your project root:

```bash
unzip -o ~/Downloads/kivyfn-store-state-refactor.zip
```
