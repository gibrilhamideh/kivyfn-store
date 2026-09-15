"""Additive history messages reject malformed wire inputs at the codec."""

from __future__ import annotations

import pytest

from kivyfn.store.codec.requests import decode_request, encode_request
from kivyfn.store.codec.responses import decode_response, encode_response
from kivyfn.store.core.operations import DataRecord
from kivyfn.store.error import HistoryError, HistoryErrorDetail, StoreErrorCode
from kivyfn.store.error.codec import decode_failure, encode_failure
from kivyfn.store.protocol.message.requests import (
    HistoryRequest,
    RedoRequest,
    RequestBatch,
    SetRequestAction,
    TransactionRequest,
    UndoRequest,
)
from kivyfn.store.protocol.message.responses import (
    HistoryResponse,
    ResponseBatch,
)
from kivyfn.store.store.history import (
    HistoryEntry,
    HistoryPage,
    HistoryPosition,
    HistoryStatus,
)


def test_history_and_metadata_request_roundtrip() -> None:
    position = HistoryPosition("instance", 9, "entry")
    request = RequestBatch(
        (
            HistoryRequest(limit=20, offset=2),
            UndoRequest("undo", position),
            RedoRequest("redo", position),
            TransactionRequest(
                "edit",
                (SetRequestAction("Values", (("value", 2**65),)),),
                "gesture",
                "Move",
                "instance",
                {"Values": 7},
            ),
        )
    )
    assert decode_request(encode_request(request)) == request


@pytest.mark.parametrize(
    "position",
    [
        None,
        {},
        {"store_instance_id": "s", "revision": True, "entry_id": "e"},
        {"store_instance_id": "s", "revision": -1, "entry_id": "e"},
        {"store_instance_id": "s", "revision": 2, "entry_id": 3},
    ],
)
def test_malformed_position_is_rejected(position) -> None:
    with pytest.raises(ValueError):
        decode_request(
            {
                "requests": [
                    {
                        "type": "undo",
                        "transaction_id": "u",
                        "expected_position": position,
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("history_group", 1),
        ("history_label", []),
        ("expected_store_instance_id", ""),
        ("expected_revisions", {"Values": True}),
    ],
)
def test_malformed_transaction_metadata_is_rejected(field, value) -> None:
    request = {"type": "transaction", "transaction_id": "x", "actions": []}
    request[field] = value
    with pytest.raises(ValueError):
        decode_request({"requests": [request]})


def test_history_row_records_roundtrip_without_order_metadata() -> None:
    position = HistoryPosition("store", 1, "entry")
    entry = HistoryEntry(
        id="entry",
        transaction_ids=("insert",),
        origin_client_id="client",
        label=None,
        group=None,
        records=(
            DataRecord("Rows", "b", None, None, {"value": 2}, False, True),
        ),
        signals=(),
    )
    page = HistoryPage(
        HistoryStatus(
            "store", 1, True, False, position, position, None, 0, 1, 1, True
        ),
        (entry,),
    )
    response = ResponseBatch((HistoryResponse(page),))
    wire = encode_response(response)
    assert "orders" not in wire["responses"][0]["page"]["entries"][0]
    assert decode_response(wire) == response


def test_history_status_and_typed_failure_roundtrip() -> None:
    position = HistoryPosition("store", 0, None)
    page = HistoryPage(
        HistoryStatus(
            "store", 0, False, False, position, position, None, 0, 0, 0, True
        ),
        (),
    )
    response = ResponseBatch((HistoryResponse(page),))
    assert decode_response(encode_response(response)) == response
    failure = HistoryError(
        "Newer value",
        code=StoreErrorCode.HISTORY_CONFLICT,
        detail=HistoryErrorDetail(schema="Values", expected=1, value=2),
    ).failure()
    assert decode_failure(encode_failure(failure)) == failure
