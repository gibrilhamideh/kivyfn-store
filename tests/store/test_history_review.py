from __future__ import annotations

from math import isnan
from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.builder import HistoryConfig
from kivyfn.store.protocol.message.requests import (
    RemoveRequestAction,
    SignalRequestAction,
)
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.fields import DataField, NumericField
from tests.core.helpers import insert, setrow, setvalue
from tests.store.test_history import (
    Root,
    Telemetry,
    UntrackedRows,
    execute,
    make,
    redo,
    undo,
)


class PayloadRows(ManySchema):
    track_history = True
    value = NumericField(default=0)
    payload = DataField[Any](default={})


@pytest.mark.parametrize("edit_other_payload", [False, True])
def test_untouched_cyclic_row_field_does_not_clear_history(
    tmp_path: Path, edit_other_payload: bool
) -> None:
    store = make(tmp_path, PayloadRows)
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    try:
        assert execute(
            store,
            insert(PayloadRows, "cyclic", payload=cycle),
            insert(PayloadRows, "ordinary"),
        ).success
        assert execute(store, setvalue(Root, value=1)).success
        previous = store.history()
        actions = [setrow(PayloadRows, "cyclic", value=7)]
        if edit_other_payload:
            actions.append(setrow(PayloadRows, "ordinary", payload=[9]))
        assert execute(store, *actions).success
        page = store.history()
        assert page.status.barrier == previous.status.barrier
        assert len(page.entries) == 2
        assert page.entries[0] == previous.entries[0]
        fields = {(item.row_id, item.key) for item in page.entries[1].records}
        expected = {("cyclic", "value")}
        if edit_other_payload:
            expected.add(("ordinary", "payload"))
        assert fields == expected
        assert undo(store).success
        rows = store.snapshot_by_schema("PayloadRows")
        assert rows["cyclic"]["value"] == 0
        assert rows["cyclic"]["payload"]["self"] is rows["cyclic"]["payload"]
        assert rows["ordinary"] == {"value": 0, "payload": {}}
        assert undo(store).success
        assert store.snapshot_by_schema("Root", key="value") == 0
        assert redo(store).success
        assert redo(store).success
        rows = store.snapshot_by_schema("PayloadRows")
        assert rows["cyclic"]["value"] == 7
        assert rows["cyclic"]["payload"]["self"] is rows["cyclic"]["payload"]
        assert rows["ordinary"]["payload"] == (
            [9] if edit_other_payload else {}
        )
    finally:
        store.shutdown()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("signal", [False, True])
def test_nan_creates_existing_history_barrier_after_successful_mutation(
    tmp_path: Path, nested: bool, signal: bool
) -> None:
    store = make(tmp_path)
    payload = {"samples": [float("nan")]} if nested else float("nan")
    try:
        assert execute(store, setvalue(Root, value=1)).success
        action = (
            SignalRequestAction("Root", "opened", payload)
            if signal
            else setvalue(Root, payload=payload)
        )
        result = execute(store, action)
        assert result.success and result.commit is not None
        retained = (
            result.commit.signals[0].data
            if signal
            else store.snapshot_by_schema("Root", key="payload")
        )
        assert isnan(retained["samples"][0] if nested else retained)
        status = store.history().status
        assert not status.can_undo and not status.can_redo
        assert status.barrier is not None and "NaN" in status.barrier
        assert store.history().entries == ()
        assert execute(store, setvalue(Root, value=2)).success
        assert undo(store).success
        assert store.snapshot_by_schema("Root", key="value") == 1
        if not signal:
            # Replacing an unsupported before-value is also a barrier. Once
            # it is gone, later changes can retain an ordinary usable entry.
            assert execute(store, setvalue(Root, payload={})).success
            assert not store.history().status.can_undo
            assert execute(
                store, setvalue(Root, payload={"finite": 1})
            ).success
            assert undo(store).success
            assert store.snapshot_by_schema("Root", key="payload") == {}
    finally:
        store.shutdown()


@pytest.mark.parametrize("recreate", [False, True])
def test_structural_row_change_still_requires_restorable_before_values(
    tmp_path: Path, recreate: bool
) -> None:
    store = make(tmp_path, PayloadRows)
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    try:
        assert execute(store, insert(PayloadRows, "a", payload=cycle)).success
        assert execute(store, setvalue(Root, value=1)).success
        assert store.history().status.can_undo
        actions: list[Any] = [RemoveRequestAction("PayloadRows", "a")]
        if recreate:
            actions.append(insert(PayloadRows, "a", value=7))
        assert execute(store, *actions).success
        assert store.history().entries == ()
        assert store.history().status.barrier is not None
        assert store.snapshot_by_schema("Root", key="value") == 1
        assert store.snapshot_by_schema("PayloadRows") == (
            {"a": {"value": 7, "payload": {}}} if recreate else {}
        )
    finally:
        store.shutdown()


