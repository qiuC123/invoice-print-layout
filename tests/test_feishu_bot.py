from __future__ import annotations

import json

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from invoice_print_layout.feishu_bot import decode_event


def test_decode_file_message_event() -> None:
    event = P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_owner"},
                    "sender_type": "user",
                },
                "message": {
                    "message_id": "om_message",
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": "file",
                    "content": json.dumps(
                        {"file_key": "file_123", "file_name": "invoice.pdf"}
                    ),
                },
            }
        }
    )

    decoded = decode_event(event)

    assert decoded is not None
    assert decoded.sender_open_id == "ou_owner"
    assert decoded.resource_key == "file_123"
    assert decoded.file_name == "invoice.pdf"
