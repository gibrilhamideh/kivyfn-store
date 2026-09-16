# Nonpersistent Store performance

The benchmark checks delivery integrity and reports whether a paced workload
meets configurable throughput and callback-latency budgets. Those budgets
describe application requirements, not built-in Store capacity limits.
Historical measurements below are specific to their stated machines.

## Run it in your repository

Save the supplied script as `scripts/benchmark_nonpersistent.py`, then run:

```sh
uv run python scripts/benchmark_nonpersistent.py \
  --transport all \
  --rates 60 120 240 1000 0 \
  --observers 1 3 \
  --duration 3 \
  --inflight 32 \
  --repeat 2 \
  --json benchmark-results.json
```

The JSON is updated after each case. Exit `0` means all paced cases met the
configured budgets and all integrity checks passed; exit `2` means at least
one paced case exceeded a budget; exit `1` means an integrity, connection,
measurement or cleanup error. Errors take precedence over budget failures.
Unsubmitted schedule slots are reported separately as `unsent_slots`; they
do not mean submitted transactions were lost.
Use rate `0` for bounded saturation. Increase duration for a longer run on your
own machine. This is a standalone benchmark, not a timing assertion in pytest.

For just the multiprocess mouse-position case:

```sh
uv run python scripts/benchmark_nonpersistent.py \
  --transport socketio --rates 120 1000 --observers 3 \
  --duration 10 --json mouse-results.json
```

The process count and observer count stay fixed during a case. One producer
can have up to `--inflight` unfinished strict transactions. These may share
the Client's existing request batches. Failure or timeout stops further
submission and is reported; changing the limit changes the workload.

## Finding your workload limit

Run increasing paced rates on the target machine:

```sh
uv run python scripts/benchmark_nonpersistent.py \
  --transport socketio \
  --rates 1000 1500 2000 2500 3000 4000 6000 8000 12000 \
  --observers 3 \
  --duration 10 \
  --inflight 32 \
  --repeat 2 \
  --min-rate-ratio 0.99 \
  --max-scheduled-p95-ms 20 \
  --stop-on-overload \
  --json benchmark-limits.json
```

Use `--transport all` to also measure Direct. The sweep finishes every
repetition at the first failing rate, then skips higher rates for that
transport/observer combination. Other combinations still run. A stage passes
only if every repetition passes. Rates must be strictly increasing and
positive; run saturation (`--rates 0`) separately.

The default budgets require both:

- At least **99% of the requested rate** confirmed successfully, including
  the time needed to drain submitted transactions after the sending period.
- Every observer's **p95 scheduled-to-callback delay at most 20 ms**.
  This includes producer scheduling lateness, transmission and Store work.

These are explicit, adjustable starting targets for this benchmark. They are
not universal definitions of responsiveness. For example, use
`--max-scheduled-p95-ms 10` for a tighter callback budget. A p95 budget allows
up to roughly 5% of measured callbacks to exceed it; inspect p99 and maximum
in the JSON when tail latency matters. Unsubmitted slots have no callback
latency sample, so the separate achieved-rate requirement is essential.

Console verdicts and JSON `load.status` mean:

| Verdict | Meaning |
| --- | --- |
| `PASS` | Submitted work is correct and both paced-workload budgets pass. |
| `OVERLOADED` | Submitted work is correct, but rate or latency misses its budget. |
| `ERROR` | Integrity, connection, cleanup or required measurements failed. |
| `SATURATION` | Integrity passed; rate 0 has no requested cadence to judge. |

Each line shows the confirmed rate, worst observer scheduled p95 and unsent
slots. Failure reasons name the exceeded budget. JSON `ok` retains its
original meaning: delivery integrity only. A result can therefore have
`ok: true` and `load.status: "OVERLOADED"` without contradiction.

The final passing/failing rates bracket this run's workload limit. They do
not establish an exact hardware limit. For example, if 2,000 passes and
2,500 exceeds a budget, repeat with rates between those values, then verify
the desired operating rate for longer and with normal application work
running. If the first rate fails, start lower; if all rates pass, extend the
list. Exit `2` is the expected outcome when this sweep finds an overload.

The outstanding-work cap intentionally stops queues growing without bound.
As load rises, backpressure delays the producer and leaves schedule slots
unsent. A crash is not required to demonstrate that an application budget
has been exceeded. This test does not search for an out-of-memory threshold.

### Your reported target-machine run

The supplied console output contains 40 cases, all with `ok=True`. Both
transports sustained approximately 1,000 confirmed updates/second for two
three-second repetitions with one and three observers. One Direct repetition
submitted 2,999 of 3,000 scheduled updates; this is one unsubmitted slot, not
a lost submitted transaction. Saturation results were:

