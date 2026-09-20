# src/kivyfn/store/devtools/server.py
from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from aiohttp import WSMsgType, web
from kivyfn.logging import get_logger

from kivyfn.store.codec.requests import decode_request
from kivyfn.store.codec.responses import encode_response
from kivyfn.store.devtools.protocol import (
    DIAGNOSTIC_VERSION,
    CaptureTooLarge,
    dumps,
    encode_checkpoint,
    encoded_size,
    from_wire,
    to_wire,
)
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    QuerySubscriptionRequest,
    SingleSubscriptionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.version import PROTOCOL_VERSION

if TYPE_CHECKING:
    from kivyfn.store.devtools.devtools import DevTools

logger = get_logger(__name__)


@dataclass(slots=True)
class BrowserSession:
    id: str
    websocket: web.WebSocketResponse
    epoch: str = field(default_factory=lambda: uuid4().hex)
    sequence: int = 0
    scope: tuple[str, ...] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    queue: deque[tuple[str, int]] = field(default_factory=deque)
    queue_bytes: int = 0
    pending_commands: int = 0
    writer: asyncio.Task[None] | None = None
    closed: bool = False
    resync: bool = False
    oversized: bool = False


class DevToolsServer:
    """Loopback HTTP commands and bounded streams on the existing loop."""

    def __init__(self, devtools: DevTools) -> None:
        self.devtools = devtools
        self.config = devtools.config
        self.host = devtools.host
        self.url: str | None = None
        self.authority: str | None = None
        self.runner: web.AppRunner | None = None
        self.sessions: dict[str, BrowserSession] = {}
        self.stopping = True
        self._stop_task: asyncio.Task[None] | None = None
        self.static = Path(
            str(files("kivyfn.store.devtools").joinpath("static"))
        )

    async def start(self) -> str:
        if self.url is not None:
            return self.url
        if not (self.static / "index.html").is_file():
            raise RuntimeError(
                "Devtools assets are missing; run the frontend release build"
            )
        self._stop_task = None
        app = web.Application(
            middlewares=[self._boundary],
            client_max_size=self.config.max_request_bytes,
        )
        app.add_routes(
            [
                web.get("/", self._index),
                web.get("/api/info", self._info),
                web.get("/api/state", self._state),
                web.get("/api/history", self._history),
                web.get("/api/activity", self._activity),
                web.get("/api/preview", self._preview),
                web.post("/api/commands", self._commands),
                web.post("/api/capture", self._capture),
                web.get("/stream", self._stream),
                web.get("/assets/{path:.*}", self._asset),
            ]
        )
        runner = web.AppRunner(app, access_log=None)
        self.runner = runner
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.config.host, self.config.port)
            await site.start()
            address = runner.addresses[0]
            port = address[1]
            host = self.config.host
            if ":" in host:
                host = f"[{host}]"
            self.authority = f"{host}:{port}"
            self.url = f"http://{self.authority}"
            self.stopping = False
            return self.url
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        task = self._stop_task
        if task is None:
            task = asyncio.create_task(self._close())
            self._stop_task = task
            task.add_done_callback(self._observe_stop)
        await asyncio.shield(task)

    @staticmethod
    def _observe_stop(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _close(self) -> None:
        self.stopping = True
        self.url = None
        sessions = tuple(self.sessions.values())
        if sessions:
            await asyncio.gather(
                *(self._close_session(session) for session in sessions),
                return_exceptions=True,
            )
        runner, self.runner = self.runner, None
        if runner is not None:
            await runner.cleanup()
        self.authority = None

    @web.middleware
    async def _boundary(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if request.host != self.authority:
            raise web.HTTPForbidden(text="Unexpected devtools Host")
        origin = request.headers.get("Origin")
        if origin is not None and origin != self.url:
            raise web.HTTPForbidden(text="Unexpected devtools Origin")
        if self.stopping:
            raise web.HTTPServiceUnavailable(text="Devtools is stopping")
        try:
            response = await handler(request)
        except (ValueError, TypeError, KeyError, CaptureTooLarge) as error:
            return web.json_response({"error": str(error)}, status=400)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.static / "index.html")

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        root = (self.static / "assets").resolve()
        path = (root / request.match_info["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    def _json(self, value: Any) -> web.Response:
        payload = to_wire(value, self.config.max_payload_bytes)
        return web.Response(
            text=dumps(payload), content_type="application/json"
        )

    def _session(self, request: web.Request) -> BrowserSession:
        identity = request.headers.get("X-DevTools-Session", "")
        session = self.sessions.get(identity)
        if session is None or session.closed:
            raise web.HTTPUnauthorized(
                text="An active browser session is required"
            )
        return session

    async def _info(self, request: web.Request) -> web.Response:
        return self._json(
            {
                "protocol_version": DIAGNOSTIC_VERSION,
                "application_protocol_version": PROTOCOL_VERSION,
                "store_instance_id": self.host.store.instance_id,
                "schemas": self.host.store.schema_catalog(),
                "capture": self.devtools.recorder.activity(0),
                "limits": self.config,
                "capabilities": [
                    "inspect",
                    "activity",
                    "commands",
                    "history",
                    "preview",
                ],
            }
        )

    def _schemas(self, value: Any) -> tuple[str, ...] | None:
        if value is None:
            return None
        if (
            not isinstance(value, list)
            or len(value) > self.config.max_filter_values
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise ValueError("Scope must be a bounded list of schema names")
        catalog = self.host.store.schema_catalog()
        if any(item not in catalog for item in value):
            raise ValueError("Scope contains an unknown schema")
        return tuple(dict.fromkeys(value))

    async def _state(self, request: web.Request) -> web.Response:
        session = self._session(request)
        scope = session.scope
        if "schemas" in request.query:
            scope = self._schemas(
                request.query["schemas"].split(",")
                if request.query["schemas"]
                else []
            )
        checkpoint = encode_checkpoint(
            self.host.store.checkpoint(scope),
            self.host.store.schema_catalog(),
            self.config.max_payload_bytes,
        )
        return web.Response(
            text=dumps(checkpoint), content_type="application/json"
        )

    @staticmethod
    def _page(request: web.Request) -> tuple[int, int]:
        limit = int(request.query.get("limit", "100"))
        offset = int(request.query.get("offset", "0"))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError(
                "Page requires limit 1..100 and nonnegative offset"
            )
        return limit, offset

    async def _history(self, request: web.Request) -> web.Response:
        self._session(request)
        limit, offset = self._page(request)
        return web.Response(
            text=dumps(self.devtools.history_page(limit, offset)),
            content_type="application/json",
        )

    async def _activity(self, request: web.Request) -> web.Response:
        self._session(request)
        limit, _ = self._page(request)
        # Recorder values are already encoded; do not encode value tags twice.
        return web.Response(
            text=dumps(self.devtools.recorder.activity(limit)),
            content_type="application/json",
        )

    async def _preview(self, request: web.Request) -> web.Response:
        session = self._session(request)
        sequence = int(request.query["sequence"])
        result = self.devtools.recorder.preview(sequence)
        if result["available"] and session.scope is not None:
            checkpoint = result["checkpoint"]
            names = session.scope
            if any(name not in checkpoint["schemas"] for name in names):
                return web.json_response(
                    {
                        "available": False,
                        "reason": "Selected schema was not captured",
                    }
                )
            checkpoint["schemas"] = {
                name: checkpoint["schemas"][name]
                for name in names
                if name in checkpoint["schemas"]
            }
            checkpoint["schema_revisions"] = {
                name: revision
                for name, revision in checkpoint["schema_revisions"].items()
                if name in names
            }
            result["scope"] = list(checkpoint["schemas"])
        return web.Response(
            text=dumps(result), content_type="application/json"
        )

    async def _commands(self, request: web.Request) -> web.Response:
        session = self._session(request)
        if session.pending_commands >= self.config.max_pending_commands:
            raise web.HTTPTooManyRequests(text="Browser command limit reached")
        session.pending_commands += 1
        try:
            try:
                payload = await request.json()
                batch = decode_request(from_wire(payload))
                prohibited = (
                    SingleSubscriptionRequest,
                    CollectionSubscriptionRequest,
                    QuerySubscriptionRequest,
                    UnsubscribeRequest,
                )
                if any(isinstance(item, prohibited) for item in batch.items):
                    raise ValueError(
                        "Application subscriptions are unavailable here"
                    )
            except (
                ValueError,
                TypeError,
                web.HTTPRequestEntityTooLarge,
            ) as error:
                self.devtools.rejected(session.id, error)
                raise
            async with session.lock:
                if session.closed or self.stopping:
                    raise web.HTTPUnauthorized(
                        text="Browser session has closed"
                    )
                response = await self.host.request(session.id, batch)
            try:
                return self._json(encode_response(response))
            except (ValueError, TypeError, KeyError) as error:
                # Host execution is complete. A diagnostic size limit cannot
                # turn its outcome into an ordinary pre-execution rejection.
                if isinstance(error, CaptureTooLarge):
                    reason = "Command response exceeds diagnostic limit"
                else:
                    reason = "Command response could not be encoded"
                    logger.exception(
                        "Devtools command response encoding failed"
                    )
                return web.json_response(
                    {
                        "error": f"{reason}; outcome is unresolved",
                        "outcome": "unknown",
                    },
                    status=502,
                )

        finally:
            session.pending_commands -= 1

    async def _capture(self, request: web.Request) -> web.Response:
        self._session(request)
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) - {
            "paused",
            "filters",
        }:
            raise ValueError("Capture controls require paused and/or filters")
        recorder = self.devtools.recorder
        paused = payload.get("paused", recorder.paused)
        filters = payload.get("filters", recorder.filters)
        allowed = {"schema", "client_id", "category", "status", "identity"}
        if type(paused) is not bool or not isinstance(filters, dict):
            raise ValueError("Invalid recording controls")
        if set(filters) - allowed or any(
            not isinstance(value, str) or len(value) > 256
            for value in filters.values()
        ):
            raise ValueError("Invalid or oversized recording filter")
        if paused != recorder.paused:
            recorder.paused = paused
            recorder.invalidate("Activity recording was paused")
            if not paused:
                self.devtools.reset_checkpoint(self.host.store.checkpoint())
        recorder.filters = dict(filters)
        self.publish("capture", recorder.activity(0))
        return web.Response(
            text=dumps(recorder.activity()), content_type="application/json"
        )

    async def _stream(self, request: web.Request) -> web.WebSocketResponse:
        if len(self.sessions) >= self.config.max_sessions:
            raise web.HTTPServiceUnavailable(
                text="Browser session limit reached"
            )
        scope = (
            self._schemas(
                request.query["schemas"].split(",")
                if request.query["schemas"]
                else []
            )
            if "schemas" in request.query
            else None
        )
        websocket = web.WebSocketResponse(
            max_msg_size=min(self.config.max_request_bytes, 65536)
        )
        session = BrowserSession(token_urlsafe(32), websocket, scope=scope)
        self.sessions[session.id] = session

        async def discard(client_id: str, message: Any) -> None:
            # Browser sessions do not register application subscriptions.
            return None

        async def close(client_id: str) -> None:
            await websocket.close()

        try:
            await websocket.prepare(request)
            if session.closed or self.stopping:
                return websocket
            await self.host.connect(
                session.id,
                send=discard,
                close=close,
                protocol_version=PROTOCOL_VERSION,
            )
            self._snapshot(session)
            session.writer = asyncio.create_task(self._write(session))
            async for message in websocket:
                if message.type == WSMsgType.TEXT:
                    try:
                        control = json.loads(message.data)
                        if not isinstance(control, dict):
                            raise ValueError(
                                "Stream control must be an object"
                            )
                        if control.get("kind") != "scope":
                            raise ValueError("Unsupported stream control")
                        session.scope = self._schemas(control.get("schemas"))
                        self._snapshot(session)
                    except (ValueError, TypeError) as error:
                        self._offer(session, "error", {"error": str(error)})
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            await self._close_session(session)
        return websocket

    async def _close_session(self, session: BrowserSession) -> None:
        async with session.lock:
            if session.closed:
                return
            session.closed = True
            self.sessions.pop(session.id, None)
            try:
                await self.host.disconnect(session.id)
            finally:
                writer = session.writer
                if writer is not None and writer is not asyncio.current_task():
                    writer.cancel()
                    await asyncio.gather(writer, return_exceptions=True)
                session.queue.clear()
                session.queue_bytes = 0
                if session.websocket.prepared:
                    await session.websocket.close()

    def _snapshot(self, session: BrowserSession) -> None:
        session.epoch = uuid4().hex
        session.sequence = 0
        session.queue.clear()
        session.queue_bytes = 0
        session.resync = False
        try:
            raw_checkpoint = self.host.store.checkpoint(session.scope)
            checkpoint = encode_checkpoint(
                raw_checkpoint,
                self.host.store.schema_catalog(),
                self.config.max_payload_bytes,
            )
            recorder = self.devtools.recorder
            if recorder.checkpoint is None and not recorder.paused:
                recorder.reset(checkpoint)
            history = self.devtools.history_page()
            activity = self.devtools.recorder.activity(20)
            payload = {
                "session_id": session.id,
                "checkpoint": checkpoint,
                "history": history,
                "activity": activity,
            }
            # Keep State usable when retained details fill the stream budget.
            # Removed details remain available through bounded page endpoints.
            budget = self.config.max_queue_bytes - 512
            while activity["records"] and encoded_size(payload) > budget:
                activity["records"].pop(0)
                activity["dropped"] += 1
            while history["entries"] and encoded_size(payload) > budget:
                history["entries"].pop()
                history["truncated"] = True
            self._offer(session, "snapshot", payload)
        except CaptureTooLarge:
            session.oversized = True
            session.ready.set()

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        for session in tuple(self.sessions.values()):
            if kind == "commit" and session.scope is not None:
                scope = session.scope
                payload_for_session = dict(payload)
                payload_for_session["events"] = [
                    event
                    for event in payload.get("events", ())
                    if event["schema"] in scope
                ]
                payload_for_session["schema_revisions"] = {
                    name: revision
                    for name, revision in payload.get(
                        "schema_revisions", {}
                    ).items()
                    if name in scope
                }
                self._offer(session, kind, payload_for_session)
            elif kind == "gap":
                session.resync = True
                session.ready.set()
            else:
                self._offer(session, kind, payload)

    def _offer(
        self, session: BrowserSession, kind: str, payload: dict[str, Any]
    ) -> None:
        if session.closed or session.resync or session.oversized:
            return
        session.sequence += 1
        envelope = {
            "version": DIAGNOSTIC_VERSION,
            "epoch": session.epoch,
            "sequence": session.sequence,
            "store_instance_id": self.host.store.instance_id,
            "kind": kind,
            "payload": payload,
        }
        text = dumps(envelope)
        size = len(text.encode("utf-8"))
        if size > self.config.max_queue_bytes:
            session.oversized = True
        elif (
            session.queue_bytes + size > self.config.max_queue_bytes
            or len(session.queue) >= self.config.max_queue_messages
        ):
            session.queue.clear()
            session.queue_bytes = 0
            session.resync = True
        else:
            session.queue.append((text, size))
            session.queue_bytes += size
        session.ready.set()

    async def _write(self, session: BrowserSession) -> None:
        try:
            while not session.closed:
                await session.ready.wait()
                if session.oversized:
                    await session.websocket.close(
                        code=1009, message=b"Diagnostic snapshot exceeds limit"
                    )
                    return
                if session.resync:
                    # Fresh checkpoint replaces any undelivered deltas.
                    marker = {
                        "version": DIAGNOSTIC_VERSION,
                        "epoch": session.epoch,
                        "sequence": session.sequence + 1,
                        "store_instance_id": self.host.store.instance_id,
                        "kind": "gap",
                        "payload": {"reason": "Diagnostic continuity lost"},
                    }
                    await session.websocket.send_str(dumps(marker))
                    self._snapshot(session)
                    continue
                while (
                    session.queue
                    and not session.resync
                    and not session.oversized
                ):
                    text, size = session.queue.popleft()
                    session.queue_bytes -= size
                    await session.websocket.send_str(text)
                if not session.resync and not session.oversized:
                    session.ready.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Devtools browser writer failed")
            await session.websocket.close()
