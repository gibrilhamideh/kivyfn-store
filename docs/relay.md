# Outward Store relay

`kivyfn-store` manages application state. The authoritative Store may run in
the Kivy application process, another process on its device, or a server.
Transport changes how clients reach that Store; it does not change which
Store validates and commits their work. Persistence still follows the existing
schema `persistent` settings and the Store's configured storage path.

`RelayConfig` adds an outward Socket.IO connection to an existing `StoreHost`.
The same host continues serving local clients. The relay creates a separate
host client for each remote client and uses the existing request handler,
subscriptions, revisions, structured failures, and event delivery queues.
It maintains no second Store or state mirror.

## Configure the existing application

```python
import os

from kivyfn.store.builder import Builder, HostConfig, RelayConfig
from kivyfn.store.host.host import StoreHost
from kivyfn.store.store import Store


async def relay_auth() -> dict[str, str]:
    # Obtain or refresh application credentials here on every attempt.
    return {"token": os.environ["STORE_RELAY_TOKEN"]}


config = (
    Builder()
    .tree(tree)  # Your existing Branch tree.
    .storage_path("./storage")
    .host(
        HostConfig(
            endpoint="direct://application",
            relay=RelayConfig(
                endpoint="https://gateway.example.com/relay/socket.io",
                store_id="installation-001",
                auth=relay_auth,
                reconnect_interval=10.0,
            ),
        )
    )
    .build()
)

store = Store(config)
host = StoreHost(store)


async def run_store(stop_requested) -> None:
    store.boot()
    try:
        await host.start()
        await stop_requested.wait()
    finally:
        try:
            await host.stop()
        finally:
            store.shutdown()
```

`tree` and `stop_requested` come from the application. Keep its current
Store/Host startup if it already has one: add `relay=RelayConfig(...)` to its
`HostConfig`. Existing StateManagers, screens, bindings, automation, schema
classes, transactions, and signals continue to use the same Store normally.

For an existing HTTP listener, keep its endpoint, for example
`http://127.0.0.1:8765`. A local listener and the outward relay are independent.
Gateway authentication applies to relay traffic; it does not add authentication
to the existing HTTP listener. Keep that listener on a trusted interface.
An in-process application can use `direct://application` with no local socket.

The relay endpoint accepts HTTP(S); use HTTPS across untrusted networks.
TLS certificate verification stays enabled. The endpoint path is the Socket.IO
HTTP path, not a Socket.IO namespace. The namespace is `/`. With no path, the
path defaults to `socket.io`. Credentials, queries, and fragments in the URL
are rejected; use `auth` for credentials.

## Public configuration and status

Import `RelayConfig`, `RelayAuthProvider`, and `RelayCredentials` from
`kivyfn.store.builder`. `HostConfig.relay` defaults to `None`.

| RelayConfig option | Default | Meaning |
| --- | --- | --- |
| `endpoint` | required | Outward HTTP(S) Socket.IO endpoint |
| `store_id` | required | Application-defined routing identity |
| `auth` | `None` | Credential mapping or zero-argument sync/async provider |
| `reconnect_interval` | `10.0` | Seconds between attempts, with ±20% jitter |
| `connect_timeout` | `10.0` | Deadline for credentials, connect, and registration together |
| `request_timeout` | `10.0` | Deadline for each outgoing gateway receipt |
| `max_clients` | `64` | Active or closing remote channels per connection |
| `max_pending_messages` | `64` | Per-client queued incoming batches and queued outgoing events |
| `max_pending_bytes` | `4194304` | Per-client queued incoming normalized JSON bytes |
| `max_frame_bytes` | `1048576` | Maximum normalized JSON size of an individual frame |

The active request and in-flight event are additional to queued counts.
Outgoing host events have a count bound; the byte limit is checked when each
event or response is encoded. Tune limits to the application's payload sizes
and client count. Oversized snapshots close the channel; this protocol does
not provide snapshot chunking.

