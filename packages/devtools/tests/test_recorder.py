# tests/devtools/test_recorder.py
from __future__ import annotations

from typing import Any

import pytest

from kivyfn.store.devtools.config import DevToolsConfig
from kivyfn.store.devtools.protocol import (
    CaptureTooLarge,
    encode_checkpoint,
    encoded_size,
    from_wire,
    to_wire,
)
from kivyfn.store.devtools.recorder import Recorder


def baseline() -> dict[str, Any]:
    return {
        "store_instance_id": "test",
        "schemas": {"Model": {"x": 0, "z": 0}},
        "schema_revisions": {"Model": 0},
        "history": {},
    }


def delta(key: str, value: int, revision: int) -> dict[str, Any]:
    return {
        "events": [
            {"type": "set", "schema": "Model", "key": key, "new": value}
        ],
        "schema_revisions": {"Model": revision},
    }


def test_exact_values_are_detached_and_tag_collisions_roundtrip() -> None:
    value = {
        "large": 2**100,
        "negative": -(2**70),
        "literal": {"$integer": "123"},
        "escaped": {"$object": [["key", "value"]]},
        "mutable": [1, {"safe": 42}],
    }
    wire = to_wire(value)
    assert wire["large"] == {"$integer": str(2**100)}
    assert from_wire(wire) == value
    value["mutable"] = []
    assert wire["mutable"] == [1, {"safe": 42}]
    with pytest.raises(ValueError, match="tag"):
        from_wire({"large": 2**100})
    with pytest.raises(ValueError, match="unsupported|Unsupported"):
        from_wire({"$unsupported": "callable"})


def test_payload_visit_is_bounded_and_does_not_call_repr() -> None:
    class Dangerous:
        def __repr__(self) -> str:
            raise AssertionError("Must not evaluate arbitrary repr")

    assert to_wire(Dangerous()) == {"$unsupported": "Dangerous"}
    with pytest.raises(CaptureTooLarge):
        to_wire(["long text" * 1000] * 1000, 256)
    with pytest.raises(CaptureTooLarge):
        to_wire({"x": "text" * 1000}, 256)


def test_preview_uses_chronological_commits_not_grouped_history() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    recorder = Recorder(DevToolsConfig(), lambda *args: published.append(args))
    source = baseline()
    recorder.reset(source)
    source["schemas"]["Model"]["x"] = 999
    recorder.commit(delta("x", 1, 1))
    first = recorder.sequence
    recorder.commit(delta("z", 42, 2))
    middle = recorder.sequence
    recorder.commit(delta("x", 2, 3))
    assert recorder.preview(first)["checkpoint"]["schemas"]["Model"] == {
        "x": 1,
        "z": 0,
    }
    assert recorder.preview(middle)["checkpoint"]["schemas"]["Model"] == {
        "x": 1,
        "z": 42,
    }
    preview = recorder.preview(recorder.sequence)
    preview["checkpoint"]["schemas"]["Model"]["x"] = -1
    assert (
        recorder.preview(recorder.sequence)["checkpoint"]["schemas"]["Model"][
            "x"
        ]
        == 2
    )


def test_record_filters_do_not_change_timeline_or_live_stream() -> None:
    emitted: list[str] = []
    recorder = Recorder(DevToolsConfig(), lambda kind, _: emitted.append(kind))
    recorder.reset(baseline())
    recorder.filters = {"schema": "Elsewhere"}
    recorder.commit(delta("x", 10, 1))
    assert emitted == ["commit"]
    assert recorder.activity()["records"] == []
    assert (
        recorder.preview(recorder.sequence)["checkpoint"]["schemas"]["Model"][
            "x"
        ]
        == 10
    )
    recorder.paused = True
    recorder.invalidate("Recording paused")
    recorder.commit(delta("x", 11, 2))
    assert emitted == ["commit", "commit"]
    assert recorder.preview(recorder.sequence)["available"] is False


