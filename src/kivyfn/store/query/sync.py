from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kivyfn.store.error import StoreProtocolError
from kivyfn.store.query.event import QueryResult, copy_query_result


@dataclass(frozen=True, slots=True)
class QuerySyncSnapshot:
    server_instance_id: str
    sequence: int
    data: QueryResult

    def payload(self) -> dict[str, Any]:
        return {
            "server_instance_id": self.server_instance_id,
            "sequence": self.sequence,
            "data": copy_query_result(self.data),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> QuerySyncSnapshot:
        if not isinstance(payload, dict):
            raise StoreProtocolError(
                "Query synchronization snapshot must be an object"
            )

        instance_id = payload.get("server_instance_id")
        sequence = payload.get("sequence")
        data = payload.get("data")

        if not isinstance(instance_id, str) or not instance_id:
            raise StoreProtocolError(
                "Query synchronization snapshot has no instance ID"
            )

        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise StoreProtocolError(
                "Query synchronization sequence must be an integer"
            )

        if sequence < 0:
            raise StoreProtocolError(
                "Query synchronization sequence cannot be negative"
            )

        if not isinstance(data, dict):
            raise StoreProtocolError(
                "Query synchronization data must be an object"
            )

        if not all(
            isinstance(row_id, str) and isinstance(row, dict)
            for row_id, row in data.items()
        ):
            raise StoreProtocolError(
                "Query synchronization rows are invalid"
            )

        return cls(
            server_instance_id=instance_id,
            sequence=sequence,
            data=copy_query_result(data),
        )


@dataclass(frozen=True, slots=True)
class QueryAuditEntry:
    token: str
    sequence: int
    dirty: bool


@dataclass(frozen=True, slots=True)
class QueryRevisionAudit:
    server_instance_id: str
    entries: dict[str, QueryAuditEntry]

    @classmethod
    def from_payload(cls, payload: Any) -> QueryRevisionAudit:
        if not isinstance(payload, dict):
            raise StoreProtocolError(
                "Query revision audit must be an object"
            )

        instance_id = payload.get("server_instance_id")
        values = payload.get("subscriptions")

        if not isinstance(instance_id, str) or not instance_id:
            raise StoreProtocolError(
                "Query revision audit has no instance ID"
            )

        if not isinstance(values, list):
            raise StoreProtocolError(
                "Query revision audit subscriptions must be a list"
            )

        entries: dict[str, QueryAuditEntry] = {}

        for value in values:
            if not isinstance(value, dict):
                raise StoreProtocolError(
                    "Query revision audit entry must be an object"
                )

            token = value.get("subscription_token")
            sequence = value.get("sequence")
            dirty = value.get("dirty")

            if not isinstance(token, str) or not token:
                raise StoreProtocolError(
                    "Query revision audit token is invalid"
                )

            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise StoreProtocolError(
                    "Query revision audit sequence is invalid"
                )

            if sequence < 0 or not isinstance(dirty, bool):
                raise StoreProtocolError(
                    "Query revision audit entry is invalid"
                )

            if token in entries:
                raise StoreProtocolError(
                    "Query revision audit repeats a token"
                )

            entries[token] = QueryAuditEntry(
                token=token,
                sequence=sequence,
                dirty=dirty,
            )

        return cls(
            server_instance_id=instance_id,
            entries=entries,
        )