| Transport | Observers | Confirmed updates/s, two repetitions |
| --- | --- | --- |
| Direct | 1 | 4,994.4–5,012.1 |
| Direct | 3 | 3,554.5–3,576.8 |
| Socket.IO | 1 | 4,779.5–4,791.5 |
| Socket.IO | 3 | 3,677.7–3,702.0 |

These results are faster than the historical container measurements below.
They establish short-run throughput and delivery integrity for this workload.
The pasted output does not include callback latencies, so it cannot establish
a responsiveness budget or a steady paced ceiling. Saturation uses a
different producer schedule and can produce different request batching.

## Workload

The workload is one nonpersistent schema representing a changing mouse
position. Each update uses the normal public State API and changes both
coordinates atomically, together with a sequence number and measurement
timestamps. It exercises Store validation, transactions, Client delivery and
actual Kivy property callbacks. No application widgets are rendered.

History and devtools are disabled. No triggers or relationship work is added
to this small baseline. Store boot can create storage metadata; startup is
outside the timed workload, and stored files must remain unchanged during
measured mutations. This is not a persistence performance test.

## Topologies

- Direct: Store, Host, writer and observer Clients share one process and
  event loop. Each Client uses its own StateManager. This is an in-process
  baseline, not a transport between processes.
- Socket.IO: the Host runs in a spawned child process; the writer runs in
  the controlling process; each observer runs in another spawned process.
  Clients connect through loopback Socket.IO. Multiprocessing pipes carry
  readiness, control messages and final results, never Store updates.

The observer count excludes the writing Client, which also receives its
canonical subscription updates. One writer plus three observers therefore
means four subscribed Clients and, for Socket.IO, five processes including
the Host.

## What the measurements mean

Connection and subscription readiness, warmup and final delivery are explicit
barriers. A producer acknowledgement alone is not proof that every observer
has received the final update. The benchmark also checks observer counts,
sequence order and final positions.

Keep these different measurements separate:

- Requested rate: the application-side schedule.
- Submitted rate: updates actually handed to State.set().
- Successful completion rate: transactions acknowledged successfully.
- Observer latency: submission timestamp to the observer's Kivy callback.
- Scheduled latency: intended generation time to that callback, including
  producer lateness for submitted samples.
- Missed schedule slots: samples never submitted by the benchmark producer.
  These are separate from submitted updates missing at an observer.
- Saturation: produce as fast as bounded outstanding work permits. This
  shows throughput and queueing under this workload, not a safe UI rate.

State.set() is strict. An optimistic local echo is not used as a receipt
measurement. Multiple transactions can share a transport request batch;
reported update rates count transactions, not network packets or batches.

Percentiles describe measured callback latency, not render latency or FPS.
Raw mouse polling, Kivy input dispatch, drawing and application calculations
are outside this workload. A busy controller, slower CPU or larger schema can
produce different results. Repeat the benchmark on the target hardware.

The paced producer preserves sequential updates and stops submitting at the
deadline. If it falls behind, unsent slots remain at the end; there is no
automatic latest-position coalescing. Scheduled latency exposes accumulated
lateness among submitted updates. Saturation has no requested cadence.

For CPU totals, Direct uses the parent's measurement only: its observer and
Host measurements overlap in the same PID. For Socket.IO, parent, Host and
observer PIDs are distinct. Their CPU intervals extend to each role's finish
barrier, so totals include a small amount of measurement/control overhead.

## Original measurement environment

Measurements run on Linux x86_64 with an Intel Xeon Platinum 8573C. The
container has nine CPUs in its affinity mask and an eight-CPU cgroup quota;
its memory limit is 20 GiB. This is not the controller's hardware.

Runtime: Python 3.11.16, Kivy 2.3.1, python-socketio 5.16.3 and aiohttp 3.14.3,
with the genuine supplied kivyfn source. No fake dependencies are used.
All processes run on the same machine and use the same monotonic performance
clock. No other benchmark or type-check process runs concurrently with the
reported matrix. Routine benchmark INFO/DEBUG logging is disabled.

## Original results

The matrix has 40 cases: two topologies, two observer counts, five rates and
two three-second repetitions. Three additional ten-second cases exercise
Socket.IO with three observers. Each case uses a fresh Store and fresh Clients.
The tables show the confirmed-rate range and the largest observer p95 across
both repetitions. These are conservative maxima of per-observer percentiles,
not pooled percentiles. Latency units are milliseconds.

### Direct, one process

