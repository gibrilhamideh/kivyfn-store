# Architecture

KivyFN Store is divided into a store-domain core, local and remote frontends, server-side coordination, and transport adapters.

## Goals

The architecture is designed to provide:

- the same expected-failure semantics locally and remotely;
- typed schema validation;
- normalized transaction behavior;
- explicit lifecycle boundaries;
- transport independence;
- structured subscription and signal delivery;
- persistent storage and migrations;
- precise error identity;
- room for multiple transports without changing application APIs.

## High-level topology

### Local

```text
Application
    |
    v
LocalStore
    |
    v
Engine
```

### Remote

```text
Application
    |
    v
RemoteStore
    |
    v
RemoteStoreClient
    |
    v
RemoteClientTransport
    |
    v
StoreServerTransport
    |
    v
StoreServerProxy
    |
    v
StoreServer
    |
    v
Engine
```

The transport may be Socket.IO today and another implementation later, such as Unix sockets, TCP, or an application-specific cloud protocol.

## Address

`Address` identifies a node within the store tree.

Internal domain code uses an `Address` object.

Normalized failures use:

```python
tuple[str, ...]
```

Wire payloads use:

```python
list[str]
```

This preserves immutability inside failure objects while remaining JSON and Socket.IO compatible.

## Schema and fields

A schema defines:

- whether a node stores one value or many rows;
- fields and their validators;
- unique keys;
- foreign keys;
- persistence behavior;
- signals;
- defaults and schema metadata.

Field types include concepts such as:

- boolean;
- string;
- numeric;
- enum;
- date;
- time;
- datetime;
- arbitrary data.

Fields validate values and raise precise semantic errors such as `FieldValidationError`.

Schema code enriches these errors with schema and field information rather than replacing them with generic compiler errors.

## Node

A `Node` owns the current value for one schema and maintains supporting indexes.

A node may be:

- `many=False`: one field dictionary;
- `many=True`: rows keyed by row ID.

Node responsibilities include:

- building values from persisted or default data;
- row existence checks;
- indexed lookup;
- snapshot resolution;
- dictionary serialization;
- mutation support;
- index maintenance.

Node-level expected failures include:

- invalid operation for the node shape;
- missing row;
- duplicate row;
- missing field;
- missing index.

## Engine

`Engine` owns the complete store-domain runtime.

It coordinates:

- metadata;
- nodes;
- storage;
- boot and migration;
- snapshots and lookups;
- transaction execution;
- undo and redo;
- event generation;
- persistence.

`Engine` contains no remote transport logic.

## Boot

The boot pipeline is:

```text
load raw store
    -> extract data
    -> determine stored version
    -> run migrations
    -> build persistent nodes
    -> validate persisted foreign keys
    -> save normalized nodes
```

`BootPolicy` determines recovery behavior.

### RAISE

Propagate boot failure.

### ERASE

Build empty persistent nodes and persist them.

Schema configuration mistakes remain exceptions. Persisted data violations use structured operation errors where appropriate.

A known follow-up is to replace silent persisted-row skipping with explicit boot diagnostics.

## Instructions

Application transactions are represented as command payloads.

Conceptually, an instruction contains:

- opcode;
- address;
- optional row ID;
- optional field key;
- value;
- source.

`Instruction.construct()` parses command payloads into concrete internal instructions.

Malformed instructions should produce `InvalidInstructionError`.

## Compiler

The compiler resolves instructions into executable operations.

Responsibilities include:

- resolving the addressed node;
- validating opcode compatibility with schema shape;
- validating required instruction properties;
- validating and building values;
- producing operation objects;
- adding triggered operations;
- applying foreign-key action policies;
- preserving source information.

The compiler does not replace lower-level semantic errors. It enriches them with compile-stage context.

## Operations

Compiled operations include:

- `SetOperation`;
- `SetRowOperation`;
- `InsertOperation`;
- `RemoveOperation`.

