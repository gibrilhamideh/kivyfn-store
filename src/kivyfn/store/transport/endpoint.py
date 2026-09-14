# src/kivyfn/store/transport/endpoint.py
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import SplitResult, urlsplit

from kivyfn.store.transport.client import ClientTransport
from kivyfn.store.transport.direct.client import DirectClientTransport
from kivyfn.store.transport.direct.host import DirectHostTransport
from kivyfn.store.transport.host import HostTransport

if TYPE_CHECKING:
    from kivyfn.store.client.client import StoreClient


def create_client_transport(
    endpoint: str, client: StoreClient
) -> ClientTransport:
    """Create a client transport for one StoreClient."""
    parsed = _parse_endpoint(endpoint)

    match parsed.scheme:
        case "direct":
            return DirectClientTransport(_direct_name(parsed), client)

        case "http" | "https":
            _, port, path = _socketio_endpoint(parsed)

            if port == 0:
                raise ValueError("Client endpoints require a nonzero port")

            from kivyfn.store.transport.socketio.client import (
                SocketIOClientTransport,
            )

            return SocketIOClientTransport(
                parsed._replace(path="").geturl(), client, socketio_path=path
            )

        case _:
            raise ValueError(f"Unsupported Store transport: {parsed.scheme!r}")


def create_host_transport(endpoint: str) -> HostTransport:
    """Create a Direct host or an HTTP Socket.IO listener."""
    parsed = _parse_endpoint(endpoint)

    match parsed.scheme:
        case "direct":
            return DirectHostTransport(_direct_name(parsed))

        case "http":
            host, port, path = _socketio_endpoint(parsed)

            if parsed.query:
                raise ValueError("Host endpoints cannot contain a query")

            from kivyfn.store.transport.socketio.server import (
                SocketIOServerTransport,
            )

            return SocketIOServerTransport(
                host=host, port=port, socketio_path=path
            )

        case "https":
            raise ValueError(
                "Socket.IO hosts listen on HTTP; terminate HTTPS upstream"
            )

        case _:
            raise ValueError(f"Unsupported Store transport: {parsed.scheme!r}")


def _parse_endpoint(endpoint: str) -> SplitResult:
    parsed = urlsplit(endpoint)

    if not parsed.scheme:
        raise ValueError("Store endpoints require a transport scheme")

    if parsed.fragment:
        raise ValueError("Store endpoints cannot contain a fragment")

    return parsed


def _direct_name(endpoint: SplitResult) -> str:
    if not endpoint.netloc:
        raise ValueError("Direct endpoints require a name")

    if endpoint.path not in ("", "/") or endpoint.query:
        raise ValueError("Direct endpoints contain only a name")

    return endpoint.netloc


def _socketio_endpoint(endpoint: SplitResult) -> tuple[str, int, str]:
    host = endpoint.hostname

    if host is None:
        raise ValueError("Socket.IO endpoints require a hostname")

    if endpoint.username is not None or endpoint.password is not None:
        raise ValueError("Socket.IO endpoints cannot contain URL credentials")

    port = endpoint.port

    if port is None:
        port = 443 if endpoint.scheme == "https" else 80

    path = endpoint.path.strip("/") or "socket.io"

    return host, port, path
