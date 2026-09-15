# KivyFN Store tests

Run the complete project checks from the repository root:

```bash
uv sync --locked
uv run pytest
uv run pyright
uv run ruff check .
uv run ruff format --check .
```

The lockfile includes runtime dependencies and all development tools. Separate
sibling KivyFN or Kivy stub repositories are not required. Python 3.11 is the
minimum supported interpreter; CI runs the same checks on 3.11 and 3.12. The
minimum dependency job also uses Kivy 2.3.0, Socket.IO 5.11.0 and aiohttp 3.11.0.
The process runner accepts a supplied process manager through structural protocols.
Its tests verify that adapter contract and run the real Store/Socket.IO child
runner in a spawned Python process without an external process framework.

To inspect executed lines and branches:

```bash
uv run pytest --cov --cov-report=term-missing
```

Coverage is a guide to missing scenarios, not a claim that every combination is
correct. Each test should protect a meaningful invariant or public behavior.

## Test organization

| Directory | Contracts exercised |
| --- | --- |
| `tests/schema/`, `tests/error/` | Field ownership and validation, schema declarations, structured errors and malformed boundary input. |
| `tests/core/` | Compilation, staged execution, constraints, triggers, relationship policies, canonical transaction atomicity. |
| `tests/storage/`, `tests/boot/` | Complete generation persistence, corruption policies, migration and deterministic I/O fault recovery. |
| `tests/store/` | Store commit publication, schema revisions and listener isolation. |
| `tests/query/` | Expressions, filters, plans, dependencies, nullable/nested joins and projection metadata. |
| `tests/client/`, `tests/host/`, `tests/codec/` | Request correlation, subscriptions, synchronization and trust boundaries. |
| `tests/transport/` | Direct and Socket.IO endpoint and lifecycle behavior. |
| `tests/state/` | Canonical data and optimistic overlays, membership and transaction lifecycle. |
| `tests/integration/` | Complete State/Client/Host/Store paths, transport parity, reconnects and feature interactions. |
| `tests/process/` | Process manager adapter, readiness, bound endpoint and child cleanup. |
| `tests/compatibility/` | Retained legacy response-envelope import and decoding behavior. |
| `tests/schemas/` | Shared realistic schemas with triggers, foreign keys and retained state. |

## Isolation and ordering

`store_stack` and `state_stack` run each shared integration scenario once over
Direct transport and once over a real Socket.IO connection. Use the
`transport_kind` fixture when calling `running_state_stack()` or
`running_store_stack()` directly, including restart tests.

`state_stack` creates an unconnected `StoreClient()`. Register desired State
targets, then call `await stack.client.connect(stack.endpoint)`. The endpoint
belongs to the running Host until the Client first connects. Later `connect()`
calls may omit it. `intercept_client_requests()` installs request gates when
the real transport is created, so initial synchronization tests also exercise
deferred transport setup.

`tests/client/test_connection.py` covers construction before an event loop,
unused disconnects, missing and invalid endpoints, remembered reconnects,
transport reuse, failed attempts, disconnect during synchronization and
switching Store instances across Direct and Socket.IO connections.

Every integration test uses pytest's isolated temporary directory. Socket.IO
listeners bind port zero and expose the actual bound endpoint; Direct endpoint
names are unique per stack. Cleanup starts before boot/connect and always
attempts client, host and Store shutdown, including startup failures.

Ordering-sensitive tests use explicit events, intercepted delivery boundaries,
awaited operations or bounded state predicates. They do not depend on arbitrary
wall-clock sleeps. `wait_until()` yields one event-loop turn between predicate
checks and has a deadline. A global 30-second test timeout catches stuck tasks;
shorter per-operation deadlines improve failure diagnostics where appropriate.

Persistence failure tests inject faults at concrete write, replace or durability
boundaries and reopen the same path. They assert complete old/new generations
and preserved evidence according to the selected boot policy.