Operations contain the information needed to apply a mutation and generate the corresponding event.

The source records whether the operation originated from:

- an application command;
- a trigger;
- an undo or redo flow;
- another framework-defined source.

## Delta

`Delta` computes the normalized future row-level state of a transaction without mutating nodes.

It tracks:

```text
node -> inserted rows
node -> removed row IDs
node -> updated fields
node -> touched fields
```

Delta responsibilities include:

- insert-after-remove replacement behavior;
- cancellation of an insertion removed in the same transaction;
- duplicate-operation detection;
- logical row existence;
- logical future field values;
- logical future row IDs.

Examples of delta failures:

```text
insert same row twice
    -> RowOperationConflictError

insert existing row
    -> RowAlreadyExistsError

remove missing row
    -> RowNotFoundError

update removed row
    -> RowOperationConflictError
```

## ConstraintGuard

`ConstraintGuard` validates relational invariants against the delta view.

It does not mutate state.

### Unique constraints

Checks:

- collisions among changed rows in the same transaction;
- collisions against unchanged base-state rows;
- rows removed or changed by the same transaction;
- composite unique values;
- nullable unique values.

Failures use `UniqueConstraintError` and `ConstraintErrorDetail`.

### Foreign-key existence

Checks the future logical state, not only current base state.

This permits transactions such as inserting a referenced row and a referencing row together.

Failures use `ForeignKeyNotFoundError`.

### Restricted delete

Checks whether a logically surviving row still references a row being removed.

Failures use `ForeignKeyRestrictedError`.

## Execution

The transaction pipeline is:

```text
construct instructions
    -> compile operations
    -> build delta
    -> validate constraints
    -> execute operations
    -> persist
    -> dispatch events
```

Expected failures are caught during non-mutating stages:

- `COMPILE`;
- `DELTA`;
- `VALIDATE`.

The original `StoreOperationError` is retained and converted to `StoreFailure` at a store boundary.

Unexpected operation execution failures propagate.

This is deliberate because catching an exception after earlier operations mutated state could make a partially applied transaction appear to be an ordinary rejected transaction.

Execution must therefore be either:

- infallible after successful validation; or
- protected by rollback.

Failure-injection tests must verify this invariant.

## ExecutionResult

`ExecutionResult` records:

- success;
- stage;
- compiled operations;
- expected error, when present;
- inserted rows;
- removed rows;
- updated addresses;
- generated events;
- touched nodes.

Its payload is used to create `TransactionResult`.

Internal exceptions remain internal objects until they cross the store boundary.

## Persistence

Persistent nodes are written through the engine storage backend.

Persistence concerns include:

- loading schema payloads;
- metadata and store version;
- migrations;
- writes after successful mutation;
- consistency between in-memory and persisted state.

Persistence failures are runtime failures, not normal validation rejection.

The implementation must test whether mutation is rolled back or otherwise protected when writing fails.

## Undo and redo

The engine records reversible transaction state.

Undo and redo:

- produce operations or events;
- mutate the same nodes and indexes;
- dispatch matching subscriptions;
- preserve expected response semantics.

`can_undo` and `can_redo` expose current availability.

## LocalStore

`LocalStore` is synchronous and owns:

- one `Engine`;
- `LocalSubscriptionManager`;
- `LocalSignalManager`;
- lifecycle flags;
- a reentrant lock.

The lock serializes public operations and allows callbacks on the same thread to re-enter the store.

Lifecycle:

```text
construct
    -> boot
    -> use
    -> shutdown
```

A shut-down instance cannot be booted again.

### Local expected failures

`LocalStore` converts only `StoreOperationError` into `FailureResponse`.

It does not convert:

- lifecycle errors;
- persistence errors;
- callback errors;
- unexpected framework defects.

Those remain exceptions.

## StoreServer

`StoreServer` owns a remotely hosted engine and server-side managers.

Its responsibilities include:

- booting the engine;
- exposing restricted operations through a proxy;
- managing remote subscriptions;
- managing remote signals;
- dispatching events and signals;
- starting and stopping a server transport.

The server transport must not directly access `Engine`. It calls `StoreServerProxy`.

## StoreServerProxy

The proxy restricts the operations exposed to transports.

It translates parsed transport arguments into server operations such as:

- snapshot;
- lookup;
- to-dict;
- transaction;
- undo;
- redo;
- subscribe;
- unsubscribe;
- signal bind;
- signal unbind;
- signal emit;
- disconnected-client cleanup.

The proxy keeps transport code independent from engine internals.

## Remote client transport

A `RemoteClientTransport` abstraction provides:

- `connect()`;
- `disconnect()`;
- `connected`;
- `call(event, payload)`;
- event-handler registration;
- signal-handler registration.

It does not interpret store responses.

### SocketIOClientTransport

The Socket.IO implementation owns `socketio.AsyncClient`.

It classifies runtime failures as:

- `StoreStateError`;
- `StoreConnectionError`;
- `StoreTimeoutError`;
- `StoreProtocolError`.

Raw acknowledgement payloads are returned to `RemoteStoreClient`.

Incoming callback exceptions are logged and isolated, while malformed remote payloads remain protocol failures.

## Server transport

A server transport owns:

- request-handler registration;
- connection identifiers;
- room membership;
- request parsing;
- response serialization;
- subscription delivery;
- signal delivery;
- disconnect notification.

### SocketIOServerTransport

The Socket.IO implementation owns:

- `socketio.AsyncServer`;
- the aiohttp application;
- runner and TCP site;
- host, port, and Socket.IO path.

It does not own:

- `Engine`;
- store metadata;
- subscription domain logic;
- signal domain logic.

### Request failure classification

```text
StoreOperationError
    -> exact FailureResponse

Malformed request payload
    -> InvalidInstructionError failure

Unexpected exception
    -> log traceback
    -> safe INTERNAL_ERROR failure
```

The server continues serving after one failed request.

## Response codec

The shared response module provides:

- `SuccessResponse[T]`;
- `FailureResponse`;
- `Response[T]`;
- `success(data)`;
- `failure(store_failure)`;
- response payload validation.

A successful response contains data and no error.

A failed response contains a `StoreFailure` and no data.

Numeric HTTP-like response statuses are not part of the store contract. Semantic status lives in `StoreFailure`.

## Failure codec

The error codec:

- converts `StoreFailure` to primitive payloads;
- reconstructs `StoreFailure` from wire data;
- validates enums;
- validates detail types;
- rejects unknown or malformed fields;
- converts internal tuples to wire lists;
- converts wire lists back to tuples.

It does not reconstruct engine exception instances on the client.

## RemoteStoreClient

`RemoteStoreClient` is transport-independent.

Responsibilities include:

- connection lifecycle;
- raw transport calls;
- response-envelope validation;
- typed response conversion;
- subscription creation and tracking;
- signal-binding tracking;
- incoming event decoding;
- incoming signal decoding;
- local cleanup after disconnect.

It raises `StoreProtocolError` when a response marked successful contains invalid data.

Examples:

- missing subscription token;
- duplicate subscription token;
- missing event token;
- malformed transaction result;
- invalid event payload;
- invalid signal payload.

## RemoteStore

`RemoteStore` is the asynchronous public facade.

It owns:

- a name;
- one `RemoteStoreClient`;
- one `RemoteSubscriptionManager`;
- one `RemoteSignalManager`.

It performs lifecycle checks and delegates operations.

It does not catch transport failures.

This preserves the public distinction:

```text
response.ok is False
    -> server processed and rejected the operation

StoreTransportError
    -> remote operation did not complete normally
```

## Subscriptions

Subscriptions observe store mutation events.

Supported event types include:

- set;
- set-row;
- insert;
- remove.

A remote subscription has:

- a subscription token;
- an event-stream token;
- a callback;
- disposed state.

