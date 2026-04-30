from __future__ import annotations
from kivy.properties import DictProperty

from ..binder import FnBinder


class FnStateBehavior:
    binders: dict[str, FnBinder] = DictProperty()
    """
    Dictionary of binders attached to this widget.

    Binders are reactive helpers declared in kv that subscribe to a store
    address and update the widget when the state changes.

    Each binder may optionally define a ``name``. If a name is provided,
    the binder will be stored in this dictionary and can later be accessed
    or disposed programmatically.

    Example::

        FnLabel:
            FnBinder:
                name: "email"
                field: "text"
                address: "user.email"

    Accessing a binder::

        widget.binders["email"].dispose()

    If a binder does not define a ``name``, it will not be stored in
    :attr:`binders`.

    :attr:`binders` is a :class:`~kivy.properties.DictProperty`
    and defaults to ``{}``.
    """

    def add_widget(self, widget, index=0, canvas=None):
        if isinstance(widget, FnBinder):
            widget.owner = self
            return

        return super().add_widget(widget, index, canvas)  # pyright: ignore

    def _register_binder(self, name: str | None, binder: FnBinder):
        if name is None:
            return

        if name in self.binders:
            raise ValueError(f"Binder '{name}' already exists in {self}")

        self.binders[name] = binder
