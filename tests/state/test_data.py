from kivyfn.store.state.reactive.data import StateData


def test_values_and_snapshots_have_independent_mutable_ownership() -> None:
    original = {"items": [1]}
    data = StateData({"payload": original})
    original["items"].append(2)
    read = data.visible("payload")
    read["items"].append(3)
    assert data.canonical("payload") == {"items": [1]}
    overlay = {"items": [4]}
    data.set_overlay("tx", {"payload": overlay})
    overlay["items"].append(5)
    snapshot = data.visible_snapshot({"payload"})
    snapshot["payload"]["items"].append(6)
    assert data.visible("payload") == {"items": [4]}


def test_rollback_only_removes_matching_transaction_fields() -> None:
    data = StateData({"a": 1, "b": 2})
    data.set_overlay("first", {"a": 3, "b": 4})
    data.set_overlay("second", {"a": 5})
    assert data.discard_overlay("first", {"a"}) == frozenset()
    assert data.visible_snapshot({"a", "b"}) == {"a": 5, "b": 4}
    assert data.discard_overlay("second") == frozenset({"a"})
    assert data.visible("a") == 1
    assert data.discard_overlay("missing") == frozenset()


def test_hidden_canonical_update_survives_all_overlay_rollbacks() -> None:
    data = StateData({"value": 0})
    data.set_overlay("first", {"value": 1})
    data.set_overlay("second", {"value": 2})
    assert data.set_canonical({"value": 3}) == frozenset()
    assert data.discard_overlay("first") == frozenset()
    assert data.visible("value") == 2
    assert data.discard_overlay("second") == frozenset({"value"})
    assert data.visible("value") == 3


def test_unchanged_updates_do_not_report_changes() -> None:
    data = StateData({"value": [1]})
    assert data.set_canonical({"value": [1]}) == frozenset()
    assert data.set_overlay("tx", {"value": [1]}) == frozenset()
    assert data.discard_overlay("tx", {"unrelated"}) == frozenset()
    assert data.discard_overlay("tx") == frozenset()


def test_default_snapshots_cover_canonical_and_optimistic_fields():
    data = StateData({"value": 1, "payload": {"items": [2]}})
    data.set_overlay("tx", {"value": 3, "extra": 4})
    assert data.canonical_snapshot() == {"value": 1, "payload": {"items": [2]}}
    snapshot = data.visible_snapshot()
    assert snapshot == {"value": 3, "payload": {"items": [2]}, "extra": 4}
    snapshot["payload"]["items"].append(5)
    assert data.visible("payload") == {"items": [2]}
