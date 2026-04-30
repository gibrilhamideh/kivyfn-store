from __future__ import annotations
from typing import Any, Literal, TYPE_CHECKING


from kivyfn.store.core import Engine
from kivyfn.store.core.transaction import Transaction
from kivyfn.store.utils.visulization.text import render_state, render_structure


if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.node import Node
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.events.subscription import Subscription
    from kivyfn.store.constants import InstructionOpCode


class Store:
    def __init__(self) -> None:
        self._engine: Engine | None = None

    # -------------------------------------------------
    # Boot
    # -------------------------------------------------

    def configure(self, root: Node, config: StoreConfig):
        if self._engine is not None:
            raise RuntimeError("Store is already configured")

        self._engine = Engine(root, config)

    def boot(self):
        assert self._engine, "Store must be configured before use"
        self._engine.boot()

    def transaction(self) -> Transaction:
        assert self._engine, "Store must be configured before use"
        return Transaction(self._engine)

    def undo(self):
        assert self._engine, "Store must be configured before use"
        self._engine.undo()

    def redo(self):
        assert self._engine, "Store must be configured before use"
        self._engine.redo()

    def subscribe(
        self,
        address: Address,
        callback,
        *,
        opcode: InstructionOpCode | None = None,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
    ) -> Subscription:
        """
        Public reactive binding API.

        Developers bind to a specific node via address.
        """

        assert self._engine, "Store must be configured before use"
        node = self._engine.resolve(address)

        return node.subscribe(
            callback,
            opcode=opcode,
            id=id,
            key=key,
            depth=depth,
            resolve_foreignkeys=resolve_foreignkeys,
        )

    # -------------------------------------------------
    # Read APIs
    # -------------------------------------------------
    def snapshot(
        self, address: Address, id=None, key=None, *, resolve_foreignkeys: bool = False
    ) -> Any:
        """
        Return a deep-copied snapshot of the current state.

        - If address is None:
            returns the entire store state.
        - If address is provided:
            returns the state rooted at that address.

        This method MUST NOT mutate store or node data.
        The returned value is safe for application use.
        """
        assert self._engine, "Store must be configured before use"
        node = self._engine.resolve(address)

        return node.snapshot(id=id, key=key, resolve_foreignkeys=resolve_foreignkeys)

    # -------------------------------------------------
    # Visualization APIs (structure, not data)
    # -------------------------------------------------
    def view(
        self,
        mode: Literal["state", "structure"] = "state",
        *,
        start: int = 0,
        depth: int | None = None,
    ):
        """
        Visualize the current tree (developer-only).

        mode:
            - 'data'      : JSON-like view of values
            - 'structure' : node + schema + cardinality

        start:
            Logical starting depth (default 0)

        depth:
            Max depth to render (None = unlimited)
        """
        assert self._engine, "Store must be configured before use"
        root = self._engine.root

        if mode == "state":
            render_state(root, start=start, depth=depth)
        elif mode == "structure":
            render_structure(root, start=start, depth=depth)
        else:
            raise ValueError(f"Unknown view mode: {mode!r}")
