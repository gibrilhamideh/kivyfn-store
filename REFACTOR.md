Yes. We are at a good point to freeze the design before changing code.

The main goal is to make state the normal application-facing API, while the raw store remains the canonical engine underneath. gabtech-pro should almost stop thinking about adapters, snapshots, setrow(), manual refreshes, and manual synchronization.

1. Core architecture

The new structure should be:

                 gabtech-pro
                      │
                      ▼
             StateManager / State
                      │
            live observable objects
                      │
          ┌───────────┴───────────┐
          │                       │
 LocalStateManager        RemoteStateManager
          │                       │
          ▼                       ▼
      LocalStore          RemoteStoreClient
                                  │
                                  ▼
                               Server
                                  │
                                  ▼
                           Canonical Store

The canonical store remains responsible for:

schemas
validation
transactions
persistence
queries
joins
signals
actual-change detection
subscriptions

The state layer is how application/UI code interacts with those things.

2. Two state managers

We decided not to pretend local and remote execution are the same.

They share the same general state shape, but implementation semantics differ.

Local
state = LocalStateManager(store)

Local state is:

synchronous
canonical immediately
no optimistic overlay
no transport batching
no reconnect logic
Remote
state = RemoteStateManager(store)

Remote state is:

live
optimistic by default through normal Python assignment
transport-batched
continuously synchronized
reconnect/recovery aware

This keeps LocalStore simple instead of forcing asyncio into something that
doesn't need it.

3. Creating state

This is the central API:

settings = state.create(SettingsSchema)
fans = state.create(FanSchema)

The manager already knows from the registered schema/address whether it is
many=False or many=True.

We don't want:

settings.data.target

We want:

settings.target
settings.enabled
settings.mode

For many=True:

fan = fans[fan_id]

fan.enabled
fan.capacity
fan.output

We can additionally expose:

fans.rows
fans.ids
fans.items

but normal lookup should simply be:

fans[fan_id]
4. state.create() always means live state

This is important.

fans = state.create(FanSchema)

does not mean:

fetch Fans once.

It means:

give me a live representation of Fans and keep it synchronized.

Therefore screens should stop doing:

snapshot
refresh
reload
rebuild because the adapter changed

Normal store changes update the existing state objects automatically.

For local state, create() is ready immediately.

For remote state, it returns immediately and establishes the remote watch.
It can expose:

fans.loaded
fans.synced

If async code ever needs initialization confirmation:

await fans.wait_ready()

But normal Kivy screens should not need to wait manually.

5. Stable Kivy objects

We retain one of the strongest ideas from the existing implementation:

Do not replace row objects when their data changes.

If:

fan = fans[fan_id]

then incoming changes modify that same EventDispatcher.

fan.output = canonical_value

internally, rather than replacing fan.

This keeps KV bindings stable.

Removed rows can retain references similarly to the current weak-reference
behavior.

6. We no longer need custom/Alias Kivy properties

We briefly considered intercepting writes with AliasProperty, but the API we
ended on removes the need.

Normal Kivy properties remain normal:

enabled = BooleanProperty(False)
capacity = NumericProperty(0)

The API itself tells us whether something is optimistic or confirmed.

That significantly simplifies implementation.

7. Remote optimistic field updates

Normal property assignment means optimistic update:

fan.enabled = True

The property changes immediately.

Internally:

visible False → True
        │
        ▼
queue remote update
        │
        ▼
server

Success:

canonical → True
visible remains True
on_success

Definitive rejection:

remove optimistic change
recalculate/revert visible state
on_error

The rollback happens before on_error is dispatched.

The screen never performs rollback itself.

8. Confirmed field updates

When code explicitly wants to wait before exposing the value:

result = await fan.set(
    enabled=True,
)

set() is the confirmed version.

Meaning:

visible remains old canonical value
        │
send remote request
        │
server accepts
        ▼
visible changes

If rejected:

visible never changes
on_error
result reports failure

This removes the need for:

optimistic=False

or:

with state.confirmed():

The API itself communicates intent:

fan.enabled = True

= optimistic.

await fan.set(enabled=True)

= confirmed.

That is one of the cleanest decisions we reached.

9. Normal store errors do not crash the Kivy application

Both optimistic and confirmed operations go through state lifecycle events:

fan.bind(
    on_success=self._on_success,
    on_error=self._on_error,
)

A store validation failure should not randomly propagate through a Kivy
property callback and kill the application.

For optimistic assignment:

fan.enabled = True

