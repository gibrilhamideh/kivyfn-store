# kivyfn/store/signals/listener.py
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from kivyfn.store.address import Address


@dataclass(frozen=True, slots=True)
class SignalListener:
    address: Address[Any]
    signal: str
    id: str | None = None

    @property
    def room(self) -> str:
        value = hash(self) & 0xFFFFFFFFFFFFFFFF
        return f"signal:{value:x}"


class SignalListenerIndex:
    def __init__(self) -> None:
        self.all: set[SignalListener] = set()

        self.by_id: dict[
            str,
            set[SignalListener],
        ] = defaultdict(set)

    def add(
        self,
        listener: SignalListener,
    ) -> None:
        if listener.id is None:
            self.all.add(
                listener,
            )
            return

        self.by_id[
            listener.id
        ].add(listener)

    def remove(
        self,
        listener: SignalListener,
    ) -> None:
        if listener.id is None:
            self.all.discard(
                listener,
            )
            return

        bucket = self.by_id.get(
            listener.id,
        )

        if bucket is None:
            return

        bucket.discard(
            listener,
        )

        if not bucket:
            self.by_id.pop(
                listener.id,
                None,
            )

    def listeners(
        self,
        id: str | None,
    ) -> set[SignalListener]:
        listeners = set(
            self.all,
        )

        if id is not None:
            listeners.update(
                self.by_id.get(
                    id,
                    (),
                )
            )

        return listeners

    def __bool__(self) -> bool:
        return bool(
            self.all
            or self.by_id
        )

    def __len__(self) -> int:
        return len(self.all) + sum(
            len(bucket)
            for bucket in self.by_id.values()
        )
