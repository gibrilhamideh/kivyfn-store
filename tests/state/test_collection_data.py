"""Membership storage without a manager, subscriptions, or Kivy objects."""

from __future__ import annotations

from typing import Any, cast

import pytest

from kivyfn.store.state.reactive.data import (
    BaseData,
    CollectionData,
    StateData,
)


@pytest.mark.parametrize("data_type", [StateData, CollectionData])
def test_shared_contract_preserves_canonical_updates_and_other_transactions(
    data_type,
):
    data: BaseData[Any, Any] = data_type({"a": 1, "b": 2})
    data.set_overlay("first", {"a": 3, "b": 4})
    data.set_overlay("second", {"a": 5})
    data.set_canonical({"a": 6})
    data.discard_overlay("first", (key for key in ("a",)))
    assert data.visible_snapshot() == {"a": 5, "b": 4}
    assert data.canonical_snapshot() == {"a": 6, "b": 2}
    data.discard_overlay("second")
    assert data.visible("a") == 6
    data.discard_overlay("first")
    assert data.visible_snapshot() == {"a": 6, "b": 2}


def test_collection_snapshots_copy_containers_and_keep_row_identity():
    a, b = object(), object()
    source = {"a": a}
    data = CollectionData(source)
    view = data.visible_view
    source.clear()
    assert data.canonical("a") is a
    changes = {"b": b}
    delta = data.set_overlay("insert", changes)
    changes.clear()
    assert delta.entered == (("b", b),)
    assert data.visible_view is view
    assert view == {"a": a, "b": b}
    snapshot = data.visible_snapshot()
    snapshot.clear()
    assert data.visible_snapshot() == {"a": a, "b": b}
    assert data.canonical_snapshot(("missing", "a")) == {"a": a}
    with pytest.raises(TypeError):
        cast(Any, view)["c"] = object()


def test_remote_delete_survives_overlapping_membership_rollbacks():
    a, b = object(), object()
    data = CollectionData({"a": a})
    data.set_overlay("remove", {"a": None})
    data.set_overlay("insert", {"a": a, "b": b})
    assert not data.set_canonical({"a": None}).changed
    assert data.visible("a") is a
    assert data.canonical("a") is None
    delta = data.discard_overlay("insert")
    assert delta.left == (("a", a), ("b", b))
    assert not data.discard_overlay("remove").changed
    assert data.visible_snapshot() == {}


def test_snapshot_replacement_preserves_overlays_and_surviving_order():
    a, b, c = object(), object(), object()
    data = CollectionData({"a": a, "b": b})
    view = data.visible_view
    assert not data.replace_canonical({"b": b, "a": a}).changed
    assert tuple(view) == ("a", "b")
    data.set_overlay("tx", {"a": None, "c": c})
    assert not data.replace_canonical({"b": b}).changed
    delta = data.discard_overlay("tx")
    assert delta.left == (("c", c),)
    assert delta.entered == ()
    assert view == {"b": b}


def test_membership_delta_notices_replaced_row_identity():
    old, new = object(), object()
    data = CollectionData({"a": old})
    delta = data.set_canonical({"a": new})
    assert delta.left == (("a", old),)
    assert delta.entered == (("a", new),)
    assert data.visible("a") is new


def test_updating_older_overlay_keeps_its_original_priority():
    original, older, newer = object(), object(), object()
    data = CollectionData({"a": original})
    data.set_overlay("first", {"a": older})
    data.set_overlay("second", {"a": newer})
    assert not data.set_overlay("first", {"a": None}).changed
    assert data.visible("a") is newer
    data.discard_overlay("second")
    assert data.visible("a") is None
    data.discard_overlay("first")
    assert data.visible("a") is original


def test_partial_discard_preserves_remaining_overlay_entries():
    a, b, c = object(), object(), object()
    data = CollectionData({"a": a, "b": b})
    data.set_overlay("tx", {"a": None, "b": None, "c": c})
    delta = data.discard_overlay("tx", {"a", "missing"})
    assert delta.entered == (("a", a),)
    assert data.visible_snapshot() == {"a": a, "c": c}
    assert not data.discard_overlay("missing").changed
    assert not data.discard_overlay("tx", ()).changed
    data.discard_overlay("tx")
    assert data.visible_snapshot() == {"a": a, "b": b}
