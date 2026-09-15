# kivyfn-store: correctness and usability work plan

Date: 2026-09-15

Status: correction pass and Builder/initializer/browser follow-up implemented
and verified with the available dependencies.
Actual kivy-stubs remains unavailable.
This document supplements [PLAN.md](PLAN.md) and the supplied release review.
It covers the review findings and the subsequent application discussion.
Latest user decision: collections are keyed by ID; row iteration order is not
part of restoration, synchronization, revision or notification guarantees.

## 1. Goal and current evidence

Keep the architecture suited to local, multiprocess, multiwindow Kivy apps.
Correct demonstrated failures, improve the browser workflow, and make the
changed code easier to read. Performance changes need measured evidence or a
specific correctness benefit; this is not a general rewrite.

The application's empty history issue is resolved. The running Store had
history disabled. Its startup now supplies `HistoryConfig()` as well as the
schema's `track_history = True`. The user confirmed that history and undo work.
Do not classify that configuration issue as an unresolved retention defect.
The separate cyclic-value and NaN defects are corrected by C05 and C06.

The existing release passed 1,293 Python tests, frontend checks, 14 frontend
unit tests, 20 real-backend browser tests, and installed distribution checks.
A subsequent test-fixture correction removed Python-version-dependent memory
size assumptions; the Python suite also passed on Python 3.12. These are
baseline results, not verification of the changes proposed here. The actual
`kivy-stubs` dependency remains an unverified gate.

The review's ordering, connection, history-value, and recovery reproductions
were checked against the implementation. Windows asset traversal is supported
by path-semantics analysis; a Windows runtime run is still required to claim
platform verification.

## 2. Ownership and contracts to preserve

### Keyed collections: revised contract

Remove `HistoryOrder`, history order plans/guards, `StoreCommit.row_orders`,
subscription `row_order`, order-only revisions and `MembershipDelta.order_changed`.
Undo/redo restores recorded IDs and values, not dictionary positions. Queries,
State and the browser treat collections as keyed mappings. Python dictionaries
still iterate normally, but callers must not rely on positions surviving
restoration or synchronization. Applications needing presentation order should
model and sort by their own meaningful field.

Keep instruction execution order, signal occurrence order, history transaction
order and diagnostic stream sequence. Those express actual execution semantics.
The private `_row_version` publishes visible membership changes only.


| Owner | Responsibility |
| --- | --- |
| Schema / Builder / boot | Declarations, configuration, defaults, loading and recovery |
| Compiler / Delta / operations | Ordered actions, trigger/cascade expansion, staged effects |
| Engine | Final relationships, uniqueness, indexes, persistence and atomic installation |
| Store / History | Canonical state, retention, grouping, guards, undo/redo and publication |
| Host | Event-loop ownership, request execution, connections and retained retry results |
| Client | Request lifecycle, reconnect policy, subscriptions and synchronization |
| State / StateManager | Reactive objects, optimistic overlays and caller transaction metadata |
| Devtools | Activity capture, detached inspection, preview and browser commands |

- All mutations of a hosted Store execute completely on the Host loop.
  Standalone Store use retains synchronous locking behavior.
- The publication guard covers canonical commit and history-status callbacks,
  including history-only notifications. Preserve callback exception isolation.
- Validate external shapes at codecs, storage decoding and archive import.
  Validate schema declarations when built. Do not repeat trusted-record shape
  validation in Store, Host and State.
- Preserve current-state conflict, relationship, uniqueness and atomicity
  checks at their existing owners.
- State may carry `history_group` and `history_label` because applications need
  grouped undo without devtools. It must not hold history records, Activity,
  capture filters, browser drafts or preview state.
- Devtools uses existing Host requests and Store observation points. It must
  remain optional; turning capture off cannot affect undo retention.
- Host/Client own cached-result retries while the same live Host retains the
  result. Direct Store undo/redo uses expected-position guards, with no second
  result cache. Repeating an applied position can return stale.
- Grouped entries have stable identities and revisioned contents. Inspection
  returns detached snapshots; callers cannot edit stored history.
- Copy as new mutation uses original caller actions retained in Activity.
  Expanded trigger/cascade effects and normalized history records are not
  executable caller intent. Disable copying without one unambiguous retained
  source request, including ambiguous groups. Add no copy-only request cache.
