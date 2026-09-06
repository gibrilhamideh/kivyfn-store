# kivyfn/store/local/signal.py
from __future__ import annotations

import inspect
import weakref

from collections import defaultdict
from collections.abc import Callable
from threading import RLock
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
    cast,
    overload,
)

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.signals.event import SignalEvent
from kivyfn.store.utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


logger = get_logger(__name__)


T = TypeVar("T")

SignalCallback = Callable[[SignalEvent[T]], None]
SignalKey = tuple[Address[Any], str, str | None]


class _CallbackReference(Generic[T]):
    """
    Holds bound methods weakly and other callables strongly.

    Bound methods are weakly referenced so signal bindings do not keep their
    owning controller, state, or widget alive. Functions, lambdas, and
    callable objects are retained strongly.
    """

    __slots__ = (
        "_strong",
        "_weak",
    )

    def __init__(
        self,
        callback: SignalCallback[T],
    ) -> None:
        self._strong: SignalCallback[T] | None = None
        self._weak: weakref.WeakMethod[Any] | None = None

        if inspect.ismethod(callback) and callback.__self__ is not None:
            self._weak = weakref.WeakMethod(
                callback,
            )
            return

        self._strong = callback

    def get(self) -> SignalCallback[T] | None:
        if self._weak is not None:
            return cast(
                SignalCallback[T] | None,
                self._weak(),
            )

        return self._strong


class LocalSignalBinding(Generic[T]):
    """
    Synchronous local signal binding.

    dispose() is idempotent. A binding using a bound-method callback is
    automatically removed after the callback owner is garbage-collected.
    """

    __slots__ = (
        "_manager_ref",
        "_token",
        "_address",
        "_signal",
        "_id",
        "_callback",
        "_disposed",
    )

    def __init__(
        self,
        manager: LocalSignalManager,
        address: Address[Any],
        signal: str,
        callback: SignalCallback[T],
        *,
        id: str | None,
    ) -> None:
        self._manager_ref = weakref.ref(
            manager,
        )
        self._token = generate_uuid()
        self._address = address
        self._signal = signal
        self._id = id
        self._callback = _CallbackReference(
            callback,
        )
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return

        manager = self._manager_ref()

        if manager is None:
            self._mark_disposed()
            return

        manager.unbind(
            self._token,
        )

    def dispatch(
        self,
        event: SignalEvent[T],
    ) -> bool:
        """
        Invoke the callback.

        Returns False when the callback no longer exists and the binding
        should be removed.
        """
        if self._disposed:
            return False

        callback = self._callback.get()

        if callback is None:
            return False

        try:
            result = callback(
                event,
            )

            if inspect.isawaitable(result):
                close = getattr(
                    result,
                    "close",
                    None,
                )

                if callable(close):
                    close()

                raise TypeError(
                    "LocalStore signal callbacks must be synchronous"
                )

        except Exception:
            logger.exception(
                (
                    "Local store signal callback failed: "
                    "token=%s signal=%s"
                ),
                self._token,
                self._signal,
            )

        return True

    def _mark_disposed(self) -> None:
        self._disposed = True

    @property
    def token(self) -> str:
        return self._token

    @property
    def address(self) -> Address[Any]:
        return self._address

    @property
    def signal(self) -> str:
        return self._signal

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def key(self) -> SignalKey:
        return (
            self._address,
            self._signal,
            self._id,
        )

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"token={self._token!r} "
            f"address={self._address!r} "
            f"signal={self._signal!r} "
            f"id={self._id!r} "
            f"disposed={self._disposed}>"
        )


