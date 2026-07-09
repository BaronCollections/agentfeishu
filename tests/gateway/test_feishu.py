from __future__ import annotations

from agentfeishu.gateway import FeishuEventNormalizer, GatewayCommandRouter


def test_normalizer_extracts_text_message_and_sender_metadata():
    raw_event = {
        "header": {"event_id": "evt_1", "tenant_key": "tenant_a"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_123"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": '{"text":" /echo hello "}',
            },
        },
    }

    message = FeishuEventNormalizer().normalize(raw_event)

    assert message.event_id == "evt_1"
    assert message.tenant_key == "tenant_a"
    assert message.sender_id == "ou_123"
    assert message.message_id == "om_1"
    assert message.chat_id == "oc_1"
    assert message.message_type == "text"
    assert message.text == "/echo hello"


def test_router_routes_slash_command_to_named_capability():
    message = FeishuEventNormalizer().normalize(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "message_type": "text",
                    "content": '{"text":"/research topic"}',
                },
            }
        }
    )

    request = GatewayCommandRouter().route(message)

    assert request.capability_id == "research"
    assert request.command == "research"
    assert request.text == "topic"
    assert request.actor_id == "ou_123"
    assert request.source == "feishu"


def test_router_routes_url_messages_to_url_ingest():
    message = FeishuEventNormalizer().normalize(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {
                    "message_type": "text",
                    "content": '{"text":"please read https://example.com/a?b=1"}',
                },
            }
        }
    )

    request = GatewayCommandRouter().route(message)

    assert request.capability_id == "url_ingest"
    assert request.command == "url"
    assert request.urls == ("https://example.com/a?b=1",)
    assert request.raw_event["event"]["message"]["message_type"] == "text"


def test_router_rejects_unroutable_message():
    message = FeishuEventNormalizer().normalize(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_123"}},
                "message": {"message_type": "image", "content": "{}"},
            }
        }
    )

    request = GatewayCommandRouter().route(message)

    assert request.capability_id == "unknown"
    assert request.command == "unsupported_message"
    assert request.metadata["gateway_status"] == "unroutable"
