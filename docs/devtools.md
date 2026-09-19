# Browser devtools

Enable the optional inspector through the same Builder configuration as the
Store and its Host:

```python
# app/store/config.py
from kivyfn.store import Store, StoreHost
from kivyfn.store.builder import Builder, HostConfig

config = (
    Builder()
    .tree(tree)
    .host(HostConfig(endpoint="direct://application", devtools=True))
    .build()
)
store = Store(config)
store.boot()
host = StoreHost(store)
await host.start()
print(host.devtools_url)
```

`tree` is the application's `Branch` declaration. `devtools=False` is the
default and disables the inspector. `True` and `DevToolsConfig()` both select
`http://127.0.0.1:3000`. For custom settings, pass
`devtools=DevToolsConfig(port=8766)` to `HostConfig`. Explicit `port=0` requests
a dynamically allocated port. An occupied configured port fails startup with a
bind error instead of silently changing the URL.

The resolved `host.devtools_url` is available only while the listener runs.
Opening a browser is an explicit application/user action.
`StoreProcess(processes, config)` uses the same configuration and reports
`devtools_url` with its bound application endpoint in readiness information.

The listener uses aiohttp on the Host's current asyncio loop. Direct applications
open only the devtools listener; Socket.IO applications retain their separate
application listener. No additional Store, application subscription, background
server process, or event loop is created. Routine HTTP access logs are disabled
for the devtools listener; startup URL and error logging remain available.

## What the views mean

The interface uses a Tokyo Midnight inspired dark theme. Schema/entry
navigation and detail panes scroll independently. Selecting a schema loads its
current data and follows live updates automatically; there is no prerequisite
Capture step. A loading state distinguishes an awaited snapshot from a loaded,
empty schema. Mutation and signal controls become available when their live
source is ready; collection signals also require a selected row.

Each main view has **Structured** and **Dictionary** modes. The preference is
remembered per view in this browser. Dictionary mode expands nested values and
lets you copy a value or its JSON Pointer path. Large branches reveal 50 children
at a time; diagnostic integer and unsupported-value tags remain visible.

State shows collections as tables and single schemas as field/value lists.
Collection tables support local search, sortable columns, column visibility and
50-row pages. These controls operate on the loaded snapshot. Selecting a row
opens all of its fields together; selection follows its ID as live data changes.
A removed selection stays explicitly unavailable until another row is chosen.
Foreign-key values can open their related schema and row. A field's Activity
button narrows the retained records to that field and row.

**Edit values** opens a local structured draft. Collections support editing
several rows, adding rows and marking rows for removal in one transaction.
Existing row IDs cannot be edited. String and numeric fields have text inputs,
booleans have checkboxes, enums have choices, and nested/custom values use JSON.
Unsupported diagnostic objects remain read-only. Null is distinct from an empty
string. Invalid input stays visible and blocks review/submission.

Choose **Review before applying** to inspect the requested before/after changes
and generated actions, then **Apply transaction** to submit one ordinary guarded
transaction. Only changed fields are sent for existing records; the Store still
validates values and runs triggers. Reverting all changes leaves nothing to
submit. Confirmed structured drafts close. **Keep draft and browse** preserves
inputs, validation errors and history labels while you inspect other views;
**Resume draft** brings them back. Closing an unfinished draft asks before
discarding it. Drafts live in this tab's memory and do not survive a page reload.

**Edit as new mutation**, **Insert row**, **Remove row**, and **Draft signal**
keep the advanced ordered-action editor available. **Advanced actions** also
shows the generated structured actions; changing that JSON switches the draft
to advanced editing. The modal traps keyboard focus and scrolls internally.
Ctrl/Command + Enter reviews or submits the active draft. A confirmed advanced
draft cannot be submitted again unchanged; editing it starts new work.
Collections are keyed by ID, with no historical row-position guarantee.

