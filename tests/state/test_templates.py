# tests/state/test_templates.py
from __future__ import annotations

import gc
import subprocess
import sys
from dataclasses import FrozenInstanceError
from typing import Any, Generic, TypeVar, cast
from weakref import ref

import pytest
from kivy.properties import AliasProperty, BooleanProperty, StringProperty

from kivyfn.store.state.template import StateTemplate, state_template
from kivyfn.store.client.subscription import SubscriptionUpdate
from kivyfn.store.error import StoreStateError
from kivyfn.store.protocol.message.event import (
    InsertEventAction,
    SetEventAction,
    SignalRowEventAction,
)
from kivyfn.store.query import Query, field
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.signal import Signal
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State
from kivyfn.store.typing import CollectionStateType, QueryStateType, StateType
from tests.state.helpers import StateUnitRows, StateUnitSingle, manager_pair
from tests.state.test_projection import StateUnitPost


class TemplateRow(StateType):
    label: str
    value: int | float
    payload: dict[str, Any]
    selected = BooleanProperty(False)

    def _get_caption(self) -> str:
        return f"{self.label}: {self.value}"

    caption = AliasProperty(_get_caption, None, bind=("label", "value"))

    def doubled(self) -> int | float:
        return self.value * 2


class OtherRow(TemplateRow):
    pass


class TemplateSingle(StateType):
    value: int | float
    other: int | float

    def total(self) -> int | float:
        return self.value + self.other


class TemplateCollection(CollectionStateType[TemplateRow]):
    def labels(self) -> tuple[str, ...]:
        return tuple(row.label for row in self.rows.values())


class TemplateQuery(QueryStateType[TemplateRow]):
    def total(self) -> int | float:
        return sum(row.value for row in self.rows.values())


@pytest.mark.parametrize("kind", ["single", "collection", "query"])
def test_templates_reuse_source_identity_and_release(kind: str) -> None:
    manager, client = manager_pair()
    if kind == "single":
        source = StateUnitSingle
        template = state_template(source, state_type=TemplateSingle)
        expected = TemplateSingle
    elif kind == "collection":
        source = StateUnitRows
        template = state_template(
            source, state_type=TemplateRow, collection_type=TemplateCollection
        )
        expected = TemplateCollection
    else:
        source = Query(StateUnitRows)
        template = state_template(
            source, state_type=TemplateRow, collection_type=TemplateQuery
        )
        expected = TemplateQuery
    target = manager.create(template)
    assert isinstance(target, expected)
    assert manager.create(source) is target
    equivalent = StateTemplate(
        template.source, template.state_type, template.collection_type
    )
    assert equivalent == template
    assert manager.create(equivalent) is target
    assert client.sources == {source}
    manager.on_subscription_status(source, True)
    manager.release(template)
    assert not target.synced
    assert not client.sources
    assert manager.create(template) is target
    manager.close()
    assert not client.sources


def test_template_is_immutable_and_creates_no_state_or_subscription() -> None:
    constructed = []

    class Custom(TemplateSingle):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append(self)
            super().__init__(*args, **kwargs)

    template = state_template(StateUnitSingle, state_type=Custom)
    manager, client = manager_pair()
    assert constructed == []
    assert not client.sources
    with pytest.raises(FrozenInstanceError):
        setattr(template, "state_type", TemplateSingle)
    state = manager.create(template)
    assert constructed == [state]


@pytest.mark.parametrize("query", [False, True])
def test_custom_rows_with_default_containers_and_all_accessors(query: bool):
    manager, _ = manager_pair()
    source = Query(StateUnitRows) if query else StateUnitRows
    template = state_template(source, state_type=TemplateRow)
    collection = manager.create(template)
    collection.enter("one")
    row = collection["one"]
    row.set_canonical({"label": "temperature", "value": 3})
    assert isinstance(row, TemplateRow)
    assert row.doubled() == 6
    assert row.schema.name == "StateUnitRows"
    assert row.row_id == "one"
    assert collection.get("missing") is None
    assert collection.get("one") is row
    assert collection.canonical_row("one") is row
    assert collection.values() == (row,)
    assert collection.items() == (("one", row),)
    assert tuple(collection) == ("one",)
    assert collection.rows["one"] is row
    with pytest.raises(TypeError):
        cast(Any, collection.rows)["other"] = row


