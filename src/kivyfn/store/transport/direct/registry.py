from __future__ import annotations

from threading import RLock
from weakref import WeakValueDictionary

_hosts: WeakValueDictionary[str, object] = WeakValueDictionary()
_lock = RLock()


def register_host(name: str, host: object) -> None:
    """Register one in-process host under a direct endpoint name."""
    _validate_name(name)

    with _lock:
        current = _hosts.get(name)

        if current is host:
            return

        if current is not None:
            raise RuntimeError(
                f"Direct Store endpoint is already registered: {name!r}"
            )

        _hosts[name] = host


def unregister_host(name: str, host: object) -> None:
    """Remove a host only when it still owns the registered endpoint."""
    _validate_name(name)

    with _lock:
        current = _hosts.get(name)

        if current is not host:
            return

        del _hosts[name]


def resolve_host(name: str) -> object:
    """Resolve the host registered for an in-process endpoint name."""
    _validate_name(name)

    with _lock:
        host = _hosts.get(name)

    if host is None:
        raise LookupError(f"Direct Store endpoint is not registered: {name!r}")

    return host


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError(
            "Direct Store endpoint name must be a non-empty string"
        )