failure is event-driven.

For an explicit awaited operation, we can return a result instead of forcing
ordinary rejection through exceptions.

Conceptually:

result = await fan.set(
    enabled=True,
)

if not result.ok:
    ...

But regardless, the normal state event still exists.

We can finalize exact result-class naming during implementation.

10. One universal operation/result description

Success and error events should receive a structured operation rather than
different callback signatures for single/many/transactions.

Conceptually it contains:

operation type
row ID, if applicable
field/fields
previous values
requested values
code/reason on rejection

So:

operation.type
operation.row_id
operation.field
operation.fields
operation.previous
operation.requested
operation.reason

For a single schema:

operation.row_id is None

For a many=True row:

operation.row_id == fan_id

Same API.

11. on_success and on_error

We settled on:

on_success
on_error

rather than on_update.

on_update would be confusing because state itself receives canonical updates
all the time.

on_success specifically means:

an outgoing logical state operation was accepted.

on_error means:

an outgoing logical operation was definitively rejected, and state recovery
has already been performed.

These should work for:

update
insert
remove
transaction

Signals have their own named events too.

12. Many-state insert API

Normal Python mapping syntax means optimistic insertion:

fans[fan_id] = {
    "name": "Tunnel Fan",
    "capacity": 18_000,
}

The row appears immediately.

If accepted:

keep it
on_success

If rejected:

remove optimistic row
on_error

I would make __setitem__ mean insert, not upsert.

If the ID already exists, updating should be done through:

fans[fan_id].enabled = True

That prevents ambiguous replacement semantics.

13. Confirmed insert

Explicit method:

result = await fans.insert(
    fan_id,
    name="Tunnel Fan",
    capacity=18_000,
)

The row does not enter live visible state until accepted.

Same conceptual distinction:

fans[id] = {...}

= optimistic.

await fans.insert(...)

= confirmed.

14. Many-state remove API

Optimistic:

del fans[fan_id]

The row disappears immediately.

If rejected, the same stable row is restored before on_error.

Confirmed:

result = await fans.remove(fan_id)

The row remains until the server confirms removal.

Again, very symmetrical:

Python mutation syntax
    → optimistic

explicit awaited method
    → confirmed
15. Transactions can contain everything

This was another important decision.

We should never require:

transaction.setrow(...)
transaction.insert(...)
transaction.remove(...)

from state-facing code.

Instead:

with state.transaction():
    fan.enabled = True

    fans[new_id] = {
        "name": "Fan 2",
    }

    del fans[old_id]

Everything performed inside the context gets grouped into one canonical
transaction.

The store layer translates the normal state operations into underlying
transaction operations.

16. Transaction semantics remain independent from transport batching

Very important:

transaction
    = atomic correctness

transport batch
    = network efficiency

Remote operations from unrelated code can travel together in one network
request without becoming one transaction.

For example:

WriteBatch
├── transaction A
├── independent update B
├── independent update C
└── transaction D

Each has independent success/failure semantics.

17. Remote transport batching

Only remote needs this.

Remote state operations occurring in the same async scheduling turn can be
micro-batched.

We don't use Kivy Clock for this.

We use asyncio/client transport scheduling.

Conceptually:

update
update
query/watch request
remove
        │
        ▼
Remote request coordinator
        │
        ▼
few network envelopes

Likely categories:

ReadBatch
WriteBatch
WatchBatch

LocalStateManager does not need this optimization.

18. Kivy Clock has one job only

Clock is not part of transactions or transport batching.

Its only responsibility is when a remote/background callback must update Kivy
objects on the UI thread:

remote event
    ↓
Kivy Clock
    ↓
EventDispatcher properties

For LocalStore operating directly on the Kivy thread, that extra hop isn't
required.

19. Signals become state events

If the schema contains:

tare = Signal()

then:

scale = state.create(ScaleSchema)

gets:

scale.bind(
    on_tare=self._on_tare,
)

Incoming canonical signal:

store tare
    ↓
state
    ↓
dispatch on_tare

For sending:

scale.tare()

This replaces raw:

store.signal.emit(...)

and string-based signal names throughout Gabtech.

20. Signals do not need optimistic/confirmed modes

A signal is an occurrence, not retained state.

There is no value to roll back.

So:

scale.tare()

is enough.

It can optionally return an operation handle if some code ever wants:

await scale.tare()

but there is no separate tare_confirmed() or optimistic setting.

Signals must never be coalesced.

Two:

scale.tare()
scale.tare()

mean two events.

