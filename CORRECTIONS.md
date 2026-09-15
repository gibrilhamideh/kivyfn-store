# Correction pass

This pass preserves the Store / Host / Client / State architecture while
repairing ownership, atomicity, ordering, and lifecycle contracts. See
[the architecture guide](docs/architecture.md) for the current design and
[the test guide](tests/README.md) for reproducible quality checks.

## Major changes

- Transactions compile against logical staged data, validate final constraints,
  construct replacement rows and indexes, persist a complete generation, then
  install canonical state. Unique-value swaps and multi-schema failures no
  longer partially mutate live Nodes.
- Persistence publishes complete Store generations through one atomic manifest.
  Boot and migrations use the same boundary. Legacy snapshots remain readable;
  malformed committed envelopes are distinguished from absent data.
- Mutable values are copied at ownership handoffs: declarations, requests,
  canonical snapshots, commit delivery, query rows, and reactive field reads.
- Trigger commands execute in logical instruction order. Delete policies use
  that logical state, terminate across cycles, and handle reparenting and
  replacement rows. Singleton references use supported RESTRICT semantics.
- Store reads share the commit lock with revision stamping. Synchronous commit
  listeners may read and change subscriptions; they schedule subsequent writes
  after publication returns.
- Host delivery is ordered per client. Request acknowledgements wait for prior
  canonical delivery. A failed query, callback delivery, or slow client is
  isolated to the affected connection. Shutdown has one completion owner.
- Client synchronization retains early subscription events, checks correlation
  and revisions, rejects malformed boundaries, and settles pending operations
  on failure. It never automatically replays an ambiguous transaction. The Host
  remembers a bounded set of completed transaction IDs within its lifetime.
- State membership and values keep canonical data separate from transaction
  overlays. Completion or failure removes only the corresponding overlay.
  Incoming commits stage every manager before callbacks and preserve signal
  occurrence order across schemas and overlapping subscriptions.
- Projected writes retain source metadata and resolve through canonical
  relationship values plus preceding writes in the same transaction.
- Field/schema validation now rejects nonfinite numbers, incompatible temporal
  comparisons, malformed declarations, and unsupported relationship policies
  through the appropriate domain error boundary.
- Public APIs, compatibility imports, process integration, packaging, and
  documentation were checked together. Runtime logging uses the standard
  library; the optional process-manager integration uses structural protocols.
  Python 3.11 is the minimum supported interpreter.

## Additional defects found through feature coverage

Tests beyond the original review exposed nonadjacent mutation coalescing that
changed trigger/projection order; delete/reinsert interactions with generated
children; inherited closed transaction contexts; repeated membership overlay
cancellation; class-versus-name query identity; nested mutable QueryRow aliases;
empty set actions disappearing before validation; delivery markers consuming
message capacity; canceled connection waiters retaining stale background tasks;
transport errors losing their specific cause; reads seeing data before its
revision was stamped; interleaved cross-schema signals changing order; and Host
startup/shutdown failures leaving misleading or unfinished lifecycle state.

Dependency checks also found that the previously declared aiohttp minimum was
incompatible with an accepted Engine.IO version. The corrected minimum is
validated in CI alongside the locked dependency set.

## Coverage and validation

The suite covers normal behavior and meaningful failures across schemas, fields,
Nodes/indexes, compiler/execution, constraints, triggers, relationships, Store
commits/revisions, persistence/boot/migrations, queries and projections,
Host/Client, codecs, both real transports, synchronization, signals, StateData,
optimistic/strict mutations, overlapping transactions, membership, cancellation,
callback failures, lifecycle cleanup, and the process adapter.

Transport-sensitive integration tests run over Direct and real Socket.IO.
Persistence tests inject failures at concrete writes, replacements, fsyncs, and
cleanup boundaries. Ordering tests use explicit events, barriers, and controlled
handoffs. Coverage measurements guide exploration; they are not a guarantee
that every possible combination has been tested.

All 986 tests passed in each final configuration: Python 3.11 with locked
dependencies, Python 3.12 with locked dependencies, and Python 3.11 with the
minimum runtime dependencies (Kivy 2.3.0, Socket.IO 5.11.0, aiohttp 3.11.0).
Pyright reported zero errors and warnings; Ruff lint and formatting checks
passed. Measured coverage on Python 3.12 was 94.1% of statements and 86.4% of
branches. Reproduce these checks with the commands in tests/README.md.

## Deliberately preserved limits

- Per-schema revisions and snapshot resynchronization remain; there is no
  durable replay journal or exactly-once promise across Host restarts or cache
  eviction. A lost acknowledgement can mean the mutation already committed.
- Live queries still reevaluate their dependencies in full. Incremental query
  maintenance and joined-schema signal aliases were not needed for correctness.
- Kivy remains the notification mechanism. Callback exceptions cannot strand
  framework completion, but native dispatch may stop remaining bindings for
  that individual notification. Pending overlays remain visible by design.
- Dynamic generated fields remain dynamically typed. The package exports its
  annotations through py.typed and provides explicit named-field access.
- Existing migration fallback behavior is retained. A directory fsync failure
  after the manifest becomes visible is reported as durability uncertainty;
  it cannot honestly be reported as a rolled-back commit. Previous generations
  remain available. One Store owns a persistence directory at a time.
- A projected nested relationship retarget must be resolvable from subscribed
  canonical data. Unsupported ambiguous routing raises an explicit error.
- Legacy response and signal imports remain for compatibility. The package tree
  was retained where its responsibilities remain sound.

The delivery archive contains repository files directly at its root and no
virtual environment, version-control database, caches, or build output.