@pytest.mark.parametrize("query", [False, True])
def test_local_kivy_properties_and_derived_bindings_stay_local(query: bool):
    manager, client = manager_pair()
    source = Query(StateUnitRows) if query else StateUnitRows
    rows = manager.create(state_template(source, state_type=TemplateRow))
    rows.enter("one")
    rows.enter("two")
    row = rows["one"]
    captions = []
    selected = []
    row.bind(caption=lambda _, value: captions.append(value))
    row.bind(selected=lambda _, value: selected.append(value))
    row.selected = True
    assert selected == [True]
    assert not rows["two"].selected
    assert not client.requests
    assert not row.is_pending
    row.set_canonical({"label": "Room", "value": 7})
    assert row.caption == "Room: 7"
    assert captions and all(value == "Room: 7" for value in captions)
    assert "selected" not in row.fields
    with pytest.raises(StoreStateError, match="Unknown State field"):
        row.set(selected=False)


@pytest.mark.parametrize("query", [False, True])
def test_factory_class_caches_isolate_custom_bases_between_managers(query):
    source = Query(StateUnitRows) if query else StateUnitRows
    first, _ = manager_pair()
    second, _ = manager_pair()
    third, _ = manager_pair()
    a = first.create(state_template(source, state_type=TemplateRow))
    b = second.create(state_template(source, state_type=OtherRow))
    c = third.create(state_template(source, state_type=TemplateRow))
    for collection in (a, b, c):
        collection.enter("one")
    assert type(a["one"]) is type(c["one"])
    assert type(a["one"]) is not type(b["one"])
    assert a["one"] is not c["one"]
    assert isinstance(b["one"], OtherRow)


@pytest.mark.parametrize("source", [StateUnitRows, Query(StateUnitRows)])
def test_default_first_rejects_later_row_customization_even_when_empty(source):
    manager, client = manager_pair()
    original = manager.create(source)
    with pytest.raises(StoreStateError, match="Conflicting state_type"):
        manager.create(state_template(source, state_type=TemplateRow))
    assert manager.create(source) is original
    assert client.sources == {source}


@pytest.mark.parametrize("source", [StateUnitRows, Query(StateUnitRows)])
def test_conflicting_explicit_row_type_is_rejected(source):
    manager, _ = manager_pair()
    original = manager.create(state_template(source, state_type=TemplateRow))
    with pytest.raises(StoreStateError, match="Conflicting state_type"):
        manager.create(state_template(source, state_type=OtherRow))
    assert manager.create(source) is original


def test_singleton_conflicting_types_are_rejected() -> None:
    manager, _ = manager_pair()
    manager.create(state_template(StateUnitSingle, state_type=TemplateSingle))
    with pytest.raises(StoreStateError, match="Conflicting state_type"):
        manager.create(state_template(StateUnitSingle, state_type=StateType))


@pytest.mark.parametrize("query", [False, True])
def test_conflicting_container_type_rejected_without_new_subscriptions(
    query: bool,
) -> None:
    manager, client = manager_pair()
    if query:

        class AlternativeQuery(TemplateQuery):
            pass

        source = Query(StateUnitRows)
        original = state_template(
            source, state_type=TemplateRow, collection_type=TemplateQuery
        )
        alternative = state_template(
            source, state_type=TemplateRow, collection_type=AlternativeQuery
        )
    else:

        class AlternativeCollection(TemplateCollection):
            pass

        source = StateUnitRows
        original = state_template(
            source, state_type=TemplateRow, collection_type=TemplateCollection
        )
        alternative = state_template(
            source,
            state_type=TemplateRow,
            collection_type=AlternativeCollection,
        )

    target = manager.create(original)
    with pytest.raises(StoreStateError, match="Conflicting collection_type"):
        manager.create(alternative)
    assert manager.create(original) is target
    assert client.sources == {source}


