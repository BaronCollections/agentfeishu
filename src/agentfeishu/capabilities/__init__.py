"""Built-in capabilities."""

from __future__ import annotations

from agentfeishu.core import CapabilityRegistry

from .url_ingest import UrlIngestCapability


def default_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(UrlIngestCapability())
    return registry


__all__ = ["UrlIngestCapability", "default_registry"]
