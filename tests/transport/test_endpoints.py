from __future__ import annotations

import pytest

from kivyfn.store.client.client import StoreClient
from kivyfn.store.transport.direct.client import DirectClientTransport
from kivyfn.store.transport.direct.host import DirectHostTransport
from kivyfn.store.transport.endpoint import (
    create_client_transport,
    create_host_transport,
)
from kivyfn.store.transport.socketio.client import SocketIOClientTransport
from kivyfn.store.transport.socketio.server import SocketIOServerTransport


@pytest.mark.parametrize(
    "endpoint",
    [
        "localhost:1234",
        "file:///tmp/store",
        "direct://",
        "direct://store/path",
        "direct://store?query=1",
        "direct://store#fragment",
        "http:///socket.io",
        "http://user:secret@localhost:1234",
        "http://localhost:65536",
        "http://localhost:invalid",
    ],
)
def test_invalid_endpoints_fail_transport_creation(endpoint: str) -> None:
    with pytest.raises(ValueError):
        create_host_transport(endpoint)
    with pytest.raises(ValueError):
        create_client_transport(endpoint, StoreClient())


@pytest.mark.parametrize("endpoint", ["direct://store", "direct://store/"])
def test_direct_endpoints_preserve_registry_name(endpoint: str) -> None:
    host = create_host_transport(endpoint)
    client = create_client_transport(endpoint, StoreClient())
    assert isinstance(host, DirectHostTransport)
    assert host.name == "store"
    assert isinstance(client, DirectClientTransport)
    assert client.name == "store"
    assert not client.connected


@pytest.mark.parametrize(
    ("endpoint", "host", "port", "path", "client_endpoint"),
    [
        (
            "http://127.0.0.1:0/custom/socket/",
            "127.0.0.1",
            0,
            "custom/socket",
            "http://127.0.0.1:0/custom/socket",
        ),
        (
            "http://0.0.0.0:9876",
            "0.0.0.0",
            9876,
            "socket.io",
            "http://127.0.0.1:9876/socket.io",
        ),
        (
            "http://[::]:9876/custom",
            "::",
            9876,
            "custom",
            "http://[::1]:9876/custom",
        ),
        (
            "http://localhost",
            "localhost",
            80,
            "socket.io",
            "http://localhost:80/socket.io",
        ),
    ],
)
def test_host_endpoint_reports_client_usable_address(
    endpoint: str, host: str, port: int, path: str, client_endpoint: str
) -> None:
    transport = create_host_transport(endpoint)
    assert isinstance(transport, SocketIOServerTransport)
    assert transport.host == host
    assert transport.port == port
    assert transport.socketio_path == path
    assert transport.endpoint["endpoint"] == client_endpoint
    assert not transport.running


def test_zero_port_is_reserved_for_listeners() -> None:
    assert isinstance(
        create_host_transport("http://127.0.0.1:0"), SocketIOServerTransport
    )
    with pytest.raises(ValueError, match="nonzero port"):
        create_client_transport("http://127.0.0.1:0", StoreClient())


def test_https_and_query_parameters_belong_to_client_endpoints() -> None:
    client = create_client_transport(
        "https://localhost/custom?tenant=example", StoreClient()
    )
    assert isinstance(client, SocketIOClientTransport)
    assert client.url == "https://localhost?tenant=example"
    assert client.socketio_path == "custom"
    assert not client.connected
    with pytest.raises(ValueError, match="HTTPS upstream"):
        create_host_transport("https://localhost/custom")
    with pytest.raises(ValueError, match="cannot contain a query"):
        create_host_transport("http://localhost/custom?tenant=example")
