# TypeScript client → gateway → StoreHost

This document specifies the **user-facing leg** of relay v1. The host-facing
leg remains [the existing Python relay contract](relay-protocol.md), carrying
Store protocol v3 unchanged. The SDK exports `RelayTransport` for this leg.
It does not implement application authentication, routing storage, or a
production gateway server.

Each user connection attaches to one store and receives one isolated virtual
host client. A room name alone is insufficient: subscription IDs, transaction
prefixes, delivery ordering, flow control and cleanup belong to that virtual
client. Canonical data and subscription execution stay in the Python host.

## Policy boundary

The package exports the types `GatewayPolicy<StorePrincipal, UserPrincipal>`
and `GatewayAuthContext`. An application's gateway implements:

```ts
interface GatewayPolicy<StorePrincipal, UserPrincipal> {
  authenticateStore(credentials, context): Promise<StorePrincipal>;
  authorizeStore(principal, storeId): Promise<boolean>;
  authenticateUser(credentials, context): Promise<UserPrincipal>;
  authorizeUser(principal, storeId): Promise<boolean>;
}
```

See the exported interface for parameter types. Context contains request
headers and optional origin/remote address. Authentication may use tokens,
cookies or another application mechanism. Throw on authentication failure;
return false for a denied binding. Credentials never enter the Store protocol.

Authenticate each physical connection and verify authorization for the claimed
store ID before registration or attachment. Bind the authenticated principal
to the socket. A controller cannot claim an arbitrary store ID, and a user
cannot choose another user's virtual channel. Define controller replacement,
permission revocation, rate limits, TLS, origin restrictions and deployment
routing in the application gateway. Unit access here grants the store API;
finer application permissions remain gateway policy.

## Opening a user channel

The user Socket.IO connection sends namespace authentication:

```json
{
  "role": "user",
  "store_id": "application-001",
  "protocol_version": 3,
  "relay_version": 1,
  "credentials": { "token": "application-provided" }
}
```

After connection, the SDK emits `store_attach` with:

```json
{
  "store_id": "application-001",
  "protocol_version": 3,
  "relay_version": 1
}
```

The gateway verifies that attachment matches the authorized binding. Reject
unsupported versions, repeated attachment, inaccessible or unavailable stores
with callback `{ "accepted": false }`. A rejection must not leave a channel
open. On success:

1. Locate the currently registered physical store session and generation.
2. Allocate a positive, strictly increasing channel ID in that generation.
3. Record socket → generation/channel ownership before forwarding any frames.
4. Forward the existing host `open` frame with input sequence zero.
5. Return callback `{ "accepted": true }` only if the host accepted the frame.
6. Forward the host's `opened` frame unchanged to that user's `store_relay` event.

The opened frame has host output sequence zero and contains Store protocol v3
and `transaction_prefix: "relay:<generation>:<channel>:"`. The SDK requires
both the attach receipt and that opened frame before becoming connected.
Either may arrive first. Opened does not imply any state snapshot is loaded.

Host channel IDs must never be reused within a generation. A fresh physical
controller connection has a fresh generation. A replacement generation
invalidates every old route, frame and transaction prefix.

## Forwarding requests

The SDK sends `store_relay` frames:

```json
{
  "generation": "GENERATION",
  "channel_id": 1,
  "sequence": 1,
  "type": "request",
  "payload": {
    "requests": [{
      "type": "transaction",
      "transaction_id": "relay:GENERATION:1:CLIENT_NONCE",
      "actions": [{
        "type": "set",
        "schema": "PressureSchema",
        "values": { "target": 25 }
      }]
    }]
  }
}
```

Validate generation, channel ownership, input sequence, allowed frame type and
size before forwarding. Use the server's bound route, never an arbitrary
client-supplied destination. Serialize each channel's input. Input sequence
starts at one after `open`; requests and `close` share it. Keep transaction
IDs and the native request payload unchanged. Python independently checks the
reserved transaction prefix before executing a batch.

The gateway's callback `{ "accepted": true }` acknowledges enqueue/delivery
only. The SDK waits separately for a host `response` frame whose
`request_sequence` matches the request. It never marks a transaction confirmed
from this callback. Rejected receipts, missing results and timeouts close the
channel; transmitted unresolved transactions have an unknown outcome.

## Forwarding store output

Route every `opened`, `event`, `response`, and `closed` frame to exactly its
own user socket. Preserve the entire frame and per-channel output order.
Output sequences start at zero and cover every output type. Do not broadcast
a single host subscription or transaction response to a store-wide room.

The store may emit canonical events before a request's response. The SDK
buffers those events while the batch is in flight, validates the complete
response batch, installs snapshots, applies buffered events, and only then
settles transactions. Signals observe the complete event's final values.

There are two independent receipt boundaries:

| Receipt | Sender → receiver | Meaning |
| --- | --- | --- |
| `store_relay_ack` for host output | Gateway → Python relay | Frame entered the gateway's bounded delivery queue. |
| `store_relay_ack` for user output | SDK → gateway | Frame was accepted by the user's SDK transport. |

Both carry `{ generation, channel_id, sequence, accepted }`. Neither means a
transaction succeeded. The gateway must not hold Python's output receipt until
React renders or a user-level request completes; that can deadlock delivery.

Maintain a bounded FIFO for each user, and consume its receipts against that
route and sequence. On a missing receipt, invalid receipt, size limit or queue
overflow, close that user channel and release its host subscriptions. Never
skip a frame and keep using the same sequence or revision stream. Other users
must remain independent. Also enforce a bound across the physical store
connection, as described by the host-facing protocol.

## Closing and reconnecting

An intentional SDK disconnect emits `close` at the next input sequence when
possible, then disconnects Socket.IO. The gateway must also handle abrupt
socket loss. If no close was forwarded, send a close using the route's next
input sequence; serialize it after earlier forwarded requests. Release routing
records when the host closes the channel. Acknowledge host closed frames even
if the user has already disappeared.

When a controller disappears, close its user channels. Do not transfer in-flight
traffic into a replacement generation or silently retry it. A reconnecting
user authenticates again, obtains a new channel/prefix, and resubscribes using
cached store identity and revisions. Python either confirms that cache or
returns snapshots. A restarted store gets a new instance ID and fresh snapshots.

No relay delivery mechanism provides durable exactly-once execution. A change
can commit before a connection fails. The client records `unknown`, removes
pending overlays, and refreshes confirmed state on reconnect. Keep that result
distinct from both rejection and confirmation.

## Implementation and tests

`packages/client/tests/gateway.ts` is a deterministic Node Socket.IO test peer.
It demonstrates both legs against the actual Python relay, with fixed
credentials and fault injection. Its queues and policies are intentionally
test-only; use this contract, not that fixture, as a production gateway design.

Tests cover simultaneous virtual clients, transaction namespace isolation,
held gateway responses, lost responses after commit, authorization rejection,
controller reconnection and store restart. The Python golden fixture in
`tests/relay/fixtures/store-v3.json` is also decoded by the TypeScript tests.
Keep Python and JavaScript releases together with their protocol fixtures;
this release accepts Store v3 / relay v1 without a version negotiation matrix.
