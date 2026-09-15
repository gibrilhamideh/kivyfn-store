# Error handling

## Results depend on the API boundary

| API | Successful operation | Expected rejection | Interrupted execution |
| --- | --- | --- | --- |
| `Store.execute(...)` | `ExecutionResult(success=True, commit=...)` | `ExecutionResult(success=False, error=...)` | Lifecycle, persistence or unexpected errors raise |
| `await client.transaction(...)` | `TransactionResponse` | `TransactionFailureResponse` with `StoreFailure` | Transport/protocol errors raise |
| `await client.query(...)` | `QueryResponse` | `FailureResponse` with `StoreFailure` | Transport/protocol errors raise |
| `await client.connect(...)` | Desired subscriptions synchronized | `StoreSubscriptionError` with `StoreFailure`; connection stays online | Transport/protocol errors raise |
| `await state_transaction` | Transaction with `success=True` | Transaction with `failed=True` and `failure` | Submitted failures become transaction failure; cancelling the waiter still raises cancellation |

The wire protocol does not use an `ok`/`data` response envelope. Match the
actual response variant. State's application-facing failure is a simplified
`TransactionFailure(type, message)`; use StoreClient when structured failure
codes and details are required.

Local State field checks and API misuse can raise synchronously before a
request is submitted. For example, an invalid numeric assignment can raise
`FieldValidationError`, an unknown State field can raise `StoreStateError`,
and an empty `.set()` or illegal collection operation can raise
`ValueError`/`KeyError`. These are distinct from a submitted transaction that
the Store later rejects.

## State transactions

A completed StateTransaction is awaitable without raising an ordinary Store
rejection. Inspect the outcome explicitly:

```python
# Inside an async function with an existing row State.
transaction = await row.set(speed=50)
if transaction.success:
    print(row.get("speed"))
else:
    failure = transaction.failure
    assert failure is not None
    print(failure.type, failure.message)
```

Normalized failure types include `field`, `foreign_key`, `trigger`, `policy`,
`conflict`, `not_found`, `network`, `timeout` and `internal`. They do not carry
all of the lower-level StoreFailure details.

Failed transactions remove their own optimistic value and membership
overlays. Other pending operations and newer canonical changes remain
visible. Completion clears participant pending state and emits lifecycle
events even if an application callback raises. Do not use callback
exceptions as a way to reject an already committed transaction.

An exception inside an open `with state.transaction()` aborts its local
intent and is re-raised. Await a transaction after leaving that context.
Cancelling one task awaiting a submitted transaction cancels the waiter,
not the transaction: the shielded completion remains available to another
waiter. Closing StateManager finishes its outstanding local transactions,
but does not promise to undo work already sent to the Host.

## Structured Store failures

Expected domain failures derive from `StoreOperationError`, including:

- `FieldNotFoundError` and `FieldValidationError`;
- `RowNotFoundError`, `RowAlreadyExistsError`,
  `RowOperationConflictError`;
- `UniqueConstraintError`, `ForeignKeyNotFoundError`,
  `ForeignKeyRestrictedError`;
- invalid instruction/operation, missing Node and missing index errors.

A low-level Store caller can inspect the original error or normalize it:

```python
# With an already booted Store and a tuple of request actions.
result = store.execute(actions, transaction_id="application-operation-id")
if not result.success:
    assert result.error is not None
    failure = result.error.failure()
    print(failure.code, failure.detail, failure.context)
```

StoreFailure contains stable `code`, `category`, `status`, structured `detail`,
optional `context`, and diagnostic `message`. Match codes/status/details;
do not parse human-readable messages for program logic. Code/category/status
combinations are checked by the failure codec instead of silently accepting
contradictory wire failures.

Context records the detection stage and, where relevant, schema, address,
opcode, source, row ID, field and value. Lower layers preserve semantic
error identity while higher layers add context. `RESOLVE`, `COMPILE` and
`VALIDATE` are the main current transaction rejection stages. The stage enum
also retains other protocol/runtime vocabulary; it is not a promise that
every error populates every context field.

## Client handling

Use a unique transaction ID for each new logical request. This example
handles typed replies and transport interruption separately:

```python
# Inside an async function with a connected StoreClient.
from uuid import uuid4

from kivyfn.store.error import StoreTransportError
from kivyfn.store.protocol.message.requests import SetRowRequestAction
from kivyfn.store.protocol.message.responses import TransactionFailureResponse

try:
    reply = await client.transaction(
        transaction_id=uuid4().hex,
        actions=(
            SetRowRequestAction(
                schema="FanSchema", row_id="fan-1", values=(("speed", 50),)
            ),
        ),
    )
except StoreTransportError as error:
    print("Connection interrupted; outcome may be unknown:", error)
else:
    if isinstance(reply, TransactionFailureResponse):
        print(reply.failure.code, reply.failure.message)
```

