# KivyFN Store
KivyFN Store is a typed state and persistence framework for Kivy applications.

It supports two interchangeable frontends:

- `LocalStore`: synchronous, in-process access to an `Engine`;
- `RemoteStore`: asynchronous access to a remotely hosted `StoreServer`.

Both frontends expose the same operation result contract:

- expected store-operation failures are returned as `FailureResponse`;
- lifecycle, transport, protocol, persistence, and unexpected framework failures remain exceptions.

## Features

- typed schemas and fields;
- single-value and row-based nodes;
- indexed lookup and composite unique constraints;
- foreign-key validation and delete policies;
- atomic transaction compilation and validation;
- undo and redo;
- local and remote subscriptions;
- local and remote signals;
- persistent storage with migrations;
- transport-independent remote clients;
- Socket.IO client and server transports;
- structured, serializable store failures.

## Installation

```bash
pip install kivyfn-store
```

Use the package name configured by your distribution if it differs from `kivyfn-store`.

## Local store

```python
from kivyfn.store.local.store import LocalStore

store = LocalStore(
    root=root_node,
    config=store_config,
)

store.boot()

response = store.snapshot(
    AddressBook.SETTINGS,
)

if response.ok:
    settings = response.data
else:
    handle_store_failure(response.error)
```

A `LocalStore` instance can only be booted once. After shutdown, create a new instance.

```python
store.shutdown()
```

## Remote store

```python
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.transport.socketio.client import (
    SocketIOClientTransport,
)

transport = SocketIOClientTransport(
    "http://127.0.0.1:8765",
)

store = RemoteStore(
    name="main",
    transport=transport,
)

try:
    await store.connect()
except StoreTransportError:
    handle_store_offline()
```

Remote operations use the same response contract as local operations:

```python
try:
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
    return

settings = response.data
```

## Transactions

Transactions may be built incrementally:

```python
transaction = (
    store.transaction()
    .setrow(
        address=AddressBook.IO,
        id=assignment_id,
        key="index",
        value=4,
    )
    .setrow(
        address=AddressBook.IO,
        id=assignment_id,
        key="kind",
        value="digital-output",
    )
)

response = await store.commit(
    transaction,
)
```

Or supplied as raw command payloads where supported:

```python
response = await store.commit(
    [
        {
            "opcode": "setrow",
            "address": ["io"],
            "id": assignment_id,
            "key": "index",
            "value": 4,
        }
    ]
)
```

Always inspect the response:

```python
if response.ok:
    result = response.data
else:
    failure = response.error

    match failure.code:
        case StoreErrorCode.FIELD_VALIDATION_FAILED:
            show_validation_error(failure)

        case StoreErrorCode.UNIQUE_CONSTRAINT_FAILED:
            show_conflict_error(failure)

        case _:
            show_store_error(failure)
```

## Error handling

Expected domain rejection does not require `try`/`except`:

```python
response = await store.commit(
    transaction,
)

if response.ok is False:
    handle_store_failure(
        response.error,
    )
```

Remote transport failures remain exceptions:

```python
try:
    response = await store.commit(
        transaction,
    )
except StoreTransportError:
    handle_store_offline()
```

Local boot should be guarded at the application startup boundary:

```python
try:
    store.boot()
except Exception:
    logger.exception(
        "Store boot failed"
    )
    raise
```

See [Error handling](docs/errors.md) for the complete failure and exception model.

## Documentation

- [Documentation index](docs/index.md)
- [Architecture](docs/architecture.md)
- [Error handling](docs/errors.md)

## Project layout

```text
kivyfn-store/
├── src/
│   └── kivyfn/
│       └── store/
├── tests/
├── docs/
│   ├── index.md
│   ├── architecture.md
│   └── errors.md
├── README.md
├── pyproject.toml
└── LICENSE
```

## Design principles

- Local and remote stores must expose the same expected-failure semantics.
- Expected operation failures are values, not runtime exceptions.
- Runtime and infrastructure failures remain exceptions.
- Error codes and structured details form the machine-readable contract.
- Diagnostic messages are useful for humans but are not stable API identifiers.
- Transports carry store responses but do not implement store-domain logic.
- Mutation begins only after compilation, delta construction, and constraint validation succeed.

## Status

KivyFN Store is under active development. Public APIs may change until the first stable release.
