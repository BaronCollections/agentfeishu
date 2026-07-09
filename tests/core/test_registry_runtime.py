from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentfeishu.config import Settings
from agentfeishu.core import (
    CapabilityDescriptor,
    CapabilityHealth,
    CapabilityRegistry,
    DependencyCheck,
    InMemoryTaskStore,
    LocalExecutionAdapter,
    RuntimeContext,
    RuntimeRequest,
    TaskRuntime,
    TaskStatus,
)


@dataclass
class EchoCapability:
    descriptor: CapabilityDescriptor = CapabilityDescriptor(
        capability_id="echo",
        name="Echo",
        description="Echo text for runtime tests.",
        required_permissions=frozenset({"echo:run"}),
    )

    def health(self, settings: Settings) -> CapabilityHealth:
        return CapabilityHealth(
            name="echo",
            enabled=True,
            status="ready",
            dependencies=(DependencyCheck(name="local", ok=True, required=True),),
        )

    def execute(self, request: RuntimeRequest, context: RuntimeContext) -> dict[str, object]:
        return {"echo": request.text, "task_id": context.task_id}


@dataclass
class UnhealthyCapability:
    descriptor: CapabilityDescriptor = CapabilityDescriptor(
        capability_id="unhealthy",
        name="Unhealthy",
        description="Fails dependency checks.",
    )

    def health(self, settings: Settings) -> CapabilityHealth:
        return CapabilityHealth(
            name="unhealthy",
            enabled=True,
            status="unavailable",
            dependencies=(
                DependencyCheck(
                    name="missing",
                    ok=False,
                    required=True,
                    message="not installed",
                ),
            ),
        )

    def execute(self, request: RuntimeRequest, context: RuntimeContext) -> dict[str, object]:
        return {"unreachable": True}


def test_registry_rejects_duplicate_capability_ids():
    registry = CapabilityRegistry()
    registry.register(EchoCapability())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoCapability())


def test_runtime_executes_capability_and_records_state(tmp_path):
    registry = CapabilityRegistry()
    registry.register(EchoCapability())
    store = InMemoryTaskStore()
    runtime = TaskRuntime(
        registry=registry,
        task_store=store,
        settings=Settings(state_dir=tmp_path),
        execution_adapter=LocalExecutionAdapter(),
    )

    task = runtime.submit(
        RuntimeRequest(
            capability_id="echo",
            text="hello",
            actor_id="ou_123",
            permissions=frozenset({"echo:run"}),
            source="feishu",
        )
    )

    assert task.status is TaskStatus.SUCCEEDED
    assert task.result == {"echo": "hello", "task_id": task.task_id}
    assert store.get(task.task_id).status is TaskStatus.SUCCEEDED
    assert store.get(task.task_id).started_at is not None
    assert store.get(task.task_id).finished_at is not None


def test_runtime_rejects_missing_permissions_without_executing(tmp_path):
    registry = CapabilityRegistry()
    registry.register(EchoCapability())
    runtime = TaskRuntime(
        registry=registry,
        task_store=InMemoryTaskStore(),
        settings=Settings(state_dir=tmp_path),
        execution_adapter=LocalExecutionAdapter(),
    )

    task = runtime.submit(
        RuntimeRequest(
            capability_id="echo",
            text="hello",
            actor_id="ou_123",
            permissions=frozenset(),
            source="feishu",
        )
    )

    assert task.status is TaskStatus.REJECTED
    assert task.error is not None
    assert task.error.code == "permission_denied"


def test_runtime_marks_required_dependency_failures_unavailable(tmp_path):
    registry = CapabilityRegistry()
    registry.register(UnhealthyCapability())
    runtime = TaskRuntime(
        registry=registry,
        task_store=InMemoryTaskStore(),
        settings=Settings(state_dir=tmp_path),
        execution_adapter=LocalExecutionAdapter(),
    )

    task = runtime.submit(RuntimeRequest(capability_id="unhealthy", text="", source="feishu"))

    assert task.status is TaskStatus.REJECTED
    assert task.error is not None
    assert task.error.code == "capability_unavailable"
    assert task.dependency_checks[0].name == "missing"