This lower-level API does not itself create a State subscription. Application
State normally uses StateManager to receive canonical data and lifecycle
updates together.

Subscription rejection is separate from connection failure:

```python
from kivyfn.store.error import StoreSubscriptionError

try:
    await client.connect(endpoint)
except StoreSubscriptionError as error:
    print(error.failure.code, error.failure.message)
    # The Client is still online; healthy State targets remain synchronized.
```

The rejected target stays unsynchronized. Once outstanding subscription
replies have been processed, `connect()` raises the first retained rejection
in desired-source order. A later call retries only inactive subscriptions;
successful bindings and the transport remain in place. Releasing a failed
source removes its synchronization requirement. A late failure for a released
subscription cannot mark its replacement failed.

An unsubscribe rejection is logged and leaves other subscriptions usable.
The local source is already released; any remaining Host binding is cleaned
up when the connection closes. Neither subscription nor unsubscribe rejection
causes automatic transaction retries.

## Runtime errors

| Exception | Meaning |
| --- | --- |
| `StoreStateError` | Invalid lifecycle or unsupported reentrant Store mutation |
| `StoreConnectionError` | Connection unavailable or lost |
| `StoreTimeoutError` | Connection/request timeout |
| `StoreProtocolError` | Malformed messages, mismatched replies, invalid instance/revision stream |
| `StoreSubscriptionError` | Host rejected a desired subscription; structured failure is available as `.failure` |
| `StoreInternalError` | Safe report of an internal runtime failure |
| `StorageCorruptionError` | Invalid or unavailable complete persisted representation |

Connection and timeout errors derive from `StoreUnavailableError`, which
derives from `StoreTransportError`. Protocol errors also derive from
`StoreTransportError`. Lifecycle and internal errors are separate runtime
branches; not every failure is an availability problem.
`StoreSubscriptionError` derives directly from `StoreException`, independently
of transport errors.

Use application startup and shutdown boundaries for boot failure, connection
failure and resource cleanup. Do not broadly suppress exceptions inside
Store operations. Unexpected Store execution errors shut down that Store;
prepared changes remain unpublished when failure occurred before the commit
point. The Host maps internal failures to safe messages and logs server-side
tracebacks rather than exposing them on the wire.

## Unknown outcomes, retries and reconnect

A timeout or lost connection does **not** prove that a transaction failed.
The Host may have committed, sent signals and lost the acknowledgement.
StoreClient fails the interrupted request and does not automatically replay
its batch. Socket.IO reconnection is explicit through `client.connect()`;
desired subscriptions are retained and resynchronized.

Reconnect and inspect canonical State before deciding whether another
operation is appropriate. An ephemeral signal cannot be reconstructed from
a snapshot. Avoid treating `network` or `timeout` as “safe to repeat” for
signals, increments or other non-idempotent effects.

The Host's bounded in-memory completed-ID cache returns the previous response
when the same ID and exact actions are seen again. Reusing that ID with
different actions is rejected. This protection survives reconnects to the
same Host object, but not cache eviction or Host replacement. It is not a
durable execution ledger, and cached acknowledgements do not replay signals.

A malformed response invalidates the connection and completes pending
requests with an error. Subscription handoff buffers are bounded. A missing
revision, a gap, a duplicate/stale revision or an unexpected Store instance
must be resolved through a new synchronization session, not ignored while
marking State synchronized.

## Persistence failure boundaries

Store prepares values and indexes before writing a complete generation.
Any error before atomic manifest replacement leaves the preceding generation
authoritative and canonical memory unchanged. Disk write errors are not
ordinary field/constraint rejections and do not invoke boot's erase fallback.

After manifest replacement the commit is already visible. If the final
directory fsync fails, storage logs uncertain crash durability, retains
recovery files and skips cleanup. Engine installs the matching canonical
values. The operation is not reported as a safe-to-retry rejection merely
because that final durability confirmation failed.

During boot, malformed saved envelopes are distinct from absent files.
`raise` preserves and rejects invalid input; `recover` and `erase` use their
configured recovery behavior. Missing or failed migrations retain the
existing fallback of validating/recovering the original selected data.
See [persistence and boot](architecture.md#persistence-and-boot) for the
complete version and recovery contract.

## Observer and cleanup failures

Store commit listeners are synchronous read-only callbacks. A reentrant
`Store.execute()` raises `StoreStateError`; schedule a later operation.
Callback exceptions are logged and isolated from later Store listeners.
State notification boundaries also isolate application failures so pending
transactions finish and subsequent messages can be processed. Native Kivy
dispatch can stop later bindings of the same event after a binding raises;
the framework does not replace Kivy's binding machinery or promise to invoke
those skipped bindings.

Per-client query or transport delivery failure disconnects the affected
client and releases its subscriptions; unrelated clients can continue.
Shut down StateManager, Client, Host and Store at their respective owning
application boundaries. `state.release(source)` and `state.close()` do not
implicitly close a shared connection.