- Undoable signals retain their declared compensation. A handler submitting
  new tracked work creates a separate transaction and may clear redo. Do not
  infer causality or suppress that history automatically.

### Accepted trigger restoration contract

Undo/redo restores recorded effects, including previous trigger results. It
does not recompute those results against current untracked inputs.

For `y = max(x, z)`, with tracked `x` and independently editable untracked `z`:

| Action | x | z | y |
| --- | ---: | ---: | ---: |
| Initial state | 0 | 0 | 0 |
| Set x to 1; record the resulting y change | 1 | 0 | 1 |
| Independently set z to 1; unchanged y write is skipped | 1 | 1 | 1 |
| Undo the recorded transaction | 0 | 1 | 0 |
| Redo it | 1 | 1 | 1 |

This acceptance case must keep succeeding exactly as shown. Conflict guards
protect recorded effects and declared constraints; they do not guarantee that
arbitrary calculations remain consistent with changed untracked inputs.
Applications needing current calculated output should derive it from current
inputs, or track independently editable dependency inputs in the same ordered
history. Do not rerun triggers, add dependency tracking, reject this mismatch,
or require every schema read by a trigger to be tracked.

## 3. Necessary correctness corrections

Each item needs a regression proving observable behavior. Review numbers are
included so none of the supplied findings disappear during implementation.

### C01. Preserve State instruction order (review 1)

Owner: `src/kivyfn/store/state/transaction/transaction.py`.

A set-to-7 followed by an observer action and a removal currently lets the
observer see the old value 1. Insert-9, observer, remove can hide the temporary
row entirely. `add_remove()` removes earlier actions across intervening work.

- [x] Preserve earlier writes/inserts whenever intervening actions exist.
  Restrict compaction to the existing adjacent-action contract; do not invent
  trigger dependency analysis or assume adjacency alone proves all rewrites.
- [x] Test both sequences through State and equivalent ordered Store actions.
  Verify trigger results, committed effects and subsequent undo/redo.

### C02. Give Client teardown one owner (review 2)

Owner: `src/kivyfn/store/client/client.py`.

Old disconnect cleanup can resume after reconnect and clear the new loop and
tasks, leaving an apparently synced Client whose next request fails.

- [x] Use one shared teardown task. A new connection waits for old cleanup.
  A cancelled waiter must not cancel shared cleanup or clear newer fields.
- [x] Gate cleanup deterministically, reconnect concurrently, release cleanup,
  and prove the replacement connection remains usable on both transports.
  Include concurrent disconnect callers and waiter cancellation.

### C03. Bind Direct callbacks to their connection (review 3)

Owner: `src/kivyfn/store/transport/direct/client.py`.

A delayed close callback from a failed connection can kill its replacement
and leave orphan Host registrations.

- [x] Associate callbacks with the existing registration identity or a small
  connection generation. Ignore obsolete delivery while completing old cleanup.
- [x] Inject delivery failure, reconnect immediately, and assert one active
  registration, working subscriptions and no stale callback interference.

### C04. Reject wrong-loop lifecycle calls before effects (review 4)

Owner: `src/kivyfn/store/host/host.py`.

`stop()` currently starts teardown before ownership rejection, so a wrong-loop
call can disconnect clients and remove the endpoint before raising.

- [x] Reuse the existing ownership check at public lifecycle entry, including
  the already-running `start()` path, before any teardown or mutation.
- [x] Verify rejection leaves clients, subscriptions, listener and Store usable.

### C05. Normalize only relevant collection fields (review 5)

Owner: `src/kivyfn/store/core/operations.py`.

Comparing an untouched cyclic field during an ordinary numeric row edit can
erase unrelated retained history through an unsupported-value barrier.

- [x] Derive candidate row/field pairs from actual effects already available
  in the execution machinery. A schema-wide field list is insufficient.
  Structural removal/recreation still needs the appropriate complete row.
- [x] Keep prior singleton history and undo the numeric row edit while leaving
  an untouched cyclic field intact. Cover another row with the same field name.

### C06. Make NaN retention honest (review 6)

Owners: history capture and its existing value-retention policy.

A recorded NaN previously advertised undo availability but immediately
conflicted with itself. The existing capture boundary now owns this policy.