`store_id` identifies a route. It is not a password or proof of ownership.
The gateway validates credentials and authorizes registration for that ID.
`auth=None` sends an empty credential object; whether this is accepted is a
gateway policy. The provider must return a mapping and must not block the
host's event loop. Use an async provider for network token refreshes.
Credentials are nested under `credentials` in the handshake and omitted from
configuration reprs and relay error messages.

After configuration, `host.relay` is a `SocketIORelay`; otherwise it is `None`.

```python
await host.start()  # Local readiness; never waits for the gateway.

if host.relay is not None:
    print(host.relay.running)  # Supervisor started, including while offline.
    print(host.relay.online)   # Connected and registration accepted.
    print(host.relay.last_error)  # Sanitized error, or None after success.
    # Optional for a readiness check; not required in application startup.
    await host.relay.wait_online(timeout=10.0)
```

`wait_online()` raises `StoreTimeoutError` on its deadline and `StoreStateError`
when stopped. Cancelling one waiter does not cancel the connection attempt.
`online` describes host-to-gateway registration, not any user's synchronization.
The host owns relay start/stop; callers normally only use its status methods.
All StoreHost and relay operations belong to the same event loop.

## StoreProcess

Use the same `HostConfig(relay=...)` in the StoreConfig passed to
`StoreProcess(process_manager, config)`. Its child creates the StoreHost and
starts/stops the relay automatically. The local process endpoint must remain
HTTP, such as `http://127.0.0.1:0`; Direct endpoints cannot cross processes.
Process readiness still reports the local endpoint and does not depend on WAN.

With multiprocessing spawn, the auth provider must be a module-level callable
or a picklable callable object. Read secrets or create token clients inside
that provider in the child. Do not pass live sockets, event loops, closures,
or asyncio clients inside StoreConfig. The included spawn test exercises
registration, a remote transaction, and supervised shutdown in a real child.

## Recovery and transaction outcomes

Every outward connection has a fresh generation. Each remote channel owns
its host subscriptions and transaction namespace. A disconnect cancels queued
work and removes those subscriptions. Reconnection refreshes credentials and
registers a fresh generation. It does not restore old channel IDs or replay
request batches, mutations, or signals.

Clients reopen channels, resubscribe desired sources, and pass cached Store
instance/revision metadata. The existing host sends a snapshot when that cache
is stale and may omit unchanged data when it matches exactly. A Store restart
changes its instance ID and requires snapshots. A relay reconnect alone does
not change the Store instance or retained state.

A transport failure after sending a mutation has an **unknown outcome**:
the Store may have committed before the response was lost. A fresh snapshot
reconciles current data, but does not prove whether a signal ran or which
writer caused a particular value. Do not automatically retry interrupted
mutations or report a gateway receipt as success. The existing in-memory
transaction deduplication cache is bounded and does not provide durable,
exactly-once execution or outcome lookup across reconnects/restarts.

Canonical events are never sampled or dropped to reduce bandwidth. A slow
client is disconnected and resynchronizes. Sample high-frequency input before
committing to Store, or debounce UI rendering while still applying every
canonical message. Any future network coalescing needs a protocol designed
to preserve revision continuity, atomic event envelopes, and signals.

## Scope and next layers

The Node gateway must implement [the relay contract](relay-protocol.md).
Authentication, permissions, routing ownership, and deployment policy remain
application integrations. A plain Socket.IO room/broadcast does not implement
per-client sessions or the completion ordering contract.

The browser/Node TypeScript client and React/MobX layer are separate work.
They can reuse the v3 message semantics and included JSON transcript fixtures.
Existing direct Python Socket.IO clients use their existing endpoint protocol;
they do not become gateway-aware simply by changing their endpoint URL.

Runtime JSON schema discovery, string-named StateManager creation, and optional
generated Python/TypeScript types remain a TODO in the relay protocol module.
Current Python schema-class APIs remain in place. The existing diagnostic
schema catalogue is not yet a portable client schema/validation contract.