def test_filtered_queries_share_custom_rows_and_reject_type_conflicts():
    manager, client = manager_pair()
    first_query = Query(StateUnitRows).where(field("value") > 0)
    second_query = Query(StateUnitRows).where(field("value") > 3)
    first = manager.create(state_template(first_query, state_type=TemplateRow))
    with pytest.raises(StoreStateError, match="Conflicting state_type"):
        manager.create(state_template(second_query, state_type=OtherRow))
    assert client.sources == {first_query}
    second = manager.create(second_query)
    assert (
        manager.create(state_template(second_query, state_type=TemplateRow))
        is second
    )
    assert isinstance(second, QueryStateType)
    first.enter("shared")
    second.enter("shared")
    assert first["shared"] is second["shared"]
    assert isinstance(second["shared"], TemplateRow)
    first["shared"].selected = True
    assert second["shared"].selected


def test_plain_and_query_rows_have_separate_customization_ownership():
    manager, _ = manager_pair()
    plain = manager.create(state_template(StateUnitRows, state_type=OtherRow))
    query = manager.create(
        state_template(Query(StateUnitRows), state_type=TemplateRow)
    )
    plain.enter("one")
    query.enter("one")
    assert plain["one"] is not query["one"]
    assert isinstance(plain["one"], OtherRow)
    assert type(query["one"]) is not type(plain["one"])
    plain["one"].selected = True
    assert not query["one"].selected


@pytest.mark.parametrize("query", [False, True])
def test_release_keeps_type_choice_but_does_not_retain_instances(query):
    manager, _ = manager_pair()
    source = Query(StateUnitRows) if query else StateUnitRows
    template = state_template(source, state_type=TemplateRow)
    collection = manager.create(template)
    collection.enter("one")
    collection_ref = ref(collection)
    row_ref = ref(collection["one"])
    manager.release(template)
    del collection
    gc.collect()
    assert collection_ref() is None
    assert row_ref() is None
    recreated = manager.create(source)
    recreated.enter("one")
    assert isinstance(recreated["one"], TemplateRow)
    with pytest.raises(StoreStateError, match="Conflicting state_type"):
        manager.create(state_template(source, state_type=OtherRow))


@pytest.mark.parametrize("declaration", ["property", "method", "value"])
def test_custom_schema_field_collisions_fail_before_empty_subscription(
    declaration,
):
    member = {
        "property": StringProperty("local"),
        "method": lambda self: "local",
        "value": "local",
    }[declaration]
    custom = type("Collision", (TemplateRow,), {"label": member})
    manager, client = manager_pair()
    with pytest.raises(StoreStateError, match="label.*conflicts"):
        manager.create(state_template(StateUnitRows, state_type=custom))
    assert not client.sources
    assert (
        manager.create(state_template(StateUnitRows, state_type=TemplateRow))
        is not None
    )


@pytest.mark.parametrize("member", ["ping", "on_ping"])
def test_signal_emitters_and_nonmethod_handlers_cannot_be_shadowed(member):
    custom = type("SignalCollision", (TemplateRow,), {member: 3})
    manager, client = manager_pair()
    with pytest.raises(StoreStateError, match=member):
        manager.create(state_template(StateUnitRows, state_type=custom))
    assert not client.sources


@pytest.mark.parametrize("base", [CollectionStateType, QueryStateType])
def test_collection_signal_handler_must_be_callable(base):
    custom = type("InvalidSignal", (base,), {"on_ping": StringProperty("")})
    source = (
        StateUnitRows if base is CollectionStateType else Query(StateUnitRows)
    )
    manager, client = manager_pair()
    with pytest.raises(StoreStateError, match="on_ping.*method"):
        manager.create(state_template(source, collection_type=custom))
    assert not client.sources


def test_builtin_event_names_remain_reserved_for_signals():
    class BadSchema(ManySchema):
        enter = Signal()

    manager, client = manager_pair()
    with pytest.raises(StoreStateError, match="on_enter.*conflicts"):
        manager.create(BadSchema)
    assert not client.sources


