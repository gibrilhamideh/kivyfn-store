from __future__ import annotations

import pickle
from typing import Any

import pytest

from kivyfn.store.builder import HostConfig, RelayConfig
from tests.relay.helpers import process_auth


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "direct://store"},
        {"endpoint": "http://user:pass@host"},
        {"endpoint": "http://host:0"},
        {"endpoint": "https://host?token=secret"},
        {"endpoint": "http://host/#fragment"},
        {"store_id": " "},
        {"reconnect_interval": 0},
        {"reconnect_interval": float("nan")},
        {"connect_timeout": True},
        {"request_timeout": float("inf")},
        {"max_clients": False},
        {"max_frame_bytes": 0},
        {"max_pending_messages": 0},
        {"max_pending_bytes": -1},
        {"auth": "token"},
    ],
)
def test_invalid_configuration(changes: dict[str, object]) -> None:
    values = {"endpoint": "https://host", "store_id": "app", **changes}
    with pytest.raises((TypeError, ValueError)):
        RelayConfig(**values)


def test_credentials_are_not_in_config_repr_and_provider_is_picklable() -> (
    None
):
    config = RelayConfig("https://host", "app", {"token": "SECRET"})
    assert "SECRET" not in repr(config)
    host = HostConfig(
        "http://127.0.0.1:0",
        relay=RelayConfig("https://host", "app", process_auth),
    )
    restored = pickle.loads(pickle.dumps(host))
    assert restored == host
    assert restored.relay.auth() == {"token": "test-only"}
    with pytest.raises(TypeError):
        invalid: Any = True
        HostConfig("http://127.0.0.1:0", relay=invalid)
