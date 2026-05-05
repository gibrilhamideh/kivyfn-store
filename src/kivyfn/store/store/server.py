from __future__ import annotations

import asyncio
import multiprocessing
from typing import TYPE_CHECKING

from kivyfn.store.transport import SocketIOServer

if TYPE_CHECKING:
    from kivyfn.store.node import Node
    from kivyfn.store.builder.config import StoreConfig


class StoreServer:
    """
    Lives in the main/owner process.

    Responsibility:
        - start the store process
        - stop the store process

    It does NOT own Engine directly.
    It does NOT handle websocket messages directly.
    """

    def __init__(self, root: Node, config: StoreConfig, host: str, port: int) -> None:
        self.root = root
        self.config = config
        self.host = host
        self.port = port

        self._process: multiprocessing.Process | None = None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("Store server already started")

        self._process = multiprocessing.Process(
            target=_run_transport,
            kwargs={
                "root": self.root,
                "config": self.config,
                "host": self.host,
                "port": self.port,
            },
            daemon=True,
        )

        self._process.start()

    def stop(self) -> None:
        if self._process is None:
            return

        self._process.terminate()
        self._process.join(timeout=2)
        self._process = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.is_alive()


def _run_transport(root, config, host: str, port: int) -> None:
    transport = SocketIOServer(
        root=root,
        config=config,
        host=host,
        port=port,
    )

    asyncio.run(transport.run())