State shows canonical Store snapshots and per-schema revisions. It does not show
client-local Kivy properties, pending optimistic overlays, or individual window
State objects. Drafts are new ordinary transactions guarded by the displayed
Store instance and schema revisions. Selecting a schema loads its live data
before a new edit or signal draft is opened; that ordinary workflow needs no
extra loading control.

A valid draft may refer to additional schemas, including actions copied from
Activity. The editor automatically loads those known schemas through the same
live inspection scope. After newly referenced schemas finish loading, choose
**Use current revisions** to include their displayed revisions in the draft's
command guards before applying it. Existing guarded revisions are never silently
replaced by incoming live data. Malformed JSON, incomplete actions and unknown
schema names remain editor errors rather than invalid live-scope requests.

A stale draft is rejected atomically. Structured drafts offer **Compare with
current**, showing original, current and proposed values. **Keep draft with
reviewed revisions** adopts that displayed baseline while preserving explicit
edits and refreshing untouched fields. Apply remains a separate action. A row
removed remotely cannot silently become an insertion, and a new row ID collision
must be resolved. Another live update can make the reviewed baseline stale again.
Advanced drafts retain **Use current revisions** for explicitly renewing their
guards. Live data never replaces an unfinished draft automatically.

History shows a before/after table alongside its dictionary representation.
Activity connects **Request**, **Response**, **Changes**, and **State afterward**
for retained operations. Missing or truncated payloads remain explicit. Grouped
history summaries are not treated as a single original request.

History belongs to Store independently of this inspector or Activity recording.
An entry has a stable identity; an open group has revisioned contents. Inspected
entries are detached snapshots and cannot modify stored history. Undo last and
Redo next operate on the currently displayed guarded history position, not the
selected historical row. Failures preserve the canonical data and cursor.

Activity captures requests, produced responses, commits, signals, queries,
subscription requests, connection changes, and boundary failures. A produced
response does not prove delivery to a remote client. A disconnected/cancelled
request can have committed even if no response was produced or delivered.
Browser commands enter the normal Host dispatcher and are captured once there.
A Store commit is recorded once regardless of application subscriber count.
Request records use **Received** to describe their arrival, not an indefinitely
pending execution. Details link a matching retained response and its outcome.
When recording was paused, filtered, truncated or evicted, a missing response is
shown as **Response unavailable**, without asserting that execution is pending.
A recorded response is still not proof of delivery to an application client.

The Activity toolbar shows **requests/s** for a rolling one-second window,
refreshed every 250 ms. It counts live request records arriving at this browser,
using the same client, schema, category, status, identity, row and field filters
as the list. Each recorded request batch counts once, even when it contains
multiple actions or touches several schemas; responses and commits do not add
to the count. Selecting a non-request category therefore shows zero requests/s.

The counter measures browser-observed captured traffic, not total Host throughput
when recording is filtered or data is missing. It uses local monotonic arrival
time, so host/browser wall-clock differences do not affect the window. Snapshot
history loaded at connection or schema selection is not counted as new traffic.
The window is independent of the list's 500-record limit, decays to zero when
idle, and stays live while display is paused or another tab is open. Disconnected
or paused recording shows an unavailable marker instead of a misleading zero;
reconnects, stream gaps and recording-control changes start a fresh window.

**Clear display filters** restores local browsing. Shared recording filters are
shown above the list with a separate **Clear recording filters** button. Clearing
them allows future activity through; it cannot recover records excluded earlier.

Copy as new mutation uses original caller actions retained in Activity. It never
turns expanded trigger/cascade changes or normalized restoration records into
new executable intent. Copying is unavailable when the request was filtered,
paused, evicted, truncated, or when a grouped entry has no unambiguous request.
The new draft receives a new transaction ID only when submitted as new work.

Undoable signals emit declared compensating commands after canonical restoration.
They do not confirm that an external handler succeeded. If a compensation handler
submits new tracked work, that work creates a separate history transaction and
can clear redo. There is no implicit causality tracking or history suppression.
See [history.md](history.md) for the recorded-effect restoration contract.

