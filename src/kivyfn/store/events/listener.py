from __future__ import annotations
from typing import TYPE_CHECKING

from dataclasses import dataclass
from collections import defaultdict

from kivyfn.store.constants import InstructionOpCode

if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.events.event import NodeEvent
    from kivyfn.store.node import Node


class ListenerIndex:
    def __init__(self):
        self.set_all: set[Listener] = set()
        self.set_by_key: dict[str, set[Listener]] = defaultdict(set)

        self.insert_all: set[Listener] = set()
        self.remove_all: set[Listener] = set()

        self.setrow_all: set[Listener] = set()
        self.setrow_by_id: dict[str, set[Listener]] = defaultdict(set)
        self.setrow_by_key: dict[str, set[Listener]] = defaultdict(set)
        self.setrow_by_id_key: dict[tuple[str, str], set[Listener]] = defaultdict(set)

        self._reverse_lookup: dict[Listener, set[Listener]] = {}

    def add(self, listener: Listener) -> None:
        if listener in self._reverse_lookup:
            return

        target_bucket: set[Listener] | None = None

        if listener.opcode is InstructionOpCode.SET:
            if listener.key is not None:
                target_bucket = self.set_by_key[listener.key]
            else:
                target_bucket = self.set_all

        elif listener.opcode is InstructionOpCode.INSERT:
            target_bucket = self.insert_all

        elif listener.opcode is InstructionOpCode.REMOVE:
            target_bucket = self.remove_all

        elif listener.opcode is InstructionOpCode.SETROW:
            if listener.id is not None and listener.key is not None:
                target_bucket = self.setrow_by_id_key[(listener.id, listener.key)]
            elif listener.id is not None:
                target_bucket = self.setrow_by_id[listener.id]
            elif listener.key is not None:
                target_bucket = self.setrow_by_key[listener.key]
            else:
                target_bucket = self.setrow_all

        if target_bucket is None:
            raise ValueError(f"Unsupported listener opcode: {listener.opcode!r}")

        target_bucket.add(listener)
        self._reverse_lookup[listener] = target_bucket

    def remove(self, listener: Listener) -> None:
        bucket = self._reverse_lookup.pop(listener, None)
        if bucket is not None:
            bucket.discard(listener)

    def listeners(self, event: NodeEvent) -> set[Listener]:
        result: set[Listener] = set()

        if event.opcode is InstructionOpCode.SET:
            result.update(self.set_all)

            if event.key is not None:
                result.update(self.set_by_key.get(event.key, ()))

        elif event.opcode is InstructionOpCode.INSERT:
            result.update(self.insert_all)

        elif event.opcode is InstructionOpCode.REMOVE:
            result.update(self.remove_all)

        elif event.opcode is InstructionOpCode.SETROW:
            result.update(self.setrow_all)

            if event.id is not None:
                result.update(self.setrow_by_id.get(event.id, ()))

            if event.key is not None:
                result.update(self.setrow_by_key.get(event.key, ()))

            if event.id is not None and event.key is not None:
                result.update(self.setrow_by_id_key.get((event.id, event.key), ()))

        return result


@dataclass(slots=True, frozen=True)
class Listener:
    address: Address
    opcode: InstructionOpCode | None = None
    id: str | None = None
    key: str | None = None
    resolve_foreignkeys: bool = False
    depth: int | None = 0

    @property
    def room(self) -> str:
        return f"room:{self.address}:{self.opcode}:{self.id}:{self.key}:{self.resolve_foreignkeys}:{self.depth}"


class ForeignListener:
    def __init__(
        self,
        owner: Node,
        foreignkey: str,
        subscriber_id: str | None,
        listener: Listener,
    ):
        self.owner = owner
        self.foreignkey = foreignkey
        self.subscriber_id = subscriber_id
        self.listener = listener

    def resolve(self, event: NodeEvent) -> list[NodeEvent]:
        matching_ids = self.owner.lookup(self.foreignkey, event.id)
        if not matching_ids:
            return []

        ids_to_notify = (
            [matching_ids] if isinstance(matching_ids, str) else matching_ids
        )

        translated_events: list[NodeEvent] = []

        for base_id in ids_to_notify:
            if self.subscriber_id is not None and base_id != self.subscriber_id:
                continue

            translated = event.copy_with(
                address=self.owner.address,
                id=base_id,
            )

            if self.listener.matches(translated):
                translated_events.append(translated)

        return translated_events
