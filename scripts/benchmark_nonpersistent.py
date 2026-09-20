"""Measure nonpersistent State updates with real Kivy and Store transports.

Direct runs one event loop. Socket.IO spawns a Host and one process per
observer; the parent is the writer. IPC carries controls and summaries only.
Run from an installed environment or with PYTHONPATH=src. No window is drawn.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
import platform
import sys
import time
import traceback
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# Keep import/startup logging outside the measured interval in spawned workers.
os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_NO_FILELOG", "1")
os.environ.setdefault("KIVY_NO_CONSOLELOG", "1")

from kivyfn.store.client.client import StoreClient
from kivyfn.store.host.host import StoreHost
from kivyfn.store.store.store import Store
from kivyfn.store.builder import Branch, Builder, HostConfig
from kivyfn.store.schema import NumericField, SingleSchema
from kivyfn.store.state.manager import StateManager
from kivyfn.store.state.reactive.state import State
from kivyfn.store.state.transaction.transaction import StateTransaction


# NOTE:
# uv run python scripts/benchmark_nonpersistent.py \
#     --transport all \
#     --rates 60 120 240 1000 0 \
#     --observers 1 3 \
#     --duration 3 \
#     --repeat 2 \
#     --json benchmark-results.json


class MouseSchema(SingleSchema):
    persistent = False
    track_history = False

    seq = NumericField(default=0)
    x = NumericField(default=0)
    y = NumericField(default=0)
    sent = NumericField(default=0)
    scheduled = NumericField(default=0)


def percentiles(values: list[float]) -> dict[str, float | int | None]:
    """Exact nearest-rank milliseconds; raw samples stay in their process."""
    ordered = sorted(values)
    result: dict[str, float | int | None] = {"count": len(ordered)}
    for name, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        result[name] = (
            ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000
            if ordered
            else None
        )
    result["max"] = ordered[-1] * 1000 if ordered else None
    return result


def files(path: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(item.relative_to(path)): (
            item.stat().st_size,
            item.stat().st_mtime_ns,
            hashlib.sha256(item.read_bytes()).hexdigest(),
        )
        for item in path.rglob("*")
        if item.is_file()
    }


class Server:
    def __init__(self, path: Path, endpoint: str) -> None:
        self.path = path
        self.store = Store(
            Builder()
            .tree(Branch(MouseSchema))
            .storage_path(path)
            .history(None)
            .host(HostConfig(endpoint, devtools=False))
            .build()
        )
        self.host = StoreHost(self.store)
        self.cpu_start = 0.0

    async def start(self) -> str:
        self.store.boot()
        self.baseline = files(self.path)
        await self.host.start()
        return str(
            self.host.transport.endpoint.get("endpoint", self.host.endpoint)
        )

    def measure(self) -> None:
        self.cpu_start = time.process_time()

    def finish(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "cpu_seconds": time.process_time() - self.cpu_start,
            "final_seq": self.store.snapshot_by_schema("MouseSchema")["seq"],
            "storage_unchanged": files(self.path) == self.baseline,
            "boot_files": len(self.baseline),
            "history_enabled": self.store.config.history is not None,
        }

    async def close(self) -> None:
        try:
            await self.host.stop()
        finally:
            self.store.shutdown()


class Observer:
    def __init__(self) -> None:
        self.client = StoreClient()
        self.manager = StateManager(self.client)
        self.state = self.manager.create(MouseSchema)
        self.state.bind(seq=self.changed)
        self.updated = asyncio.Event()
        self.last_seq = 0
        self.count = 0
        self.gaps = 0
        self.out_of_order = 0
        self.bad_payloads = 0
        self.latencies: list[float] = []
        self.scheduled_latencies: list[float] = []
        self.cpu_start = 0.0

    def changed(self, state: State[MouseSchema], value: int | float) -> None:
        now = time.perf_counter()
        seq = int(value)
        previous = max(0, self.last_seq)
        self.last_seq = seq
        self.updated.set()
        if seq <= 0:
            return
        self.count += 1
        self.gaps += max(0, seq - previous - 1)
        self.out_of_order += seq <= previous
        self.bad_payloads += (
            state.get("x") != seq % 1920 or state.get("y") != (seq * 7) % 1080
        )
        self.latencies.append(now - float(state.get("sent")))
        self.scheduled_latencies.append(now - float(state.get("scheduled")))

    async def wait_for(self, seq: int, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while self.last_seq != seq:
                self.updated.clear()
                await self.updated.wait()

    def measure(self) -> None:
        self.cpu_start = time.process_time()

    async def finish(self, expected: int, timeout: float) -> dict[str, Any]:
        timed_out = False
        try:
            await self.wait_for(expected if expected else -1, timeout)
        except TimeoutError:
            timed_out = True
        cpu_seconds = time.process_time() - self.cpu_start
        return {
            "pid": os.getpid(),
            "count": self.count,
            "final_seq": self.last_seq,
            "gaps": self.gaps,
            "out_of_order": self.out_of_order,
            "bad_payloads": self.bad_payloads,
            "timed_out": timed_out,
            "online": self.client.online,
            "synced": bool(self.state.synced),
            "cpu_seconds": cpu_seconds,
            "callback_ms": percentiles(self.latencies),
            "scheduled_callback_ms": percentiles(self.scheduled_latencies),
        }

    async def close(self) -> None:
        self.manager.close()
        await self.client.disconnect()


def receive(pipe: Connection, timeout: float) -> dict[str, Any]:
    if not pipe.poll(timeout):
        raise TimeoutError("Worker control response timed out")
    message = pipe.recv()
    if "error" in message:
        raise RuntimeError(message["error"])
    return message


async def reply(pipe: Connection, timeout: float) -> dict[str, Any]:
    return await asyncio.to_thread(receive, pipe, timeout)


async def child(
    pipe: Connection, role: str, address: str, timeout: float
) -> None:
    resource = (
        Server(Path(address), "http://127.0.0.1:0")
        if role == "host"
        else Observer()
    )
    try:
        if isinstance(resource, Server):
            pipe.send({"endpoint": await resource.start()})
        else:
            await resource.client.connect(address)
            if not resource.state.synced:
                raise RuntimeError("Observer did not synchronize")
            pipe.send({"ready": True})
        while True:
            command = await reply(pipe, 3600)
            action = command["action"]
            if action == "stop":
                break
            if action == "warmup" and isinstance(resource, Observer):
                await resource.wait_for(-1, timeout)
                pipe.send({"ready": True})
            elif action == "measure":
                resource.measure()
                pipe.send({"ready": True})
            elif action == "finish":
                result = (
                    resource.finish()
                    if isinstance(resource, Server)
                    else await resource.finish(command["expected"], timeout)
                )
                pipe.send(result)
            else:
                raise RuntimeError(f"Unknown worker command: {action}")
    finally:
        await resource.close()


def child_main(
    pipe: Connection, role: str, address: str, timeout: float
) -> None:
    logging.disable(logging.INFO)
    try:
        asyncio.run(child(pipe, role, address, timeout))
    except BaseException:
        try:
            pipe.send({"error": traceback.format_exc()})
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        pipe.close()


def spawn(
    role: str, address: str, timeout: float
) -> tuple[BaseProcess, Connection]:
    context = mp.get_context("spawn")
    parent, worker = context.Pipe()
    process = context.Process(
        target=child_main, args=(worker, role, address, timeout), name=role
    )
    process.start()
    worker.close()
    return process, parent


async def stop_workers(
    workers: list[tuple[BaseProcess, Connection]],
) -> list[str]:
    errors: list[str] = []
    for _, pipe in workers:
        try:
            pipe.send({"action": "stop"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    for process, pipe in reversed(workers):
        await asyncio.to_thread(process.join, 3)
        if process.is_alive():
            errors.append(f"{process.name} required termination")
            process.terminate()
            await asyncio.to_thread(process.join, 3)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 3)
        if process.exitcode:
            errors.append(f"{process.name} exited with {process.exitcode}")
        pipe.close()
        process.close()
    return errors


def update(
    state: State[MouseSchema], seq: int, sent: float, scheduled: float
) -> StateTransaction:
    return state.set(
        seq=seq,
        x=seq % 1920,
        y=(seq * 7) % 1080,
        sent=sent,
        scheduled=scheduled,
    )


async def produce(
    state: State[MouseSchema], rate: float, args: argparse.Namespace
) -> dict[str, Any]:
    acknowledgements: list[float] = []
    lateness: list[float] = []
    errors: list[str] = []
    counts = {"succeeded": 0, "failed": 0, "timed_out": 0}
    pending: set[asyncio.Task[None]] = set()
    submitted = maximum = blocked = 0
    blocked_seconds = 0.0

    async def complete(operation: StateTransaction, sent: float) -> None:
        try:
            async with asyncio.timeout(args.timeout):
                await operation
            if not operation.success:
                raise RuntimeError(str(operation.failure))
            counts["succeeded"] += 1
            acknowledgements.append(time.perf_counter() - sent)
        except Exception as error:
            counts["failed"] += 1
            counts["timed_out"] += isinstance(error, TimeoutError)
            if len(errors) < 10:
                errors.append(f"{type(error).__name__}: {error}")

    start = time.perf_counter()
    deadline = start + args.duration
    last_sent = start
    while time.perf_counter() < deadline and not counts["failed"]:
        scheduled = start + submitted / rate if rate else time.perf_counter()
        if scheduled >= deadline:
            break
        delay = scheduled - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        pending = {task for task in pending if not task.done()}
        if len(pending) >= args.inflight:
            blocked += 1
            blocked_at = time.perf_counter()
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            blocked_seconds += time.perf_counter() - blocked_at
            pending = {task for task in pending if not task.done()}
        sent = time.perf_counter()
        if sent >= deadline or counts["failed"]:
            break
        submitted += 1
        last_sent = sent
        lateness.append(max(0.0, sent - scheduled) if rate else 0.0)
        try:
            operation = update(
                state, submitted, sent, scheduled if rate else sent
            )
        except Exception as error:
            counts["failed"] += 1
            errors.append(f"{type(error).__name__}: {error}")
            break
        pending.add(asyncio.create_task(complete(operation, sent)))
        maximum = max(maximum, len(pending))
    sending_finished = time.perf_counter()
    await asyncio.gather(*pending)
    finished = time.perf_counter()
    expected = math.ceil(rate * args.duration) if rate else None
    return {
        "submitted": submitted,
        "requested_slots": expected,
        "unsent_slots": max(0, expected - submitted) if expected else None,
        **counts,
        "errors": errors,
        "send_updates_per_second": submitted / args.duration,
        "confirmed_updates_per_second": counts["succeeded"]
        / max(args.duration, finished - start),
        "last_submission_seconds": last_sent - start,
        "wall_seconds": finished - start,
        "drain_seconds": finished - sending_finished,
        "max_inflight": maximum,
        "backpressure_waits": blocked,
        "backpressure_seconds": blocked_seconds,
        "ack_ms": percentiles(acknowledgements),
        "scheduler_lateness_ms": percentiles(lateness),
        "late_over_one_period": sum(value > 1 / rate for value in lateness)
        if rate
        else None,
        "final_seq": state.get("seq"),
        "stopped_after_failure": bool(counts["failed"]),
    }


async def run_case(
    transport: str, observer_count: int, rate: float, args: argparse.Namespace
) -> dict[str, Any]:
    workers: list[tuple[BaseProcess, Connection]] = []
    observers: list[Observer] = []
    server: Server | None = None
    writer = Observer()
    # The writer is subscribed normally, but is not an extra latency observer.
    writer.state.unbind(seq=writer.changed)
    result: dict[str, Any] = {
        "transport": transport,
        "parent_pid": os.getpid(),
        "observers": observer_count,
        "rate_hz": rate,
        "duration_seconds": args.duration,
        "inflight_limit": args.inflight,
        "cpu_scope": (
            "One shared process; do not sum parent/host/observer CPU"
            if transport == "direct"
            else "Separate writer, Host and observer processes"
        ),
        "ok": False,
    }
    with TemporaryDirectory(prefix="kivyfn-benchmark-") as directory:
        try:
            if transport == "direct":
                server = Server(Path(directory), "direct://benchmark")
                endpoint = await server.start()
                for _ in range(observer_count):
                    observer = Observer()
                    observers.append(observer)
                    await observer.client.connect(endpoint)
            else:
                workers.append(spawn("host", directory, args.timeout))
                endpoint = (await reply(workers[0][1], args.timeout))[
                    "endpoint"
                ]
                for _ in range(observer_count):
                    workers.append(spawn("observer", endpoint, args.timeout))
                await asyncio.gather(
                    *(reply(pipe, args.timeout) for _, pipe in workers[1:])
                )
            await writer.client.connect(endpoint)
            if not writer.state.synced or any(
                not observer.state.synced for observer in observers
            ):
                raise RuntimeError("Initial subscriptions did not synchronize")
            for seq in range(-20, 0):
                now = time.perf_counter()
                operation = update(writer.state, seq, now, now)
                async with asyncio.timeout(args.timeout):
                    await operation
                if not operation.success:
                    raise RuntimeError(f"Warmup failed: {operation.failure}")
            for _, pipe in workers[1:]:
                pipe.send({"action": "warmup"})
            await asyncio.gather(
                *(
                    observer.wait_for(-1, args.timeout)
                    for observer in observers
                ),
                *(reply(pipe, args.timeout) for _, pipe in workers[1:]),
            )
            for _, pipe in workers:
                pipe.send({"action": "measure"})
            await asyncio.gather(
                *(reply(pipe, args.timeout) for _, pipe in workers)
            )
            for observer in observers:
                observer.measure()
            if server is not None:
                server.measure()
            cpu_start = time.process_time()
            producer = await produce(writer.state, rate, args)
            result["writer"] = producer
            result["parent_cpu_seconds"] = time.process_time() - cpu_start
            expected = producer["submitted"]
            for _, pipe in workers[1:]:
                pipe.send({"action": "finish", "expected": expected})
            reports = await asyncio.gather(
                *(
                    observer.finish(expected, args.timeout)
                    for observer in observers
                ),
                *(reply(pipe, args.timeout + 2) for _, pipe in workers[1:]),
            )
            result["observer_results"] = reports
            if server is None:
                workers[0][1].send({"action": "finish"})
                host_result = await reply(workers[0][1], args.timeout)
            else:
                host_result = server.finish()
            result["host"] = host_result
            result["ok"] = (
                producer["failed"] == 0
                and producer["succeeded"] == expected
                and producer["final_seq"] == expected
                and host_result["final_seq"] == expected
                and host_result["storage_unchanged"]
                and not host_result["history_enabled"]
                and all(
                    report["count"] == expected
                    and report["final_seq"] == expected
                    and not report["gaps"]
                    and not report["out_of_order"]
                    and not report["bad_payloads"]
                    and not report["timed_out"]
                    and report["online"]
                    and report["synced"]
                    for report in reports
                )
            )
        except Exception:
            result["error"] = traceback.format_exc()
        finally:
            cleanup_errors: list[str] = []
            try:
                closed = await asyncio.gather(
                    writer.close(),
                    *(observer.close() for observer in observers),
                    return_exceptions=True,
                )
                cleanup_errors.extend(
                    str(error)
                    for error in closed
                    if isinstance(error, BaseException)
                )
            finally:
                try:
                    cleanup_errors.extend(await stop_workers(workers))
                finally:
                    if server is not None:
                        try:
                            await server.close()
                        except Exception as error:
                            cleanup_errors.append(str(error))
            if cleanup_errors:
                result["cleanup_errors"] = cleanup_errors
                result["ok"] = False
    return result


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport", choices=("all", "direct", "socketio"), default="all"
    )
    parser.add_argument(
        "--rates",
        nargs="+",
        type=float,
        default=[60, 120, 240, 1000, 0],
        help="Updates/second; 0 runs at bounded saturation",
    )
    parser.add_argument("--observers", nargs="+", type=int, default=[1, 3])
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--inflight", type=int, default=32)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--min-rate-ratio",
        type=float,
        default=0.99,
        help="Minimum confirmed/requested rate for a paced workload",
    )
    parser.add_argument(
        "--max-scheduled-p95-ms",
        type=float,
        default=20.0,
        help="Maximum observer p95 from intended schedule to callback",
    )
    parser.add_argument(
        "--stop-on-overload",
        action="store_true",
        help="Stop each increasing rate sweep after its first failed stage",
    )
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if any(not math.isfinite(rate) or rate < 0 for rate in args.rates):
        parser.error("rates must be finite and nonnegative")
    if any(count < 1 for count in args.observers):
        parser.error("observer counts must be positive")
    if args.inflight < 1 or args.repeat < 1:
        parser.error("inflight and repeat must be positive")
    for name in ("duration", "timeout", "max_scheduled_p95_ms"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name} must be finite and positive")
    if not math.isfinite(args.min_rate_ratio) or not (
        0 < args.min_rate_ratio <= 1
    ):
        parser.error("min-rate-ratio must be greater than 0 and at most 1")
    if args.stop_on_overload and (
        any(rate <= 0 for rate in args.rates)
        or any(
            left >= right for left, right in zip(args.rates, args.rates[1:])
        )
    ):
        parser.error("stop-on-overload requires increasing positive rates")
    return args


def assess_load(
    result: dict[str, Any], min_rate_ratio: float, max_scheduled_p95_ms: float
) -> dict[str, Any]:
    """Separate workload budgets from the existing delivery checks."""
    assessment: dict[str, Any] = {
        "status": "ERROR",
        "reasons": [],
        "achieved_ratio": None,
        "worst_scheduled_p95_ms": None,
    }
    reasons = assessment["reasons"]
    if not result["ok"]:
        reasons.append("Delivery, state, connection or cleanup check failed")
        return assessment

    latencies = [
        observer["scheduled_callback_ms"]["p95"]
        for observer in result["observer_results"]
    ]
    if not latencies or any(value is None for value in latencies):
        reasons.append("No complete observer latency measurements")
        return assessment

    latency = max(latencies)
    assessment["worst_scheduled_p95_ms"] = latency
    rate = result["rate_hz"]
    if rate == 0:
        assessment["status"] = "SATURATION"
        return assessment

    ratio = result["writer"]["confirmed_updates_per_second"] / rate
    assessment["achieved_ratio"] = ratio
    if ratio < min_rate_ratio:
        reasons.append(
            f"Achieved {ratio:.2%} of requested rate; "
            f"requires {min_rate_ratio:.2%}"
        )
    if latency > max_scheduled_p95_ms:
        reasons.append(
            f"Worst observer scheduled p95 {latency:.2f} ms "
            f"exceeds {max_scheduled_p95_ms:.2f} ms"
        )
    assessment["status"] = "OVERLOADED" if reasons else "PASS"
    return assessment


async def main(args: argparse.Namespace) -> int:
    logging.disable(logging.INFO)
    results: list[dict[str, Any]] = []
    output = {
        "python": sys.version,
        "platform": platform.platform(),
        "clock": vars(time.get_clock_info("perf_counter")),
        "load_limits": {
            "min_rate_ratio": args.min_rate_ratio,
            "max_scheduled_p95_ms": args.max_scheduled_p95_ms,
            "stop_on_overload": args.stop_on_overload,
        },
        "results": results,
    }
    transports = (
        ("direct", "socketio")
        if args.transport == "all"
        else (args.transport,)
    )
    for transport in transports:
        for observer_count in args.observers:
            last_passed_rate: float | None = None
            for rate in args.rates:
                stage_passed = True
                for repetition in range(1, args.repeat + 1):
                    result = await run_case(
                        transport, observer_count, rate, args
                    )
                    result["repetition"] = repetition
                    load = assess_load(
                        result, args.min_rate_ratio, args.max_scheduled_p95_ms
                    )
                    result["load"] = load
                    results.append(result)
                    writer = result.get("writer", {})
                    latency = load["worst_scheduled_p95_ms"]
                    latency_text = (
                        f"{latency:.2f}ms" if latency is not None else "n/a"
                    )
                    print(
                        f"{transport} observers={observer_count} "
                        f"rate={rate:g} "
                        f"repeat={repetition} "
                        f"integrity={'PASS' if result['ok'] else 'FAIL'} "
                        f"load={load['status']} "
                        f"submitted={writer.get('submitted', 0)} "
                        f"confirmed/s="
                        f"{writer.get('confirmed_updates_per_second', 0):.1f} "
                        f"scheduled_p95={latency_text} "
                        f"unsent={writer.get('unsent_slots')}",
                        flush=True,
                    )
                    for reason in load["reasons"]:
                        print(f"  {reason}", flush=True)
                    stage_passed &= load["status"] == "PASS"
                    if "error" in result:
                        print(result["error"], file=sys.stderr, flush=True)
                    if args.json is not None:
                        args.json.parent.mkdir(parents=True, exist_ok=True)
                        args.json.write_text(
                            json.dumps(output, indent=2) + "\n"
                        )
                if stage_passed:
                    last_passed_rate = rate
                elif args.stop_on_overload:
                    print(
                        f"Stopped {transport} observers={observer_count}: "
                        f"last passing rate={last_passed_rate}, "
                        f"first exceeded/error rate={rate:g}. "
                        "This brackets the tested workload, not a hard limit.",
                        flush=True,
                    )
                    break
            else:
                if args.stop_on_overload:
                    print(
                        f"{transport} observers={observer_count}: "
                        "all tested rates passed; increase the rate list.",
                        flush=True,
                    )
    if args.json is None:
        print(json.dumps(output, indent=2))
    statuses = {result["load"]["status"] for result in results}
    if "ERROR" in statuses:
        return 1
    return 2 if "OVERLOADED" in statuses else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(arguments())))
