from __future__ import annotations

from typing import Any

from kivyfn.store.schema import ManySchema


def render_state(
    node: Any, *, start: int = 0, depth: int | None = None, _level: int = 0
) -> None:
    if depth is not None and _level > depth:
        return

    if _level < start:
        for child in node.children.values():
            render_state(child, start=start, depth=depth, _level=_level + 1)

        return

    indent = "  " * (_level - start)

    print(f"{indent}value: {node.value!r}")

    if node.children:
        print(f"{indent}children:")

        for name, child in node.children.items():
            child_indent = "  " * (_level - start + 1)

            print(f"{child_indent}{name}:")

            render_state(child, start=start, depth=depth, _level=_level + 2)


def render_structure(
    node: Any, *, start: int = 0, depth: int | None = None, _level: int = 0
) -> None:
    if depth is not None and _level > depth:
        return

    prefix = "  " * (_level - start) if _level >= start else ""

    schema_name = node.schema.name

    if _level >= start:
        schema_kind = (
            "many" if isinstance(node.schema, ManySchema) else "single"
        )

        print((f"{prefix}- Node(schema={schema_name}, kind={schema_kind})"))

    for name in node.schema.fields():
        if _level + 1 >= start:
            print(f"{prefix}  field {name!r}:")

    for name, child in node.children.items():
        if _level + 1 >= start:
            print(f"{prefix}  child {name!r}:")

        render_structure(child, start=start, depth=depth, _level=_level + 1)
