"""Canonical data and transaction overlays, independent of Kivy.

StateData copies field values across its boundary. CollectionData preserves
row identity: a row reference means present, and None means absent. Neither
class creates rows, tracks subscription ownership, or dispatches callbacks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeVar

ValueT = TypeVar("ValueT")
ChangeT = TypeVar("ChangeT")
RowT = TypeVar("RowT")


class BaseData(ABC, Generic[ValueT, ChangeT]):
    """Shared contract for canonical data plus per-transaction overlays.

    Mutators return visible changes without notifying observers. Updating an
    existing overlay keeps its priority; later-created overlays take priority.
    Discarding an overlay always preserves the current canonical data.
    """

    __slots__ = ()

    @abstractmethod
    def canonical(self, key: str) -> ValueT | None:
        raise NotImplementedError

    @abstractmethod
    def visible(self, key: str) -> ValueT | None:
        raise NotImplementedError

    @abstractmethod
    def set_canonical(self, values: Mapping[str, ValueT | None]) -> ChangeT:
        """Patch confirmed entries, leaving unspecified entries unchanged."""
        raise NotImplementedError

    @abstractmethod
    def set_overlay(
        self, transaction_id: str, values: Mapping[str, ValueT | None]
    ) -> ChangeT:
        raise NotImplementedError

    @abstractmethod
    def discard_overlay(
        self, transaction_id: str, keys: Iterable[str] | None = None
    ) -> ChangeT:
        """Remove one transaction's overlay, optionally for selected keys."""
        raise NotImplementedError

    @abstractmethod
    def canonical_snapshot(
        self, keys: Iterable[str] | None = None
    ) -> Mapping[str, ValueT]:
        raise NotImplementedError

    @abstractmethod
    def visible_snapshot(
        self, keys: Iterable[str] | None = None
    ) -> Mapping[str, ValueT]:
        raise NotImplementedError


class StateData(BaseData[Any, frozenset[str]]):
    """Canonical and optimistic values for one reactive State."""

    __slots__ = ("_canonical", "_overlays")

    def __init__(self, canonical: Mapping[str, Any]) -> None:
        self._canonical = self.copy_values(canonical)
        self._overlays: dict[str, dict[str, Any]] = {}

    @staticmethod
    def copy(value: Any) -> Any:
        """Copy one value crossing the reactive State boundary."""
        return deepcopy(value)

    @staticmethod
    def copy_values(values: Mapping[str, Any]) -> dict[str, Any]:
        """Copy a mapping of reactive State values."""
        return {key: StateData.copy(value) for key, value in values.items()}

    def canonical(self, key: str) -> Any:
        return self.copy(self._canonical.get(key))

    def visible(self, key: str) -> Any:
        return self.copy(self._visible(key))

    def set_canonical(self, values: Mapping[str, Any]) -> frozenset[str]:
        keys = frozenset(values)
        before = self._visible_snapshot(keys)

        for key, value in values.items():
            self._canonical[key] = self.copy(value)

        return self._visible_changes(before, keys)

    def set_overlay(
        self, transaction_id: str, values: Mapping[str, Any]
    ) -> frozenset[str]:
        keys = frozenset(values)
        before = self._visible_snapshot(keys)
        overlay = self._overlays.setdefault(transaction_id, {})

        for key, value in values.items():
            overlay[key] = self.copy(value)

        return self._visible_changes(before, keys)

    def discard_overlay(
        self, transaction_id: str, keys: Iterable[str] | None = None
    ) -> frozenset[str]:
        overlay = self._overlays.get(transaction_id)

        if overlay is None:
            return frozenset()

        selected = (
            frozenset(overlay)
            if keys is None
            else frozenset(key for key in keys if key in overlay)
        )

        if not selected:
            return frozenset()

        before = self._visible_snapshot(selected)

        if keys is None:
            del self._overlays[transaction_id]
        else:
            for key in selected:
                del overlay[key]

            if not overlay:
                del self._overlays[transaction_id]

        return self._visible_changes(before, selected)

    def canonical_snapshot(
        self, keys: Iterable[str] | None = None
    ) -> dict[str, Any]:
        selected = self._canonical if keys is None else keys
        return {key: self.copy(self._canonical.get(key)) for key in selected}

    def visible_snapshot(
        self, keys: Iterable[str] | None = None
    ) -> dict[str, Any]:
        if keys is None:
            selected = dict.fromkeys(self._canonical)
            for overlay in self._overlays.values():
                selected.update(dict.fromkeys(overlay))
            keys = selected
        return {key: self.copy(self._visible(key)) for key in keys}

    def _visible(self, key: str) -> Any:
        for overlay in reversed(self._overlays.values()):
            if key in overlay:
                return overlay[key]

        return self._canonical.get(key)

    def _visible_snapshot(self, keys: frozenset[str]) -> dict[str, Any]:
        return {key: self.copy(self._visible(key)) for key in keys}

    def _visible_changes(
        self, before: Mapping[str, Any], keys: frozenset[str]
    ) -> frozenset[str]:
        return frozenset(
            key for key in keys if before.get(key) != self._visible(key)
        )