## Recording, preview, and limits

Activity recording retains bounded diagnostic events for later inspection and
preview. It is independent of the schemas currently loaded in a browser.
Recording pause and display pause are independent of live State feeds, normal
application synchronization, and Store undo retention. Activity filters
are applied before payload retention. Filters select a schema, client ID,
category, status, or transaction/capture identity. They do not remove commits
from the chronological preview timeline. Client and identity text filters match
substrings consistently in display and recording; schema, category and status
filters match exactly. Row/field filters are local display filters only; they do
not change recorder retention. Shared recording pause/filter changes update all browser
tabs; display pause belongs only to the current tab. Recording controls live in
Activity. Selecting a schema for live inspection does not turn recording or
history retention on or off.

Preview reconstructs a detached checkpoint plus complete chronological commit
deltas. It does not execute requests, triggers, signals, undo, or queries against
the live Store. Grouped history cannot replace this timeline because independent
untracked commits can occur between grouped commits. Live cache updates continue
separately while the preview remains fixed. Previous/Next commit steps through
retained commit positions; mutation controls remain disabled. Return to live
exits preview.

Preview records all schemas when the baseline fits its budget, then presents
only the browser's selected scope. If the complete baseline does not fit, a
selected-scope connection establishes a bounded baseline for that scope.
Values outside the recorded scope are absent, not filled with current data.
A recorded query response can be inspected as its original Activity record;
running it again is a new live query. Query responses have table and dictionary
views and show their fetch time. They are one-shot snapshots, not live-query
subscriptions; run again to refresh. Query tables initially preserve the host's
result order. Evaluating new historical joins/queries is unavailable in this
release.

The diagnostic budget is separate from undo retention. Half of `max_bytes` bounds
Activity records and half bounds checkpoint/delta retention. Oldest complete
Activity records are evicted. Preview eviction advances the checkpoint by
applying complete chronological deltas; earlier positions become unavailable.
Recording pause, a failed capture, or an oversized delta invalidates continuity.
Resume recording establishes a fresh checkpoint. A checkpoint larger than its
budget remains explicitly unavailable. Huge history detail is marked truncated;
head status remains available. Unsupported values are displayed as inert tagged
representations and cannot be submitted as editable values.

Defaults:

| Setting | Default |
| --- | ---: |
| `port` | 3000 |
| `max_records` | 1000 |
| `max_bytes` | 8 MiB |
| `max_payload_bytes` | 1 MiB |
| `max_queue_bytes` | 2 MiB per browser |
| `max_queue_messages` | 256 per browser |
| `max_request_bytes` | 1 MiB |
| `max_pending_captures` | 256 |
| `max_pending_commands` | 64 per browser |
| `max_sessions` | 16 |
| `max_filter_values` | 64 schema names |

A slow browser has one bounded queue and one writer task. Overflow announces a
gap and replaces its state with a new checkpoint and epoch. A single snapshot
that cannot fit closes that diagnostic connection with code 1009. It does not
interrupt application clients or reject their mutations. The browser selects
its live scope automatically; custom diagnostic clients can select an initial
WebSocket scope (`/stream?schemas=ComponentSchema`). Increase the applicable
limits for a larger inspector workload. Recorder and
browser failures never change a committed operation into a failure.

## Diagnostic protocol

The HTTP application and assets share one literal loopback origin. Host and
Origin checks reject unexpected origins. Each WebSocket receives a server-issued
session identity; HTTP requests use `X-DevTools-Session`. Client IDs are never
accepted from command payloads. Distinct tabs receive distinct sessions.
Subscription/unsubscribe commands are rejected at this boundary. Session
commands and disconnection are serialized. Admission is bounded before body
parsing or awaiting the session lock; excess commands receive HTTP 429. V1 is
local-only and adds no remote
administration or authorization system.

