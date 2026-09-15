"""Exercise installed wheel/sdist releases outside the checkout, without Node.

Run with the actual project runtime dependencies, pip, setuptools and wheel
already installed. Dependency substitution is not performed by this check.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

SMOKE = """
import asyncio
import re
from pathlib import Path
from shutil import which

from aiohttp import ClientSession

from kivyfn.store import (
    DevToolsConfig, HistoryConfig, HostConfig, Store, StoreClient, StoreHost,
)
from kivyfn.store import Initializer as BaseInitializer
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.protocol.message.requests import SetRequestAction
from kivyfn.store.schema import NumericField, SingleSchema


class InstalledSettings(SingleSchema):
    track_history = True
    value = NumericField(default=0)


class Initializer(BaseInitializer):
    def run(self, data):
        data["InstalledSettings"] = {"value": 5}
        return data


async def check(transport):
    endpoint = (
        "direct://installed" if transport == "direct"
        else "http://127.0.0.1:0"
    )
    config = (
        Builder().tree(Branch(InstalledSettings))
        .storage_path(Path("data") / transport)
        .initializer(Initializer()).history(HistoryConfig())
        .host(HostConfig(endpoint, devtools=DevToolsConfig(port=0))).build()
    )
    store = Store(config)
    store.boot()
    assert store.snapshot_by_schema("InstalledSettings")["value"] == 5
    assert not store.history().status.can_undo
    host = StoreHost(store)
    client = StoreClient()
    try:
        await host.start()
        address = (
            endpoint if transport == "direct"
            else host.transport.endpoint["endpoint"]
        )
        await client.connect(address)
        await client.transaction(
            transaction_id="installed-edit",
            actions=(SetRequestAction("InstalledSettings", (("value", 1),)),),
        )
        assert store.snapshot_by_schema("InstalledSettings")["value"] == 1
        await client.undo()
        assert store.snapshot_by_schema("InstalledSettings")["value"] == 5
        await client.redo()
        assert store.snapshot_by_schema("InstalledSettings")["value"] == 1
        async with ClientSession() as http:
            async with http.get(host.devtools_url) as response:
                assert response.status == 200
                html = await response.text()
            assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
            assert any(path.endswith(".js") for path in assets), html
            for path in assets:
                url = host.devtools_url.rstrip("/") + path
                async with http.get(url) as response:
                    assert response.status == 200, url
                    assert await response.read()
            async with http.ws_connect(
                host.devtools_url + "/stream?schemas=InstalledSettings"
            ) as stream:
                initial = await stream.receive_json(timeout=5)
                assert initial["kind"] == "snapshot"
                headers = {
                    "X-DevTools-Session": initial["payload"]["session_id"]
                }
                batch = {"requests": [{
                    "type": "transaction",
                    "transaction_id": "installed-browser-edit",
                    "actions": [{
                        "type": "set", "schema": "InstalledSettings",
                        "values": {"value": 2},
                    }],
                }]}
                async with http.post(
                    host.devtools_url + "/api/commands",
                    headers=headers, json=batch,
                ) as response:
                    assert response.status == 200
                    reply = await response.json()
                    assert "failure" not in reply["responses"][0], reply
                while True:
                    event = await stream.receive_json(timeout=5)
                    if event["kind"] == "commit":
                        assert event["payload"]["transaction_id"] == (
                            "installed-browser-edit"
                        )
                        assert event["payload"]["events"][0]["new"] == 2
                        break
                assert store.snapshot_by_schema(
                    "InstalledSettings"
                )["value"] == 2
    finally:
        await client.disconnect()
        await host.stop()
        store.shutdown()
    assert host.devtools_url is None


async def main():
    assert which("node") is None
    assert which("npm") is None
    for transport in ("direct", "socketio"):
        await check(transport)


asyncio.run(main())
print("Installed assets, browser stream/commands, and history passed")
"""


def check(archive: Path) -> None:
    with TemporaryDirectory(prefix="kivyfn-installed-") as temporary:
        root = Path(temporary)
        site = root / "site"
        empty_path = root / "no-node"
        empty_path.mkdir()
        environment = dict(os.environ)
        environment.update(
            PATH=str(empty_path),
            PYTHONPATH=str(site),
            KIVY_NO_ARGS="1",
            KIVY_NO_CONSOLELOG="1",
            KIVY_NO_FILELOG="1",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-build-isolation",
                "--target",
                str(site),
                str(archive),
            ],
            cwd=root,
            env=environment,
            check=True,
        )
        subprocess.run(
            [sys.executable, "-c", SMOKE],
            cwd=root,
            env=environment,
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory.resolve()
    wheels = sorted(directory.glob("kivyfn_store-*.whl"))
    sources = sorted(directory.glob("kivyfn_store-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error("Use a directory with exactly one wheel and one sdist")
    for archive in [*wheels, *sources]:
        print(f"Checking {archive.name}", flush=True)
        check(archive)


if __name__ == "__main__":
    main()
