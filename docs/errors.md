# Error Handling

KivyFN Store separates expected operation rejection from failures that prevent an operation from completing normally.

## Core rule

```text
Expected store-domain rejection
    -> FailureResponse(StoreFailure)

Lifecycle, transport, protocol, persistence,
or unexpected framework failure
    -> exception
```

This distinction allows application code to handle user-facing validation and constraint failures without hiding infrastructure defects.

## Responses

Store operations return one of two response variants.

### Success

```python
response.ok is True
response.data
response.error is None
```

### Failure

```python
response.ok is False
response.data is None
response.error
```

A failure response does not raise by itself. The caller must inspect `response.ok`.

```python
response = await store.commit(
    transaction,
)

if response.ok is False:
    handle_store_failure(
        response.error,
    )
    return

result = response.data
```

## StoreFailure

`StoreFailure` is an immutable, transport-safe description of an expected operation failure.

Conceptually it contains:

```python
StoreFailure(
    code=...,
    category=...,
    status=...,
    message=...,
    detail=...,
    context=...,
)
```

### Stable machine-readable fields

Use these fields for application logic:

- `code`;
- `category`;
- `status`;
- `detail`;
- `context`.

Do not parse `message` to determine behavior. Messages are diagnostic text and may change without constituting an API break.

## Expected operation failures

Expected failures derive internally from `StoreOperationError` and are converted to `FailureResponse` at the store boundary.

Examples include:

- `InvalidInstructionError`;
- `InvalidOperationError`;
- `AddressNotFoundError`;
- `NodeNotFoundError`;
- `FieldNotFoundError`;
- `FieldValidationError`;
- `RowNotFoundError`;
- `RowAlreadyExistsError`;
- `RowOperationConflictError`;
- `IndexNotFoundError`;
- `UniqueConstraintError`;
- `ForeignKeyNotFoundError`;
- `ForeignKeyRestrictedError`.

These should normally be handled through the response:

```python
response = await store.commit(
    transaction,
)

if response.ok is False:
    failure = response.error

    match failure.code:
        case StoreErrorCode.FIELD_VALIDATION_FAILED:
            show_field_error(failure)

        case StoreErrorCode.ROW_NOT_FOUND:
            refresh_view()

        case StoreErrorCode.UNIQUE_CONSTRAINT_FAILED:
            show_conflict(failure)

        case _:
            show_store_error(failure)
```

## Structured details

Failure details are specific to the failure category.

Examples include:

- `InstructionErrorDetail`;
- `AddressErrorDetail`;
- `NodeErrorDetail`;
- `FieldErrorDetail`;
- `RowErrorDetail`;
- `IndexErrorDetail`;
- `ConstraintErrorDetail`;
- `RuntimeErrorDetail`.

A field validation failure may include:

```python
FieldErrorDetail(
    schema="StaticPressureSettings",
    field="step",
    value=-1,
    rule="minimum",
    minimum=0,
)
```

A unique collision may include:

```python
ConstraintErrorDetail(
    schema="IoAssignment",
    fields=("kind", "index"),
    value=["digital-output", 1],
    row_id="new-row",
    existing_row_id="existing-row",
)
```

Addresses are tuples internally and lists on the wire.

## Error context

`StoreErrorContext` records where a failure occurred.

Relevant stages include:

- `REQUEST`;
- `RESOLVE`;
- `COMPILE`;
- `DELTA`;
- `VALIDATE`;
- `EXECUTE`;
- `PERSIST`;
- `DISPATCH`.

Context may also contain:

- schema;
- address;
- opcode;
- source;
- row ID;
- field;
- value;
- instruction index.

Lower layers raise the most specific semantic error available. Higher layers enrich the same error with context instead of replacing it with a generic wrapper.

## Runtime exception hierarchy

```text
StoreException
├── StoreOperationError
└── StoreRuntimeError
    ├── StoreStateError
    ├── StoreTransportError
    │   ├── StoreUnavailableError
    │   │   ├── StoreConnectionError
    │   │   └── StoreTimeoutError
    │   └── StoreProtocolError
    └── StoreInternalError
```

## StoreStateError

`StoreStateError` indicates incorrect lifecycle usage.

Examples:

- using `LocalStore` before `boot()`;
- booting a local store twice;
- using a local store after shutdown;
- using `RemoteStore` before `connect()`;
- using a transport before handlers are configured;
- calling a disconnected transport directly.

These usually indicate a developer error and should remain loud.

A state error may be caught when a shutdown or reconnect race is expected, but it should not be broadly suppressed.

## StoreTransportError

`StoreTransportError` groups every remote transport-level exception.

Catch it when all transport failures enter the same application workflow:

```python
try:
    response = await store.commit(
        transaction,
    )
except StoreTransportError:
    handle_store_offline()
    return
```

### StoreUnavailableError

Availability failures are commonly temporary and retryable.

Subclasses:

- `StoreConnectionError`;
- `StoreTimeoutError`.

```python
try:
    await store.connect()
except StoreUnavailableError:
    schedule_reconnect()
```

### StoreConnectionError

Raised when:

- a remote connection cannot be established;
- an active connection is lost;
- a request fails because the transport is no longer usable;
- disconnect fails.

### StoreTimeoutError

Raised when:

- initial connection exceeds the configured timeout;
- a remote request does not receive an acknowledgement in time.

Timeout does not prove that the server performed no work. Avoid automatically replaying non-idempotent operations unless the protocol provides request IDs or deduplication.

### StoreProtocolError

Raised when the client or remote peer violates the store protocol.

