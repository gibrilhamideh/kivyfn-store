# src/kivyfn/store/devtools/devtools.py
from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from kivyfn.logging import get_logger

from kivyfn.store.codec.requests import encode_request
from kivyfn.store.codec.responses import encode_response
from kivyfn.store.devtools.config import DevToolsConfig
from kivyfn.store.devtools.protocol import (
    CaptureTooLarge,
    encode_checkpoint,
    encoded_size,
    to_wire,
)
from kivyfn.store.devtools.recorder import Recorder
from kivyfn.store.events import StoreCommit
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost

logger = get_logger(__name__)


class DevTools:
    """Central best-effort diagnostics; Store remains the execution owner."""

    def __init__(self, host: StoreHost, config: DevToolsConfig) -> None:
        from kivyfn.store.devtools.server import DevToolsServer

        self.host = host
        self.config = config
        self.recorder = Recorder(config, self._publish)
        self.server = DevToolsServer(self)
        self._unsubscribers: list[Callable[[], None]] = []

    @property
    def url(self) -> str | None:
        return self.server.url

    async def start(self) -> str:
        try:
            checkpoint, unsubscribe = self.host.store.observe_checkpoint(
                self.event
            )
            self._unsubscribers.append(unsubscribe)
            self.reset_checkpoint(checkpoint)
            self._unsubscribers.append(
                self.host.store.subscribe_history(self.history)
            )
            return await self.server.start()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        try:
            await self.server.stop()
        finally:
            self.recorder.close()

    def reset_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        try:
            encoded_checkpoint = encode_checkpoint(
                checkpoint,
                self.host.store.schema_catalog(),
                self.config.max_bytes // 2,
            )
        except CaptureTooLarge:
            self.recorder.invalidate("Checkpoint exceeds diagnostic budget")
        else:
            self.recorder.reset(encoded_checkpoint)

    def request(self, client_id: str, batch: RequestBatch) -> str | None:
        try:
            if self.recorder.paused:
                return None
            capture_id = uuid4().hex
            if len(self.recorder.pending) >= self.config.max_pending_captures:
                self.recorder.dropped += 1
                return None
            wire = encode_request(batch)
            transactions = [
                item["transaction_id"]
                for item in wire["requests"]
                if "transaction_id" in item
            ]
            names: list[str] = []
            for item in wire["requests"]:
                names.extend(
                    action["schema"] for action in item.get("actions", ())
                )
                name = item.get("schema")
                if name is None:
                    name = (
                        item.get("query", {}).get("source", {}).get("schema")
                    )
                if name is not None:
                    names.append(name)
            schemas = tuple(dict.fromkeys(names))
            transaction_id = (
                transactions[0] if len(transactions) == 1 else None
            )
            self.recorder.capture(
                "request",
                {"request": wire},
                status="received",
                client_id=client_id,
                capture_id=capture_id,
                transaction_id=transaction_id,
                schemas=schemas,
            )
            self.recorder.pending[capture_id] = (
                client_id,
                monotonic(),
                schemas,
                transaction_id,
            )
            return capture_id
        except Exception:
            logger.exception("Devtools request capture failed")
            return None

    def response(self, capture: str | None, batch: ResponseBatch) -> None:
        self._finish(capture, response=batch)

    def failure(
        self,
        capture: str | None,
        error: BaseException,
        *,
        cancelled: bool = False,
    ) -> None:
        self._finish(capture, error=error, cancelled=cancelled)

    def _finish(
        self,
        capture: str | None,
        *,
        response: ResponseBatch | None = None,
        error: BaseException | None = None,
        cancelled: bool = False,
    ) -> None:
        if capture is None:
            return
        pending = self.recorder.pending.pop(capture, None)
        if pending is None:
            return
        try:
            client_id, started, schemas, transaction_id = pending
            detail: dict[str, Any] = {
                "duration_ms": (monotonic() - started) * 1000,
                "delivery": "response produced; delivery not confirmed",
            }
            status = "ok"
            if response is not None:
                wire = encode_response(response)
                detail["response"] = wire
                if any("failure" in item for item in wire["responses"]):
                    status = "failed"
            else:
                detail["error"] = type(error).__name__
                detail["message"] = str(error)
                detail["delivery"] = "no response produced; outcome may vary"
                status = "cancelled" if cancelled else "failed"
            self.recorder.capture(
                "response",
                detail,
                status=status,
                client_id=client_id,
                capture_id=capture,
                schemas=schemas,
                transaction_id=transaction_id,
            )
        except Exception:
            logger.exception("Devtools response capture failed")

    def event(self, commit: StoreCommit) -> None:
        try:
            encoded_commit = to_wire(commit, self.config.max_payload_bytes)
            for original, event in zip(
                commit.events, encoded_commit["events"]
            ):
                event["type"] = original.opcode.value
            self.recorder.commit(encoded_commit)
        except CaptureTooLarge:
            self.recorder.sequence += 1
            self.recorder.invalidate("Oversized commit broke continuity")
            self.recorder.dropped += 1
            self._publish("gap", {"reason": "Oversized committed payload"})
        except Exception:
            self.recorder.sequence += 1
            self.recorder.invalidate("Commit capture failed")
            logger.exception("Devtools commit capture failed")
            self._publish("gap", {"reason": "Commit capture failed"})

    def history(self, status: Any) -> None:
        try:
            self._publish("history", self.history_page())
        except Exception:
            logger.exception("Devtools history capture failed")

    def history_page(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """Bound detached inspection without hiding navigation status."""
        page = self.host.store.history(limit=limit, offset=offset)
        payload = {
            "status": to_wire(page.status, self.config.max_payload_bytes),
            "entries": [],
            "offset": offset,
            "truncated": False,
        }
        entries: list[dict[str, Any]] = payload["entries"]
        remaining = self.config.max_payload_bytes - encoded_size(payload)
        for entry in page.entries:
            try:
                item = to_wire(entry, max(0, remaining - 32))
            except CaptureTooLarge:
                payload["truncated"] = True
                item = {
                    "id": entry.id,
                    "transaction_ids": [],
                    "truncated": True,
                    "reason": "History detail exceeds diagnostic budget",
                }
                if encoded_size(item) > remaining:
                    break
            entries.append(item)
            remaining -= encoded_size(item) + 2
        return payload

    def lifecycle(
        self, client_id: str, event: str, details: Any = None
    ) -> None:
        try:
            self.recorder.capture(
                "lifecycle",
                {"event": event, "details": details},
                client_id=client_id,
            )
        except Exception:
            logger.exception("Devtools lifecycle capture failed")

    def rejected(self, client_id: str, error: BaseException) -> None:
        try:
            self.recorder.capture(
                "boundary",
                {"error": type(error).__name__, "message": str(error)},
                client_id=client_id,
                status="failed",
            )
        except Exception:
            logger.exception("Devtools boundary capture failed")

    def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.server.publish(kind, payload)
        except Exception:
            logger.exception("Devtools stream capture failed")
