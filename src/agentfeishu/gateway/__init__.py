"""Gateway contracts for external operator surfaces."""

from .feishu import (
    FeishuEventNormalizer,
    GatewayCommandRouter,
    NormalizedFeishuMessage,
    normalize_feishu_event,
    route_event,
)

__all__ = [
    "FeishuEventNormalizer",
    "GatewayCommandRouter",
    "NormalizedFeishuMessage",
    "normalize_feishu_event",
    "route_event",
]
