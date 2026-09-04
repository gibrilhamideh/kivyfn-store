from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.error import StoreProtocolError


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SyncSnapshot(Generic[T]):
    """Authoritative node snapshot with synchronization metadata."""

    server_instance_id: str
    revision: int
    data: T

    @classmethod
    def from_payload(
        cls,
        payload: Any,
    ) -> SyncSnapshot[Any]:
        if not isinstance(payload, dict):
            raise StoreProtocolError(
                "Revisioned snapshot data must be an object"
            )

        instance_id = payload.get("server_instance_id")
        revision = payload.get("revision")

        if not isinstance(instance_id, str) or not instance_id:
            raise StoreProtocolError(
                "Revisioned snapshot has no server instance ID"
            )

        if isinstance(revision, bool) or not isinstance(revision, int):
            raise StoreProtocolError(
                "Revisioned snapshot has an invalid revision"
            )

        if revision < 0:
            raise StoreProtocolError(
                "Revisioned snapshot revision cannot be negative"
            )

        if "data" not in payload:
            raise StoreProtocolError(
                "Revisioned snapshot has no data"
            )

        return cls(
            server_instance_id=instance_id,
            revision=revision,
            data=payload["data"],
        )


@dataclass(frozen=True, slots=True)
class RevisionAudit:
    """Current revisions for a set of store addresses."""

    server_instance_id: str
    revisions: dict[Address[Any], int]

    @classmethod
    def from_payload(
        cls,
        payload: Any,
    ) -> RevisionAudit:
        if not isinstance(payload, dict):
            raise StoreProtocolError(
                "Revision audit data must be an object"
            )

        instance_id = payload.get("server_instance_id")
        values = payload.get("revisions")

        if not isinstance(instance_id, str) or not instance_id:
            raise StoreProtocolError(
                "Revision audit has no server instance ID"
            )

        if not isinstance(values, list):
            raise StoreProtocolError(
                "Revision audit revisions must be a list"
            )

        revisions: dict[Address[Any], int] = {}

        for value in values:
            if not isinstance(value, dict):
                raise StoreProtocolError(
                    "Every revision audit entry must be an object"
                )

            parts = value.get("address")
            revision = value.get("revision")

            if not isinstance(parts, list) or not all(
                isinstance(part, str)
                for part in parts
            ):
                raise StoreProtocolError(
                    "Revision audit entry has an invalid address"
                )

            if isinstance(revision, bool) or not isinstance(revision, int):
                raise StoreProtocolError(
                    "Revision audit entry has an invalid revision"
                )

            if revision < 0:
                raise StoreProtocolError(
                    "Revision audit revision cannot be negative"
                )

            address = Address(*parts)

            if address in revisions:
                raise StoreProtocolError(
                    f"Revision audit repeats address {address}"
                )

            revisions[address] = revision

        return cls(
            server_instance_id=instance_id,
            revisions=revisions,
        )
