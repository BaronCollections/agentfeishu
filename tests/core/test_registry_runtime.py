from __future__ import annotations

from dataclasses import dataclass
import threading
import time

import pytest

from agentfeishu.config import Settings
from agentfeishu.core import (
    BackgroundTaskRuntime,
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


class BrokenHealthCapability:
    descriptor = CapabilityDescriptor(
        capability_id="broken_health",
        name="Broken Health",
        description="Raises while reporting health.",
    )

    def health(self, settings: Settings) -> CapabilityHealth:
        raise RuntimeError("health exploded")

    def execute(self, request: RuntimeRequest, context: RuntimeContext) -> dict[str, object]:
        return {"unreachable": True}


class BlockingCapability:
    descriptor = CapabilityDescriptor(
        capability_id="blocking",
        name="Blocking",
        description="Blocks until the test releases it.",
    )

    def __init__(self) -> None:
        self.release = threading.Event()
        self.both_started = threading.Event()
        self._lock = threading.Lock()
        self.started_texts: list[str] = []

    def health(self, settings: Settings) -> CapabilityHealth:
        return CapabilityHealth(name="blocking", enabled=True, status="ready")

    def execute(self, request: RuntimeRequest, context: RuntimeContext) -> dict[str, object]:
        with self._lock:
            self.started_texts.append(request.text)
            if len(self.started_texts) == 2:
                self.both_started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release blocking capability")
        return {"text": request.text, "task_id": context.task_id}


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


def test_background_runtime_marks_dependency_check_exceptions_failed(tmp_path):
    registry = CapabilityRegistry()
    registry.register(BrokenHealthCapability())
    store = InMemoryTaskStore()
    runtime = BackgroundTaskRuntime(
        registry=registry,
        task_store=store,
        settings=Settings(state_dir=tmp_path),
        max_workers=1,
    )

    task = runtime.submit(RuntimeRequest(capability_id="broken_health", text="hello"))

    assert _wait_for_status(store, task.task_id, TaskStatus.FAILED)
    failed = store.get(task.task_id)
    assert failed.error is not None
    assert failed.error.code == "runtime_failed"


def test_background_runtime_queues_and_processes_tasks_concurrently(tmp_path):
    capability = BlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    store = InMemoryTaskStore()
    runtime = BackgroundTaskRuntime(
        registry=registry,
        task_store=store,
        settings=Settings(state_dir=tmp_path),
        max_workers=2,
    )

    first = runtime.submit(RuntimeRequest(capability_id="blocking", text="one"))
    second = runtime.submit(RuntimeRequest(capability_id="blocking", text="two"))

    assert first.status is TaskStatus.QUEUED
    assert second.status is TaskStatus.QUEUED
    assert capability.both_started.wait(timeout=2)
    assert store.get(first.task_id).status is TaskStatus.RUNNING
    assert store.get(second.task_id).status is TaskStatus.RUNNING

    capability.release.set()
    assert _wait_for_status(store, first.task_id, TaskStatus.SUCCEEDED)
    assert _wait_for_status(store, second.task_id, TaskStatus.SUCCEEDED)


def test_background_runtime_marks_queued_tasks_cancelled_on_shutdown(tmp_path):
    capability = BlockingCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    store = InMemoryTaskStore()
    runtime = BackgroundTaskRuntime(
        registry=registry,
        task_store=store,
        settings=Settings(state_dir=tmp_path),
        max_workers=1,
    )

    running = runtime.submit(RuntimeRequest(capability_id="blocking", text="running"))
    queued = runtime.submit(RuntimeRequest(capability_id="blocking", text="queued"))
    assert _wait_for_status(store, running.task_id, TaskStatus.RUNNING)
    assert store.get(queued.task_id).status is TaskStatus.QUEUED

    runtime.shutdown(wait=False, cancel_futures=True)

    assert _wait_for_status(store, queued.task_id, TaskStatus.CANCELLED)
    capability.release.set()
    assert _wait_for_status(store, running.task_id, TaskStatus.SUCCEEDED)


def _wait_for_status(store: InMemoryTaskStore, task_id: str, status: TaskStatus) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if store.get(task_id).status is status:
            return True
        time.sleep(0.01)
    return False