@dataclass(frozen=True, slots=True)
class MembershipDelta(Generic[RowT]):
    """Visible membership changes, retaining the affected row objects."""

    entered: tuple[tuple[str, RowT], ...] = ()
    left: tuple[tuple[str, RowT], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.entered or self.left)


class CollectionData(BaseData[RowT, MembershipDelta[RowT]]):
    """Confirmed rows and optimistic row membership.

    Updates map row IDs to row objects or None (removal). The reactive
    collection resolves IDs to objects before calling these methods. Row
    objects are never copied; snapshots copy only their containing mapping.
    """

    def __init__(self, canonical: Mapping[str, RowT] | None = None) -> None:
        self._canonical: dict[str, RowT] = dict(canonical or {})
        self._overlays: dict[str, dict[str, RowT | None]] = {}
        self._visible: dict[str, RowT] = dict(self._canonical)
        self._view: Mapping[str, RowT] = MappingProxyType(self._visible)

    @property
    def visible_view(self) -> Mapping[str, RowT]:
        """A stable, read-only view of the merged rows; not another overlay."""
        return self._view

    def canonical(self, key: str) -> RowT | None:
        return self._canonical.get(key)

    def visible(self, key: str) -> RowT | None:
        return self._visible.get(key)

    def set_canonical(
        self, values: Mapping[str, RowT | None]
    ) -> MembershipDelta[RowT]:
        for key, row in values.items():
            if row is None:
                self._canonical.pop(key, None)
            else:
                self._canonical[key] = row
        return self._refresh()

    def replace_canonical(
        self, values: Mapping[str, RowT]
    ) -> MembershipDelta[RowT]:
        """Replace the confirmed set, preserving overlays and surviving order.

        Unlike set_canonical(), missing row IDs are removed. Reordering an
        otherwise identical snapshot does not change membership or row order.
        """
        for key in tuple(self._canonical):
            if key not in values:
                del self._canonical[key]
        self._canonical.update(values)
        return self._refresh()

    def set_overlay(
        self, transaction_id: str, values: Mapping[str, RowT | None]
    ) -> MembershipDelta[RowT]:
        overlay = self._overlays.setdefault(transaction_id, {})
        overlay.update(values)
        return self._refresh()

    def discard_overlay(
        self, transaction_id: str, keys: Iterable[str] | None = None
    ) -> MembershipDelta[RowT]:
        overlay = self._overlays.get(transaction_id)
        if overlay is None:
            return MembershipDelta()
        if keys is None:
            del self._overlays[transaction_id]
        else:
            for key in keys:
                overlay.pop(key, None)
            if not overlay:
                del self._overlays[transaction_id]
        return self._refresh()

    def canonical_snapshot(
        self, keys: Iterable[str] | None = None
    ) -> dict[str, RowT]:
        if keys is None:
            return dict(self._canonical)
        return {
            key: self._canonical[key] for key in keys if key in self._canonical
        }

    def visible_snapshot(
        self, keys: Iterable[str] | None = None
    ) -> dict[str, RowT]:
        if keys is None:
            return dict(self._visible)
        return {
            key: self._visible[key] for key in keys if key in self._visible
        }

    def _refresh(self) -> MembershipDelta[RowT]:
        """Merge overlays in creation order and compare object identities."""
        rows = dict(self._canonical)
        for overlay in self._overlays.values():
            for key, row in overlay.items():
                if row is None:
                    rows.pop(key, None)
                else:
                    rows[key] = row
        delta = MembershipDelta(
            entered=tuple(
                (key, row)
                for key, row in rows.items()
                if self._visible.get(key) is not row
            ),
            left=tuple(
                (key, row)
                for key, row in self._visible.items()
                if rows.get(key) is not row
            ),
        )
        # Keep the mapping object alive so existing read-only views stay live.
        self._visible.clear()
        self._visible.update(rows)
        return delta
