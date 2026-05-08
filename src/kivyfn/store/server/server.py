from __future__ import annotations

import asyncio
import multiprocessing
import queue
import traceback
from typing import TYPE_CHECKING, Any

from kivyfn.store.errors.client import StoreStateError
from kivyfn.store.transport import SocketIOServer

if TYPE_CHECKING:
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.node import Node


class StoreServer:
    def __init__(self, root: Node, config: StoreConfig, host: str, port: int) -> None:
        self.root = root
        self.config = config
        self.host = host
        self.port = port

        self._process: multiprocessing.Process | None = None
        self._ready_queue: multiprocessing.Queue[dict[str, Any]] | None = None

    async def start(self, *, timeout: float = 5.0) -> None:
        if self._process is not None:
            raise StoreStateError("Store server already started")

        self._ready_queue = multiprocessing.Queue()

        self._process = multiprocessing.Process(
            target=_run_transport,
            kwargs={
                "root": self.root,
                "config": self.config,
                "host": self.host,
                "port": self.port,
                "ready_queue": self._ready_queue,
            },
            daemon=True,
        )

        self._process.start()

        try:
            message = await asyncio.to_thread(
                self._ready_queue.get,
                True,
                timeout,
            )
        except queue.Empty as exc:
            self.stop()
            raise StoreStateError("Store server did not become ready in time") from exc

        if message["type"] == "ready":
            return

        if message["type"] == "error":
            self.stop()
            raise StoreStateError(
                "Store server failed to start:\n"
                f"{message['error']}\n\n"
                f"{message['traceback']}"
            )

        self.stop()
        raise StoreStateError(f"Unexpected store server startup message: {message}")

    def stop(self) -> None:
        if self._process is None:
            return

        self._process.terminate()
        self._process.join(timeout=2)

        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=2)

        self._process = None
        self._ready_queue = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.is_alive()


def _run_transport(
    root,
    config,
    host: str,
    port: int,
    ready_queue: multiprocessing.Queue,
) -> None:
    try:
        transport = SocketIOServer(
            root=root,
            config=config,
            host=host,
            port=port,
            on_ready=lambda: ready_queue.put({"type": "ready"}),
        )

        asyncio.run(transport.run())

    except Exception as exc:
        ready_queue.put(
            {
                "type": "error",
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }
        )
        raise

