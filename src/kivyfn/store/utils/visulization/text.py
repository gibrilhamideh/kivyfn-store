def render_state(
    node,
    *,
    start: int = 0,
    depth: int | None = None,
    _level: int = 0,
):
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
            render_state(
                child,
                start=start,
                depth=depth,
                _level=_level + 2,  # <-- key fix
            )


def render_structure(
    node, *, start: int = 0, depth: int | None = None, _level: int = 0
):
    if depth is not None and _level > depth:
        return

    prefix = "  " * (_level - start) if _level >= start else ""
    schema_name = node.schema.__name__

    if _level >= start:
        print(f"{prefix}- Node(schema={schema_name}, many={node.many})")

    for name, field in node.schema.fields().items():
        if _level + 1 >= start:
            print(f"{prefix}  field '{name}':")

    for name, child in node.children.items():
        render_structure(
            child,
            start=start,
            depth=depth,
            _level=_level + 1,
        )
