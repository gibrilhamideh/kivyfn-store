from __future__ import annotations
from typing import TYPE_CHECKING, Any

import asyncio
import queue
import traceback
import multiprocessing

from multiprocessing.context import SpawnProcess


from kivyfn.logging.multiprocessing import configure_process_logging
from kivyfn.store.errors.client import StoreConnectionError, StoreStateError
from kivyfn.store.transport import SocketIOServer

if TYPE_CHECKING:
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.node import Node


class StoreServer:
    def __init__(
            self,
            root: Node,
            config: StoreConfig,
            host: str,
            port: int,
            log_queue: multiprocessing.Queue
    ) -> None:

        self.root = root
        self.config = config
        self.host = host
        self.port = port
        self.log_queue = log_queue

        self._ctx = multiprocessing.get_context("spawn")
        self._process: SpawnProcess | None = None
        self._ready_queue: multiprocessing.Queue[dict[str, Any]] | None = None


    async def start(self, *, timeout: float = 5.0) -> None:
        if self._process is not None:
            raise StoreStateError("Store server already started")

        self._ready_queue = self._ctx.Queue()

        self._process = self._ctx.Process(
            target=_run_transport,
            kwargs={
                "root": self.root,
                "config": self.config,
                "host": self.host,
                "port": self.port,
                "log_queue": self.log_queue,
                "ready_queue": self._ready_queue
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
        except queue.Empty:
            self.stop()
            raise StoreConnectionError("Store server did not become ready in time")

        if message["type"] == "ready":
            return

        if message["type"] == "error":
            self.stop()
            raise StoreConnectionError(
                "Store server failed to start:\n"
                f"{message['error']}\n\n"
                f"{message['traceback']}"
            )

        self.stop()
        raise StoreConnectionError(f"Unexpected startup message: {message}")


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
    log_queue: multiprocessing.Queue,
    ready_queue: multiprocessing.Queue,
) -> None:

    configure_process_logging(log_queue)

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