| Observers | Requested Hz | Confirmed updates/s | Callback p95 ms | Scheduled p95 ms | Unsent slots, both runs |
| --- | --- | --- | --- | --- | --- |
| 1 | 60 | 60.0 | 0.81 | 1.77 | 0 |
| 1 | 120 | 120.0 | 0.89 | 1.79 | 0 |
| 1 | 240 | 240.0 | 0.73 | 1.70 | 0 |
| 1 | 1000 | 999.5–1000.0 | 1.38 | 2.38 | 0 |
| 1 | Saturation | 1407.9–1639.3 | 25.39 | 25.39 | n/a |
| 3 | 60 | 60.0 | 1.31 | 2.30 | 0 |
| 3 | 120 | 120.0 | 1.31 | 2.15 | 0 |
| 3 | 240 | 240.0 | 1.04 | 1.96 | 0 |
| 3 | 1000 | 999.8–999.9 | 3.20 | 4.79 | 0 |
| 3 | Saturation | 1183.7–1219.5 | 27.11 | 27.11 | n/a |

### Socket.IO, separate processes

| Observers | Requested Hz | Confirmed updates/s | Callback p95 ms | Scheduled p95 ms | Unsent slots, both runs |
| --- | --- | --- | --- | --- | --- |
| 1 | 60 | 60.0 | 1.84 | 2.75 | 0 |
| 1 | 120 | 120.0 | 1.87 | 2.73 | 0 |
| 1 | 240 | 240.0 | 1.94 | 2.77 | 0 |
| 1 | 1000 | 701.9–795.3 | 54.33 | 877.43 | 1453 |
| 1 | Saturation | 1283.4–1515.5 | 21.69 | 21.69 | n/a |
| 3 | 60 | 60.0 | 2.63 | 3.96 | 0 |
| 3 | 120 | 120.0 | 2.18 | 3.06 | 0 |
| 3 | 240 | 240.0 | 2.37 | 3.12 | 0 |
| 3 | 1000 | 597.5–619.1 | 69.29 | 1192.00 | 2294 |
| 3 | Saturation | 949.0–952.2 | 30.39 | 30.39 | n/a |

### Longer Socket.IO checks

Three observer processes, ten seconds per case, outstanding limit 32:

| Requested Hz | Confirmed updates/s | Callback p95 ms | Scheduled p95 ms | Unsent slots |
| --- | --- | --- | --- | --- |
| 120 | 120.0 | 2.73 | 3.54 | 0 |
| 1000 | 588.8 | 82.23 | 3845.91 | 4078 |
| Saturation | 1146.2 | 24.57 | 24.57 | n/a |

Across both result files, **79,507 submitted transactions** produced **172,139
observer callbacks**. Every submitted transaction succeeded, every observer
received the complete sequence with matching coordinates, and no file changed
after boot. There were no gaps, out-of-order callbacks, failed transactions,
timeouts or child-cleanup errors in the measured cases. Unsent scheduled
samples are explicitly excluded from those delivery claims.

The ten-second 1,000 Hz case submitted only 5,922 of 10,000 scheduled updates.
Its worst observer p95 was 82.23 ms from submission and 3,845.91 ms from the
intended schedule. That is not acceptable evidence of smooth 1,000 Hz mouse
sharing, even though submitted transactions remained consistent.

Saturation and paced traffic have different request-batching patterns. The
Client freezes each event-loop turn into a batch and sends batches serially.
The saturation producer initially issues 32 transactions before yielding;
paced updates typically make smaller batches. This is the likely explanation
for saturation exceeding paced 1,000 Hz throughput, supported by code review;
wire batch sizes were not instrumented. Do not use saturation results as a
promise of paced high-frequency delivery.

For context, the three-observer 120 Hz Socket.IO cases used at most about
37% of one CPU core when summing process CPU time. The Direct three-observer
1,000 Hz case reached about 96% of one core, leaving little headroom for
rendering or application work. CPU measurements are approximate and include
small control/summary overheads; raw per-process values are retained.

### Practical recommendation

Keep the local cursor responsive through normal Kivy input handling. When
other processes need its position, publish the latest coordinates together
at a chosen cadence such as 60 Hz, or 120 Hz if target-hardware measurements
justify it. Application-side sampling can discard obsolete intermediate
positions; discrete clicks or commands have different semantics and should
not be discarded that way.

The measured multiprocess path handled 60–240 Hz for this small workload.
Direct handled 1,000 Hz here, but Direct cannot connect different processes.
On this container, the Socket.IO path did not meet lossless, individually
paced 1,000 Hz throughput. Your reported hardware sustained that rate;
application requirements and target-hardware latency should guide any
optimization. No transport, Store batching, authentication or production
scheduling code was changed.

## Validation and evidence

- Eight preliminary smoke cases covered both transports, one/three observers,
  paced traffic and saturation. Their timing is not used in the results above.