- [x] Treat NaN as unsupported during existing
  bounded history capture, including nested values. Use the existing barrier;
  preserve the successful canonical mutation and explain the limitation.
- [x] Test that this never creates an entry which advertises a usable undo
  while inevitably failing its own unchanged-value guard.

NaN-aware restoration comparison is an alternative if required by applications;
it would need consistent normalization, grouping and guard semantics. It is
not an additional requirement of this correction pass. Do not silently change
ordinary `DataField` equality, including the existing `True == 1` behavior.

### C07. Route JSON parser limits through boot policy (review 7)

Owner: `src/kivyfn/store/storage/file.py`.

Excessive integer digits and deeply nested JSON can raise `ValueError` or
`RecursionError`, bypassing corruption recovery and valid legacy fallback.

- [x] Normalize these decoding failures to `StorageCorruptionError` at the
  JSON-reading boundary. Keep filesystem `OSError` handling distinct.
- [x] Cover RAISE, RECOVER and ERASE, generation data and legacy temporary
  fallback with deterministic malformed files on supported Python versions.

### C08. Reject invalid canonical row identities (review 8)

Owner: canonical row construction in `src/kivyfn/store/node/node.py`.

Persisted rows keyed by an empty string currently boot but cannot be subscribed
to by a real State client.

- [x] Enforce the nonempty row-ID invariant where canonical rows are created,
  including direct construction. Let boot's existing policy handle bad input.
  Avoid repeating field validation down the subscription path.
- [x] Test all boot policies and a real subscription after successful recovery.

### C09. Preserve committed command outcomes (review 9)

Owners: `devtools/server.py` and frontend `api.ts`, under their existing roots.

A successful mutation followed by a large query response can exceed the
diagnostic cap and produce HTTP 400 after the mutation has already committed.

- [x] Separate pre-execution rejection from post-execution response overflow.
  A response that cannot be conveyed after execution returns bounded HTTP
  502 with `outcome: "unknown"`, using existing browser retry handling. An
  oversized query can remain unavailable on retry without repeating its
  cached transaction; no second response or result-cache framework is added.
- [x] Preserve response bounds and existing Host result-cache ownership.
  Never silently truncate a success into an ordinary rejection.
- [x] Exercise a supported mixed mutation/query batch against a real Host;
  verify canonical data, acknowledgement or unresolved status, and same-ID
  retry behavior without repeating committed work while its result is retained.

### C10. Separate structural maps from field payloads (review 10)

Owners: devtools checkpoint/preview encoding and frontend state consumption.

A collection with the valid sole row ID `$integer` can become a tagged
`$object`, breaking initial inspection, live updates and historical preview.

- [x] Keep schema/row maps structural and encode arbitrary field payloads at
  one boundary, or consistently decode structural maps at their consumers.
  Choose one representation; do not ban valid row IDs.
- [x] Cover reserved tag IDs, literal reserved-key field dictionaries, large
  integers and prototype-like IDs through HTTP, WebSocket,
  browser selection, live edits and preview.

### C11. Encode Activity commit values once (review 11)

Owners: `src/kivyfn/store/devtools/devtools.py` and `recorder.py`.

A commit is currently encoded before the recorder encodes it again. Large
integers and reserved-key objects then display differently across views.

- [x] Give raw-to-wire conversion one owner. Name already encoded inputs
  explicitly and retain payload accounting; add no general serializer framework.
- [x] Assert equivalent tagged values in live events, Activity and preview.

### C12. Align display and capture filters (review 12)

Owners: recorder matching, frontend `model.ts` and capture controls.

Display uses partial identity matching but capture uses exact matching.
Adopting display filters therefore drops future records that should match.

- [x] Keep the same matching rules and identity fields on both sides.
  Rule: substring matching for client/identity text and exact schema
  and category selection. Clarify status matching consistently as well.
  Include capture and transaction identities where the UI offers them.
- [x] Test records before and after adopting filters, including partial IDs.

### C13. Include signals in capture schema metadata (review 13)

Owner: `src/kivyfn/store/devtools/recorder.py`.

Signal-only commits have no data revisions and currently disappear under a
schema capture filter.

- [x] Union signal target schemas with changed-data schemas for capture
  matching. Keep data revision semantics unchanged.
