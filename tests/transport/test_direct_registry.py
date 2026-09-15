from __future__ import annotations

import gc
import weakref
from uuid import uuid4

import pytest

from kivyfn.store.transport.direct.registry import (
    register_host,
    resolve_host,
    unregister_host,
)


class RegistryHost:
    pass


def test_registry_rejects_collision_without_displacing_owner() -> None:
    name = uuid4().hex
    first = RegistryHost()
    second = RegistryHost()
    register_host(name, first)
    try:
        register_host(name, first)
        with pytest.raises(RuntimeError, match="already registered"):
            register_host(name, second)
        assert resolve_host(name) is first
        unregister_host(name, second)
        assert resolve_host(name) is first
    finally:
        unregister_host(name, first)
    with pytest.raises(LookupError, match="not registered"):
        resolve_host(name)
    register_host(name, second)
    try:
        assert resolve_host(name) is second
    finally:
        unregister_host(name, second)


def test_registry_does_not_keep_abandoned_transport_alive() -> None:
    name = uuid4().hex
    host = RegistryHost()
    reference = weakref.ref(host)
    register_host(name, host)
    del host
    gc.collect()
    assert reference() is None
    with pytest.raises(LookupError, match="not registered"):
        resolve_host(name)


def test_registry_rejects_empty_names_at_public_boundary() -> None:
    host = RegistryHost()
    with pytest.raises(ValueError, match="non-empty"):
        register_host("", host)
    with pytest.raises(ValueError, match="non-empty"):
        resolve_host("")
    with pytest.raises(ValueError, match="non-empty"):
        unregister_host("", host)
