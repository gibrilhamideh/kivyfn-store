# @kivyfn/store

TypeScript state objects for the authoritative Python `kivyfn-store`.
The core runs in browsers and Node.js. React is an optional separate export.
This package lives in the Python repository at `packages/client`.

The Python store owns schemas, defaults, validation, committed state, queries,
signals, history and persistence. This client owns subscriptions, confirmed
snapshots and pending optimistic overlays. A gateway authenticates and routes;
its receipts never confirm a store transaction.

## Build and use locally

From the repository root, with Node.js 22.12+ or 24+ and npm:

```bash
npm ci
npm run build:client
npm run check:client
npm pack --workspace=@kivyfn/store
```

The pack command creates `kivyfn-store-0.1.0.tgz` at the repository root.
In a separate Vite/React application, install that tarball:

```bash
npm install /absolute/path/kivyfn-store/kivyfn-store-0.1.0.tgz
npm install mobx@^7 mobx-react-lite@^5
```

React 18.3+ or 19 and ReactDOM are peer dependencies for the React export.
A Node application only needs the package and MobX. Published output is ESM
with declarations; use `import`, not CommonJS `require`. Browser builds need
modern `Proxy`, `AbortController`, and `crypto.randomUUID` support; serve them
over HTTPS (localhost also provides a secure context).

For strict checking of MobX 7's declarations, include `"ESNext"` and `"DOM"`
in your TypeScript `lib` option. Your JavaScript target can remain `ES2022`.

Vite resolves and bundles this installed package normally. The package has
not been published to npm. Its proposed package name is `@kivyfn/store`;
publishing under that scope is a separate release step.

## Core API

```ts
import {
  RelayTransport,
  StateManager,
  StoreClient,
  collection,
  single,
} from "@kivyfn/store";

interface Pressure {
  actual: number;
  target: number;
}

// Match the names of the Python schema classes. No duplicated validators.
const PressureSchema = single<Pressure, { reset: void }>("PressureSchema");
const SensorSchema = collection<{ label: string; value: number }>(
  "SensorSchema",
);

const client = new StoreClient({
  transport: new RelayTransport({
    endpoint: "https://gateway.example/relay/socket.io",
    storeId: "application-001",
    auth: async () => ({ token: await getAccessToken() }),
  }),
  reconnectInterval: 5000,
  onListenerError: (error) => console.error(error),
});
const states = new StateManager(client);
const pressure = states.create(PressureSchema);
const sensors = states.create(SensorSchema);

await client.connect();

pressure.target = 25; // Optimistic; lastTransaction tracks the result.
const optimistic = pressure.lastTransaction!;
const result = await optimistic;
if (!result.success) console.log(result.status, result.error);

// Default .set() behavior waits for authoritative data before changing fields.
await pressure.set({ target: 30 }).unwrap();
await sensors.insert("sensor-1", { label: "Room", value: 18 }).unwrap();
await sensors.get("sensor-1")!.set({ value: 19 }).unwrap();

const unbind = pressure.bind({
  actual: (value, previous) => console.log(previous, "->", value),
});
const stopSignals = pressure.signals.reset.bind(() => console.log("reset"));
await pressure.signals.reset.emit().unwrap();

// During shutdown:
unbind();
stopSignals();
states.close();
await client.disconnect();
```

`getAccessToken` is application code. Cookie authentication is also possible:
omit credentials and have your gateway policy inspect its request context.
The gateway must implement [the client relay contract](../../docs/client-relay.md).

`single<T, Signals>(name)` and `collection<T, Signals>(name)` are immutable,
typed references. They do not define defaults or validate fields. TypeScript
types disappear at runtime; Python remains the authority. Fields are
`undefined` until the first snapshot, and `synced` is false until that snapshot
is installed. A disconnected state retains its last confirmed values with
`synced = false`; writes through state objects require a live subscription.

For a direct Python Socket.IO host, replace the transport:

```ts
import { DirectSocketIOTransport } from "@kivyfn/store";

const transport = new DirectSocketIOTransport({
  endpoint: "http://127.0.0.1:8765/socket.io",
});
```

The direct transport adds no authentication policy to the existing Python
host. Expose direct hosts only within the application deployment's intended
trust boundary; use the authenticated gateway for external users.

## State, collections and transactions

