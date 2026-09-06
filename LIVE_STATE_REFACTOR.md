# KivyFN Store Live-State Refactor

This archive is a root overlay for `kivyfn-store`.

## Apply it

From the repository root:

```bash
unzip kivyfn-store-live-state-refactor.zip -d .
bash cleanup_live_state_refactor.sh
```

The cleanup script removes the obsolete adapter package, the replaced state
modules, Python caches, test caches, and generated `egg-info` metadata.

## Public API

```python
from kivyfn.store import Query, field
from kivyfn.store.state import LocalStateManager, RemoteStateManager

state = RemoteStateManager(store)
settings = state.create(SettingsSchema)
fans = state.create(FanSchema)

active_fans = state.create(
    Query(FanSchema).where(
        field("enabled") == True,
    )
)
```

Schema fields are normal live Kivy properties:

```python
fan = fans[fan_id]
fan.enabled = True
```

Normal assignment is immediate. Remote assignment is optimistic and rolls
back before `on_failed` if the canonical store rejects it.

Confirmed operations start immediately and return an awaitable operation:

```python
operation = fan.set(enabled=True)
result = await fan.set(enabled=True)

fans[fan_id] = values
result = await fans.insert(fan_id, **values)

del fans[fan_id]
result = await fans.remove(fan_id)
```

Every state exposes the same two operation events:

```python
fan.bind(
    on_success=self._on_success,
    on_failed=self._on_failed,
)
```

`pending` and `synced` are optional informational Kivy properties. Normal
application synchronization happens through the schema properties themselves.

Schema signals become meaningful methods and Kivy events:

```python
scale.bind(on_tare=self._on_tare)

scale.tare()
result = await scale.tare()
```

Calling a signal starts it immediately. Awaiting the returned operation is
optional. Signal occurrences are never coalesced.

Transactions capture normal state syntax:

```python
with state.transaction() as transaction:
    fans[first_id].enabled = True
    fans[second_id].enabled = False
    scales[scale_id].tare()

result = await transaction
```

Query joins use schema relationships:

```python
fans = state.create(
    Query(FanSchema)
    .join("component")
    .join("component.house")
)

house_name = fans[fan_id].component.house.name
```

Joined entities share manager-wide identity and remain writable.

## Reconnection

Remote query subscriptions own synchronization. A disconnect marks live views
unsynchronized but preserves their current Kivy objects and values. On
reconnection, each query receives an authoritative snapshot and reconciles the
same objects. Screens do not refresh, resubscribe, or rebuild state manually.

New mutations attempted while the remote transport is disconnected or the
client is recovering fail through `on_failed`; occurrence signals are not
queued for delayed equipment action.

## Breaking removals

The following application-facing layers are removed:

- `kivyfn.store.adapter`
- `ManyNodeState`
- `SingleNodeState`
- `NodeState`
- `StateChange`

Migrate application code to `LocalStateManager` or `RemoteStateManager` and
`state.create(...)`.

## Validation performed

The overlay was checked for:

- Python syntax across `src`;
- 79-character Python line limits;
- whitespace errors with `git diff --check`;
- local direct and collection state;
- optimistic and confirmed updates;
- no-op suppression;
- optimistic rollback;
- filtered query membership;
- nested relationship joins;
- stable entity identity;
- local and remote signals;
- atomic state transactions;
- remote disconnect, reconnect, and authoritative resnapshot.

The supplied source archive did not include the project test suite or Kivy
runtime. Executable validation therefore used lightweight Kivy-compatible
test doubles while exercising the real local store, remote client/server,
query, signal, transaction, and state implementations.