@pytest.mark.parametrize("signal", [False, True])
def test_cyclic_transient_value_keeps_mutation_and_bounds_history(
    tmp_path: Path, signal: bool
) -> None:
    store = make(tmp_path)
    payload = {}
    payload["self"] = payload
    action = (
        SignalRequestAction("Root", "opened", payload)
        if signal
        else setvalue(Root, payload=payload)
    )
    try:
        result = execute(store, action)
        assert result.success
        if signal:
            assert result.commit is not None
            retained = result.commit.signals[0].data
        else:
            retained = store.snapshot_by_schema("Root", key="payload")
        assert retained["self"] is retained
        history = store.history()
        assert not history.entries
        assert history.status.barrier is not None
        assert history.status.retained_bytes <= HistoryConfig().max_bytes
        # A retention limitation cannot poison later ordinary work, including
        # cloning a schema whose other field still contains this cycle.
        assert execute(store, setvalue(Root, value=1)).success
        assert store.snapshot_by_schema("Root", key="value") == 1
    finally:
        store.shutdown()


def test_group_growth_keeps_intervening_bookkeeping_inside_byte_limit(
    tmp_path: Path,
) -> None:
    group = {"origin_client_id": "owner", "history_group": "group"}
    payload = {"text": "x" * 2800}
    probe = make(tmp_path / "probe", Telemetry)
    try:
        assert execute(probe, setvalue(Root, value=1), **group).success
        assert execute(probe, setvalue(Telemetry, value=1)).success
        assert execute(probe, setvalue(Root, payload=payload), **group).success
        reference = probe.history()
        assert len(reference.entries) == 1
        entry = reference.entries[0]
        assert len(entry.transaction_ids) == 2
        assert reference.status.retained_bytes > entry.byte_size
        # Object sizes vary by Python version. Fit the merged entry itself,
        # but leave no room for its retained intervening bookkeeping.
        config = HistoryConfig(max_bytes=entry.byte_size)
    finally:
        probe.shutdown()

    store = make(tmp_path / "bounded", Telemetry, history=config)
    try:
        assert execute(store, setvalue(Root, value=1), **group).success
        assert execute(store, setvalue(Telemetry, value=1)).success
        assert store.history().status.retained_bytes < config.max_bytes
        assert execute(store, setvalue(Root, payload=payload), **group).success
        assert store.snapshot_by_schema("Root", key="payload") == payload
        assert store.history().status.retained_bytes <= config.max_bytes
        # The oversized merge becomes a complete standalone transaction;
        # it must not disappear through eviction with a dangling open ID.
        assert len(store.history().entries[-1].transaction_ids) == 1
        store.close_history_group("owner")
        assert undo(store).success
        assert store.snapshot_by_schema("Root", key="payload") == {}
        assert store.snapshot_by_schema("Root", key="value") == 1
        assert store.snapshot_by_schema("Telemetry", key="value") == 1
    finally:
        store.shutdown()


def test_group_retains_intervening_footprint_through_multiple_merges(
    tmp_path: Path,
) -> None:
    store = make(tmp_path, Telemetry)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    try:
        assert execute(store, setvalue(Root, value=1), **group).success
        assert execute(store, setvalue(Telemetry, value=7)).success
        for value in (2, 3):
            assert execute(store, setvalue(Root, value=value), **group).success
        assert len(store.history().entries) == 1
        assert execute(
            store,
            setvalue(Telemetry, value=8),
            setvalue(Root, value=4),
            **group,
        ).success
        assert len(store.history().entries) == 2
        assert undo(store).success
        assert store.snapshot_by_schema("Root", key="value") == 3
        assert store.snapshot_by_schema("Telemetry", key="value") == 7
        assert undo(store).success
        assert store.snapshot_by_schema("Root", key="value") == 0
        assert store.snapshot_by_schema("Telemetry", key="value") == 7
    finally:
        store.shutdown()


def test_group_can_span_independent_untracked_row_insertion(
    tmp_path: Path,
) -> None:
    store = make(tmp_path, UntrackedRows)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    try:
        assert execute(
            store,
            setvalue(Root, value=1),
            insert(UntrackedRows, "edited", value=1),
            **group,
        ).success
        assert execute(
            store, insert(UntrackedRows, "independent", value=2)
        ).success
        assert execute(
            store,
            setvalue(Root, value=2),
            setrow(UntrackedRows, "edited", other=7),
            **group,
        ).success
        entry = store.history().entries[0]
        assert len(store.history().entries) == 1
        assert len(entry.transaction_ids) == 2
        assert undo(store).success
        assert store.snapshot_by_schema("Root", key="value") == 0
        assert store.snapshot_by_schema("UntrackedRows") == {
            "independent": {"value": 2, "other": 0}
        }
        assert redo(store).success
        assert store.snapshot_by_schema("Root", key="value") == 2
        assert store.snapshot_by_schema("UntrackedRows") == {
            "edited": {"value": 1, "other": 7},
            "independent": {"value": 2, "other": 0},
        }
    finally:
        store.shutdown()