| API | Behavior |
| --- | --- |
| `state.field = value` | Optimistic assignment; inspect `lastTransaction`. |
| `state.set({...}, options?)` | Confirmed update by default; returns `Transaction`. |
| `{ optimistic: true }` | Opts `.set`, insert, remove or a transaction into overlays. |
| `state.getField("name")` | Reads fields whose names collide with API members. |
| `state.bind({...})` | Local field-change callbacks; returns an unbind function. |
| `state.signals.name.emit(payload)` | Sends a named signal as a transaction. |
| `state.signals.name.bind(callback)` | Subscribes locally to the named signal. |
| `collection.rows`, `.get(id)`, `.has(id)`, `.size` | Observable collection access. |
| `collection.insert(id, values)`, `.remove(id)` | Confirmed membership changes by default. |
| `collection.bind({ onInsert, onRemove })` | Observes visible membership, including overlays. |
| `collection.signals.name.bind(callback)` | Receives row signals with `info.rowId`. |
| `state.pendingCount`, `.lastTransaction` | Observable transaction information. |
| `state.storeInstanceId`, `.revisions` | Immutable metadata for the confirmed snapshot, also available on collections/queries. |
| `states.release(source)` | Releases the manager's permanent subscription claim. |

State objects and collection rows are MobX observables through their property
accessors. Nested data is a frozen JSON snapshot: replace a whole field instead
of mutating a nested array or object. Use `.set({ id: ... })` and
`.getField("id")` for a schema field called `id`, since `.id` identifies a row.
The same escape applies to fields called `set`, `signals`, `synced`, or `then`.
Collections expose a readonly row list; mutate membership through the API.

A manager shares one object and subscription for equivalent source references.
Rows retain their object identity when refreshed by snapshots or updates.
Detached rows have `exists = false` and cannot be written. Cache lifetime is
the manager's lifetime; create a separate manager and client for each selected
store, and close/discard them when that session is finished. Use one manager
per client.

Build atomic multi-source transactions with a synchronous callback:

```ts
const transaction = states.transaction((tx) => {
  tx.set(pressure, { target: 35 });
  tx.set(sensors.get("sensor-1")!, { value: 20 });
  tx.emit(pressure.signals.reset);
}, { optimistic: true, historyLabel: "Adjust settings" });

const result = await transaction;
if (!result.success) console.log(result.error?.failure);
```

The callback must not be async. Throwing while building sends nothing and
stages no overlays. Action order is preserved across sources. Python applies
or rejects the atomic transaction. Client-side misuse, such as writing a
detached row or passing non-JSON data, can throw synchronously.

`await transaction` resolves a result with `id`, `status`, `success`, and
`error`. It does not throw for a store rejection; `await transaction.unwrap()`
does. The transaction itself exposes observable `status`, `error`, `done`,
`success`, `failed`, and `onSettled(callback)`.

| Status | Meaning |
| --- | --- |
| `pending` | Waiting for the controller/store result. |
| `confirmed` | The authoritative store returned success. |
| `rejected` | A structured store rejection was received. |
| `not_sent` | The request was rejected locally before transmission. |
| `unknown` | The request may have committed; its outcome was not confirmed. |

Signals are delivered after the complete event's retained values and overlay
retirement, ordered by occurrence ID. Transaction completion callbacks run
after state publication and overlay cleanup; promise continuations run after
those callbacks. Listener exceptions go to `onListenerError` and cannot stop
synchronization. Without that handler, listener exceptions are isolated.

## React

React components use the same client and manager. The React export provides
context and subscription lifetime; `observer` is the standard MobX integration.

```tsx
import { observer, StoreProvider, useStoreState } from "@kivyfn/store/react";

const PressurePanel = observer(function PressurePanel() {
  const pressure = useStoreState(PressureSchema);
  if (!pressure.synced) return <p>Connecting…</p>;

  return (
    <label>
      Target pressure
      <input
        type="number"
        value={pressure.target ?? ""}
        onChange={(event) => {
          pressure.target = Number(event.currentTarget.value);
        }}
      />
      <span>{pressure.lastTransaction?.status}</span>
    </label>
  );
});

// The application creates the manager and starts client.connect().
const application = (
  <StoreProvider manager={states}>
    <PressurePanel />
  </StoreProvider>
);
```

No manual `.bind()` or hook per field is needed in an `observer` component.
Read observable properties inside its render. Hooks retain subscriptions in
effects and release them on unmount; StrictMode and shared consumers are
supported. Render-only preparation does not start network subscriptions.

`StoreProvider` does not start or stop the connection. Application startup
owns `client.connect()`, and shutdown owns `states.close()` and
`client.disconnect()`. Handle connection errors there, including
`SubscriptionError`, which leaves healthy subscriptions connected. When a
selected store changes, provide that store's own manager; don't retarget a
live client. `useStore()` returns the manager when a component needs an atomic
transaction or connection status.