21. Query creation uses the same API

Filtered live state:

active_fans = state.create(
    Query(FanSchema).where(
        field("enabled") == True,
    )
)

This is still a collection of Fan rows:

active_fans[fan_id]

A query can return:

0 rows
1 row
100 rows

Filtering changes membership, not the object shape.

If a fan begins matching, it appears automatically.

If it stops matching, it disappears automatically.

22. Query joins

We simplified the proposed join API.

Instead of awkward:

.join(
    HouseSchema,
    from_="component",
    as_="house",
)

we want relationship/path-based joins:

query = (
    Query(FanSchema)
    .join("component")
    .join("component.house")
)

Foreign-key metadata tells the query engine what schema each relationship
targets.

Then:

fans = state.create(query)

and:

fan = fans[fan_id]

fan.enabled
fan.component.name
fan.component.house.name

No manual from_ or as_.

23. Explicit joins only

Plain:

state.create(FanSchema)

does not recursively load foreign keys.

It might expose:

fan.component_id

Only:

Query(FanSchema).join("component")

adds:

fan.component

That prevents runaway object graphs and unnecessary queries.

24. Join depth

Joins may be nested as deeply as explicitly requested:

.join("component")
.join("component.house")
.join("component.house.farm")

giving:

fan.component.house.farm.name

No arbitrary fixed join-depth limit is necessary initially.

But cycles must be prevented.

Version one should focus on singular foreign-key joins, not nested reverse
collections.

25. Joined fields remain writable

A joined field knows its canonical origin.

So:

fan.component.name = "Tunnel Fan"

resolves back to:

ComponentSchema
component_id
name

and performs a normal state mutation.

The query itself is only a projection.

Computed query values, if we add them later, are read-only.

26. Query membership is server/store-owned

If:

active_fans = state.create(
    Query(FanSchema).where(
        field("enabled") == True,
    )
)

and:

fans["b"].enabled = True

then the query engine determines that B now belongs.

The frontend receives an insert into that query state.

It does not locally rerun the query or rebuild registry indexes.

This is a major part of how we remove Gabtech's manual registry/rebuild code.

27. Live watch synchronization

We decided the simplest robust mechanism is:

Per-watch sequence + authoritative snapshot recovery.

Not global revision-based adapter audits.

Every direct/query watch gets:

watch_id
sequence

Initial creation:

snapshot
watch
sequence = 0
synced = True

Relevant events:

1
2
3
4

Client checks continuity.

If it expects:

4

and receives:

5

then:

synced = False
fresh authoritative snapshot
reconcile
new watch sequence
synced = True
28. Reconnect behavior

We aren't going to overcomplicate version one with replay logs or resume
tokens.

Disconnect:

state.synced = False

Reconnect:

fresh snapshot + new watch
        ↓
reconcile stable state objects
        ↓
synced = True

Later, if bandwidth actually matters, we can add opaque resume tokens/replay
without changing the public state API.

29. Global revision can leave the new frontend stale-recovery system

The current global revision mismatch mechanism doesn't need to drive state
synchronization anymore.

Canonical revision can remain internally if useful for:

transactions
debugging
persistence
store snapshots

but live-state correctness uses:

watch sequence
+
authoritative reset

That's cleaner, especially for queries and joins where unrelated global
revisions are meaningless.

30. Canonical store suppresses no-op state writes

The store itself should own this rule:

incoming normalized value == canonical value
    ↓
nothing

Meaning:

no store event
no watch sequence increment
no persistence
no query reevaluation
no remote traffic

So no producer needs to keep its own previous-value cache just to avoid
publishing unchanged state.

31. Runtime/automation structure changes

We discussed reconcile(), but your newer idea is better for Gabtech's
runtime schemas.

Runtime row existence should be structural.

Example:

Fan component created
    ↓
FanRuntime row created

Fan component exists
    ↓
FanRuntime always exists

Fan component removed
    ↓
FanRuntime row removed

Then automation does not repeatedly rebuild/reconcile the runtime collection.

It only updates fields on existing runtime rows.

32. Runtime metadata

A runtime row can represent that the component exists even before automation
has meaningful calculated state.

For example:

initialized
available
valid
active

depending on domain meaning.

I would avoid using pending for this because pending already naturally
means an unconfirmed remote mutation.

This gives the frontend a meaningful distinction between:

component doesn't exist

and:

component exists but runtime isn't initialized/available
33. Runtime lifecycle trigger

If the invariant truly is:

