from __future__ import annotations
from typing import TYPE_CHECKING

from dataclasses import dataclass
from collections import defaultdict

from kivyfn.store.constants import InstructionOpCode

if TYPE_CHECKING:
    from kivyfn.store.address import Address
    from kivyfn.store.events.event import StoreEvent
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

        self._reverse_lookup: dict[
            Listener,
            tuple[set[Listener], dict | None, object | None],
        ] = {}

    def add(self, listener: Listener) -> None:
        if listener in self._reverse_lookup:
            return

        target_bucket: set[Listener] | None = None
        bucket_map: dict | None = None
        bucket_key: object | None = None

        if listener.opcode is InstructionOpCode.SET:
            if listener.key is not None:
                bucket_map = self.set_by_key
                bucket_key = listener.key
                target_bucket = self.set_by_key[listener.key]
            else:
                target_bucket = self.set_all

        elif listener.opcode is InstructionOpCode.INSERT:
            target_bucket = self.insert_all

        elif listener.opcode is InstructionOpCode.REMOVE:
            target_bucket = self.remove_all

        elif listener.opcode is InstructionOpCode.SETROW:
            if listener.id is not None and listener.key is not None:
                bucket_map = self.setrow_by_id_key
                bucket_key = (listener.id, listener.key)
                target_bucket = self.setrow_by_id_key[bucket_key]

            elif listener.id is not None:
                bucket_map = self.setrow_by_id
                bucket_key = listener.id
                target_bucket = self.setrow_by_id[listener.id]

            elif listener.key is not None:
                bucket_map = self.setrow_by_key
                bucket_key = listener.key
                target_bucket = self.setrow_by_key[listener.key]

            else:
                target_bucket = self.setrow_all

        if target_bucket is None:
            raise ValueError(f"Unsupported listener opcode: {listener.opcode!r}")

        target_bucket.add(listener)

        self._reverse_lookup[listener] = (
            target_bucket,
            bucket_map,
            bucket_key,
        )

    def remove(self, listener: Listener) -> None:
        entry = self._reverse_lookup.pop(listener, None)
        if entry is None:
            return

        bucket, bucket_map, bucket_key = entry

        bucket.discard(listener)

        if bucket_map is not None and not bucket:
            bucket_map.pop(bucket_key, None)

    def listeners(self, event: StoreEvent) -> set[Listener]:
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
        *,
        owner: Node,
        referenced: Node,
        foreignkey: str,
        subscriber_id: str | None,
    ) -> None:
        self.owner = owner
        self.referenced = referenced
        self.foreignkey = foreignkey
        self.subscriber_id = subscriber_id

    @property
    def referenced_address(self) -> Address:
        return self.referenced.address

    def translate(self, event: StoreEvent) -> list[StoreEvent]:
        if event.id is None:
            return []

        matching_ids = self.owner.lookup(self.foreignkey, event.id)
        if not matching_ids:
            return []

        ids_to_notify = (
            [matching_ids]
            if isinstance(matching_ids, str)
            else matching_ids
        )

        translated_events: list[StoreEvent] = []

        for owner_id in ids_to_notify:
            if self.subscriber_id is not None and owner_id != self.subscriber_id:
                continue

            translated_events.append(
                event.copy_with(
                    address=self.owner.address,
                    opcode=InstructionOpCode.SETROW,  # ← this line
                    id=owner_id,
                )
            )

        return translated_events