## Queries, guards and history

```ts
import { query, field } from "@kivyfn/store";

const source = query(SensorSchema).where(field("value").gte(20));
const current = await client.query(source); // Detached rows: { id, values }.
const live = states.create(source);
await client.whenSynced();
console.log(live.rows);

const history = await client.history(100, 0);
await client.undo(history.status.undo_position).unwrap();
await client.redo((await client.history()).status.redo_position).unwrap();
```

Expressions include `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `notIn`, and
the exported `and`, `or`, and `not` helpers. Chained `.where()` calls combine
with AND. Query literals are finite scalars or arrays representing Python
tuples. `.join(path, alias, "inner" | "left")` defaults to an inner join.
Joined output uses Python's flattened names, such as `author_label`.
Use `.join<JoinedRow>(...)` to describe that output in TypeScript.

Live query views are **read-only in this release**. Write through the source
schema's collection. Runtime schema discovery and portable joined-field write
routing remain deferred; the source contains a TODO for metadata discovery.

Pass `expectedStoreInstanceId` and `expectedRevisions` in transaction options
when an edit must reject a stale base revision. Capture the confirmed metadata
when opening an editor, then submit it with the eventual edit:

```ts
const base = {
  expectedStoreInstanceId: pressure.storeInstanceId!,
  expectedRevisions: { ...pressure.revisions },
};
// Later, while subscribed:
const result = await pressure.set({ target: 40 }, base);
```

State metadata describes confirmed data, even while optimistic values are
visible. Disconnected states retain that metadata alongside their cached
values; `synced` indicates whether the subscription is live. Unguarded writes use normal
store ordering and validation. History restoration requires an explicit
position obtained from `history()`, and rejects stale positions. Wait for
pending local mutations before undo/redo. History metadata is returned as
detached JSON; Python decides which fields and signals participate.

## Recovery and limits

`connect()` is shared across concurrent callers and waits for current desired
subscriptions. Additional sources can be awaited with `whenSynced()`.
Connection states are `disconnected`, `connecting`, and `connected`; a
connected transport alone does not imply every state has synchronized.

The client keeps confirmed data below ordered pending overlays. Rejections
remove only their own overlay. Revision gaps, instance changes within a live
connection, malformed batches and event buffer overflow close the connection.
Reconnect requests snapshots or exact cached revisions. A new store instance
always requires fresh snapshots.

No mutation is replayed after disconnection. Queued writes become `not_sent`;
transmitted, unresolved writes become `unknown`. Reconnecting restores current
state, but does not retroactively turn an unknown transaction into success.
Do not automatically retry signals or other non-idempotent operations.

All timeouts below are **milliseconds** (Python RelayConfig uses seconds):

| Configuration | Default |
| --- | --- |
| `StoreClient.reconnectInterval` | `0` — automatic retries disabled |
| `StoreClient.maxPendingRequests` | `1024` |
| `StoreClient.maxPendingBytes` | `4 MiB` |
| `StoreClient.maxBufferedEvents` | `1024` |
| `StoreClient.maxBufferedBytes` | `4 MiB` |
| Transport `connectTimeout` / `requestTimeout` | `10000` each |
| Transport `maxFrameBytes` | `1 MiB` |
| Transport `withCredentials` | `true` |

The authentication provider runs for every fresh connection. Explicit
`disconnect()` cancels automatic retries and releases the Socket.IO session.
Socket.IO's own retry and reconnect mechanisms are disabled so they cannot
replay mutations behind the client's synchronization layer.

Values must be finite JSON. Unsafe JavaScript integers, undefined values,
cycles, dates, BigInt, binary objects and class instances are rejected. Use
strings for identifiers or integers that exceed JavaScript's safe range.

This client does not throttle canonical events. Reduce UI rendering frequency
or sample a derived display value if needed. Never drop protocol revisions,
signals, transaction responses, or subscription messages to throttle readings.

## Tests

Install the Python repository's normal dependencies first. From the root:

```bash
PYTHON="$(pwd)/.venv/bin/python" npm run test:client
```

Or set `PYTHON` to another Python interpreter with `kivyfn-store` dependencies.
The tests load the repository's `src` directly and spawn real Python hosts.
No external gateway service is required: a Node test peer implements the
relay contract with fixed test credentials. It is not a production gateway.
See [validation notes](../../docs/client-validation.md).

`npm run test:package --workspace=@kivyfn/store` additionally verifies a packed
tarball in isolated Node and React/Vite consumers.
