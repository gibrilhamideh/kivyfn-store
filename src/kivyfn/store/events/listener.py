from __future__ import annotations
from typing import Callable, Optional, Any, TYPE_CHECKING

import weakref
from dataclasses import dataclass

from kivyfn.store.constants import InstructionOpCode

if TYPE_CHECKING:
    from kivyfn.store.events.event import NodeEvent
    from kivyfn.store.node import Node


@dataclass(slots=True)
class Listener:
    """
    Internal reactive listener descriptor.

    Stores:
        - Weak reference to callback
        - Optional filtering constraints
    """

    _callback_ref: weakref.ReferenceType

    opcode: Optional[InstructionOpCode] = None
    id: Optional[str] = None
    key: Optional[str] = None

    resolve_foreignkeys: Optional[bool] = False

    # ============================================================
    # Construction
    # ============================================================

    @staticmethod
    def create(
        callback: Callable[[NodeEvent], Any],
        *,
        opcode: InstructionOpCode | None = None,
        id: str | None = None,
        key: str | None = None,
        resolve_foreignkeys: bool = False,
    ) -> Listener:
        """
        Create listener with proper weak reference handling.
        """

        # Bound method
        if hasattr(callback, "__self__") and callback.__self__ is not None:  # pyright: ignore
            ref = weakref.WeakMethod(callback)
        else:
            ref = weakref.ref(callback)

        return Listener(
            _callback_ref=ref,
            opcode=opcode,
            id=id,
            key=key,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    # ============================================================
    # Runtime
    # ============================================================

    def callback(self) -> Optional[Callable[[NodeEvent], Any]]:
        """
        Returns live callback or None if dead.
        """
        return self._callback_ref()

    def matches(self, event: NodeEvent) -> bool:
        """
        Fast filtering logic.
        """

        if self.opcode is not None and event.opcode is not self.opcode:
            return False

        if self.id is not None and event.id != self.id:
            return False

        if self.key is not None and event.key != self.key:
            return False

        return True


class ForeignListener:
    def __init__(
        self,
        owner: Node,
        foreignkey: str,
        callback: Callable,
        subscriber_id: str | None,
    ):
        self.owner = owner
        self.foreignkey = foreignkey

        # Use WeakMethod for bound methods (like self.insert on a Screen)
        # Use weakref.ref for plain functions
        if hasattr(callback, "__self__"):
            self._callback_ref = weakref.WeakMethod(callback)
        else:
            self._callback_ref = weakref.ref(callback)

        self.subscriber_id = subscriber_id

    def __call__(self, event: NodeEvent):
        # 1. Resolve the callback
        callback = self._callback_ref()
        if callback is None:
            return  # The UI/Screen is dead, ignore the event

        # 2. Perform translation
        matching_ids = self.owner.lookup(self.foreignkey, event.id)
        if not matching_ids:
            return

        ids_to_notify = (
            [matching_ids] if isinstance(matching_ids, str) else matching_ids
        )

        for base_id in ids_to_notify:
            if self.subscriber_id is not None and base_id != self.subscriber_id:
                continue

            translated = event.copy_with(address=self.owner.address(), id=base_id)
            callback(translated)