def test_custom_projected_field_collision_fails_before_subscription():
    class Joined(StateType):
        author_label = StringProperty("local")

    manager, client = manager_pair()
    source = Query(StateUnitPost).left_join("author_id", as_="author")
    with pytest.raises(StoreStateError, match="author_label"):
        manager.create(state_template(source, state_type=Joined))
    assert not client.sources


def test_custom_events_and_inherited_signal_handlers_survive_generation():
    observed = []

    class Parent(TemplateRow):
        __events__ = ("on_local", "on_ping")

        def on_local(self, value: str) -> None:
            observed.append(("local", value))

        def on_ping(self, data: Any = None) -> None:
            observed.append(("row", data))

    class Child(Parent):
        __events__ = ("on_child",)

        def on_child(self) -> None:
            observed.append(("child", None))

    class Container(TemplateCollection):
        __events__ = ("on_local",)

        def on_local(self) -> None:
            observed.append(("container-local", None))

        def on_ping(self, row_id: str, data: Any = None) -> None:
            observed.append(("container", row_id, data))

    manager, _ = manager_pair()
    rows = manager.create(
        state_template(
            StateUnitRows, state_type=Child, collection_type=Container
        )
    )
    rows.enter("one")
    row = rows["one"]
    row.dispatch("on_local", "yes")
    row.dispatch("on_child")
    rows.dispatch("on_local")
    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                StateUnitRows, (SignalRowEventAction("one", "ping", 5),)
            ),
        )
    )()
    assert observed == [
        ("local", "yes"),
        ("child", None),
        ("container-local", None),
        ("row", 5),
        ("container", "one", 5),
    ]
    assert tuple(type(row).__events__).count("on_ping") == 1


def test_custom_enter_callbacks_observe_all_staged_targets_and_rows():
    observed = []
    manager, _ = manager_pair()
    single = manager.create(
        state_template(StateUnitSingle, state_type=TemplateSingle)
    )

    class Container(TemplateCollection):
        def on_enter(self, row_id: str, state: TemplateRow) -> None:
            observed.append(
                (row_id, state.value, self.labels(), single.total())
            )

    rows = manager.create(
        state_template(
            StateUnitRows, state_type=TemplateRow, collection_type=Container
        )
    )
    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                StateUnitRows,
                (
                    InsertEventAction("one", (("label", "A"), ("value", 4))),
                    InsertEventAction("two", (("label", "B"), ("value", 5))),
                ),
            ),
            SubscriptionUpdate(
                StateUnitSingle,
                (SetEventAction((("value", 8), ("other", 9))),),
            ),
        )
    )()
    assert rows.labels() == ("A", "B")
    assert observed == [("one", 4, ("A", "B"), 17), ("two", 5, ("A", "B"), 17)]


