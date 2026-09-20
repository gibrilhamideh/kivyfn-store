"""Keyed membership and the order of reactive publication phases."""

from __future__ import annotations

from typing import Any, cast

import pytest
from kivy.event import EventDispatcher
from kivy.lang import Builder
from kivy.properties import NumericProperty, ObjectProperty

from kivyfn.store.state.template import state_template
from kivyfn.store.client.subscription import SubscriptionUpdate
from kivyfn.store.protocol.message.event import (
    InsertEventAction,
    RemoveEventAction,
    SetRowEventAction,
    SignalRowEventAction,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    QuerySubscriptionResponse,
)
from kivyfn.store.query import Query
from kivyfn.store.query.result import QueryRow
from tests.state.helpers import StateUnitRows, manager_pair
from tests.state.test_templates import TemplateRow


class MembershipBindingProbe(EventDispatcher):
    """Receive KV property bindings without creating a widget or window."""

    collection = ObjectProperty(None)
    row_count = NumericProperty(0)


@pytest.mark.parametrize("query", [False, True])
def test_rows_bindings_observe_membership_after_reading_live_mapping(query):
    manager, _ = manager_pair()
    source = Query(StateUnitRows) if query else StateUnitRows
    rows = manager.create(state_template(source, state_type=TemplateRow))
    view = rows.rows
    observed = []
    rows.bind(rows=lambda _, value: observed.append(set(value)))
    probe: Any = MembershipBindingProbe(collection=rows)
    filename = "test-membership-binding.kv"
    Builder.load_string(
        "<MembershipBindingProbe>:\n"
        "    row_count: len(self.collection.rows)\n",
        filename=filename,
    )
    try:
        Builder.apply_rules(probe, "MembershipBindingProbe")
        for action, expected in (
            (InsertEventAction("a", (("value", 3),)), {"a"}),
            (InsertEventAction("b", (("value", 4),)), {"a", "b"}),
            (RemoveEventAction("a"), {"b"}),
            (InsertEventAction("a", (("value", 5),)), {"a", "b"}),
        ):
            manager.prepare_subscription_updates(
                (SubscriptionUpdate(source, (action,)),)
            )()
            assert set(rows.rows) == expected
            assert observed[-1] == expected
            assert probe.row_count == len(expected)
        assert len(observed) == rows._row_version == 4
        assert rows.rows is view
        with pytest.raises(TypeError):
            cast(Any, view)["c"] = rows["a"]
    finally:
        Builder.unbind_widget(probe.uid)
        Builder.unload_file(filename)


@pytest.mark.parametrize("query", [False, True])
def test_reordered_membership_is_silent_and_preserves_local_row_identity(
    query,
):
    manager, _ = manager_pair()
    source = Query(StateUnitRows) if query else StateUnitRows
    rows = manager.create(state_template(source, state_type=TemplateRow))
    rows.apply_snapshot(tuple(QueryRow(key, {}) for key in ("a", "b", "c")))
    first = rows["a"]
    first.selected = True
    changes = []
    membership = []
    rows.bind(rows=lambda _, value: changes.append(set(value)))
    rows.bind(on_enter=lambda *args: membership.append(args))
    rows.bind(on_leave=lambda *args: membership.append(args))

    version = rows._row_version
    rows.replace_canonical({key: rows[key] for key in ("c", "a", "b")})
    assert not changes and not membership
    assert rows._row_version == version
    assert set(rows) == {"a", "b", "c"}
    assert rows["a"] is first and first.selected


@pytest.mark.parametrize("query", [False, True])
def test_canonical_values_and_final_membership_precede_every_callback(query):
    manager, _ = manager_pair()
    source = Query(StateUnitRows) if query else StateUnitRows
    rows = manager.create(source)
    rows.apply_snapshot(tuple(QueryRow(key, {}) for key in ("a", "b")))
    observed = []

    def record(kind):
        observed.append((kind, set(rows), rows["a"].get("value")))

    rows["a"].bind(value=lambda *_: record("value"))
    rows.bind(rows=lambda *_: record("rows"))
    rows["a"].bind(on_ping=lambda *_: record("signal"))
    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                source,
                (
                    SignalRowEventAction("a", "ping", occurrence_id=0),
                    SetRowEventAction("a", (("value", 9),)),
                    RemoveEventAction("b"),
                    InsertEventAction("c", (("value", 8),)),
                ),
            ),
        )
    )()

    assert [kind for kind, _, _ in observed] == [
        "value",
        "rows",
        "rows",
        "signal",
    ]
    assert all(ids == {"a", "c"} for _, ids, _ in observed)
    assert all(value == 9 for _, _, value in observed)
    assert rows["c"].get("value") == 8


