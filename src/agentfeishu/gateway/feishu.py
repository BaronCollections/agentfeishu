"""Feishu event normalization and command routing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agentfeishu.core import RuntimeRequest


URL_RE = re.compile(r"https?://[^\s<>'\"]+")


@dataclass(frozen=True)
class NormalizedFeishuMessage:
    event_id: str = ""
    tenant_key: str = ""
    sender_id: str = ""
    message_id: str = ""
    chat_id: str = ""
    message_type: str = ""
    text: str = ""
    raw_event: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class FeishuEventNormalizer:
    """Convert Feishu callback payloads into a stable internal message shape."""

    def normalize(self, raw_event: dict[str, Any]) -> NormalizedFeishuMessage:
        header = raw_event.get("header") or {}
        event = raw_event.get("event") or raw_event
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        message = event.get("message") or event
        message_type = str(message.get("message_type", ""))
        content = _decode_content(message.get("content"))
        text = _extract_text(message_type, content).strip()
        return NormalizedFeishuMessage(
            event_id=str(header.get("event_id", "")),
            tenant_key=str(header.get("tenant_key", "")),
            sender_id=str(sender_id.get("open_id") or sender_id.get("user_id") or ""),
            message_id=str(message.get("message_id", "")),
            chat_id=str(message.get("chat_id", "")),
            message_type=message_type,
            text=text,
            raw_event=raw_event,
            metadata={"content": content},
        )


class GatewayCommandRouter:
    """Route normalized Feishu messages to runtime requests without executing them."""

    def route(self, message: NormalizedFeishuMessage) -> RuntimeRequest:
        if message.message_type == "text" and message.text.startswith("/"):
            command, text = _split_command(message.text)
            return self._request(
                message,
                capability_id=command or "unknown",
                command=command,
                text=text,
            )

        urls = tuple(_extract_urls(message.text))
        if urls:
            return self._request(
                message,
                capability_id="url_ingest",
                command="url",
                text=message.text,
                urls=urls,
            )

        return self._request(
            message,
            capability_id="unknown",
            command="unsupported_message",
            text=message.text,
            metadata={"gateway_status": "unroutable"},
        )

    def _request(self, message: NormalizedFeishuMessage, *,
                 capability_id: str,
                 command: str,
                 text: str,
                 urls: tuple[str, ...] = (),
                 metadata: dict[str, Any] | None = None) -> RuntimeRequest:
        merged_metadata = {
            "message_id": message.message_id,
            "chat_id": message.chat_id,
            "message_type": message.message_type,
        }
        merged_metadata.update(metadata or {})
        return RuntimeRequest(
            capability_id=capability_id,
            command=command,
            text=text,
            urls=urls,
            actor_id=message.sender_id,
            tenant_id=message.tenant_key,
            source="feishu",
            raw_event=message.raw_event,
            metadata=merged_metadata,
        )


def _decode_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content:
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}
        return value if isinstance(value, dict) else {"value": value}
    return {}


def _extract_text(message_type: str, content: dict[str, Any]) -> str:
    if message_type == "text":
        return str(content.get("text", ""))
    if message_type == "post":
        return _flatten_post(content)
    return str(content.get("text", ""))


def _flatten_post(content: dict[str, Any]) -> str:
    parts: list[str] = []
    for locale_value in content.values():
        if not isinstance(locale_value, dict):
            continue
        for block in locale_value.get("content", []):
            for item in block:
                if isinstance(item, dict) and item.get("tag") == "text":
                    parts.append(str(item.get("text", "")))
    return " ".join(part for part in parts if part)


def _split_command(text: str) -> tuple[str, str]:
    command_text = text[1:].strip()
    command, _, rest = command_text.partition(" ")
    return command.strip(), rest.strip()


def _extract_urls(text: str) -> list[str]:
    return [match.group(0).rstrip("，,。.)）]") for match in URL_RE.finditer(text)]


def normalize_feishu_event(raw_event: dict[str, Any]) -> NormalizedFeishuMessage:
    """Compatibility helper for callers that do not need a normalizer instance."""

    return FeishuEventNormalizer().normalize(raw_event)


def route_event(message: NormalizedFeishuMessage) -> RuntimeRequest:
    """Compatibility helper for callers that do not need a router instance."""

    return GatewayCommandRouter().route(message)
