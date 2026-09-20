# src/kivyfn/store/devtools/recorder.py
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from copy import deepcopy
from time import time
from typing import Any
from uuid import uuid4

from kivyfn.store.devtools.config import DevToolsConfig
from kivyfn.store.devtools.protocol import (
    CaptureTooLarge,
    encoded_size,
    to_wire,
)


class Recorder:
    """Bounded detached Activity plus a checkpoint and chronological deltas."""

    def __init__(
        self,
        config: DevToolsConfig,
        publish: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.config = config
        self.publish = publish
        self.sequence = 0
        self.paused = False
        self.filters: dict[str, str] = {}
        self.records: deque[tuple[dict[str, Any], int]] = deque()
        self.pending: dict[
            str, tuple[str, float, tuple[str, ...], str | None]
        ] = {}
        self.record_bytes = 0
        self.checkpoint: dict[str, Any] | None = None
        self.checkpoint_sequence = 0
        self.deltas: deque[tuple[int, dict[str, Any], int]] = deque()
        self.preview_bytes = 0
        self.unavailable_reason = "No checkpoint has been captured"
        self.dropped = 0

    def reset(self, encoded_checkpoint: dict[str, Any]) -> None:
        self.deltas.clear()
        self.checkpoint_sequence = self.sequence
        size = encoded_size(encoded_checkpoint)
        if size > self.config.max_bytes // 2:
            self.invalidate("Checkpoint exceeds the diagnostic budget")
            return
        self.checkpoint = deepcopy(encoded_checkpoint)
        self.preview_bytes = size
        self.unavailable_reason = "Earlier recording was evicted"

    def invalidate(self, reason: str) -> None:
        self.checkpoint = None
        self.deltas.clear()
        self.preview_bytes = 0
        self.unavailable_reason = reason

    def accepts(self, metadata: dict[str, Any]) -> bool:
        if self.paused:
            return False
        for key, expected in self.filters.items():
            if not expected:
                continue
            if key == "schema":
                if expected not in metadata.get("schemas", ()):
                    return False
            elif key == "identity":
                identifiers = (
                    metadata.get("id"),
                    metadata.get("transaction_id"),
                    metadata.get("capture_id"),
                )
                if not any(expected in (value or "") for value in identifiers):
                    return False
            elif key == "client_id":
                if expected not in (metadata.get(key) or ""):
                    return False
            elif metadata.get(key) != expected:
                return False
        return True

    def capture(
        self,
        category: str,
        detail: Any,
        *,
        status: str = "ok",
        client_id: str | None = None,
        capture_id: str | None = None,
        transaction_id: str | None = None,
        schemas: tuple[str, ...] = (),
        detail_is_encoded: bool = False,
    ) -> dict[str, Any] | None:
        metadata = {
            "id": uuid4().hex,
            "category": category,
            "status": status,
            "client_id": client_id,
            "capture_id": capture_id,
            "transaction_id": transaction_id,
            "schemas": list(schemas),
        }
        if not self.accepts(metadata):
            return None
        truncated = False
        try:
            if detail_is_encoded:
                if encoded_size(detail) > self.config.max_payload_bytes:
                    raise CaptureTooLarge("Encoded detail exceeds its budget")
                payload = deepcopy(detail)
            else:
                payload = to_wire(detail, self.config.max_payload_bytes)
        except CaptureTooLarge:
            payload = {"reason": "Payload exceeds diagnostic capture limit"}
            truncated = True
            self.dropped += 1
        self.sequence += 1
        record = {
            "sequence": self.sequence,
            "time": time(),
            **metadata,
            "detail": payload,
            "truncated": truncated,
        }
        size = encoded_size(record)
        budget = self.config.max_bytes // 2
        while self.records and (
            len(self.records) >= self.config.max_records
            or self.record_bytes + size > budget
        ):
            _, removed = self.records.popleft()
            self.record_bytes -= removed
            self.dropped += 1
        if size <= budget:
            self.records.append((record, size))
            self.record_bytes += size
        else:
            self.dropped += 1
        self.publish("activity", record)
        return record

    def commit(self, encoded_commit: dict[str, Any]) -> None:
        # Timeline is independent of Activity filters and grouped undo entries.
        self.sequence += 1
        encoded_commit["sequence"] = self.sequence
        if not self.paused and self.checkpoint is not None:
            size = encoded_size(encoded_commit)
            if size > self.config.max_payload_bytes:
                self.invalidate("An oversized commit broke continuity")
            else:
                self.deltas.append(
                    (self.sequence, deepcopy(encoded_commit), size)
                )
                self.preview_bytes += size
                self._trim_preview()
        self.publish("commit", encoded_commit)
        schemas = dict.fromkeys(encoded_commit.get("schema_revisions", {}))
        schemas.update(
            dict.fromkeys(
                signal["schema"]
                for signal in encoded_commit.get("signals", ())
            )
        )
        self.capture(
            "commit",
            encoded_commit,
            transaction_id=encoded_commit.get("transaction_id"),
            client_id=encoded_commit.get("origin_client_id"),
            schemas=tuple(schemas),
            detail_is_encoded=True,
        )

    def _trim_preview(self) -> None:
        budget = self.config.max_bytes // 2
        while self.deltas and (
            self.preview_bytes > budget
            or len(self.deltas) > self.config.max_records
        ):
            sequence, delta, size = self.deltas.popleft()
            checkpoint = self.checkpoint
            if checkpoint is None:
                return
            old_size = encoded_size(checkpoint)
            apply_delta(checkpoint, delta)
            new_size = encoded_size(checkpoint)
            self.preview_bytes += new_size - old_size - size
            self.checkpoint_sequence = sequence
        if self.preview_bytes > budget:
            self.invalidate("Current checkpoint exceeds diagnostic budget")

    def preview(self, sequence: int) -> dict[str, Any]:
        checkpoint = self.checkpoint
        if (
            checkpoint is None
            or sequence < self.checkpoint_sequence
            or sequence > self.sequence
            or self.paused
        ):
            return {"available": False, "reason": self.unavailable_reason}
        result = deepcopy(checkpoint)
        for position, delta, _ in self.deltas:
            if position > sequence:
                break
            apply_delta(result, delta)
        return {
            "available": True,
            "checkpoint": result,
            "sequence": sequence,
            "scope": list(result["schemas"]),
        }

    def activity(self, limit: int = 100) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        retained = 512
        for record, size in reversed(self.records):
            if len(records) >= limit:
                break
            if retained + size > self.config.max_payload_bytes:
                break
            records.append(record)
            retained += size
        return {
            "records": deepcopy(list(reversed(records))),
            "paused": self.paused,
            "filters": dict(self.filters),
            "dropped": self.dropped,
            "sequence": self.sequence,
            "oldest_preview_sequence": self.checkpoint_sequence,
            "preview_available": self.checkpoint is not None,
            "retained_bytes": self.record_bytes + self.preview_bytes,
        }

    def close(self) -> None:
        self.pending.clear()
        self.records.clear()
        self.record_bytes = 0
        self.invalidate("Recorder is closed")


def apply_delta(checkpoint: dict[str, Any], delta: dict[str, Any]) -> None:
    """Apply captured canonical effects to a detached diagnostic snapshot."""
    schemas = checkpoint["schemas"]
    for event in delta.get("events", ()):
        schema = event["schema"]
        if schema not in schemas:
            continue
        data = schemas[schema]
        kind = event["type"]
        if kind == "set":
            data[event["key"]] = deepcopy(event["new"])
        elif kind == "setrow":
            data[event["id"]][event["key"]] = deepcopy(event["new"])
        elif kind == "insert":
            data[event["id"]] = deepcopy(event["new"])
        elif kind == "remove":
            del data[event["id"]]
    for schema, revision in delta.get("schema_revisions", {}).items():
        if schema in schemas:
            checkpoint["schema_revisions"][schema] = revision