@pytest.mark.parametrize("query", [False, True])
def test_fresh_snapshot_notifies_new_membership_after_all_values(query):
    manager, _ = manager_pair()
    source = Query(StateUnitRows) if query else StateUnitRows
    rows = manager.create(source)
    rows.enter("a")
    first = rows["a"]
    version = rows._row_version
    observed = []
    rows.bind(
        rows=lambda _, value: observed.append(
            {row_id: state.get("value") for row_id, state in value.items()}
        )
    )
    data = (QueryRow("b", {"value": 7}), QueryRow("a", {"value": 6}))
    if query:
        response = QuerySubscriptionResponse("rows", "store", {}, data)
    else:
        response = CollectionSubscriptionResponse("rows", "store", 1, data)
    manager.on_subscription_snapshot(source, response)
    assert observed == [{"a": 6, "b": 7}]
    assert rows["a"] is first
    manager.on_subscription_snapshot(source, response)
    assert len(observed) == 1
    assert rows._row_version == version + 1
    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                source, (SetRowEventAction("a", (("value", 9),)),)
            ),
        )
    )()
    assert rows["a"].get("value") == 9
    assert len(observed) == 1
    assert rows._row_version == version + 1


@pytest.mark.parametrize("query", [False, True])
def test_raising_rows_callback_preserves_events_and_future_publication(query):
    manager, _ = manager_pair()
    rows = manager.create(Query(StateUnitRows) if query else StateUnitRows)
    entered = []

    def broken(*_):
        raise RuntimeError("rows callback failed")

    rows.bind(rows=broken)
    rows.bind(on_enter=lambda _, row_id, __: entered.append(row_id))
    rows.enter("a")
    assert entered == ["a"]
    assert rows._row_version == 1
    rows.unbind(rows=broken)
    observed = []
    rows.bind(rows=lambda _, value: observed.append(set(value)))
    rows.enter("b")
    assert observed == [{"a", "b"}]
    assert entered == ["a", "b"]
    assert rows._row_version == 2


@pytest.mark.asyncio
async def test_hidden_canonical_change_waits_for_membership_rollback():
    manager, client = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.apply_snapshot(tuple(QueryRow(key, {}) for key in ("a", "b", "c")))
    hidden = rows["b"]
    observed = []
    rows.bind(rows=lambda _, value: observed.append(set(value)))
    with manager.transaction() as transaction:
        del rows["b"]
        rows["pending"] = {"value": 7}
    version = rows._row_version
    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                StateUnitRows, (SetRowEventAction("b", (("value", 5),)),)
            ),
        )
    )()
    assert rows._row_version == version
    assert set(rows) == {"a", "c", "pending"}
    await client.fail(transaction)
    assert set(rows) == {"a", "b", "c"}
    assert observed[-1] == {"a", "b", "c"}
    assert rows["b"] is hidden and hidden.get("value") == 5


@pytest.mark.asyncio
async def test_pending_reinsert_preserves_identity_over_foreign_value_update():
    manager, client = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.apply_snapshot(tuple(QueryRow(key, {}) for key in ("a", "b", "c")))
    row = rows["a"]
    with manager.transaction() as transaction:
        del rows["a"]
        rows["a"] = {"value": 8}
    assert set(rows) == {"a", "b", "c"}
    assert rows["a"] is row
    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                StateUnitRows, (SetRowEventAction("a", (("value", 5),)),)
            ),
        )
    )()
    assert rows["a"].get("value") == 8
    await client.fail(transaction)
    assert set(rows) == {"a", "b", "c"}
    assert rows["a"] is row and row.get("value") == 5
