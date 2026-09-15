# History and undo/redo

History belongs to a Store instance and works with devtools disabled. Enable
retention explicitly and opt editable schemas into selection:

```python
from kivyfn.store.builder import Builder, HistoryConfig
from kivyfn.store.schema import ManySchema


class ComponentSchema(ManySchema):
    track_history = True


# tree contains the application schemas.
config = Builder().tree(tree).history(HistoryConfig()).build()
```

`HistoryConfig(max_entries=500, max_bytes=16 * 1024 * 1024)` sets fixed positive
limits. History retention is disabled by default (`config.history is None`). `track_history` defaults to
false, follows ordinary schema inheritance, and is independent of persistence.

Any effective operation or signal on a tracked schema selects its whole
successful transaction, including untracked caller actions, trigger changes,
and cascades. A true no-op assignment is skipped before trigger execution.
Selection occurs before first-before/final-after normalization: a tracked value
can return to its starting value while leaving a derived untracked effect.
Failed transactions and net-zero data-only transactions create no entry.

Enable retention on Builder before `build()`, `Store(config)`, or
`StoreProcess(processes, config)` startup. Storage, migrations, initialization,
hosting and history settings belong to that one configuration. A restart is
required after changing startup configuration. Earlier changes are not
backfilled. The History view distinguishes disabled retention,
enabled-but-empty history and barriers.

Loaded and initialized values form the startup baseline, without history
entries. Initialization runs before boot validation and does not execute
triggers, cascades or signals. Later tracked transactions record their full
normal effects, including changes to nonpersistent schemas. Initialization
adds no general history-suppression option.

Collections restore row IDs and values, not dictionary iteration positions.
There are no order records or whole-collection order guards. Removing and
restoring a row may change its iteration position, and independent rows remain
independent subject to declared relationships. Use an application field for
presentation ordering when needed. Instruction and signal ordering are unchanged.

## Navigation and conflicts

```python
await client.undo()
await client.redo()
response = await client.history()
```

The convenience methods fetch a current position then send one guarded command.
They never turn a stale request into another action automatically.
`client.history()` returns `HistoryResponse` (with `.page`) or `FailureResponse`,
following the existing Client reply convention. For a UI acting on displayed
history, pass the displayed position explicitly:

```python
from kivyfn.store.protocol.message.responses import HistoryResponse

if isinstance(response, HistoryResponse):
    await client.undo(expected_position=response.page.status.undo_position)
else:
    print(response.failure)
```

Client navigation raises `StoreStateError` while local mutations remain
unresolved, including requests whose waiters were cancelled. Close and await
open `StateTransaction` work before navigating history. Another client's undo
continues to synchronize through canonical events while local overlays are
pending.

Synchronous Store callers supply their operation identity and position:

```python
from uuid import uuid4

page = store.history()
result = store.undo(
    transaction_id=uuid4().hex, expected_position=page.status.undo_position
)
if not result.success:
    print(result.error)
```

`store.history(limit=100, offset=0)` returns a detached `HistoryPage` with
`status` and an ordered tuple of entries. Pages accept limits 1–1000. Status
includes the Store instance, history revision, cursor, readiness, current undo
and redo positions, barrier reason, entry count, and accounted retained bytes.
Positions are `HistoryPosition(store_instance_id, revision, entry_id)`.

A stale position never executes. Navigation targets the current head; it cannot
skip a conflicting entry. Value guards compare only restored fields for field
writes. Structural changes guard existence and full affected rows by ID.
Unrelated surviving row fields remain untouched. A value changed away and back
to the expected value may pass: these are value guards, not intent tracking.
Final uniqueness, foreign keys, incoming references under RESTRICT and CASCADE,
and signal target existence remain checked against the complete staged result.
No new unrecorded cascades run during restoration.

Undo/redo installs fresh canonical commits, increases affected schema revisions,
and moves the history cursor. It never applies optimistic inverse overlays.
Signals alone move history without increasing data revisions. A restoration
failure leaves data and history unchanged; unexpected persistence/install
failures retain the existing fail-closed Store policy. After a durable manifest
replacement, a later failure cannot be described as disk rollback.

Host/Client retain cached results for same-ID retries while that same live Host
still retains the outcome. Changed request contents with a reused ID are rejected.
Direct Store has no retry cache: repeating an already-applied position may return
stale without executing again. Reconnect to a live Store keeps history; Store
restart, recovery, and migration establish a new instance and empty history.

## Recorded trigger results

Undo/redo restores recorded effects, including previous trigger results.
It does not recompute those results against current untracked inputs.

