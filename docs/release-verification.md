# Release implementation and verification

The release, OPTOMIZE.md corrections and Builder/initializer/browser follow-up
are implemented. Verification uses actual Kivy and the genuine supplied kivyfn
source. Actual kivy-stubs remains unavailable, so that typing gate remains open.

## Changes in this patch

- Builder.build() returns one StoreConfig with a detached Branch definition,
  boot settings, migrations, Initializer, history and optional HostConfig.
  Each Store constructs fresh Nodes, indexes, revisions and history. Store(config),
  StoreHost(store) and StoreProcess(manager, config) use that definition.
- Initializer.run(data) transforms detached migrated raw data before defaults,
  field/unique checks and final foreign-key validation. It supports single,
  collection and nonpersistent schemas. Persistence remains persistent-only.
  Initialization establishes the startup baseline without triggers, signals,
  transactions or history entries. Later edits keep ordinary history behavior.
- Initializer exceptions and invalid top-level return values fail startup
  outside ERASE. Invalid schema data follows existing recovery policies. An
  ERASE fallback initializes empty data once, with no retry loop. Raw saved data
  stays detached so initializer edits cannot conceal required persistence writes.
- HostConfig owns endpoint and devtools settings. Devtools=True and
  DevToolsConfig() bind http://127.0.0.1:3000. Explicit port=0 remains dynamic;
  an occupied configured port fails and cleans up. Client reconnect and parent
  process lifecycle settings retain their existing owners.
- State selection automatically loads live schema data. Valid multi-schema
  drafts also load referenced schemas, with explicit revision-guard refresh.
  Manual Capture controls are removed. Activity recording remains independent
  of live State, application subscriptions and Store history.
- Activity request records say received and show matching retained responses.
  Missing responses say unavailable; they do not imply work is still pending.
  The browser uses a Tokyo Midnight inspired navy, blue, purple and cyan theme,
  preserving modal editing, independent scrolling and preview restrictions.

This intentionally changes application construction APIs. See
[updating application startup](upgrading-builder.md) before launching existing
applications. The application and diagnostic protocol versions are unchanged.

Follow-up regression coverage adds 57 Python, two frontend unit and 12 browser
cases. Additional bugs found while implementing the automatic-loading workflow:
scope snapshots previously replaced retained Activity with their short initial
page, and selecting another live schema could allow an older delayed preview
to replace it. Both now have real-backend browser coverage. The existing delayed
commit test now delays the remaining ordered WebSocket stream, preserving actual
transport ordering instead of manufacturing a sequence gap.

## Earlier corrections retained

- Collections are keyed by row ID. Removed HistoryOrder, history order guards,
  commit/checkpoint row_orders, subscription row_order, and order-only State
  notifications. Undo/redo restores recorded values and row identities, without
  rebuilding dictionary positions. Instruction, signal, history-transaction and
  diagnostic sequence ordering retain their existing meaning.
- CollectionState and QueryCollectionState expose uncached rows AliasProperties
  bound to private _row_version. The existing publication phase advances it for
  visible membership changes after reconciliation; ordinary field edits notify
  the row. Stable readonly mappings and surviving State objects remain intact.
- Store history, hosted loop ownership, publication isolation, grouping,
  preview, signals and persistence keep their existing owners. Devtools remains
  optional and separate from core State and undo retention.
- Client cleanup has one shared owner. Direct callbacks belong to a connection
  generation. Optional StoreClient(reconnect_interval=10.0) recovers established
  connections on both transports; the default None preserves explicit lifecycle
  behavior. Initial connect is bounded, explicit disconnect stops recovery,
  fresh subscriptions resynchronize, and interrupted mutations are never replayed.
- Application protocol 3 and diagnostic protocol 2 use exact handshakes. Upgrade
  Host, Clients and compiled browser assets together. No compatibility branches,
  negotiated behavior or second Store result cache were added.
- Browser drafts use one modal with keyboard focus handling and internal scroll.
  State, History and Activity have independent navigation/detail scrolling.
  Selected-schema commands and automatic loading are reachable. Disabled, empty and
  barrier history states are explained. Routine devtools access logging is quiet;
  errors remain logged. The production assets are included.

The accepted contract is unchanged: **undo/redo restores recorded effects,
including previous trigger results. It does not recompute those results against
current untracked inputs.** The x/z/y acceptance case passes exactly, including
undo producing (0, 1, 0). No new calculation consistency guard was introduced.
Compensation handlers submitting tracked work create separate transactions and
can clear redo. Grouped entries retain stable identities and revisioned contents;
inspection is detached, and copying requires original unambiguous Activity actions.

## Earlier correctness fixes

All 18 supplied review findings have targeted fixes and regression coverage:

- Preserve State writes/inserts that intervening trigger actions must observe.
- Serialize old Client cleanup, reject obsolete Direct callbacks, and reject
  wrong-loop Host lifecycle calls before teardown effects.
- Normalize actual row/field effects without traversing unrelated cyclic fields.
  NaN, including nested values, uses the existing unsupported-history barrier;
  otherwise valid canonical mutations still commit. Ordinary equality is unchanged.
- Route excessive JSON nesting/integer digits through boot recovery. Reject
  empty row IDs at canonical construction. Keep filesystem failures distinct.
- Report postexecution diagnostic response overflow as bounded HTTP 502 with
  outcome unknown. Same-ID retries remain owned by the live Host result cache.