Every configured Fan must have exactly one FanRuntime.

then I'd put creation/removal into a store lifecycle trigger rather than
leaving it to automation.

Conceptually:

FanSchema insert
    ↓
FanRuntimeSchema insert

FanSchema remove
    ↓
FanRuntimeSchema remove

Same for other structurally paired runtime schemas.

Then automation can safely assume rows already exist.

34. Automation no longer keeps published shadow state

This should let us remove:

_published_single
_published_many

and most of the current manual:

snapshot
diff
insert/remove/update calculation

Automation can simply assign/update runtime fields.

The canonical store suppresses same-value mutations.

This is significantly simpler.

35. reconcile() can still remain a generic store feature

We don't need to delete the idea.

It's still useful for:

imports
snapshot restoration
external mirroring
tests
bulk replacement/recovery tooling

But we don't need to structure Gabtech runtime publishing around it.

36. Gabtech adapter layer largely disappears

Normal frontend code should no longer have to do:

adapter.load()
adapter.data
adapter.bind(on_setrow=...)
adapter.setrow(...)
adapter.insert(...)
adapter.remove(...)
_refresh()
_rebuild()

Instead:

fans = state.create(FanSchema)

and the widget works directly against the live state.

The current adapter classes can survive temporarily as compatibility wrappers
during migration, then be removed.

Since you are comfortable doing a full development-branch refactor, we can also
choose to remove them more aggressively instead of maintaining long
compatibility if that makes the architecture cleaner.

37. Gabtech registry layer gets split

Registry code that exists mainly to:

join adapters
filter records
index IDs
rebuild on adapter changes

should migrate to Store Query + live QueryState.

Registry methods containing actual business logic remain.

For example:

duration_is_valid()
is_first()
next_onset()
can_append_program()

are domain behavior, not data synchronization, and should stay.

38. Public remote API we're targeting

The essence is:

state = RemoteStateManager(store)

settings = state.create(SettingsSchema)
fans = state.create(FanSchema)

active_fans = state.create(
    Query(FanSchema).where(
        field("enabled") == True,
    )
)

fans_with_components = state.create(
    Query(FanSchema)
    .join("component")
    .where(
        field("component.enabled") == True,
    )
)

Optimistic:

fan.enabled = True

fans[new_id] = {
    "name": "Fan 2",
}

del fans[old_id]

Confirmed:

await fan.set(
    enabled=True,
)

await fans.insert(
    new_id,
    name="Fan 2",
)

await fans.remove(old_id)

Transaction:

with state.transaction():
    fan.enabled = True

    fans[new_id] = {
        "name": "Fan 2",
    }

    del fans[old_id]

Signals:

scale.bind(
    on_tare=self._on_tare,
)

scale.tare()

Errors:

fan.bind(
    on_success=self._on_success,
    on_error=self._on_error,
)

That is roughly the whole frontend store API.

What I would implement first

Given that both repositories are on safe development branches and you're
comfortable with a substantial refactor, I would implement in this order:

Refactor kivyfn-store core semantics: actual-value no-op suppression,
normalized canonical change handling, structural lifecycle hooks, and clean
signal semantics.
Build the new remote watch protocol: atomic initial snapshot/watch,
watch_id, per-watch sequence, resnapshot recovery and synced.
Build RemoteStateManager: create(), single/many live state, stable
row identity, Kivy thread dispatch.
Add optimistic property/collection mutation: normal assignment,
__setitem__, __delitem__, rollback and on_success/on_error.
Add confirmed methods: set(), insert(), remove() with result
objects.
Add remote transport batching: read/write/watch micro-batching while
preserving transaction boundaries.
Add state.transaction(): capture all normal state mutations and turn
them into one canonical transaction.
Expose schema signals as state events/methods.
Implement live filtered QueryState.
Implement path-based joins and nested joined state.
Implement LocalStateManager separately and simply, using the same
state shape but direct synchronous store behavior.
Refactor Gabtech runtime lifecycle, removing runtime structural
publishing and published-state diff caches.
Migrate Gabtech frontend screens/controllers to state.create().
Replace data-rebuilding registries with queries/joins where appropriate.
Remove the old adapter/state synchronization layer once migration is
complete.

The biggest thing I would lock before implementation is that we should not
try to preserve the old adapter architecture internally just to make migration
easier. Since this is a development-branch total refactor, the new State API
should be designed correctly first, and then gabtech-pro should be migrated
to it. That will give us a much cleaner result than layering the new semantics
on top of the old ones.