- The original 40-case matrix and three longer cases each returned exit 0
  under the earlier integrity-only exit policy.
  Raw result validation checked all `ok` flags, maximum outstanding work,
  expected distinct process IDs and unchanged storage.
- A separate deterministic stalled-delivery check used a real Direct Host and
  gated its event sender. Both pending transactions timed out; the producer
  stopped at two actual outstanding transactions and drained after release.
  This confirms timeouts do not silently defeat the configured work bound.
- Focused Pyright on the benchmark: zero errors, warnings or information
  diagnostics. Ruff lint/format and the 79-character source limit passed.
- Genuine Kivy and kivyfn were used. Actual kivy-stubs remains unavailable;
  the earlier dependency-specific typing limitation is not claimed resolved.

The JSON result files contain every repetition and observer, including p50,
p95, p99, maximum latency, CPU seconds, queue/backpressure counters, process
IDs, final state checks, Python/platform and clock information:

- `nonpersistent-results.json`: the 40-case matrix.
- `nonpersistent-stress-results.json`: the three ten-second Socket.IO cases.

Benchmark source SHA-256 at the time of those original measurements:
`eebc582bff2013ce8faebddd5b9be401caaf7651bc601a7914ccaf97f93e318a`.
The raw historical JSON files are unchanged; they predate `load.status`.

### Load-verdict update validation

The updated script was exercised against real Direct and Socket.IO backends
using the same container and genuine dependencies described above:

- An increasing-rate sweep with three observers, three seconds and two
  repetitions ran 12 cases. Direct passed 1,000 Hz and exceeded both budgets
  at 2,000 Hz. Socket.IO passed 60 Hz; at 240 Hz one repetition had a 67.71 ms
  scheduled p95 despite full throughput, while the other passed at 2.91 ms.
  The sweep correctly stopped after both repetitions of each failing stage.
  This variation is evidence for repeating measurements, not an exact
  capacity conclusion about either machine.
- Four additional one-second cases requested 100,000 Hz and saturation with
  three observers on both transports. The high requested rate correctly
  reported `OVERLOADED`; saturation reported `SATURATION`. Both runs returned
  exit `2`, as expected for budget failures.
- Across these 16 cases, all 20,683 submitted transactions and 62,049 observer
  callbacks passed integrity checks. Outstanding work stayed at or below 32;
  no storage change or cleanup failure occurred.
- All 29 deterministic regression cases passed. They cover budget boundaries,
  independently slow throughput and callbacks, the slowest observer, missing
  measurements, integrity-error precedence, saturation, CLI arguments and
  sweep stopping across repetitions and groups.
- The full Python suite passed: **1,522 tests in 8.20 seconds**. Focused
  Pyright reported zero errors, warnings or information diagnostics for the
  script and its tests. Focused Ruff lint and formatting passed. Actual
  kivy-stubs remains unavailable; the dependency-specific typing gate remains
  unverified.

New evidence files are `nonpersistent-limit-validation.json` and
`nonpersistent-overload-validation.json`. These exercise the benchmark's
verdicts on this container; run the increasing-rate command on your hardware
to establish its own workload bracket. Frontend and packaging behavior is
unchanged and was not re-tested for this benchmark-only update.

## Optional authentication recommendation

Keep the library a reactive Store with optional hosting. Authentication is
useful when the connecting application or process crosses a trust boundary;
it does not require an account system or a cloud backend.

The current Socket.IO handshake carries the protocol version, with no
credential verification. The smallest future addition is an optional Host
admission verifier configured by the application, with credentials supplied
by the Client. Check admission before registering the connection or serving
subscriptions. Transport code owns the external message shape; the verifier
owns credential meaning. Core Store validation, schemas, history and storage
keep their current responsibilities.

Initially, admitted clients can have full Store access. Per-user or per-row
authorization is a separate requirement: it would have to cover reads,
subscriptions, mutations, signals and history consistently. Do not add it
without a concrete application need. Verify credentials on reconnect too,
and distinguish permanent rejection from a transient network failure.

Socket.IO already has a connect authentication payload and rejection
mechanism, so no second authentication transport is needed. See the official
[server connection documentation](https://python-socketio.readthedocs.io/en/latest/server.html#connect-and-disconnect-events)
and [Client authentication API](https://python-socketio.readthedocs.io/en/stable/api_client.html).
For communication across an untrusted network, protect credentials and data
with TLS; the Client supports
[HTTPS connections](https://python-socketio.readthedocs.io/en/latest/client.html#tls-ssl-support).

Direct shares the Python process and is not a security boundary against code
inside that process. Devtools is a separate local administrative interface;
application-client authentication would not automatically protect it. No
authentication or authorization implementation is included in this benchmark.


