"""Benchmark load limits distinguish timely delivery from data integrity."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "benchmark_nonpersistent",
    Path(__file__).resolve().parents[2] / "scripts/benchmark_nonpersistent.py",
)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    disabled = logging.root.manager.disable
    yield
    logging.disable(disabled)


def measured_result(
    *,
    rate: float = 100,
    confirmed: float = 100,
    latencies: tuple[float | None, ...] = (2, 3),
    ok: bool = True,
) -> dict[str, Any]:
    """Results of already-validated measurements, without running a load."""
    return {
        "transport": "direct",
        "observers": len(latencies),
        "rate_hz": rate,
        "duration_seconds": 1,
        "ok": ok,
        "writer": {
            "submitted": int(confirmed),
            "confirmed_updates_per_second": confirmed,
        },
        "observer_results": [
            {"scheduled_callback_ms": {"p95": latency}}
            for latency in latencies
        ],
    }


def test_inclusive_rate_and_latency_limits_pass() -> None:
    result = measured_result(confirmed=99, latencies=(2, 20))

    assessment = BENCHMARK.assess_load(result, 0.99, 20)

    assert assessment == {
        "status": "PASS",
        "reasons": [],
        "achieved_ratio": 0.99,
        "worst_scheduled_p95_ms": 20,
    }


def test_correct_but_insufficient_throughput_is_overloaded() -> None:
    result = measured_result(confirmed=98.9)

    assessment = BENCHMARK.assess_load(result, 0.99, 20)

    assert result["ok"] is True
    assert assessment["status"] == "OVERLOADED"
    assert assessment["achieved_ratio"] == pytest.approx(0.989)
    assert assessment["reasons"]


@pytest.mark.parametrize("latencies", [(20.1, 2), (2, 20.1)])
def test_slow_observer_overloads_even_with_full_throughput(
    latencies: tuple[float, float],
) -> None:
    result = measured_result(latencies=latencies)

    assessment = BENCHMARK.assess_load(result, 0.99, 20)

    assert result["ok"] is True
    assert assessment["status"] == "OVERLOADED"
    assert assessment["achieved_ratio"] == 1
    assert assessment["worst_scheduled_p95_ms"] == 20.1
    assert assessment["reasons"]


def test_configured_limits_determine_the_result() -> None:
    result = measured_result(confirmed=95, latencies=(10, 40))

    assert BENCHMARK.assess_load(result, 0.99, 20)["status"] == "OVERLOADED"
    assert BENCHMARK.assess_load(result, 0.95, 40)["status"] == "PASS"


@pytest.mark.parametrize("rate", [0, 100])
def test_integrity_failure_is_an_error_even_with_good_performance(
    rate: float,
) -> None:
    assessment = BENCHMARK.assess_load(
        measured_result(rate=rate, ok=False), 0.99, 20
    )

    assert assessment["status"] == "ERROR"
    assert assessment["reasons"]


def test_saturation_has_no_requested_rate_or_latency_target() -> None:
    result = measured_result(rate=0, confirmed=5000, latencies=(100, 200))

    assessment = BENCHMARK.assess_load(result, 0.99, 20)

    assert assessment["status"] == "SATURATION"
    assert assessment["achieved_ratio"] is None
    assert assessment["worst_scheduled_p95_ms"] == 200


@pytest.mark.parametrize("rate", [0, 100])
@pytest.mark.parametrize("latencies", [(), (None,), (2, None), (None, 2)])
def test_missing_observer_measurements_cannot_pass(
    rate: float, latencies: tuple[float | None, ...]
) -> None:
    result = measured_result(rate=rate, latencies=latencies)

    assessment = BENCHMARK.assess_load(result, 0.99, 20)

    assert assessment["status"] == "ERROR"
    assert assessment["reasons"]


def benchmark_arguments(path: Path, **changes: Any) -> argparse.Namespace:
    values = {
        "transport": "all",
        "rates": [100, 200, 400],
        "observers": [1, 3],
        "duration": 1,
        "inflight": 32,
        "repeat": 2,
        "timeout": 10,
        "json": path,
        "min_rate_ratio": 0.99,
        "max_scheduled_p95_ms": 20,
        "stop_on_overload": True,
    }
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    "options",
    [
        ["--min-rate-ratio", "0"],
        ["--min-rate-ratio", "1.01"],
        ["--min-rate-ratio", "nan"],
        ["--max-scheduled-p95-ms", "0"],
        ["--max-scheduled-p95-ms", "inf"],
        ["--stop-on-overload", "--rates", "100", "0"],
        ["--stop-on-overload", "--rates", "200", "100"],
        ["--stop-on-overload", "--rates", "100", "100"],
    ],
)
def test_invalid_limits_or_ramp_order_fail_at_cli_boundary(
    monkeypatch: pytest.MonkeyPatch, options: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark", *options])

    with pytest.raises(SystemExit) as error:
        BENCHMARK.arguments()

    assert error.value.code == 2


def test_default_matrix_keeps_saturation_and_explicit_limit_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["benchmark"])

    args = BENCHMARK.arguments()

    assert 0 in args.rates
    assert args.min_rate_ratio == 0.99
    assert args.max_scheduled_p95_ms == 20
    assert not args.stop_on_overload


@pytest.mark.asyncio
async def test_ramp_finishes_repeats_and_continues_other_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, float]] = []

    async def run_case(
        transport: str,
        observer_count: int,
        rate: float,
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        call = transport, observer_count, rate
        calls.append(call)
        # First repetition exceeds the budget; a good repeat must not hide it.
        latency = 21 if rate == 200 and calls.count(call) == 1 else 2
        result = measured_result(
            rate=rate, confirmed=rate, latencies=(latency,) * observer_count
        )
        result["transport"] = transport
        return result

    monkeypatch.setattr(BENCHMARK, "run_case", run_case)
    path = tmp_path / "results.json"

    exit_code = await BENCHMARK.main(benchmark_arguments(path))

    assert exit_code == 2
    assert calls == [
        (transport, observers, rate)
        for transport in ("direct", "socketio")
        for observers in (1, 3)
        for rate in (100, 100, 200, 200)
    ]
    results = json.loads(path.read_text())["results"]
    assert [result["load"]["status"] for result in results] == [
        "PASS",
        "PASS",
        "OVERLOADED",
        "PASS",
    ] * 4
    assert all(result["ok"] for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("integrity_failure", "slow", "expected_exit"),
    [(False, False, 0), (False, True, 2), (True, True, 1)],
)
async def test_exit_status_distinguishes_overload_from_integrity_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    integrity_failure: bool,
    slow: bool,
    expected_exit: int,
) -> None:
    rates_seen: list[float] = []

    async def run_case(
        transport: str,
        observer_count: int,
        rate: float,
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        rates_seen.append(rate)
        return measured_result(
            rate=rate,
            confirmed=1000 if rate == 0 else rate,
            latencies=(21 if slow else 2,),
            ok=not (integrity_failure and rate == 200),
        )

    monkeypatch.setattr(BENCHMARK, "run_case", run_case)
    args = benchmark_arguments(
        tmp_path / "results.json",
        transport="direct",
        rates=[100, 200, 0],
        observers=[1],
        repeat=1,
        stop_on_overload=False,
    )

    assert await BENCHMARK.main(args) == expected_exit
    assert rates_seen == [100, 200, 0]
    results = json.loads(args.json.read_text())["results"]
    assert results[-1]["load"]["status"] == "SATURATION"
