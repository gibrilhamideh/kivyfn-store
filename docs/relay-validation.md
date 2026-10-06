# Python relay validation

Validated against the attached `kivyfn-store-review(3).zip` source. This is an
implementation of the Python relay phase, not certification of a complete
cloud deployment.

## Results

| Check | Result |
| --- | --- |
| Existing core tests before relay-specific tests | 1,474 passed |
| Core tests including new relay tests | 1,537 passed |
| New relay tests | 63 passed |
| Relay/config statement and branch coverage combined | 92% |
| Pyright on changed/new Python code and new relay tests | 0 errors, 0 warnings |
| Ruff lint and formatting on changed/new Python files | Passed |
| Whitespace/diff validation | Passed |
| Python wheel build | Passed |
| Wheel content, imports, and host/relay lifecycle smoke check | Passed |
| ZIP extraction at repository root and file hashes | Verified during packaging |

Tests use real Kivy, Store, asyncio, Socket.IO, and aiohttp. They exercise a
local Socket.IO gateway test peer over actual sockets, including WebSocket
upgrade and polling-only fallback, plus a real spawned StoreProcess. The core
regression run also used asyncio debug mode.

Test environment: Linux, Python 3.12.14, Kivy 2.3.1, python-socketio 5.17.0,
python-engineio 4.14.0, aiohttp 3.13.5, pytest 9.1.1, pytest-asyncio 1.4.0,
Pyright 1.1.414, and Ruff 0.16.9. The project dependency declarations and lockfile
were not changed.

The private sibling `kivyfn` package was not supplied and has no public package
available here. Its sole external import used by this repository is
`kivyfn.logging.get_logger`. Tests used a temporary module exposing
`logging.getLogger` under that name. That stand-in is **not shipped** and does
not replace the application's real dependency. Store, Kivy, codec, network,
transaction, and persistence implementations were not stubbed. Re-run tests
with the real sibling packages in your application environment.

## Covered behavior

- Per-client subscription ownership, collections, live/one-shot queries, and
  events originating from local application writes.
- Canonical event delivery before transaction response, ordered batches,
  no-op completion, named signals, and duplicate-transaction deduplication.
- Simultaneous guarded writes, structured validation rejection, and rejection
  of a batch attempting to use another channel's transaction namespace.
- Slow-client isolation, bounded incoming message/byte queues, bounded host
  event queues, receipt timeout, and terminal close fencing.
- Credential refresh, authentication failures, exact registration versions,
  nonblocking local startup, status waiters, shutdown cancellation, restart,
  polling transport cleanup, and process lifecycle integration.
- Disconnect after commit before response, no hidden mutation replay, stale
  generation rejection, subscription cleanup, exact-cache resume, fresh
  snapshots when revisions differ, and new Store instances after restart.
- JSON-only frames, finite-number validation, JavaScript-safe routing integer
  boundaries, malformed envelope rejection, and channel ID reuse protection.
- A checked-in wire transcript compared with actual transport output and
  round-tripped through the existing v3 codecs for future TypeScript tests.

## Existing optional devtools issue

The separate `packages/devtools/tests` suite cannot complete collection in
the supplied repository: `test_server.py` imports `Store`, `StoreClient`, and
`StoreHost` from `kivyfn.store`, but those top-level exports are absent.
The same collection failure was reproduced in an untouched extraction of the
original archive. This update uses the repository's concrete import paths
and does not change those public exports or the optional devtools package.
The 1,537 passing tests above refer to the core `tests` directory.

## Deployment checks still needed

The actual Node gateway, browser/Node TypeScript SDK, and React integration
are not implemented in this update. Test their authentication/access policy,
unit routing, ordering, reconnect handling, and UI state behavior against
`relay-protocol.md` before release. TLS/reverse-proxy deployment, real credential
services, multi-worker gateway ownership, application workload sizing, and
other operating systems/Python versions were not exercised here.

The Store remains the authority. No transport can infer success from a lost
reply, and this feature does not add a durable transaction-outcome ledger or
exactly-once signal execution across disconnections.

## Reproduce in the application checkout

```bash
uv sync --group dev
uv run pytest tests
uv run pytest tests/relay --cov=kivyfn.store.transport.relay \
  --cov=kivyfn.store.builder.relay --cov-report=term-missing
uv run pyright src/kivyfn/store/transport/relay \
  src/kivyfn/store/builder/relay.py tests/relay
uv run ruff check src/kivyfn/store/transport/relay \
  src/kivyfn/store/builder/relay.py tests/relay
```

Use the existing sibling `kivyfn` and `kivy-stubs` packages specified by the
project's uv configuration. Do not install a logging stand-in in production.