@pytest.mark.parametrize(
    "source", [StateUnitSingle, StateUnitRows, Query(StateUnitRows)]
)
def test_failing_custom_constructor_does_not_poison_configuration(source):
    class Broken(StateType):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("construction failed")

    class BrokenCollection(CollectionStateType[TemplateRow]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("construction failed")

    class BrokenQuery(QueryStateType[TemplateRow]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("construction failed")

    manager, client = manager_pair()
    if source is StateUnitSingle:
        template = state_template(source, state_type=Broken)
    elif isinstance(source, Query):
        template = state_template(
            source, state_type=TemplateRow, collection_type=BrokenQuery
        )
    else:
        template = state_template(
            source, state_type=TemplateRow, collection_type=BrokenCollection
        )
    with pytest.raises(RuntimeError, match="construction failed"):
        manager.create(template)
    assert not client.sources
    assert manager.create(source) is not None


def test_recursive_target_construction_has_clear_error_and_can_recover():
    manager, client = manager_pair()

    class Recursive(TemplateSingle):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            manager.create(StateUnitSingle)
            super().__init__(*args, **kwargs)

    with pytest.raises(StoreStateError, match="Recursive State creation"):
        manager.create(state_template(StateUnitSingle, state_type=Recursive))
    assert not client.sources
    assert manager.create(StateUnitSingle) is not None


@pytest.mark.parametrize(
    ("source", "kwargs", "message"),
    [
        (object, {}, "State source"),
        ("StateUnitRows", {}, "State source"),
        (StateUnitSingle, {"state_type": object}, "State subclass"),
        (StateUnitSingle, {"state_type": object()}, "State subclass"),
        (
            StateUnitSingle,
            {"collection_type": TemplateCollection},
            "SingleSchema",
        ),
        (
            StateUnitRows,
            {"collection_type": TemplateQuery},
            "CollectionStateType subclass",
        ),
        (
            Query(StateUnitRows),
            {"collection_type": TemplateCollection},
            "QueryStateType subclass",
        ),
        (
            StateUnitRows,
            {"collection_type": TemplateCollection},
            "requires state_type",
        ),
    ],
)
def test_template_validates_construction_boundary(source, kwargs, message):
    with pytest.raises(TypeError, match=message):
        state_template(source, **kwargs)


def test_inherited_generic_collection_row_contract_is_checked():
    RowT = TypeVar("RowT", bound=StateType)

    class GenericCollection(CollectionStateType[RowT], Generic[RowT]):
        pass

    class Concrete(GenericCollection[TemplateRow]):
        pass

    class Inherited(Concrete):
        pass

    with pytest.raises(TypeError, match="requires state_type.*TemplateRow"):
        state_template(
            StateUnitRows, state_type=TemplateSingle, collection_type=Inherited
        )
    template = state_template(
        StateUnitRows, state_type=OtherRow, collection_type=Inherited
    )
    manager, _ = manager_pair()
    rows = manager.create(template)
    rows.enter("one")
    assert isinstance(rows["one"], OtherRow)


def test_default_templates_preserve_original_target_kinds():
    manager, _ = manager_pair()
    assert isinstance(manager.create(state_template(StateUnitSingle)), State)
    assert isinstance(
        manager.create(state_template(StateUnitRows)), CollectionState
    )
    assert isinstance(
        manager.create(state_template(Query(StateUnitRows))),
        QueryCollectionState,
    )


def test_server_imports_do_not_load_reactive_kivy_classes():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from kivyfn.store.store.store import Store\n"
            "assert 'kivyfn.store.typing' not in sys.modules\n"
            "assert 'kivy.event' not in sys.modules\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_custom_container_must_use_the_public_row_typed_base():
    for source, container in (
        (StateUnitRows, CollectionState),
        (Query(StateUnitRows), QueryCollectionState),
    ):
        with pytest.raises(TypeError, match="subclass"):
            StateTemplate(source, TemplateRow, cast(Any, container))


def test_subclass_can_bind_fields_in_constructor_and_keeps_instance_identity():
    class BoundRow(TemplateRow):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.observed: list[str] = []
            self.bind(label=self._label_changed)

        def _label_changed(self, instance: BoundRow, label: str) -> None:
            assert instance is self
            self.observed.append(label)

    manager, _ = manager_pair()
    rows = manager.create(state_template(StateUnitRows, state_type=BoundRow))
    rows.enter("one")
    row = rows["one"]
    row.set_canonical({"label": "Changed"})
    rows.leave("one")
    rows.enter("one")
    assert rows["one"] is row
    assert row.observed == ["Changed"]


def test_query_templates_accept_equivalent_schema_class_and_name_sources():
    manager, client = manager_pair()
    first = state_template(Query(StateUnitRows), state_type=TemplateRow)
    second = state_template(Query("StateUnitRows"), state_type=TemplateRow)
    assert manager.create(first) is manager.create(second)
    assert len(client.sources) == 1


def test_different_query_shapes_can_use_different_custom_row_types():
    manager, _ = manager_pair()

    class Post(StateType):
        label: str

    class JoinedPost(Post):
        author_label: str | None

    plain_query = manager.create(
        state_template(Query(StateUnitPost), state_type=Post)
    )
    joined_query = manager.create(
        state_template(
            Query(StateUnitPost).left_join("author_id", as_="author"),
            state_type=JoinedPost,
        )
    )
    plain_query.enter("one")
    joined_query.enter("one")
    assert plain_query["one"] is not joined_query["one"]
    assert isinstance(joined_query["one"], JoinedPost)


def test_custom_singleton_local_properties_and_factory_cache_isolation():
    class SelectedSingle(TemplateSingle):
        selected = BooleanProperty(False)

    first, _ = manager_pair()
    second, _ = manager_pair()
    a = first.create(
        state_template(StateUnitSingle, state_type=SelectedSingle)
    )
    b = second.create(
        state_template(StateUnitSingle, state_type=TemplateSingle)
    )
    a.selected = True
    a.set_canonical({"value": 3, "other": 4})
    assert a.total() == 7
    assert b.total() == 0
    assert not isinstance(b, SelectedSingle)
    assert a.selected


@pytest.mark.parametrize("query", [False, True])
def test_constructor_can_inspect_membership_using_the_configured_row_factory(
    query,
):
    base = QueryStateType if query else CollectionStateType

    def initialize(self: Any, *args: Any, **kwargs: Any) -> None:
        base.__init__(self, *args, **kwargs)
        self.enter("one")
        assert isinstance(self["one"], TemplateRow)

    custom: Any = type("Custom", (base,), {"__init__": initialize})
    source = Query(StateUnitRows) if query else StateUnitRows
    manager, _ = manager_pair()
    target = manager.create(
        state_template(source, state_type=TemplateRow, collection_type=custom)
    )
    assert isinstance(target["one"], TemplateRow)


def test_reentrant_query_constructor_cannot_choose_conflicting_shared_rows():
    manager, client = manager_pair()
    outer_source = Query(StateUnitRows).where(field("value") > 0)
    inner_source = Query(StateUnitRows).where(field("value") > 4)

    class RecursiveQuery(QueryStateType[TemplateRow]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            manager.create(state_template(inner_source, state_type=OtherRow))

    template = state_template(
        outer_source, state_type=TemplateRow, collection_type=RecursiveQuery
    )
    with pytest.raises(StoreStateError, match="shared row identity"):
        manager.create(template)
    assert not client.sources
    result = manager.create(state_template(inner_source, state_type=OtherRow))
    result.enter("one")
    assert isinstance(result["one"], OtherRow)


@pytest.mark.parametrize("query", [False, True])
@pytest.mark.parametrize("preexisting", [False, True])
def test_failed_container_releases_only_rows_created_by_its_constructor(
    query: bool, preexisting: bool
) -> None:
    manager, client = manager_pair()
    base = QueryStateType if query else CollectionStateType
    source = Query(StateUnitRows) if query else StateUnitRows
    retained = []
    existing = None
    if preexisting:
        if query:
            sibling = manager.create(
                state_template(
                    Query(StateUnitRows).where(field("value") > 5),
                    state_type=TemplateRow,
                )
            )
            sibling.enter("existing")
            existing = sibling["existing"]
        else:
            existing = manager._registry.create_row(
                StateUnitRows, "existing", base_type=TemplateRow
            )
        existing.selected = True
    original_sources = set(client.sources)

    def initialize(self: Any, *args: Any, **kwargs: Any) -> None:
        base.__init__(self, *args, **kwargs)
        self.enter("new")
        retained.append(self["new"])
        if existing is not None:
            self.enter("existing")
            assert self["existing"] is existing
        raise RuntimeError("failed after creating rows")

    broken: Any = type("BrokenAfterEnter", (base,), {"__init__": initialize})
    with pytest.raises(RuntimeError, match="failed after creating rows"):
        manager.create(
            state_template(
                source, state_type=TemplateRow, collection_type=broken
            )
        )
    assert client.sources == original_sources
    target = manager.create(
        state_template(
            source, state_type=TemplateRow if preexisting else OtherRow
        )
    )
    target.enter("new")
    assert target["new"] is not retained[0]
    if preexisting:
        target.enter("existing")
        assert target["existing"] is existing
        assert existing is not None and existing.selected
    else:
        assert isinstance(target["new"], OtherRow)
