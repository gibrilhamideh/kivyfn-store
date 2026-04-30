from __future__ import annotations
from typing import TYPE_CHECKING

from kivy.event import EventDispatcher
from kivy.properties import StringProperty, ObjectProperty

from kivyfn.state import InstructionOpCode

from ...state import store

if TYPE_CHECKING:
    from ...state import Address
    from ...state.events.event import NodeEvent
    from ...state.events.subscription import Subscription

    from ..widget import FnWidget
    from ..behaviors.state import FnStateBehavior


class FnBinder(EventDispatcher):
    __events__ = ("on_kv_post",)

    # --- General
    owner: FnWidget | FnStateBehavior = ObjectProperty(None)
    name: str = StringProperty(None)

    # --- Subscription
    address: Address | None = ObjectProperty(None)
    id: str | None = StringProperty(None)
    key: str | None = StringProperty(None)
    opcode: InstructionOpCode | None = ObjectProperty(None)
    field: str = StringProperty("", allownone=True)
    callback = StringProperty(None, allownone=True)
    subscription: Subscription | None = ObjectProperty(None)

    _callback = None
    _kv_posted = False

    def __init__(self, **_) -> None:
        super().__init__()

    # ==================================== #
    # Overrides
    # ==================================== #
    def apply_class_lang_rules(self, **_):
        pass

    # ==================================== #
    # internal
    # ==================================== #
    def subscribe(self):
        if not self._kv_posted:
            return

        if self.subscription:
            self.subscription.dispose()

        assert self.address is not None, "failed to subscribe no address set"
        self.subscription = store.subscribe(
            self.address, self.on_event, opcode=self.opcode, id=self.id, key=self.key
        )

    # ==================================== #
    # Event
    # ==================================== #
    def on_kv_post(self, *_):
        self._kv_posted = True
        if self.address:
            self.subscribe()

    def on_name(self, *_):
        self.owner._register_binder(self.name, self)

    def on_field(self, *_):
        if self.field is None:
            return

        if hasattr(self.owner, self.field):
            return

        raise ValueError(
            f"FnBinder: '{self.field}' is not a valid property on {type(self.owner).__name__}"
        )

    def on_callback(self, _, callback: str | None):
        if callback is None:
            self._callback = None
            return

        func = getattr(self.owner, callback, None)
        if not callable(func):
            raise ValueError(
                f"Invalid FnBinder callback '{callback}'. "
                f"{type(self.owner).__name__} has no callable method with that name."
            )

        self._callback = func

    def on_address(self, *_):
        self.subscribe()

    def on_id(self, *_):
        self.subscribe()

    def on_key(self, *_):
        self.subscribe()

    def on_event(self, event: NodeEvent):
        if self.field is not None:
            setattr(self.owner, self.field, event.new_value)

        if callable(self._callback):
            self._callback(event)
