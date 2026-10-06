# Gateway integration contract

This contract adds a small routing envelope around unchanged Store protocol
v3 batches and events. Relay envelope version is `1`. Both sides require exact
versions; there is no compatibility translation or multi-version negotiation.
The gateway is trusted by the Store and must enforce the application policy.

## Gateway integration responsibilities

An application gateway supplies these integration boundaries. Names below are
illustrative interfaces, not an implemented Node package:

```ts
interface GatewayPolicy {
  authenticateStore(credentials: unknown, storeId: string): Promise<StoreGrant>;
  authenticateUser(credentials: unknown): Promise<UserPrincipal>;
  authorizeUnit(user: UserPrincipal, storeId: string): Promise<void>;
}
```

`StoreGrant` must authorize that authenticated host to register the specific
store ID. `UserPrincipal` is the application's user identity. Keep those types,
credentials, session renewal, permissions, and revocation rules outside the
store package. The gateway must:

1. Authenticate each store connection before accepting registration. Match
   registration metadata to the authenticated connection, not a supplied room
   name alone. Enforce a single current owner/generation for each store ID.
2. Authenticate users and authorize their selected store before opening a
   channel. Bind channel ownership to that authenticated user socket. Generate
   channel IDs on the gateway; do not trust client-supplied routing envelopes.
3. Maintain bounded ordered queues for each channel in both directions. Route
   responses and subscription events only to that channel's client. A room
   may locate a Store connection; do not broadcast its responses to the room.
4. Close a channel when its user leaves, loses access, or cannot keep up.
   Reject new work when its Store route is unavailable. Do not spool mutations
   offline. On Store disconnect/replacement, fail pending operations as
   uncertain and discard that generation's routing queues.
5. Keep the inner request, response, and event payload unchanged. Never turn a
   transport receipt into a Store transaction response. If forwarding fails
   after acceptance, tell the client the channel is lost so it resynchronizes.

Horizontal gateway deployments need ownership fencing across gateway workers.
Reconnect/resume must never allow two authoritative hosts for the same route.
Connection recovery or Socket.IO retries must not replay old relay frames.

## Host registration

The Python host initiates Socket.IO on namespace `/`. Its configured endpoint
path selects the HTTP Socket.IO path. Authentication has this shape:

```json
{
  "role": "store",
  "store_id": "installation-001",
  "generation": "<fresh random hex string>",
  "relay_version": 1,
  "protocol_version": 3,
  "credentials": {"token": "<application credential>"}
}
```

After namespace authentication, the host calls `store_register` with
`store_id`, `generation`, `relay_version`, and `protocol_version` (no credentials).
The gateway replies using its Socket.IO acknowledgement callback:

```json
{
  "accepted": true,
  "generation": "<same generation>",
  "relay_version": 1,
  "protocol_version": 3
}
```

Any other acknowledgement prevents the adapter becoming online. The gateway
must reject unsupported versions. Registration must finish before the gateway
routes users; the adapter also buffers bounded traffic arriving at the ACK
handoff. Generation identifies a physical host connection, not the lifetime
of canonical Store state (`store_instance_id` has that separate role).

## Frames and channel lifecycle

Both directions use the Socket.IO event `store_relay`, containing one JSON
object. Binary payloads and non-finite numbers are unsupported. Frame limits
count ASCII JSON bytes with `ensure_ascii=true`, no spaces, and finite values.

Every frame has `generation`, `channel_id`, `sequence`, and `type`.
`channel_id` is an integer in `1..2^53-1`; `sequence` is in `0..2^53-1`.
Use a strictly increasing channel ID for each open within a generation,
including after rejected opens. Never reuse one until a new generation.
Sequences are independent in each direction, per channel, starting at zero.
Out-of-order or repeated frames close that channel instead of being replayed.

| Direction | type | Fields beyond envelope | Meaning |
| --- | --- | --- | --- |
| Gateway → host | `open` | `payload: {protocol_version: 3}` | Sequence 0; allocate a distinct host client |
| Host → gateway | `opened` | `payload: {protocol_version: 3, transaction_prefix: string}` | Sequence 0; channel ready |
| Gateway → host | `request` | `payload: {requests: [...]}` | One unchanged ordered v3 batch |
| Host → gateway | `response` | `request_sequence: number`, `payload: {responses: [...]}` | Actual host result for that request frame |
| Host → gateway | `event` | `payload: {event: {...}}` | Unchanged v3 canonical subscription event |
| Gateway → host | `close` | None required | Abort queued work and release subscriptions |
| Host → gateway | `closed` | `payload: {code: string}` | Terminal channel notification |

`closed` can be the first frame when opening was interrupted. A close is an
abort, not a request to drain unexecuted mutations. Already executed work
cannot be rolled back. Reopen with a new channel and resubscribe.

Example request frame after open:

