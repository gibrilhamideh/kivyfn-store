# tests/state/typing_templates.py
# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Checked by the project's Pyright command, including expected errors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, assert_type

from kivyfn.store.state.template import StateTemplate, state_template
from kivyfn.store.query import Query
from kivyfn.store.schema import Schema
from kivyfn.store.state.manager import StateManager
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State
from kivyfn.store.state.transaction.transaction import StateTransaction
from kivyfn.store.typing import CollectionStateType, QueryStateType, StateType
from tests.state.helpers import StateUnitRows, StateUnitSingle
from tests.state.test_templates import (
    TemplateCollection,
    TemplateQuery,
    TemplateRow,
    TemplateSingle,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    class TypedCollection(CollectionStateType[TemplateRow]):
        def on_enter(self, row_id: str, state: TemplateRow) -> None:
            assert_type(state.label, str)
            assert_type(self.rows, Mapping[str, TemplateRow])
            assert_type(self.rows[row_id].doubled(), int | float)
            assert_type(self[row_id], TemplateRow)
            assert_type(self.get(row_id), TemplateRow | None)
            assert_type(self.values(), tuple[TemplateRow, ...])
            assert_type(self.items(), tuple[tuple[str, TemplateRow], ...])
            assert_type(self.canonical_row(row_id), TemplateRow | None)
            assert_type(self.insert("new", label="New"), StateTransaction)

        def on_leave(self, row_id, state) -> None:
            assert_type(state, TemplateRow)
            assert_type(row_id, str)

        def on_insert(self, row_id, state) -> None:
            assert_type(state, TemplateRow)

        def on_remove(self, row_id, state) -> None:
            assert_type(state, TemplateRow)

    class TypedQuery(QueryStateType[TemplateRow]):
        def on_enter(self, row_id, state) -> None:
            assert_type(state, TemplateRow)
            assert_type(self.rows[row_id], TemplateRow)
            assert_type(self.get(row_id), TemplateRow | None)
            assert_type(self.values(), tuple[TemplateRow, ...])
            assert_type(self.items(), tuple[tuple[str, TemplateRow], ...])
            assert_type(state.set(label="Updated"), StateTransaction)

        def on_leave(self, row_id, state) -> None:
            assert_type(state, TemplateRow)

    class Joined(StateType):
        label: str
        author_label: str | None

        def author_text(self) -> str:
            assert_type(self.schema, Schema)
            return self.author_label or "Unknown"

    def check_widened_template(
        manager: StateManager, template: StateTemplate[State[Any]]
    ) -> None:
        assert_type(manager.create(template), State[Any])

    def check_templates(manager: StateManager) -> None:
        single_template = state_template(
            StateUnitSingle, state_type=TemplateSingle
        )
        assert_type(single_template, StateTemplate[TemplateSingle])
        assert_type(manager.create(single_template), TemplateSingle)
        single = manager.create(single_template)
        assert_type(single.total(), int | float)
        assert_type(single.row_id, str | None)
        assert_type(single.set(value=1), StateTransaction)

        rows_template = state_template(StateUnitRows, state_type=TemplateRow)
        assert_type(
            rows_template, StateTemplate[CollectionStateType[TemplateRow]]
        )
        rows = manager.create(rows_template)
        assert_type(rows, CollectionStateType[TemplateRow])
        assert_type(rows["x"], TemplateRow)
        assert_type(rows["x"].label, str)
        rows["x"].label = "Updated"
        rows["x"].value = 25
        rows["x"].value = "wrong"  # type: ignore
        rows.rows["x"] = rows["x"]  # type: ignore
        _wrong: str = rows["x"].value  # type: ignore

        custom_template = state_template(
            StateUnitRows,
            state_type=TemplateRow,
            collection_type=TemplateCollection,
        )
        assert_type(custom_template, StateTemplate[TemplateCollection])
        custom = manager.create(custom_template)
        assert_type(custom, TemplateCollection)
        assert_type(custom.labels(), tuple[str, ...])
        assert_type(custom["x"], TemplateRow)

        source = Query(StateUnitRows)
        query_template = state_template(source, state_type=TemplateRow)
        assert_type(query_template, StateTemplate[QueryStateType[TemplateRow]])
        query = manager.create(query_template)
        assert_type(query, QueryStateType[TemplateRow])
        assert_type(query["x"].label, str)
        query.rows["x"] = query["x"]  # type: ignore
        assert_type(
            manager.create(state_template(source, state_type=Joined))["x"],
            Joined,
        )
        query_custom = state_template(
            source, state_type=TemplateRow, collection_type=TemplateQuery
        )
        assert_type(query_custom, StateTemplate[TemplateQuery])
        assert_type(manager.create(query_custom).total(), int | float)

        assert_type(manager.create(StateUnitSingle), State[StateUnitSingle])
        assert_type(
            manager.create(StateUnitRows), CollectionState[StateUnitRows]
        )
        assert_type(
            manager.create(source), QueryCollectionState[StateUnitRows]
        )
        assert_type(
            manager.create(state_template(StateUnitSingle)),
            State[StateUnitSingle],
        )
        assert_type(
            manager.create(state_template(StateUnitRows)),
            CollectionState[StateUnitRows],
        )
        assert_type(
            manager.create(state_template(source)),
            QueryCollectionState[StateUnitRows],
        )
        manager.create(StateUnitRows, state_type=TemplateRow)  # type: ignore
        state_template(
            StateUnitSingle,  # type: ignore
            collection_type=TemplateCollection,
        )
        check_widened_template(manager, single_template)
        manager.release(single_template)
        manager.release(rows_template)
        manager.release(query_custom)