- Keep schema/row maps structural, encode field payloads and Activity values
  once, and preserve reserved-tag IDs, prototype-like IDs and large integers.
- Align display/capture text matching, include signal schemas, synchronize shared
  recording controls across tabs, and discard obsolete preview responses.
- Reject Windows-style archive traversal before any asset writes. Roll back
  registrations from failed custom State containers. Count real compiler
  operations after no-op and cascade-boundary handling.

Implementation also found and fixed initial connect returning success after the
transport dropped between synchronization and its return. A gated regression
covers both transports. The earlier history-byte fixture correction remains
independent of interpreter object sizes. Native-dialog keyboard handling keeps
focus inside the draft, including Tab at the first/last control.

## Exact final verification

Checks ran against a fresh repository copy with the actual pinned dependencies.
No failing tests were skipped, diagnostics suppressed or fake dependencies used.
The npm lockfile was installed from the local cache, then assets were rebuilt.

| Check | Exact result |
| --- | --- |
| npm ci --offline --no-audit --no-fund | Exit 0 |
| npm run build | Exit 0; Vite production assets rebuilt |
| scripts/build_release.py --stamp-assets / --check-assets | Both exit 0; source fingerprint and hashes match |
| python -m pytest -q, Python 3.11.16 | **1,493 passed**, zero skipped/failures/errors, exit 0; 10.42 seconds command wall time |
| python -m pytest -q, Python 3.12.14 | **1,493 passed**, zero skipped/failures/errors, exit 0; 10.53 seconds command wall time |
| python -m pyright --pythonpath verification-python | **0 errors, 0 warnings, 0 informations**, exit 0; actual kivy-stubs unavailable |
| python -m ruff check . | **All checks passed**, exit 0 |
| python -m ruff format --check . | **254 files already formatted**, exit 0 |
| npm run typecheck | Exit 0 |
| npm run lint | Exit 0 |
| npm run format:check | Exit 0 |
| npm run test | **18 passed in 2 files**, exit 0 |
| npm run test:browser | **50 passed in 57.7 seconds**, exit 0; 25 each on Direct and Socket.IO |
| python -m build --no-isolation --outdir distributions | Wheel and sdist built, exit 0 |
| python scripts/check_install.py distributions | Both independently installed and passed, exit 0 |

Pytest JUnit results confirm 1,493 successful cases on each interpreter.
The reported Python timings are process wall time, including startup/teardown.
Tests include real Kivy/KV bindings and actual Socket.IO, deterministic lifecycle
and response gates, injected persistence/parser failures and the accepted x/z/y
restoration case. Browser tests use Chromium against actual backend processes,
including multiple tabs, restart, delayed previews, postcommit HTTP ambiguity,
rapid schema selection, request/response recording and multi-schema drafts.

Installed checks run outside the checkout with Node/npm absent from PATH. Each
wheel/sdist installation serves its packaged HTML/JS/CSS and exercises both
transports, normal edits, undo/redo, browser HTTP commands and diagnostic WebSocket
events. They also verify configured initialization and its empty initial history.
Compiled assets are index-Bh_b9i04.js and index-Cj0mz7BF.css.

Runtime: Python **3.11.16 / 3.12.14**, Kivy **2.3.1**, python-socketio **5.16.3**,
aiohttp **3.14.3**. Tools: pytest **9.1.1**, Pyright **1.1.414**, Ruff **0.16.7**,
setuptools **84.0.0**, wheel **0.48.0**, pip **24.0**, build **1.6.1**, Node
**24.19.0**, npm **11.9.0**, Playwright **1.55.0**. The genuine supplied kivyfn
source provides kivyfn.logging. No fabricated peer distribution was installed.

## Deliberate limits and unavailable combinations

Actual kivy-stubs is absent from the supplied files and installed distributions.
The genuine kivyfn source supplies kivyfn.logging; no replacement logger or fake
peer package is used. Pyright without the actual sibling stubs does not complete
that dependency-specific gate. Run the documented sibling-dependency setup on
an environment containing the real kivy-stubs package.

Linux Python 3.11 and 3.12 are tested. Windows/macOS runtime jobs, other Kivy
versions, minimum-dependency combinations, physical touchscreen/window effects
and workload benchmarks are not claimed. Windows archive path forms are covered
by deterministic tests on Linux; that does not constitute a Windows runtime run.

History does not retain NaN, cyclic or opaque unsupported payloads. Capture and
history budgets, live Host retry results, preview scope and continuity retain
their documented bounds. Retrying an oversized mixed response may remain unknown
while its query still exceeds the cap; it does not repeat the retained committed
transaction. Inspect current State and use the existing explicit retry contract.

Automatic reconnect needs a known endpoint; a process restarted on port zero may
choose another port. Discovery, process supervision, offline queues and replay
journals are not added. Cross-version compatibility, persistent or branching undo,
editable history, trigger re-execution, dependency graphs, arbitrary code execution,
remote administration and implicit signal side-effect reversal remain deferred.
Initializer is synchronous and runs at startup only. Initialization is the baseline,
so it has no undo entry. Process configuration must contain module-level classes
and serializable application instance data, as with migrations and schemas.

See [the coverage matrix](coverage-matrix.md) for all PLAN areas and review
regressions. [OBSOLETE_FILES.md](../OBSOLETE_FILES.md) lists exact deletions from
the previous release. The ZIP extracts directly over the repository root and
includes source, tests, documentation, frontend source/lockfile and compiled assets;
it excludes Git metadata, environments, node_modules, caches and temporary output.