def test_bounds_evict_complete_activity_and_advance_preview_baseline() -> None:
    config = DevToolsConfig(max_records=2, max_bytes=4096)
    recorder = Recorder(config, lambda *_: None)
    recorder.reset(baseline())
    for value in range(10):
        recorder.commit(delta("x", value, value + 1))
    assert len(recorder.records) <= 2
    assert len(recorder.deltas) <= 2
    assert recorder.preview(0)["available"] is False
    latest = recorder.preview(recorder.sequence)
    assert latest["checkpoint"]["schemas"]["Model"]["x"] == 9
    assert recorder.activity()["retained_bytes"] <= config.max_bytes
    assert recorder.activity()["dropped"] > 0
    recorder.capture("request", {"huge": "x" * 10_000})
    assert recorder.activity()["retained_bytes"] <= config.max_bytes


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "0.0.0.0"},
        {"port": -1},
        {"max_bytes": 0},
        {"max_sessions": True},
    ],
)
def test_listener_limits_validate_only_at_configuration(kwargs: Any) -> None:
    with pytest.raises(ValueError):
        DevToolsConfig(**kwargs)


def test_preview_applies_collection_membership_by_id() -> None:
    recorder = Recorder(DevToolsConfig(), lambda *_: None)
    recorder.reset(
        {
            "schemas": {"Rows": {"10": {"x": 10}, "2": {"x": 2}}},
            "schema_revisions": {"Rows": 0},
        }
    )
    recorder.commit(
        {
            "events": [
                {
                    "type": "insert",
                    "schema": "Rows",
                    "id": "1",
                    "new": {"x": 1},
                }
            ],
            "schema_revisions": {"Rows": 1},
        }
    )
    inserted = recorder.sequence
    assert recorder.preview(inserted)["checkpoint"]["schemas"]["Rows"] == {
        "10": {"x": 10},
        "2": {"x": 2},
        "1": {"x": 1},
    }
    recorder.commit(
        {
            "events": [{"type": "remove", "schema": "Rows", "id": "10"}],
            "schema_revisions": {"Rows": 2},
        }
    )
    result = recorder.preview(recorder.sequence)["checkpoint"]
    assert result["schemas"]["Rows"] == {"1": {"x": 1}, "2": {"x": 2}}
    assert "row_orders" not in result


@pytest.mark.parametrize("row_id", ["$integer", "$object", "$unsupported"])
def test_checkpoint_keeps_reserved_row_ids_structural(row_id: str) -> None:
    source = {
        "schemas": {"Rows": {row_id: {"value": {"$integer": 2**80}}}},
        "schema_revisions": {"Rows": 0},
    }
    result = encode_checkpoint(source, {"Rows": {"kind": "many"}}, 4096)
    assert set(result["schemas"]["Rows"]) == {row_id}
    assert from_wire(result["schemas"]["Rows"][row_id]["value"]) == {
        "$integer": 2**80
    }
    assert source["schemas"]["Rows"][row_id]["value"] == {"$integer": 2**80}
    assert encoded_size(result) <= 4096
    with pytest.raises(CaptureTooLarge):
        encode_checkpoint(source, {"Rows": {"kind": "many"}}, 50)


@pytest.mark.parametrize(
    ("filters", "accepted"),
    [
        ({"client_id": "client", "identity": "transaction"}, True),
        ({"identity": "capture"}, True),
        ({"identity": "record"}, True),
        ({"schema": "Model", "category": "commit", "status": "ok"}, True),
        ({"client_id": "", "identity": ""}, True),
        ({"schema": "Mod"}, False),
        ({"category": "com"}, False),
        ({"status": "o"}, False),
        ({"identity": "missing"}, False),
        ({"client_id": "missing"}, False),
    ],
)
def test_capture_matches_display_filter_rules(
    filters: dict[str, str], accepted: bool
) -> None:
    recorder = Recorder(DevToolsConfig(), lambda *_: None)
    recorder.filters = filters
    assert (
        recorder.accepts(
            {
                "id": "record-123",
                "client_id": "client-123",
                "transaction_id": "transaction-123",
                "capture_id": "capture-123",
                "schemas": ["Model"],
                "category": "commit",
                "status": "ok",
            }
        )
        is accepted
    )


def test_commit_activity_preserves_encoded_values_once() -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    recorder = Recorder(DevToolsConfig(), lambda *args: published.append(args))
    recorder.reset(baseline())
    payload = delta("x", 2**80, 1)
    encoded_commit = to_wire(payload)
    recorder.commit(encoded_commit)
    commit = next(value for kind, value in published if kind == "commit")
    activity = recorder.activity()["records"][-1]
    assert activity["detail"] == commit
    assert activity["detail"]["events"][0]["new"] == {"$integer": str(2**80)}
    preview = recorder.preview(recorder.sequence)
    assert preview["checkpoint"]["schemas"]["Model"]["x"] == {
        "$integer": str(2**80)
    }