- [x] Cover ordinary signals and undo/redo compensation occurrences.

### C14. Discard obsolete preview responses (review 14)

Owner: frontend `App.tsx` preview request lifecycle.

A delayed response can reinstall preview after Return to live.

- [x] Use one preview request generation, invalidated by exit, newer requests,
  scope changes and Store restart. Ignore obsolete results and errors.
- [x] Gate browser responses and prove that old requests cannot restore
  preview, replace a newer selection or disable live commands again.

### C15. Contain imported wheel assets (review 15)

Owner: `scripts/build_release.py` archive-import boundary.

POSIX-only path checks do not reject Windows backslash traversal or drives
before native destination paths are constructed.

- [x] Reject backslashes and Windows drive/root forms before writing members,
  retaining the existing forward-slash traversal protection.
- [x] Cover traversal, drive-relative and drive-absolute names and valid
  assets. Assert no file is written outside staging. Report actual platform runs.

### C16. Roll back failed container construction (review lower-priority)

Owner: `src/kivyfn/store/state/registry.py`.

A custom constructor can call `enter()`, then fail and leave a newly selected
row type pinned, preventing a legitimate retry with another type.

- [x] Roll back registrations and configuration introduced by the failed
  construction while preserving previously established objects.
- [x] Cover ordinary and query collections, retry with another type, and
  preservation of preexisting registrations.

### C17. Count real compiler operations (review lower-priority)

Owner: `src/kivyfn/store/core/compiler.py`.

A cascade boundary consumes no operation, yet checking the cap before handling
it makes exactly-at-limit removals fail while equivalent insert counts pass.

- [x] Enforce the cap when appending a real operation, after boundary and
  no-op handling. Keep protection against genuinely excessive expansion.
- [x] Test exactly-at-limit success and over-limit atomic rejection.

### C18. Synchronize shared recording controls (review lower-priority)

Owners: devtools server and frontend capture state.

One browser tab can pause shared capture while another still displays capture
as active.

- [x] Publish pause/filter status through the existing diagnostic WebSocket
  stream and include current status in reconnect snapshots.
- [x] Test two real browser sessions after pause, resume and filter changes.
  Keep display pause local to its own tab. Add no observer framework.

## 4. Requested rows binding enhancement

Priority: required application-facing enhancement. Owners:
`src/kivyfn/store/state/reactive/collection.py` and
`src/kivyfn/store/state/reactive/query.py`.

The current explicit `rows` dispatch works even with its cached live mapping.
The requested design makes the dependency visible using a private Kivy
`_row_version`, with applications continuing to bind to public `rows`.

Implemented declaration in both classes:

```python
# Both CollectionState and QueryCollectionState
_row_version = NumericProperty(0)


def _get_rows(self) -> Mapping[str, State[SchemaT]]:
    return self._rows_view


rows = AliasProperty(_get_rows, None, bind=("_row_version",))
```

Keep caching disabled. A focused Kivy 2.3.1 probe confirmed that this dependency
path dispatches membership changes for the same live mapping
object. Keeping `cache=True` suppressed those dependency-driven notifications;
`force_dispatch=True` did not correct that cached path. Neither that option nor
new mapping snapshots are needed for the noncached declaration.

- [x] Increment `_row_version` once when publishing a visible membership
  change, after canonical values and overlays have been reconciled.
  Use the existing publication phase and callback exception boundary.
  Preserve existing publication counts; do not promise one notification per
  transaction when a message already publishes several membership deltas.
- [x] Replace the direct `rows` dispatch in that path; do not keep two
  notification mechanisms and emit duplicate callbacks.
- [x] Leave no-op updates silent. A hidden canonical change under an optimistic
  overlay must not masquerade as a visible membership change.
- [x] Keep the mapping read-only and live; preserve surviving row identity,
  local properties, nullable query projections and established event order.
- [x] Keep `_row_version` local and monotonic for that container instance.
  It is not a Store schema revision, history revision, persisted value or
  protocol field. Consumers normally bind to `rows`, not this private detail.
- [x] Ordinary row-field edits notify the row's properties. Do not subscribe
  to every field just to refresh a whole collection.