| Endpoint | Contract |
| --- | --- |
| `GET /api/info` | Versions, capabilities, schema catalog, limits |
| `GET /stream` | Establish session; initial snapshot then diagnostics |
| `POST /api/commands` | Existing RequestBatch wire format / ResponseBatch |
| `GET /api/state` | Selected canonical checkpoint |
| `GET /api/history?limit=100&offset=0` | Bounded detached history page |
| `GET /api/activity?limit=100` | Bounded Activity records and recording status |
| `GET /api/preview?sequence=N` | Available detached checkpoint or reason |
| `POST /api/capture` | `{ "paused": true, "filters": {} }` |

Except `/api/info`, inspection and command routes require the active session
header. No browser-provided trusted history records or validation-bypass flag is
accepted. Ordinary mutation, query, history, undo, and redo requests use the shared
request codec and Host entry point.

Every WebSocket envelope contains `version`, `store_instance_id`, `epoch`,
`sequence`, `kind`, and `payload`. Envelope sequences are consecutive for a
session, including filtered views. Snapshot payloads contain `session_id`,
`checkpoint`, `history`, and `activity`. A scope control
`{ "kind": "scope", "schemas": ["ComponentSchema"] }` establishes a fresh
epoch and checkpoint. Reconnect also replaces stale views with a new snapshot.
The recorder's separate `sequence` in commit/Activity payloads selects preview
positions. It is not a schema revision or application synchronization cursor.

Commit payloads contain event types `set`, `setrow`, `insert`, and `remove` with
schema, key/row ID, and old/new values, plus signals, schema
revisions, transaction attribution, and original/undo/redo kind. Apply each live
commit once. HTTP command responses acknowledge outcomes; they do not apply a
second copy of the mutation. Either HTTP or WebSocket delivery may arrive first.
Shared recording-control changes use the existing stream's `capture` message.

A diagnostic response-size limit can be reached after a command executes. That
case returns an explicit HTTP 502 unresolved outcome, not an ordinary rejected
mutation. The browser preserves the original IDs for its existing retry flow.
A retry can remain unresolved if the result still exceeds the diagnostic cap;
inspect canonical State before deciding whether to submit any new work.

Diagnostic values outside JavaScript's safe integer range use
`{"$integer":"1208925819614629174706176"}`. Commands preserve those tags until
the server decodes them before the normal request codec. Literal dictionaries
that collide with a reserved diagnostic tag are escaped as
`{"$object":[["$integer","literal data"]]}`. Unsupported objects use
`{"$unsupported":"type name"}`; their repr/callables are never executed.
Schema and row maps are structural dictionaries; only arbitrary field payloads
use value escaping. Valid row IDs such as `$integer` therefore remain selectable.
Python and JavaScript integers are never silently rounded during copying/editing.

Logical transaction IDs survive permitted retries. Cached-result retries belong
to the same live Host while it retains the outcome. Reconnect does not cause a
fresh-ID command or draft resubmission. If a reply is lost, the outcome is
ambiguous; keep the original ID and guarded position for an explicit same-ID
retry. A restart invalidates the old Store-instance guard. Direct Store
navigation has position guards and no second result cache.

## Lifecycle and distribution

Startup checks packaged assets and reports bind/asset errors clearly. Partial
startup unwinds the listener and Store observers. Stop first prevents new browser
commands, serializes session cleanup, observes writer outcomes, and detaches
observers. Cancelling a stop waiter does not cancel the underlying cleanup.

Release wheels and source distributions include compiled React assets. Ordinary
installation requires no Node runtime or CDN. Assets resolve relative to the
installed package, independently of the current working directory. Source
contributors must build the frontend once or obtain matching prebuilt release
assets using the release helper. See the repository README for release build and
complete verification commands.

Application connections require exact protocol version 3. The bundled browser
and diagnostic server use diagnostic version 2. Upgrade the packaged browser
assets and server together; neither handshake negotiates older behavior.
