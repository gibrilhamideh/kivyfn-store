# KivyFN Store Documentation

KivyFN Store provides a typed store engine that can be used synchronously in-process or asynchronously through a remote transport.

## Guides

### [Architecture](architecture.md)

Covers:

- `Engine`;
- schemas, fields, nodes, and addresses;
- transaction instructions and compiled operations;
- compiler, delta, constraint, execution, persistence, and dispatch stages;
- `LocalStore`;
- `StoreServer`, proxy, and transports;
- `RemoteStoreClient` and `RemoteStore`;
- subscriptions and signals;
- boot, migration, undo, and redo.

### [Error handling](errors.md)

Covers:

- `SuccessResponse` and `FailureResponse`;
- `StoreFailure`;
- operation errors versus runtime exceptions;
- transport exception hierarchy;
- local and remote parity;
- startup, request, and shutdown handling;
- retry guidance;
- internal-error safety;
- testing requirements.

## Minimal workflow

### Local

```python
store = LocalStore(
    root=root_node,
    config=store_config,
)

store.boot()

response = store.snapshot(
    AddressBook.SETTINGS,
)

if response.ok:
    use(response.data)
else:
    handle_store_failure(response.error)
```

### Remote

```python
store = RemoteStore(
    name="main",
    transport=transport,
)

try:
    await store.connect()
    response = await store.snapshot(
        AddressBook.SETTINGS,
    )
except StoreTransportError:
    handle_store_offline()
    return

if response.ok is False:
    handle_store_failure(
        response.error,
    )
```

## Core contract

```text
Expected store-domain rejection
    -> FailureResponse(StoreFailure)

Lifecycle, transport, protocol, persistence,
or unexpected framework failure
    -> exception
```

This contract is the same whether the application uses `LocalStore` or `RemoteStore`.
