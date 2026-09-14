# src/kivyfn/store/state/reactive/data.py
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


class StateData:
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
        fields = frozenset(values)
        before = self._visible_snapshot(fields)

        for key, value in values.items():
            self._canonical[key] = self.copy(value)

        return self._visible_changes(before, fields)

    def set_overlay(
        self, transaction_id: str, values: Mapping[str, Any]
    ) -> frozenset[str]:
        fields = frozenset(values)
        before = self._visible_snapshot(fields)
        overlay = self._overlays.setdefault(transaction_id, {})

        for key, value in values.items():
            overlay[key] = self.copy(value)

        return self._visible_changes(before, fields)

    def remove_overlay(
        self,
        transaction_id: str,
        fields: set[str] | frozenset[str] | None = None,
    ) -> frozenset[str]:
        overlay = self._overlays.get(transaction_id)

        if overlay is None:
            return frozenset()

        selected = (
            frozenset(overlay)
            if fields is None
            else frozenset(key for key in fields if key in overlay)
        )

        if not selected:
            return frozenset()

        before = self._visible_snapshot(selected)

        if fields is None:
            del self._overlays[transaction_id]
        else:
            for key in selected:
                del overlay[key]

            if not overlay:
                del self._overlays[transaction_id]

        return self._visible_changes(before, selected)

    def canonical_snapshot(
        self, fields: set[str] | frozenset[str]
    ) -> dict[str, Any]:
        return {key: self.copy(self._canonical.get(key)) for key in fields}

    def visible_snapshot(
        self, fields: set[str] | frozenset[str]
    ) -> dict[str, Any]:
        return {key: self.copy(self._visible(key)) for key in fields}

    def _visible(self, key: str) -> Any:
        for overlay in reversed(self._overlays.values()):
            if key in overlay:
                return overlay[key]

        return self._canonical.get(key)

    def _visible_snapshot(self, fields: frozenset[str]) -> dict[str, Any]:
        return {key: self.copy(self._visible(key)) for key in fields}

    def _visible_changes(
        self, before: Mapping[str, Any], fields: frozenset[str]
    ) -> frozenset[str]:
        return frozenset(
            key for key in fields if before.get(key) != self._visible(key)
        )
