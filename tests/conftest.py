from __future__ import annotations

import logging
import sys
import types
import uuid
from collections.abc import Callable
from typing import cast


try:
    import uuid6  # noqa: F401
except ModuleNotFoundError:
    uuid6_module = types.ModuleType("uuid6")
    setattr(uuid6_module, "uuid7", uuid.uuid4)
    sys.modules["uuid6"] = uuid6_module


try:
    import kivy.event  # noqa: F401
except ModuleNotFoundError:
    kivy_module = types.ModuleType("kivy")
    event_module = types.ModuleType("kivy.event")

    class FallbackEventDispatcher:
        def __init__(self, **_kwargs: object) -> None:
            self._bindings: dict[
                str,
                list[Callable[..., object]],
            ] = {}

        def bind(
            self,
            **kwargs: Callable[..., object],
        ) -> None:
            for name, callback in kwargs.items():
                self._bindings.setdefault(name, []).append(callback)

        def unbind(
            self,
            **kwargs: Callable[..., object],
        ) -> None:
            for name, callback in kwargs.items():
                bucket = self._bindings.get(name, [])

                if callback in bucket:
                    bucket.remove(callback)

        def dispatch(
            self,
            name: str,
            *args: object,
        ) -> None:
            handler = cast(
                Callable[..., object],
                getattr(self, name),
            )
            handler(*args)

            for callback in tuple(self._bindings.get(name, ())):
                callback(self, *args)

    setattr(
        event_module,
        "EventDispatcher",
        FallbackEventDispatcher,
    )
    setattr(kivy_module, "event", event_module)
    sys.modules["kivy"] = kivy_module
    sys.modules["kivy.event"] = event_module


try:
    import kivyfn.logging  # noqa: F401
except ModuleNotFoundError:
    logging_module = types.ModuleType("kivyfn.logging")
    setattr(logging_module, "get_logger", logging.getLogger)
    sys.modules["kivyfn.logging"] = logging_module


try:
    import kivyfn.runtime.process  # noqa: F401
except ModuleNotFoundError:
    runtime_module = types.ModuleType("kivyfn.runtime")
    process_module = types.ModuleType("kivyfn.runtime.process")

    class FallbackFnProcessContext:
        pass

    setattr(
        process_module,
        "FnProcessContext",
        FallbackFnProcessContext,
    )
    setattr(runtime_module, "process", process_module)
    sys.modules["kivyfn.runtime"] = runtime_module
    sys.modules["kivyfn.runtime.process"] = process_module