Bound methods are weakly held so subscriptions do not keep their owner alive.

Functions, lambdas, and callable objects are held strongly.

Remote subscription callbacks are synchronous. Returning an awaitable is treated as callback misuse and logged.

Dead weak references cause remote unsubscription.

## Signals

Signals provide application-defined event delivery associated with:

- an address;
- a signal name;
- an optional row ID;
- data.

Remote signal callbacks may be synchronous or asynchronous.

Remote signal bindings use weak references for bound methods and are disposed remotely through their binding token.

## Event and signal dispatch

Server-side managers produce dispatch messages containing:

- channel;
- event token;
- serialized event or signal payload.

The Socket.IO server delivers these messages through rooms.

The client resolves the event token to local subscriptions or signal bindings and dispatches locally.

Consumer callback failures should be logged and isolated.

## Error model

The internal hierarchy separates:

- semantic operation errors;
- runtime lifecycle errors;
- transport failures;
- protocol failures;
- internal failures.

Expected operation errors carry:

- stable code;
- category;
- semantic status;
- structured detail;
- stage context;
- diagnostic message.

See [Error handling](errors.md) for the full contract.

## Logging

Server and client transports use:

```python
from kivyfn.logging import get_logger
```

The application may configure logging through a multiprocessing queue so errors from store server processes arrive in the main process.

Unexpected server exceptions must be logged with a traceback before a safe internal failure is returned remotely.

## Process hosting

A store server may run:

- in another process on the same machine;
- in another application;
- on another machine;
- in a cloud environment.

A process manager may:

- spawn the server process;
- wait for readiness;
- report startup failure;
- signal shutdown;
- collect exit state;
- route logs to the main process.

Process lifecycle errors remain outside the expected store-operation response model.

## Public invariants

The architecture should preserve these invariants:

1. Local and remote expected failures have equivalent `StoreFailure` semantics.
2. Transports do not implement schema or transaction logic.
3. The client never reconstructs server-side exception classes.
4. Expected failures are detected before mutation.
5. Unexpected server exceptions are logged and hidden from remote clients.
6. Callback failures do not invalidate successful transactions.
7. A disconnected or malformed remote session raises a runtime exception.
8. Diagnostic messages are not machine contracts.
9. Address representation is immutable internally and primitive on the wire.
10. Persistent and in-memory state remain consistent after failures.

## Recommended test boundaries

Test each layer independently:

### Fields and schemas

- valid and invalid values;
- bounds;
- enum membership;
- nullable behavior;
- context enrichment.

### Node and indexes

- row insertion;
- row removal;
- lookup;
- composite indexes;
- unique indexes;
- index mutation consistency.

### Compiler

- every opcode;
- missing arguments;
- invalid node shape;
- trigger operations;
- foreign-key policies;
- semantic-error preservation.

### Delta

- duplicate inserts;
- remove-after-insert;
- insert-after-remove;
- duplicate removes;
- update-after-remove;
- logical values and IDs.

### ConstraintGuard

- intra-transaction uniqueness;
- base-state uniqueness;
- changed conflicting rows;
- missing foreign key;
- insert referenced and referencing rows together;
- restricted deletion.

### Execution

- success result;
- each failure stage;
- event collection;
- failure context;
- unexpected execute failure;
- rollback or partial-mutation detection.

### LocalStore

- boot lifecycle;
- response conversion;
- thread serialization;
- callback isolation;
- shutdown lifecycle.

### Server and transports

- request parsing;
- safe internal failures;
- error logging;
- response serialization;
- connection loss;
- timeout;
- malformed payload;
- room dispatch.

### RemoteStoreClient

- response validation;
- typed transaction conversion;
- subscription token tracking;
- signal token tracking;
- disconnect cleanup;
- malformed event and signal handling.

### Parity

For each expected error, assert that local and remote operations return the same:

- code;
- category;
- status;
- detail;
- relevant context.
