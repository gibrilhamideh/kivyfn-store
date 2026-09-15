# Updating application startup

This patch intentionally replaces the old `(root, config)` Builder result and
separate endpoint/devtools constructor arguments. Update application callers
alongside the library; Store protocol 3 and diagnostic protocol 2 are unchanged.

Configure boot and hosting through Builder:

```python
# app/store/__init__.py
from kivyfn.store.builder import Builder, HistoryConfig, HostConfig


def create_builder() -> Builder:
    return (
        Builder()
        .tree(tree)
        .storage_path(storage_path)
        .initializer(Initializer())
        .history(HistoryConfig())
        .host(HostConfig(endpoint="http://127.0.0.1:0", devtools=True))
    )
```

Here `tree`, `storage_path`, and your application `Initializer` are existing
application declarations/imports. Omit the optional initializer until you have
written it. Omit history or hosting when those features are not needed.
Declare `track_history = True` on schemas whose edits should select transactions.
Devtools defaults to `http://127.0.0.1:3000`; custom settings use a
`DevToolsConfig` instance. Explicit `port=0` requests a dynamic browser port.

When using the managed StoreProcess entry directly, register the same target,
then pass only the completed configuration:

```python
# app/windows/primary/window.py: startup excerpt
config = create_builder().build()
process = self.processes.start(
    name="store-server", args=(config,), wait=True, timeout=10.0, daemon=False
)
```

Read `process.ready_payload["endpoint"]` for application Client.connect as
before. `ready_payload["devtools_url"]` contains the actual browser address.
The application endpoint can use a dynamic port independently of devtools.
The convenience runner is now `StoreProcess(processes, config)`.

For an in-process Store:

```python
# app/store/startup.py: startup excerpt
config = create_builder().build()
store = Store(config)
store.boot()
host = StoreHost(store)
await host.start()
```

`StoreHost(store)` reads `config.host`. An explicit `HostConfig` can be supplied
as the second argument when needed. A standalone `Store(config)` does not
start listeners or devtools. Each Store constructs fresh Nodes from the
configuration's Branch definition.

Your initializer class is named `Initializer`; import the library base under
an alias when defining it. It implements `run(data) -> data` and may use helper
methods. Define schema, migration and initializer classes at module scope with
serializable instance state for child processes. See
[configuration and initialization](architecture.md#configuration-and-startup)
for the complete example and recovery contract.

Initializer input is migrated raw data. Defaults, field/index and foreign-key
checks run afterward. The result is the initial state, with no trigger replay
or undo entry; ordinary later transactions retain the existing history rules.