- [x] Test Python `bind(rows=...)` and real KV expressions in both collection
  types, after reading `rows` before binding. Cover insert/remove/reinsert,
  undo/redo, fresh snapshots, optimistic rollback, unchanged membership and a
  raising callback. Every callback must see final data and membership. A change
  only to incidental dictionary iteration order must not dispatch `rows`.

Keep `__getitem__` as a direct row lookup. Do not add a new container hierarchy
or reactive framework for this change.

## 5. Automatic reconnect enhancement

Priority: requested enhancement, implemented only after C02 and C03 pass.
Owner: `StoreClient`; transports provide individual connection attempts.

Use one Client-owned retry task for Direct and Socket.IO. Reuse the existing
connect/resubscribe/snapshot path; do not also enable an independent Socket.IO
retry loop. A fixed interval is sufficient for the intended local applications.

Implemented API:

- `StoreClient(reconnect_interval=10.0)` enables automatic recovery, waiting
  ten seconds after a failed attempt before the next attempt.
- `None` disables it. `None` is the default to preserve existing
  lifecycle behavior, with applications explicitly opting in to ten seconds.
- Validate a supplied interval once at Client construction as positive and
  finite. Do not create a configuration class for this single setting.

The following behavior is verified once recovery is enabled:

- [x] After unexpected connection loss, finish obsolete connection cleanup
  and retry without overlapping attempts. Manual `connect()` shares lifecycle
  ownership with recovery rather than creating a competing attempt.
- [x] Explicit `disconnect()` disables pending recovery and awaits cleanup.
  A cancelled waiter cannot clear a newer connection's state.
- [x] Receive fresh snapshots for desired subscriptions before reporting
  synchronization. Preserve stable State objects and existing subscription
  failure isolation. A rejected subscription must not reconnect healthy peers.
- [x] Never queue offline edits or automatically replay interrupted mutations.
  Retain the existing unknown-outcome and explicit cached-result retry contract.
- [x] A recognized protocol incompatibility stops retries and reports clearly.
  Preserve that classification through transport error handling.
- [x] Initial-connect behavior: one bounded attempt that reports
  failure. Automatic indefinite startup waiting is not included implicitly.
- [x] Test repeated failures followed by success, Host replacement, cancellation,
  explicit stop and active subscriptions on both transports. Use controlled
  attempt/timer synchronization rather than real ten-second sleeps.

Reconnect uses a known endpoint. A Store listening on port zero can get a new
port after process restart. The application must either use a stable endpoint
or obtain the new endpoint from process readiness and reconnect to it through
the existing API. Automatic discovery and process supervision are not added.

## 6. Protocol policy

- [x] Document that applications should run matching library/application
  releases. Do not promise compatibility across different releases.
- [x] Preserve the existing single exact protocol-version handshake check.
  It rejects incompatible wire formats; it does not negotiate older behavior.
- [x] Keep mismatch tests for both transports and a clear diagnostic.
- [x] Do not add compatibility branches, fallback codecs, version registries
  or a second release-handshake system. Matching protocol numbers alone do
  not prove different application schemas or releases are compatible.

Store persistence versions and migrations serve a different purpose and keep
their existing contract. The protocol discussion does not remove recovery or
migration support for saved data.

## 7. Devtools usability and logging

These are requested usability changes, separate from the correctness defects.

### U01. One visible draft modal

- [x] Move the existing DraftEditor into one reusable modal for signals,
  edits, insertions, removals and copying retained caller actions.
- [x] Keep focus inside while open, restore it on close, support Escape/Close,
  and make large content scroll inside the modal. Avoid accidental backdrop
  dismissal of edits. Reuse the editor's current draft and guard state.
- [x] Keep submission outcomes visible in the modal or persistent result area;
  a failed/conflicting request must preserve the draft. Preserve explicit stale
  revision refresh, preview restrictions and unresolved-command handling.
- [x] Browser tests cover keyboard use, small viewports, long actions, failed
  submission and exactly one request per intended Apply.

### U02. Independent navigation and detail scrolling

- [x] Bound the application layout to the viewport. State schema navigation,
  History entries and Activity records scroll independently of their details.
- [x] Keep tabs, relevant filters and actions reachable while scrolling.
  Provide a usable narrow-screen layout without horizontal clipping.