Examples:

- malformed response envelope;
- unknown response version;
- missing required response fields;
- invalid serialized failure;
- successful subscription response without tokens;
- malformed event or signal payload;
- malformed transaction result.

Protocol failures are usually not fixed by immediately retrying the same request.

```python
try:
    response = await store.commit(
        transaction,
    )
except StoreProtocolError:
    logger.exception(
        "Store protocol failure"
    )
    await disconnect_safely()
    show_store_internal_error()
```

## StoreInternalError

`StoreInternalError` represents a safe report of an unexpected remote failure.

The server must log the original exception and traceback. The remote response must not expose internal traceback details.

A safe internal failure may include a request identifier for log correlation.

## Startup

### Local store boot

Boot can fail because of:

- corrupted storage;
- failed migration;
- incompatible stored version;
- disk or permission error;
- invalid persisted data;
- boot-time foreign-key violation;
- unexpected framework failure.

Catch boot failure once at the application startup boundary:

```python
try:
    store.boot()
except Exception:
    logger.exception(
        "Local store failed to boot"
    )
    show_fatal_store_error()
    raise
```

`BootPolicy.ERASE` may recover from boot failure by rebuilding empty persistent nodes. `BootPolicy.RAISE` propagates the failure.

### Remote connection

```python
try:
    await store.connect()
except StoreTransportError:
    logger.exception(
        "Remote store connection failed"
    )
    show_store_offline()
```

If the remote server is a managed child process, process-startup failures should also be handled at this boundary.

## Normal operations

### Remote operation

```python
try:
    response = await store.commit(
        transaction,
    )
except StoreTransportError:
    handle_store_offline()
    return

if response.ok is False:
    handle_store_failure(
        response.error,
    )
    return

handle_success(
    response.data,
)
```

### Local operation

Expected operation rejection is returned as a response:

```python
response = store.commit(
    transaction,
)

if response.ok is False:
    handle_store_failure(
        response.error,
    )
```

Unexpected local persistence or framework failures still propagate. Catch them only where the application has a meaningful recovery strategy.

## Shutdown

Shutdown code should continue cleanup even when one resource fails.

```python
try:
    await remote_store.disconnect()
except StoreTransportError:
    logger.exception(
        "Remote store disconnect failed"
    )
```

```python
try:
    local_store.shutdown()
except Exception:
    logger.exception(
        "Local store shutdown failed"
    )
```

A broad catch is acceptable at the top-level shutdown boundary when every exception is logged and cleanup continues.

## Subscriptions and signals

Remote subscription and signal registration may raise `StoreTransportError`.

```python
try:
    response = await store.subscription.setrow(
        callback=on_change,
        address=address,
    )
except StoreTransportError:
    handle_store_offline()
    return
```

Binding disposal can also fail because the connection was lost.

```python
try:
    await binding.dispose()
except StoreTransportError:
    logger.exception(
        "Remote binding disposal failed"
    )
```

Callback exceptions should be isolated and logged so a consumer callback does not terminate the transport task.

Local callback dispatch must follow the same rule: a successful transaction must not appear unsuccessful only because an observer callback raised.

## Server request boundary

The remote server transport classifies failures as follows:

### Expected StoreOperationError

Converted to its exact `StoreFailure`.

### Invalid wire request

Malformed request input is converted to an invalid-instruction failure.

Examples:

- address is not a list of strings;
- opcode is invalid;
- required token is missing;
- instructions are not objects.

### Unexpected exception

The server:

1. logs the full exception and traceback;
2. returns a safe `INTERNAL_ERROR` failure;
3. does not expose internal details;
4. keeps serving other requests.

## Execution-stage failures

Expected failures must be discovered before mutation during:

```text
COMPILE
DELTA
VALIDATE
```

The execution loop applies already validated operations to real node state.

An unexpected exception after one operation has mutated state may cause partial mutation unless rollback exists. Therefore execution-stage exceptions are not converted into ordinary expected failures.

The required invariant is one of:

- execution is effectively infallible after validation; or
- execution is rollback-protected.

This must be verified with failure-injection tests.

## Persistence failures

A persistence write failure is not an expected domain rejection.

For `LocalStore`, it propagates to the caller.

For `RemoteStore`, the server logs the original exception and returns a safe internal failure.

Test in-memory and persisted-state consistency when forcing storage writes to fail.

## Recommended application boundary

```python
async def commit_store(
    transaction: TransactionInput,
) -> TransactionResult | None:
    try:
        response = await store.commit(
            transaction,
        )

    except StoreUnavailableError:
        set_store_connected(False)
        show_store_offline()
        return None

    except StoreProtocolError:
        logger.exception(
            "Store protocol failure"
        )
        show_store_internal_error()
        return None

    if response.ok is False:
        handle_store_failure(
            response.error,
        )
        return None

    return response.data
```

## Testing checklist

Test at least:

1. invalid field value locally and remotely;
2. missing field;
3. missing row;
4. duplicate row;
5. duplicate operation in one transaction;
6. unique collision;
7. missing foreign key;
8. restricted delete;
9. invalid instruction;
10. invalid address;
11. server unavailable;
12. connection timeout;
13. request timeout;
14. malformed acknowledgement;
15. malformed event;
16. malformed signal;
17. remote callback failure isolation;
18. local callback failure isolation;
19. unexpected server exception logging;
20. execution failure after partial mutation;
21. persistence write failure;
22. corrupted boot data;
23. missing migration;
24. `BootPolicy.RAISE`;
25. `BootPolicy.ERASE`.
