# TypeScript client validation

Validated against the supplied repository plus the earlier Python relay
overlay, using the existing Store v3 and relay v1 protocol. This client overlay
does not alter Python runtime behavior.

## Results

| Check | Result |
| --- | --- |
| TypeScript client suite | 50 tests passed across four test files. |
| Existing Python core and relay suite | 1,537 tests passed. |
| TypeScript checking | Passed, including tests and peer declarations. |
| Client formatting | Prettier check passed. |
| Python interoperability fixture | Ruff lint and format checks passed; 79-column limit. |
| npm tarball, isolated Node consumer | Imported and synchronized without React installed. |
| Installed TypeScript declarations | Valid positive and negative consumer type checks passed. |
| npm tarball, isolated React consumer | Vite production browser bundle built successfully. |

The client tests exercise actual Python Socket.IO hosts and the outward Python
relay through a Node Socket.IO test gateway. They also check the shared Python
golden fixture. Covered behavior includes:

- snapshots, cached revisions, fresh store instances and structured failures;
- optimistic overlays, concurrent writes, no-op completion and revision guards;
- transaction-wide publication before signals and transaction completion;
- collections, flattened joined queries, history and stale undo positions;
- two concurrent user channels, isolated transaction namespaces and cleanup;
- held gateway responses and loss of a response after a successful commit;
- authentication rejection, refreshed credentials and physical relay reconnect;
- malformed batches, revision gaps, JSON limits and bounded event/request queues;
- React observer updates, initially absent rows, StrictMode, shared subscriptions
  and switching managers for a newly selected store.

## Environment

Python 3.12.14, Kivy 2.3.1, python-socketio 5.17.0, python-engineio 4.14.0,
aiohttp 3.13.5, pytest 9.1.1; Node 24.19.0, npm 11.9.0, TypeScript 7.0.2,
MobX 7.0.5, mobx-react-lite 5.1.0, React 19.3.0, Socket.IO 4.8.4 and
Vitest 5.0.2. The root npm lockfile records the complete JavaScript dependency
set. The package accepts Node 22.12+; that lower runtime boundary was not
separately exercised.

The attached repository's private sibling `kivyfn` dependency was unavailable
in this environment. Python validation used a test-only replacement for its
sole imported function, `kivyfn.logging.get_logger`, delegating to Python's
standard logger. The real Kivy/store/Socket.IO code was used. That replacement
is **not included in this overlay**. Run the tests with your normal sibling
dependency before releasing the package.

The optional devtools package's pre-existing collection error from top-level
Python imports is outside this change; the passing Python command targets
`tests`, including the relay suite. No claim is made that the unrelated
devtools test suite was repaired.

## Reproduce

From the repository root:

```bash
npm ci
npm run check:client
npm run build:client
PYTHON="$(pwd)/.venv/bin/python" npm run test:client
npm run test:package --workspace=@kivyfn/store
uv run pytest -q tests
```

`test:package` packs into a temporary directory, installs the actual tarball
in an isolated consumer, verifies Node without React, checks installed types,
and builds a React/Vite browser bundle. It removes its temporary files and
does not publish to npm.

## Scope and remaining work

The gateway fixture supplies test credentials and deterministic routing;
production authentication, permission checks, distributed routing, queue
budgets and deployment remain application integration work. The exported
policy interfaces and relay contract define that boundary.

React was exercised in jsdom and as a Vite bundle. A deployed gateway with a
real browser, mobile browsers, long-duration load, and multi-worker routing
were not exercised here. Production deployment should verify those actual
environments, including CORS/cookies and credential renewal.

Automatic schema discovery remains a TODO. Typed references currently supply
schema names and compile-time hints, with Python validation authoritative.
Live query views are read-only; modify source collections. Store v3 / relay v1
are checked directly, without a compatibility/version negotiation matrix.