For triggers maintaining `y = max(x, z)`, with tracked `x` and independent
untracked `z`, this is the required result:

| Action | x | z | y |
| --- | ---: | ---: | ---: |
| Initial | 0 | 0 | 0 |
| Set x | 1 | 0 | 1 |
| Independently set z; y write is unchanged and skipped | 1 | 1 | 1 |
| Undo | 0 | 1 | 0 |
| Redo | 1 | 1 | 1 |

The mismatch after undo is accepted. No triggers run during navigation and no
extra history entry is created. Conflict guards protect recorded effects and
Store constraints; they do not guarantee arbitrary application calculations
remain consistent with changed untracked inputs. Derive outputs from current
inputs when that behavior is required, or track independently editable dependency
inputs in the same ordered history. There is no requirement to track every
schema read by a trigger, dependency discovery, or extra calculation validation.

## Grouping

Use a fresh group token per gesture and preserve transaction boundaries:

```python
with manager.transaction(
    history_group=gesture_id, history_label="Move component"
):
    component.x = x
    component.y = y
```

Compatible commits with the same token and originating client form one undo
step. Entries retain a stable identity while their contents and history revision
change. Inspection returns detached snapshots, never editable stored history.
Anonymous synchronous Store calls do not group; supply `origin_client_id` to
identify the owner when calling Store directly.

Independent untracked updates may occur between commits. Their bounded write
footprints survive every merge. An overlap, another selected group/client,
navigation, disconnect, or barrier closes the segment. Reusing a token after
closure starts a new entry. A later failure leaves previous successful commits
undoable. Net-zero data-only groups disappear; repeated signals remain.

Groups are best effort within byte limits. An oversized merged group splits
between complete transactions. No transaction is partially retained. An
oversized transaction still commits normally and clears the reachable history
segment, recording a barrier. Later work can be undone back to that boundary.
Oldest complete entries are evicted at normal capacity; their effects become
part of the baseline. NaN, cyclic or opaque Python values accepted by a transient
Direct `DataField[Any]` cannot be safely compared and bounded as history values.
Such selected work still commits and creates an unsupported-history barrier;
it does not add new field validation or shut down Store. A later unrelated field
change remains usable even when the cyclic value is still present.

## Compensating signals

```python
from kivyfn.store.schema import SingleSchema
from kivyfn.store.schema.signal import Signal


class WindowCommands(SingleSchema):
    track_history = True
    opened = Signal[dict[str, str]](undo="closed")
    closed = Signal[dict[str, str]](undo="opened")
```

Inverse names resolve within the same schema at construction. Reciprocal
annotations are optional; self-inverse signals are supported. Known generic
payload contracts must match. Both directions retain detached original payloads;
no payload mapper or recursive inverse lookup is used.

Undo emits inverse occurrences in reverse order, after installing the entire
restored data state. Redo emits original occurrences in original order. Equal
occurrences are never deduplicated. A selected transaction containing any signal
without an applicable inverse creates a barrier; it cannot partly undo only its
data. Row signal targets must exist on both recorded sides. Insert-and-signal
therefore creates a barrier; an unsuitable group merge instead starts a new
individually supported segment.

Signals reach current subscribers using normal schema/query membership. Origin
client IDs are attribution, not recipient routing. Use stable application-owned
window/owner IDs in payloads and implement ownership filtering in handlers.
Disconnected consumers receive no replay. Store guarantees restoration and
publication, not atomic success of OS, UI, or hardware effects.

A compensation handler submitting new tracked work creates a separate history
transaction and can clear redo. For authoritative window lifecycle state, use
retained desired-state fields. There is no implicit causality tracking or history
suppression for compensation handlers.

## Thread and observer ownership

All mutations of a Store with an active Host execute on that Host's event loop.
A worker must schedule the complete operation on that loop, for example with
`asyncio.run_coroutine_threadsafe`; handing off only commit delivery is unsafe.
Standalone Store execution retains its synchronous locking behavior.

Commit and history-status observers run synchronously against installed data.
Both share one no-reentrant-mutation guard, including history-only notifications.
Schedule follow-up writes after delivery returns. Callback failures are isolated
and cannot roll back a committed transaction. Existing shutdown-from-callback
behavior stops remaining delivery.

Devtools uses the detached schema catalog, atomic checkpoint/commit observation,
and history-status observation; it does not own or implement undo. History
retention and diagnostic recording remain separate. Copy-as-new drafts require
original caller actions from Activity; expanded history effects are never
executable request drafts.