class LocalSignalManager:
    """
    Synchronous in-process signal manager.

    Signal scope behavior:

    - A binding with id=None receives every emission of that signal.
    - A binding with a specific id receives only emissions for that row.
    - An emission with id=None is delivered only to signal-wide bindings.
    - An emission with an id is delivered to both signal-wide bindings and
      bindings for that specific row.

    It contains no asyncio, client IDs, transport rooms, or serialization.
    """

    def __init__(
        self,
        engine: Engine,
    ) -> None:
        self._engine = engine
        self._lock = RLock()

        self._bindings: dict[
            str,
            LocalSignalBinding[Any],
        ] = {}

        self._bindings_by_key: dict[
            SignalKey,
            set[str],
        ] = defaultdict(set)

    # -------------------------------------------------
    # Public API
    # -------------------------------------------------

    def validate(
        self,
        address: Address[Any],
        signal: str,
        *,
        id: str | None = None,
    ) -> None:
        """Validate one signal target without dispatching it."""
        node = self._engine.get_node_by_address(
            address,
        )

        self._validate_scope(
            node=node,
            signal=signal,
            id=id,
        )

    def bind(
        self,
        address: Address[Any],
        signal: str,
        callback: SignalCallback[T],
        *,
        id: str | None = None,
    ) -> LocalSignalBinding[T]:
        node = self._engine.get_node_by_address(
            address,
        )

        self._validate_scope(
            node=node,
            signal=signal,
            id=id,
        )

        binding = LocalSignalBinding(
            manager=self,
            address=address,
            signal=signal,
            callback=callback,
            id=id,
        )

        with self._lock:
            self._bindings[
                binding.token
            ] = binding

            self._bindings_by_key[
                binding.key
            ].add(
                binding.token,
            )

        return binding

    def unbind(
        self,
        token: str,
    ) -> bool:
        """
        Remove a signal binding.

        Returns False when the token was already absent.
        """
        with self._lock:
            binding = self._bindings.pop(
                token,
                None,
            )

            if binding is None:
                return False

            bucket = self._bindings_by_key.get(
                binding.key,
            )

            if bucket is not None:
                bucket.discard(
                    token,
                )

                if not bucket:
                    self._bindings_by_key.pop(
                        binding.key,
                        None,
                    )

            binding._mark_disposed()

            return True

    @overload
    def emit(
        self,
        address: Address[Any],
        signal: str,
        *,
        id: str | None = None,
    ) -> SignalEvent[None]:
        ...

    @overload
    def emit(
        self,
        address: Address[Any],
        signal: str,
        data: T,
        *,
        id: str | None = None,
    ) -> SignalEvent[T]:
        ...

    def emit(
        self,
        address: Address[Any],
        signal: str,
        data: Any = None,
        *,
        id: str | None = None,
    ) -> SignalEvent[Any]:
        node = self._engine.get_node_by_address(
            address,
        )

        self._validate_scope(
            node=node,
            signal=signal,
            id=id,
        )

        event = SignalEvent(
            address=address,
            signal=signal,
            id=id,
            data=data,
        )

        bindings = self._collect_bindings(
            address=address,
            signal=signal,
            id=id,
        )

        dead_tokens: set[str] = set()

        for binding in bindings:
            if not binding.dispatch(
                event,
            ):
                dead_tokens.add(
                    binding.token,
                )

        for token in dead_tokens:
            self.unbind(
                token,
            )

        return event

    def clear(self) -> None:
        """Dispose all signal bindings."""
        with self._lock:
            bindings = tuple(
                self._bindings.values(),
            )

            self._bindings.clear()
            self._bindings_by_key.clear()

            for binding in bindings:
                binding._mark_disposed()

    # -------------------------------------------------
    # Dispatch collection
    # -------------------------------------------------

    def _collect_bindings(
        self,
        *,
        address: Address[Any],
        signal: str,
        id: str | None,
    ) -> tuple[LocalSignalBinding[Any], ...]:
        with self._lock:
            tokens: set[str] = set()

            tokens.update(
                self._bindings_by_key.get(
                    (
                        address,
                        signal,
                        None,
                    ),
                    (),
                )
            )

            if id is not None:
                tokens.update(
                    self._bindings_by_key.get(
                        (
                            address,
                            signal,
                            id,
                        ),
                        (),
                    )
                )

            return tuple(
                binding
                for token in tokens
                if (
                    binding := self._bindings.get(
                        token,
                    )
                ) is not None
            )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    @staticmethod
    def _validate_scope(
        *,
        node: Node,
        signal: str,
        id: str | None,
    ) -> None:
        node.schema.get_signal(
            signal,
        )

        if isinstance(
            node.schema,
            SingleSchema,
        ):
            if id is not None:
                raise ValueError(
                    f"Signal {signal!r} on {node.schema.name} "
                    "does not accept a row id"
                )

            return

        if not isinstance(
            node.schema,
            ManySchema,
        ):
            raise TypeError(
                (
                    f"Schema {node.schema.name!r} must inherit "
                    "SingleSchema or ManySchema"
                )
            )

        if id is None:
            return

        if not node.exists(
            id,
        ):
            raise ValueError(
                f"Row {id!r} does not exist in "
                f"{node.schema.name}"
            )

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(
                self._bindings,
            )

    def __bool__(self) -> bool:
        return len(self) > 0
