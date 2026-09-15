# kivyfn-store documentation

`kivyfn-store` provides a synchronous canonical Store and an asynchronous,
reactive Kivy State API over Direct or Socket.IO transport.

- [README and runnable example](../README.md): installation, connection,
  mutations, transactions, queries, signals, cleanup and development checks.
- [Custom states and templates](state-templates.md): typed application states,
  local properties, callbacks, source identity, and reusable templates.
- [Architecture](architecture.md): ownership boundaries, staging, constraints,
  ordered triggers, revisions, subscriptions, query writes and persistence.
- [Error handling](errors.md): the actual Store, Client and State result
  types, expected rejection, infrastructure failure and ambiguous outcomes.
- [Test guide](../tests/README.md): test layout and execution.
- [Correction summary](../CORRECTIONS.md): changes, edge cases and validation.

Applications normally create a `StateManager` around a `StoreClient`.
`StateManager.create(SingleSchemaType)` returns a State;
`create(ManySchemaType)` returns a CollectionState;
`create(Query(...))` returns a QueryCollectionState.
`create(StateTemplate[T])` returns the configured custom type `T`.

`StoreHost` exposes an already booted `Store`. Connect its client with
`await client.connect(endpoint)`: `direct://name` in-process, or an HTTP
Socket.IO endpoint remotely. A listener bound to port zero reports its
actual endpoint after startup; pass that endpoint to the Client.
These are one architecture with two transports, not separate Store APIs.

The Client and desired State targets can be created before Store startup.
The minimum lifecycle is: build and boot Store, start Host, connect Client
with the listening endpoint, perform work, close StateManager, disconnect
Client, stop Host, shut down Store. Connecting waits for current desired
subscriptions to synchronize. Later `await client.connect()` calls reuse
the remembered endpoint; changing endpoints requires disconnecting first.
