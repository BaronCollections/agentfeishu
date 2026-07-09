"""Capability registry and base protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentfeishu.config import Settings

from .models import CapabilityDescriptor, CapabilityHealth, CapabilityResult, DependencyCheck, RuntimeRequest
if TYPE_CHECKING:
    from .runtime import RuntimeContext


class Capability(Protocol):
    descriptor: CapabilityDescriptor

    def health(self, settings: Settings) -> CapabilityHealth:
        ...

    def execute(self, request: RuntimeRequest,
                context: RuntimeContext) -> CapabilityResult:
        ...


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        name = capability.descriptor.capability_id or capability.descriptor.name
        if not name:
            raise ValueError("capability id is required")
        if name in self._capabilities:
            raise ValueError(f"capability already registered: {name}")
        self._capabilities[name] = capability

    def get(self, name: str) -> Capability:
        try:
            return self._capabilities[name]
        except KeyError as exc:
            raise KeyError(f"unknown capability: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._capabilities)

    def descriptors(self) -> list[CapabilityDescriptor]:
        return [self._capabilities[name].descriptor for name in self.names()]

    def health(self, settings: Settings) -> list[CapabilityHealth]:
        return [
            normalize_health(self._capabilities[name], settings)
            for name in self.names()
        ]


def normalize_health(capability: Capability, settings: Settings) -> CapabilityHealth:
    """Normalize capability health for admin/runtime consumers."""

    health = capability.health(settings)
    if isinstance(health, CapabilityHealth):
        return health
    if isinstance(health, (list, tuple)) and all(
        isinstance(item, DependencyCheck) for item in health
    ):
        return CapabilityHealth(
            name=capability.descriptor.capability_id or capability.descriptor.name,
            enabled=settings.capability_enabled(
                capability.descriptor.capability_id or capability.descriptor.name
            ),
            status="ready" if all(
                (not item.required) or item.ok for item in health
            ) else "unavailable",
            dependencies=tuple(health),
        )
    raise TypeError(
        "capability.health() must return CapabilityHealth or dependency checks"
    )
