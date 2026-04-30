from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..node import Node


class Subscription:
    """
    Opaque subscription handle.

    Returned by Node.subscribe().

    The only public API:
        - dispose()

    Developers must not interact with tokens directly.
    """

    __slots__ = ("_node", "_token", "_active")

    def __init__(self, node: Node, token: str) -> None:
        self._node = node
        self._token = token
        self._active = True

    # ============================================================
    # Public API
    # ============================================================

    def dispose(self) -> None:
        """
        Unsubscribe this listener.

        Safe to call multiple times.
        """
        if not self._active:
            return

        self._node.unsubscribe(self._token)
        self._active = False

    # ============================================================
    # Introspection
    # ============================================================

    @property
    def active(self) -> bool:
        return self._active

    def __repr__(self) -> str:
        return f"<Subscription active={self._active}>"