- [x] Test many schemas, many records and long details: selecting a distant
  item must not require scrolling the whole page back to its heading.

### U03. Discoverable State commands and scope

- [x] Move Edit as new mutation, Insert row and Remove row near the selected
  schema heading, with the row selector and clear availability reasons.
- [x] Superseded by the current follow-up: select a schema to load it
  automatically. Remove the manual Capture selected/all controls.
- [x] Preserve scoped live data and preview read-only behavior. Selection
  loads the selected schema; valid drafts also load their referenced schemas.
- [x] Explain empty collections and outside-scope data without making the
  user search for a hidden command editor.

### U04. Explain history configuration where it matters

- [x] Make the existing disabled-history message prominent in the empty
  History view and show that both Store retention and schema selection matter.
  The current UI already has a small status line; this is a visibility fix.
- [x] Distinguish disabled history, enabled-but-empty history and a barrier,
  using the status fields the backend already returns.
- [x] Document `HistoryConfig()` plus `track_history = True`, including the
  standard Builder's default and configuring retention with Builder.history()
  before process startup. Keep devtools enablement independent of history enablement.
- [x] State that startup changes require a Store restart and old edits are
  not backfilled. Do not add runtime history toggles for this empty-state UI.

### U05. Quiet routine devtools HTTP access logs

- [x] Set `access_log=None` on the devtools `web.AppRunner` so ordinary asset,
  API and WebSocket requests do not fill application INFO logs.
- [x] Keep devtools error logging and the startup URL. Do not disable the
  application's logger globally or alter Socket.IO logging incidentally.
- [x] Verify successful requests remain functional and actual failures retain
  their existing diagnostic logging.

## 8. Naming, readability and proportional abstractions

Readable code is an acceptance criterion for every changed area.

- Name lifecycle state after its owner: for example `disconnect_task`,
  `reconnect_task` and `connection_generation`. Do not add all of these if an
  existing registration identity already provides the needed distinction.
- Use `_row_version` consistently for the requested private notification
  dependency. Do not conflate it with canonical schema or history revisions.
- Make raw versus encoded data apparent in names such as `commit` and
  `encoded_commit`. A conversion function should have one unambiguous direction.
- Prefer functions named for a concrete operation over generic `handle`,
  `process` or `data` names when those hide which phase owns the work.
- Extract a helper only when it removes actual duplication or names a coherent
  operation. Avoid pass-through wrappers, speculative base classes, callback
  registries and interfaces with a single artificial implementation.
- Reuse existing status, callback-isolation, codec and publication machinery.
  Do not scatter extra validation or devtools calls into Nodes and fields.
- Preserve public names and behavior unless this plan explicitly changes them.
  Restrict private renames and cleanup to code involved in these fixes.
- Keep comments for invariants and reasons: why stale cleanup must be ignored,
  why the mapping needs a notification version, or where persistence commits.
  Ordinary control flow should be clear from names and small functions.
- Keep Python and KV lines at most 79 characters; keep fitting expressions on
  one line. KV multiline expressions use supported continuations or parentheses
  with the opening, expression and closing lines aligned consistently.

A shared abstraction is justified by two real callers and common semantics,
not merely similar spelling. Small direct code is acceptable where ownership
differs. No reorganization of the entire package is planned.

## 9. Implementation order and coverage gates

| Phase | Work | Required evidence |
| --- | --- | --- |
| 1 | C01-C04, C16-C17 | Ordered State execution, atomic limits, lifecycle races and rollback |
| 2 | `_row_version` binding and reconnect | Real Kivy/KV notifications and both transport recovery paths |
| 3 | C05-C08 | History value policy, boot recovery, canonical IDs and navigation |
| 4 | C09-C15, C18 | Real HTTP/WS outcomes, tags, filters, delayed preview and multiple tabs |
| 5 | U01-U05, protocol/docs clarity | Real browser usability and clear configuration behavior |
| 6 | Integrated release verification | Full matrix, rebuilt assets and installed distributions |

Within each phase, reproduce the failure, make the smallest correction, and
run focused regressions. Update checkboxes only after implementation and its
acceptance evidence exist. Record naming/ownership adjustments with the fix
they support. Update PLAN.md, coverage documentation and release notes to match
actual progress rather than describing pending work as complete.

