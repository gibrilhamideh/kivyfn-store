# src/kivyfn/store/process.py
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from itertools import count
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast
from urllib.parse import urlsplit

from kivyfn.store.host.host import StoreHost
from kivyfn.store.store import Store

if TYPE_CHECKING:
    from kivyfn.store.builder.config import StoreConfig


class ProcessContext(Protocol):
    """Child-process hooks required by the Store runner."""

    @property
    def args(self) -> tuple[Any, ...]: ...

    def listen(self, callback: Callable[[], None]) -> None: ...

    def ready(self, payload: dict[str, Any]) -> None: ...


class ProcessHandle(Protocol):
    """Read-only lifecycle information exposed by a process manager."""

    @property
    def name(self) -> str: ...

    @property
    def running(self) -> bool: ...

    @property
    def ready(self) -> bool: ...

    @property
    def ready_payload(self) -> dict[str, Any] | None: ...

    @property
    def pid(self) -> int | None: ...


class ProcessManager(Protocol):
    """The process-management operations used by StoreProcess."""

    def register(
        self, name: str, target: Callable[..., None]
    ) -> ProcessHandle: ...

    def start(
        self,
        name: str,
        *,
        args: tuple[Any, ...],
        wait: bool,
        timeout: float | None,
        daemon: bool,
    ) -> object: ...

    def stop(self, name: str, *, timeout: float | None) -> object: ...


class StoreProcess:
    """
    Run a Store and its host in a managed child process.

    The child constructs the Store, host, and transport from one
    configuration. Readiness includes the bound transport address. Direct
    transport is limited to callers in the same process.
    """

    _counter: ClassVar[Iterator[int]] = count(1)
    _name_prefix: ClassVar[str] = "kivyfn-store-server"

    def __init__(
        self,
        processes: ProcessManager,
        config: StoreConfig,
        *,
        name: str | None = None,
        startup_timeout: float | None = 10.0,
        shutdown_timeout: float | None = 5.0,
    ) -> None:
        if config.host is None:
            raise ValueError("StoreProcess requires a HostConfig")
        if urlsplit(config.host.endpoint).scheme == "direct":
            raise ValueError(
                "Direct transport cannot connect clients across processes"
            )

        self._processes = processes
        self._config = config
        self._name = (
            f"{self._name_prefix}.{next(self._counter):03d}"
            if name is None
            else name.strip()
        )
        if not self._name:
            raise ValueError("Store process name cannot be empty")

        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
        self._process: ProcessHandle | None = None

    def start(self) -> ProcessHandle:
        """Start the child and wait until its host accepts connections."""
        process = self._process
        if process is None:
            process = self._processes.register(self._name, StoreProcess.entry)
            self._process = process

        if not process.running:
            self._processes.start(
                process.name,
                args=(self._config,),
                wait=True,
                timeout=self._startup_timeout,
                daemon=False,
            )

        return process

    def stop(self) -> None:
        """Request host cleanup and Store shutdown in the child."""
        process = self._process
        if process is None or not process.running:
            return

        self._processes.stop(process.name, timeout=self._shutdown_timeout)

    @staticmethod
    def entry(context: ProcessContext) -> None:
        """Entry point called by the supplied manager in the child process."""
        (config,) = context.args
        asyncio.run(StoreProcess._run(context, cast("StoreConfig", config)))

    @staticmethod
    async def _run(context: ProcessContext, config: StoreConfig) -> None:
        loop = asyncio.get_running_loop()
        stopped = asyncio.Event()

        def stop() -> None:
            loop.call_soon_threadsafe(stopped.set)

        context.listen(stop)

        store = Store(config)
        try:
            store.boot()
            host = StoreHost(store)
            try:
                await host.start()
                payload = dict(host.transport.endpoint)
                payload["devtools_url"] = host.devtools_url
                context.ready(payload)
                stop_waiter = asyncio.create_task(stopped.wait())
                host_waiter = asyncio.create_task(host.wait_closed())
                try:
                    done, _ = await asyncio.wait(
                        (stop_waiter, host_waiter),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if host_waiter in done:
                        await host_waiter
                finally:
                    stop_waiter.cancel()
                    host_waiter.cancel()
                    await asyncio.gather(
                        stop_waiter, host_waiter, return_exceptions=True
                    )
            finally:
                await host.stop()
        finally:
            store.shutdown()

    @property
    def name(self) -> str:
        return self._name

    @property
    def endpoint(self) -> str:
        """Bound client endpoint, or the configured endpoint before ready."""
        return self.client_endpoint or self.configured_endpoint

    @property
    def configured_endpoint(self) -> str:
        host = self._config.host
        assert host is not None
        return host.endpoint

    @property
    def client_endpoint(self) -> str | None:
        """Endpoint accepted by StoreClient once the child is ready."""
        payload = self.ready_payload
        if payload is None:
            return None

        endpoint = payload.get("endpoint")
        return endpoint if isinstance(endpoint, str) and endpoint else None

    @property
    def process(self) -> ProcessHandle | None:
        return self._process

    @property
    def registered(self) -> bool:
        return self._process is not None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.running

    @property
    def ready(self) -> bool:
        return self._process is not None and self._process.ready

    @property
    def ready_payload(self) -> dict[str, Any] | None:
        process = self._process
        return None if process is None else process.ready_payload

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid
