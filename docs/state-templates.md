# Custom states and typed templates

A template connects a schema or Query to client-side State classes. It is an
immutable definition: importing or creating a template creates no State,
transport, or subscription. The same template works with multiple managers.

`manager.create()` accepts a schema, Query, or `StateTemplate[T]`. A template
preserves the complete return type `T`, including custom methods and typed
rows. Customization arguments belong on `state_template()`, not `create()`.
`manager.release()` accepts the same template when its subscription is no
longer needed.

## Application layout

| Application path | Responsibility |
| --- | --- |
| `app/store/schemas/` | Shared data contracts and validation. |
| `app/store/states/` | Client-side fields, methods, properties, and handlers. |
| `app/store/templates/` | Reusable source-to-class definitions. |

Small applications can use individual modules instead of these directories.
Schemas should not import custom states or templates: server processes need
the shared data contract without depending on UI behavior.

## Define your states

Declare schema fields with annotations **without assigning defaults or Kivy
properties**. The factory adds the real Kivy properties from the schema.
Methods and additional Kivy properties on your subclass remain available.

```python
# app/store/states/component.py
from kivy.properties import AliasProperty, BooleanProperty

from kivyfn.store.typing import CollectionStateType, QueryStateType, StateType


class ComponentState(StateType):
    kind: str
    name: str
    selected = BooleanProperty(False)

    def _get_display_name(self) -> str:
        return self.name.strip() or "Unnamed component"

    display_name = AliasProperty(_get_display_name, None, bind=("name",))

    def is_temperature(self) -> bool:
        return self.kind == "temperature"


class ComponentsState(CollectionStateType[ComponentState]):
    def by_kind(self, kind: str) -> tuple[ComponentState, ...]:
        return tuple(row for row in self.rows.values() if row.kind == kind)

    def on_enter(self, row_id: str, state: ComponentState) -> None:
        # self.rows[row_id] and state both have ComponentState hints.
        pass

    def on_leave(self, row_id: str, state: ComponentState) -> None:
        # Release any bindings or resources you installed for this member.
        pass


class TemperatureComponentsState(QueryStateType[ComponentState]):
    def names(self) -> tuple[str, ...]:
        return tuple(row.name for row in self.rows.values())
```

`StateType` exposes the common State API and is not schema-generic.
`CollectionStateType[Row]` and `QueryStateType[Row]` specialize row access and
membership handlers. `.rows` is a read-only mapping; `.values()` and
`.items()` return tuples. The editable collection also types `on_insert`
and `on_remove`. Query membership remains read-only, while supported row
fields remain writable.

Type annotations must describe the schema's actual values. For example,
`NumericField` accepts `int | float`, and nullable fields need `| None`.
The library does not infer schema field names into Python types or validate
application annotations against schemas. `.set(**values)` still performs
runtime validation; Python does not statically check those field keywords.
External callbacks passed through Kivy's `.bind(**kwargs)` should declare
their parameter types explicitly.

## Define templates once

Assume `ComponentSchema` is a ManySchema with `kind` and `name` StringFields.

```python
# app/store/templates/components.py
from kivyfn.store import Query, field, state_template

from app.store.schemas.component import ComponentSchema
from app.store.states.component import (
    ComponentsState,
    ComponentState,
    TemperatureComponentsState,
)

components_template = state_template(
    ComponentSchema, state_type=ComponentState, collection_type=ComponentsState
)

temperature_template = state_template(
    Query(ComponentSchema).where(field("kind") == "temperature"),
    state_type=ComponentState,
    collection_type=TemperatureComponentsState,
)
```

Use those definitions wherever the state is needed:

```python
# Inside application code with an existing StateManager named manager.
from app.store.templates.components import (
    components_template,
    temperature_template,
)

components = manager.create(components_template)  # ComponentsState
component = components["sensor-1"]  # ComponentState
component.name = "House temperature"
component.selected = True

temperatures = manager.create(temperature_template)
print(temperatures.names())
```

Omit `collection_type` when only the row needs custom behavior:
`state_template(ComponentSchema, state_type=ComponentState)` returns a
`StateTemplate[CollectionStateType[ComponentState]]`. The equivalent Query
template returns a `StateTemplate[QueryStateType[ComponentState]]`.

For a SingleSchema, pass only `state_type`. The returned template describes
that custom singleton directly. Templates without custom classes preserve
the existing schema-based return annotations.

Custom containers must inherit `CollectionStateType[Row]` or
`QueryStateType[Row]`, respectively. Supply a `state_type` compatible with the
container's declared row class. Inherited generic container declarations are
supported; pass a concrete subclass as the runtime class argument.

## Identity and configuration

- In one manager, equivalent sources return the same retained target.
  Templates are construction definitions, not additional cache keys.
- Choose custom classes before creating that source or its rows. Conflicting
  explicit classes raise `StoreStateError`, even if a collection is empty.
- Calling `create(schema)` after `create(template)` reuses the custom object,
  but only using the typed template preserves its specific autocomplete.
- Queries with the same projection shape share row objects across filters.
  They must use the same custom row class; their containers can differ.
- Normal collection rows and query rows have separate identities. Different
  projection shapes also have separate row identities.
- Configuration belongs to the manager for its lifetime. `release()` removes
  the subscription without resetting the chosen classes. A retained target
  can be reused and refreshed; unreferenced targets and rows can be collected.
- Different managers own separate instances and may choose different classes.

## Local properties, events, and cleanup

A local property such as `selected` is not a schema field. Changing it uses
normal Kivy behavior, creates no Store request, and does not mark a transaction
pending. Store updates and reconnects do not overwrite it on a retained
instance. It is not persisted or synchronized between processes. Views that
share the same State instance also share its local properties.

Schema fields, generated signal methods, and framework names cannot be
replaced by local properties or methods. A collision raises at creation,
before subscribing. Annotation-only declarations of generated names are
allowed. Generated signal events preserve custom `on_signal` methods,
including inherited handlers; custom Kivy events remain registered.

Custom constructors must accept the inherited arguments and call
`super().__init__()`. Prefer the default constructor when possible. Install
bindings after the base initialization, and initialize attributes used by
initial property callbacks before calling it. Constructors run before
subscription synchronization, so perform data-dependent work in handlers or
after `synced` becomes true. Do not recursively create the same source or another new Query with the
same row shape from its constructor.

Membership and mutation handlers keep the existing publication ordering:
canonical values are staged before callbacks; row signal handlers run before
their collection handler. Callback exceptions are logged and framework
progress continues, though Kivy can stop later bindings on that same event.

`on_enter` and `on_leave` describe visible membership, including optimistic
changes and rollback. They are not general object lifecycle callbacks.
`release()` and disconnect change synchronization ownership; they do not
empty the collection or emit leave events for every row. Clean up external
bindings, tasks, and resources at the application lifetime that owns them.
There is no automatic async constructor or disposal hook.

`by_kind()` above computes a selection when called. To keep membership live
when a row's `kind` changes, use the Query template. Maintaining a local
selection through membership events alone would miss those field changes.

## Joined query fields

A Query maps root row IDs to one flattened State shape. Joined schemas are
not nested State objects. For a join aliased as `author`, declare fields such
as `author_nickname: str | None` on the query row class when the relationship
is nullable. Use that row class as the template's `state_type`.

Projected writes retain the existing canonical routing: changing
`author_nickname` targets the related author. A missing nullable relationship
cannot receive a projected write until it has a target. Custom methods can
call `.set()` normally; they do not need another request or routing layer.
