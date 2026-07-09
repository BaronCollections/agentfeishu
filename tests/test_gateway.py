from __future__ import annotations

from agentfeishu.gateway import normalize_feishu_event, route_event


def test_normalize_feishu_text_event():
    event = normalize_feishu_event({
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": '{"text":"解析 https://example.com"}',
            },
        }
    })
    assert event.text == "解析 https://example.com"
    assert event.sender_id == "ou_1"
    request = route_event(event)
    assert request.source == "feishu"
    assert request.metadata["message_id"] == "om_1"


def test_normalize_bad_json_content_as_text():
    event = normalize_feishu_event({
        "message_type": "text",
        "content": "plain text",
    })
    assert event.text == "plain text"
