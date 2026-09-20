"""Real Store/Host/Client process used by the browser release checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from uuid import uuid4

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.builder.config import HistoryConfig, HostConfig
from kivyfn.store.codec.requests import decode_request
from kivyfn.store.devtools import DevToolsConfig
from kivyfn.store.devtools.protocol import from_wire
from kivyfn.store.protocol.message.requests import TransactionRequest
from kivyfn.store.protocol.message.responses import TransactionFailureResponse
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.fields import DataField, NumericField, StringField
from kivyfn.store.schema.signal import Signal


class BrowserModel(SingleSchema):
    track_history = True
    x = NumericField(default=0)
    title = StringField(default="hello")
    open_window = Signal[dict[str, str]](undo="close_window")
    close_window = Signal[dict[str, str]](undo="open_window")


class BrowserRows(ManySchema):
    track_history = True
    title = StringField(default="row")
    number = NumericField(default=0)
    payload = DataField(default=None, allow_none=True)
    inspect_row = Signal[None]()


async def run(transport: str, history_disabled: bool) -> None:
    with TemporaryDirectory(prefix="store-browser-") as directory:
        tree = Branch(BrowserModel).child("rows", Branch(BrowserRows))
        for index in range(35):
            schema = type(
                f"BrowserExtra{index:02}",
                (SingleSchema,),
                {"value": NumericField(default=index)},
            )
            tree.child(f"extra{index}", Branch(schema))
        history = None if history_disabled else HistoryConfig()
        endpoint = (
            f"direct://browser-{uuid4().hex}"
            if transport == "direct"
            else "http://127.0.0.1:0"
        )
        config = (
            Builder()
            .tree(tree)
            .storage_path(Path(directory))
            .history(history)
            .host(HostConfig(endpoint, devtools=DevToolsConfig(port=0)))
            .build()
        )
        store = Store(config)
        host = StoreHost(store)
        client = StoreClient()
        store.boot()
        try:
            await host.start()
            await client.connect(
                endpoint
                if transport == "direct"
                else host.transport.endpoint["endpoint"]
            )
            print(json.dumps({"ready": host.devtools_url}), flush=True)
            while line := await asyncio.to_thread(sys.stdin.readline):
                request = json.loads(line)
                if request.get("stop"):
                    break
                if request.get("restart"):
                    assert host.devtools_url is not None
                    port = urlparse(host.devtools_url).port
                    await client.disconnect()
                    await host.stop()
                    store.shutdown()
                    config = replace(
                        config,
                        host=HostConfig(
                            endpoint, devtools=DevToolsConfig(port=port or 0)
                        ),
                    )
                    store = Store(config)
                    store.boot()
                    host = StoreHost(store)
                    await host.start()
                    await client.connect(
                        endpoint
                        if transport == "direct"
                        else host.transport.endpoint["endpoint"]
                    )
                    print(json.dumps({"ack": request["id"]}), flush=True)
                    continue
                if request.get("disconnect_browsers"):
                    assert host.devtools is not None
                    for session in tuple(
                        host.devtools.server.sessions.values()
                    ):
                        await session.websocket.close()
                    print(json.dumps({"ack": request["id"]}), flush=True)
                    continue
                try:
                    batch = decode_request(from_wire({"requests": [request]}))
                    transaction = batch.items[0]
                    assert isinstance(transaction, TransactionRequest)
                    response = await client.transaction(
                        transaction_id=transaction.transaction_id,
                        actions=transaction.actions,
                        history_group=transaction.history_group,
                        history_label=transaction.history_label,
                    )
                    if isinstance(response, TransactionFailureResponse):
                        raise RuntimeError(str(response.failure))
                    result = {"ack": transaction.transaction_id}
                except Exception as error:
                    result = {
                        "ack": request["transaction_id"],
                        "error": str(error),
                    }
                print(json.dumps(result), flush=True)
        finally:
            await client.disconnect()
            await host.stop()
            store.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("direct", "socketio"))
    parser.add_argument("--history-disabled", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.transport, args.history_disabled))