Mandatory regression coverage retains the full existing PLAN matrix, especially
the exact x/z/y example, signals and redo, grouping limits, history-only
publication, hosted ownership, persistence atomicity, membership under optimistic
overlays, and original-action-only copying.

- [x] Exercise Direct and real Socket.IO for shared lifecycle, subscription,
  State ordering, reconnect and navigation behavior.
- [x] Test browser behavior against the actual backend. Include multiple tabs,
  delayed responses, Store replacement and scoped captures.
- [x] Synchronize races with events/barriers and controlled response release.
  Inject persistence/parser failures deterministically. Avoid timing-only tests.
- [x] Keep history byte-budget tests independent of interpreter object sizes.
- [ ] Use real `kivyfn`, Kivy and `kivy-stubs`. Do not fake dependencies,
  suppress diagnostics or weaken tests to report a green result. Genuine
  kivyfn and Kivy are used; actual kivy-stubs remains unavailable.

Final release gates, rerun on the finished changes:

| Check | Required result |
| --- | --- |
| Full pytest | All regressions and existing suite pass; record counts and interpreter |
| Pyright with actual peer dependencies | Zero errors; report missing stubs as an open gate |
| Ruff lint and format check | Pass, with Python/KV line limits checked |
| Frontend dependency install from lockfile | Record command, source/cache availability and result |
| Frontend typecheck, lint and format | Pass |
| Frontend unit tests | Pass with reported counts |
| Real-backend browser tests | Pass on both application transports |
| Production assets and source/asset verification | Rebuild, stamp and verify matching packaged assets |
| Wheel and sdist | Build and independently install outside the checkout |
| Installed runtime without Node/npm | Serve assets, issue commands and exercise history |
| Supported platform/dependency jobs | Record actual runs and unavailable combinations honestly |

Use the existing release scripts and package configuration. Ship frontend
source, lockfile and compiled assets together. The delivery ZIP must
extract over the repository root without an enclosing directory and exclude
Git data, virtual environments, node_modules, caches and temporary build output.
List exact obsolete files in `OBSOLETE_FILES.md`, including superseded assets
if required; do not guess deletions before the final build.

## 10. Explicitly deferred or optional

- Initializer is included in the current follow-up, as a prevalidation
  snapshot transformation. General asynchronous startup hooks stay deferred.
- Cross-version compatibility, negotiation and automatic schema negotiation
  remain outside scope.
- Reconnect does not introduce endpoint discovery, a process supervisor,
  offline mutation queues, replay journals or automatic request replay.
- Persistent/migrated undo history, selective or branching undo and editing
  stored history remain deferred.
- Trigger reruns, dependency graphs and validation of current arbitrary
  application calculations during restoration are excluded.
- Signal payload mappers, cross-schema inverse references, targeted delivery,
  external acknowledgements and automatic side-effect reversal remain deferred.
- Generic exact-type equality changes, including `True` versus `1`, require a
  separate contract decision. Do not slip them into the NaN correction.
- Broad performance work, new storage backends and large package refactors
  require workload evidence and a separate scope decision.

Implementation preserves the existing architecture. Intentional API changes
are the keyed-collection wire contract and the centralized Builder/Store/Host/
process constructors. Exact final results and unavailable gates are recorded
in docs/release-verification.md.

## 11. Agreed Builder/initializer and browser follow-up

See PLAN.md Current follow-up patch for acceptance checkboxes and exact
ownership. Initializer is a class matching Migration.run(data), without a
version. Class examples use the name Initializer. Root is an owned Branch
definition; compiled Nodes belong to each Store. Configuration enters through
Builder and one StoreConfig. No transport-options framework is added.

The browser loads the selected schema automatically, distinguishes live
inspection from Activity recording, displays request arrival as received, and
uses a Tokyo Midnight inspired dark palette. Devtools defaults to port 3000.

Final follow-up evidence: 1,493 Python tests on each of 3.11 and 3.12,
18 frontend unit tests, 50 real-backend browser tests across Direct and
Socket.IO, zero Pyright errors, Ruff and frontend checks passed, and rebuilt
wheel/sdist independently installed and exercised. Actual kivy-stubs remains
unavailable. See docs/release-verification.md for exact results and limits.