```json
{
  "generation": "G",
  "channel_id": 1,
  "sequence": 1,
  "type": "request",
  "payload": {
    "requests": [{"type": "subscribe_single", "schema": "PressureSchema"}]
  }
}
```

`request_sequence` is batch correlation, separate from each transaction ID.
Preserve the order of items in both batches. Inner subscription IDs, Store
instance IDs, revisions, transaction IDs, signal occurrence IDs, and structured
failures must not be translated or regenerated by the gateway.

### Transaction namespace

`opened.payload.transaction_prefix` is `relay:<generation>:<channel_id>:`.
Forward it to the remote client during channel setup. The client's transaction
ID factory appends a fresh UUID/nonce before creating transaction, undo, or
redo requests. This is a transport concern, hidden by the future client API.
It must be applied before creating optimistic overlays keyed by that ID.

All mutation IDs in an incoming batch must use this prefix; otherwise the
whole batch is rejected before execution and the channel closes. IDs reach
the Store unchanged. This prevents one remote client using another client's
IDs in the existing host-wide deduplication cache or overlay matching.
Local application code must not manufacture IDs in the reserved `relay:`
namespace. A new channel gets a new namespace; it cannot retry an old pending
transaction to obtain its result. Durable outcome lookup is not implemented.

## Receipts, flow control, and actual completion

For gateway-to-host frames, the host's optional Socket.IO callback is an
immediate receipt: `{"accepted": true}` means enqueued, not executed. On
rejection it returns `{"accepted": false, "code": "..."}`. Codes include
`disconnected`, `invalid_frame`, `stale_generation`, `unknown_channel`,
`invalid_sequence`, `protocol_mismatch`, `client_limit`, and `queue_full`.
The gateway must act on rejections and terminate affected client operations.
Uncorrelatable malformed/oversized frames invalidate the physical session.
Stale-generation traffic is rejected without advancing a current channel.

For **every host-to-gateway frame**, the gateway sends a separate
`store_relay_ack` event after accepting that frame into its bounded, ordered
forwarding queue:

```json
{
  "generation": "G",
  "channel_id": 1,
  "sequence": 2,
  "accepted": true
}
```

This is an application event, not the Socket.IO acknowledgement callback for
`store_relay`. Reply with `accepted: false` if that channel cannot accept the
frame. Always acknowledge `closed` after removing its route, even if the user
socket has already gone away; this cleanup is idempotent. Do not wait for a
user to submit another request or for transaction
completion to send this receipt: that would deadlock the host's delivery
barrier. Receipts are used only for flow control. They are never forwarded as
transaction results. A rejected/timed-out delivery closes its channel. If the
gateway cannot acknowledge the final `closed` notification, the adapter
resets the physical connection to invalidate all possibly lingering routes.

The host serializes request batches within each channel. Other channels have
independent workers. Store execution is synchronous on the Store's owner loop,
so concurrent client mutations commit in that loop's order. Multi-writer
conflicts use the existing optional expected-instance/revision guards;
unguarded writes keep their existing last-committed behavior.

Before returning a batch response, `StoreHost.request()` waits for all already
queued events for the requesting client. Relay sends preserve that barrier:
corresponding canonical events precede its response in the channel's outgoing
stream. Other clients do not have to acknowledge those events before the
requesting client can complete. A no-op may correctly confirm without an event.

The gateway must preserve the combined event/response stream all the way to
the remote client. Do not resolve a separate browser acknowledgement faster
than preceding events can be delivered/applied. If snapshot/subscription
responses are pending, the client must buffer events, install the subscription
snapshot, drain buffered events with revision validation, and only then settle
transactions. Apply an entire canonical event before publishing callbacks or
signals, just as the Python client does today.

Only an inner `transaction` response is a successful Store confirmation.
An inner `transaction_failure` is a structured Store rejection. A channel or
connection failure leaves unresolved sent mutations with unknown outcomes.
Preserve confirmed state beneath pending overlays, remove only the failed or
completed overlay, and reconcile uncertain outcomes by resubscribing.

## Compatibility fixtures and tests

`tests/relay/fixtures/store-v3.json` is a normalized transcript from a real
host and Socket.IO gateway test peer. It covers opening, subscription snapshots,
canonical changes with a named signal, transaction confirmations, structured
validation rejection, and no-op confirmation. Its generation, Store instance,
and subscription IDs are placeholders. The test suite compares actual traffic
against it and round-trips its inner payloads through the existing codecs.

Use this transcript as a shared fixture in the future TypeScript codec suite.
Then run the same scenarios against a real Python host and the application's
Node gateway: multiple clients, pending overlays, event-before-confirmation,
subscription handoff, revision gaps, guarded writes, disconnect after commit,
Store restart, signal non-replay, auth revocation, duplicate host registration,
and slow clients. Test route isolation between two different store IDs as well
as two users of one Store. This Python update does not yet implement or validate
a production Node gateway or TypeScript/React SDK.
