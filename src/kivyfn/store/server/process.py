# kivyfn/store/server/process.py
from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any, cast

from kivyfn.runtime.process import FnProcessContext
from kivyfn.store.server.server import StoreServer
from kivyfn.store.server.types import ServerTransportFactory


if TYPE_CHECKING:
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.node import Node


class StoreServerProcess:
    """
    Child-process target for running one StoreServer.

    StoreServerProcess contains only the execution logic required inside the
    spawned process. Registration, process startup, shutdown timeouts, native
    process state, readiness state, and termination are owned by FnProcess and
    FnProcessManager.

    Register `StoreServerProcess.entry` as the target of an FnProcess. Supply
    the following launch arguments in order:

    - store root node
    - store configuration
    - server transport factory
    - keyword arguments for the transport factory

    The concrete transport and StoreServer are constructed inside the spawned
    process. This avoids attempting to pickle transport instances that may own
    event loops, sockets, locks, callbacks, or other process-local resources.
    """

    @staticmethod
    def entry(
        context: FnProcessContext,
    ) -> None:
        """
        Enter the StoreServer child process.

        This synchronous method is registered as the FnProcess target. The
        generic KivyFN process entry has already configured process logging,
        constructed the FnProcessContext, and installed lifecycle reporting
        before invoking this method.

        The store-specific launch values are read from `context.args`, then
        passed into the asynchronous StoreServer lifecycle through
        `asyncio.run()`.
        """
        root, config, transport_factory, transport_kwargs = (
            context.args
        )

        asyncio.run(
            StoreServerProcess.run(
                context=context,
                root=cast(
                    "Node",
                    root,
                ),
                config=cast(
                    "StoreConfig",
                    config,
                ),
                transport_factory=cast(
                    ServerTransportFactory,
                    transport_factory,
                ),
                transport_kwargs=cast(
                    dict[str, Any],
                    transport_kwargs,
                ),
            )
        )

    @staticmethod
    async def run(
        context: FnProcessContext,
        root: Node,
        config: StoreConfig,
        transport_factory: ServerTransportFactory,
        transport_kwargs: dict[str, Any],
    ) -> None:
        """
        Run one StoreServer until the parent requests process shutdown.

        The transport is created inside the child process, followed by the
        transport-independent StoreServer. Once startup completes, the
        transport endpoint is reported to the parent through
        `context.ready()`.

        The coroutine then waits for FnProcessManager to set the process stop
        event. The context listener safely forwards that notification from the
        context listener thread to the active asyncio event loop.

        StoreServer.stop() is always awaited before the child exits, including
        when startup, waiting, or another part of the lifecycle raises.
        """
        transport = transport_factory(
            **transport_kwargs,
        )

        server = StoreServer(
            root=root,
            config=config,
            transport=transport,
        )

        loop = asyncio.get_running_loop()
        stopped = asyncio.Event()

        def stop() -> None:
            """
            Forward the process stop request to the asyncio event loop.
            """
            loop.call_soon_threadsafe(
                stopped.set,
            )

        context.listen(
            stop,
        )

        try:
            await server.start()

            context.ready(
                transport.endpoint,
            )

            await stopped.wait()
        finally:
            await server.stop()
