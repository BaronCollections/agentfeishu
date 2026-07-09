"""Runtime orchestration for capability execution."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
import threading
from typing import Any, Protocol

from agentfeishu.config import Settings

from .models import (
    CapabilityRequest,
    CapabilityResult,
    DependencyCheck,
    Limitation,
    RuntimeRequest,
    RuntimeErrorInfo,
    Task,
    TaskStatus,
)
from .registry import CapabilityRegistry, normalize_health
from .task_store import TaskStore


class TaskStateStore(Protocol):
    def create(self, task: Task) -> Task:
        ...

    def update(self, task: Task) -> Task:
        ...


@dataclass(frozen=True)
class RuntimeContext:
    task_id: str
    settings: Settings
    metadata: dict[str, Any]


class LocalExecutionAdapter:
    """Synchronous adapter with the same boundary a queue worker would call."""

    def execute(self, capability: Any, request: RuntimeRequest,
                context: RuntimeContext) -> CapabilityResult:
        return capability.execute(request, context)


class TaskRuntime:
    """Adapter-first task runtime for gateway-submitted capability requests."""

    def __init__(self, registry: CapabilityRegistry,
                 task_store: TaskStateStore,
                 settings: Settings,
                 execution_adapter: LocalExecutionAdapter | None = None) -> None:
        self.registry = registry
        self.task_store = task_store
        self.settings = settings
        self.execution_adapter = execution_adapter or LocalExecutionAdapter()

    def submit(self, request: RuntimeRequest) -> Task:
        task = self.create_task(request, status=TaskStatus.PENDING)
        return self.execute_task(task, request)

    def create_task(self, request: RuntimeRequest, *,
                    status: TaskStatus = TaskStatus.PENDING) -> Task:
        return self.task_store.create(Task.create(
            self._capability_request(request),
            status=status,
        ))

    def execute_task(self, task: Task, request: RuntimeRequest) -> Task:
        capability_id = request.capability_id or request.capability_hint
        try:
            capability = self.registry.get(capability_id)
        except KeyError:
            return self._reject(task, "unknown_capability", f"unknown capability: {capability_id}")

        descriptor = capability.descriptor
        if not self.settings.capability_enabled(descriptor.capability_id):
            return self._reject(
                task,
                "capability_disabled",
                f"capability disabled: {descriptor.capability_id}",
            )

        missing_permissions = descriptor.required_permissions - request.permissions
        if missing_permissions:
            return self._reject(
                task,
                "permission_denied",
                "missing permissions: " + ", ".join(sorted(missing_permissions)),
            )

        try:
            checks = self._dependency_checks(capability)
        except Exception as exc:
            failed = task.transition(
                TaskStatus.FAILED,
                error=RuntimeErrorInfo(
                    code="runtime_failed",
                    message=str(exc),
                    recoverable=True,
                ),
            )
            return self.task_store.update(failed)
        unavailable = [
            check for check in checks
            if check.required and not check.ok
        ]
        if unavailable:
            return self._reject(
                task,
                "capability_unavailable",
                "required dependencies are unavailable",
                dependency_checks=tuple(checks),
            )

        running = self.task_store.update(
            task.transition(TaskStatus.RUNNING, dependency_checks=tuple(checks))
        )
        context = RuntimeContext(
            task_id=running.task_id,
            settings=self.settings,
            metadata=request.metadata,
        )
        try:
            result = self.execution_adapter.execute(capability, request, context)
        except Exception as exc:  # runtime boundary: capture, don't crash gateway
            failed = running.transition(
                TaskStatus.FAILED,
                error=RuntimeErrorInfo(code="execution_failed", message=str(exc)),
            )
            return self.task_store.update(failed)

        status = _status_from_result(result)
        finished = running.transition(status, result=result)
        return self.task_store.update(finished)

    def _capability_request(self, request: RuntimeRequest) -> CapabilityRequest:
        capability_id = request.capability_id or request.capability_hint
        return CapabilityRequest(
            capability=capability_id or "unknown",
            payload={
                "text": request.text,
                "command": request.command,
                "urls": list(request.urls),
            },
            raw_text=request.text,
            sender_id=request.actor_id or request.sender_id,
            source=request.source,
            metadata=request.metadata,
        )

    def _dependency_checks(self, capability: Any) -> list[DependencyCheck]:
        return list(normalize_health(capability, self.settings).dependencies)

    def _reject(self, task: Task, code: str, message: str, *,
                dependency_checks: tuple[DependencyCheck, ...] = ()) -> Task:
        rejected = task.transition(
            TaskStatus.REJECTED,
            error=RuntimeErrorInfo(code=code, message=message),
            dependency_checks=dependency_checks,
        )
        return self.task_store.update(rejected)


class BackgroundTaskRuntime:
    """Queue-backed runtime for Feishu callbacks and other interactive gateways."""

    def __init__(self, registry: CapabilityRegistry,
                 task_store: TaskStateStore,
                 settings: Settings,
                 execution_adapter: LocalExecutionAdapter | None = None,
                 *,
                 max_workers: int | None = None) -> None:
        workers = max_workers if max_workers is not None else settings.runtime_max_workers
        if workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._runtime = TaskRuntime(
            registry=registry,
            task_store=task_store,
            settings=settings,
            execution_adapter=execution_adapter,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="agentfeishu-task",
        )
        self._futures: dict[str, Future[Task]] = {}
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def submit(self, request: RuntimeRequest) -> Task:
        task = self._runtime.create_task(request, status=TaskStatus.QUEUED)
        try:
            future = self._executor.submit(self._runtime.execute_task, task, request)
        except RuntimeError as exc:
            failed = task.transition(
                TaskStatus.FAILED,
                error=RuntimeErrorInfo(
                    code="queue_unavailable",
                    message=str(exc),
                    recoverable=True,
                ),
            )
            return self._runtime.task_store.update(failed)
        with self._lock:
            self._futures[task.task_id] = future
            self._tasks[task.task_id] = task
        future.add_done_callback(lambda done, task_id=task.task_id: self._finish(task_id, done))
        return task

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _finish(self, task_id: str, future: Future[Task]) -> None:
        with self._lock:
            self._futures.pop(task_id, None)
            task = self._tasks.pop(task_id, None)
        if future.cancelled():
            if task is not None:
                cancelled = task.transition(
                    TaskStatus.CANCELLED,
                    error=RuntimeErrorInfo(
                        code="task_cancelled",
                        message="task cancelled before execution",
                        recoverable=True,
                    ),
                )
                self._runtime.task_store.update(cancelled)
            return
        try:
            future.result()
        except CancelledError:
            if task is not None:
                cancelled = task.transition(
                    TaskStatus.CANCELLED,
                    error=RuntimeErrorInfo(
                        code="task_cancelled",
                        message="task cancelled before execution",
                        recoverable=True,
                    ),
                )
                self._runtime.task_store.update(cancelled)
        except Exception as exc:
            # execute_task captures capability exceptions. Reaching this boundary
            # means queue/runtime orchestration itself failed; keep the process up.
            if task is not None:
                failed = task.transition(
                    TaskStatus.FAILED,
                    error=RuntimeErrorInfo(
                        code="runtime_failed",
                        message=str(exc),
                        recoverable=True,
                    ),
                )
                self._runtime.task_store.update(failed)
            else:
                print(f"AgentFeishu background task {task_id} failed: {exc}")


class AgentRuntime:
    """Compatibility wrapper around the canonical adapter-first TaskRuntime."""

    def __init__(self, settings: Settings, registry: CapabilityRegistry,
                 store: TaskStore | None = None) -> None:
        self.settings = settings
        self.registry = registry
        self.store = store or TaskStore(settings.task_store_path)
        self._runtime = TaskRuntime(
            registry=registry,
            task_store=self.store,
            settings=settings,
            execution_adapter=LocalExecutionAdapter(),
        )

    def create_capability_request(self, request: RuntimeRequest) -> CapabilityRequest:
        capability = request.capability_hint or self._infer_capability(request.text)
        payload = {"text": request.text}
        if capability == "url_ingest":
            payload["url"] = _first_url(request.text)
        return CapabilityRequest(
            capability=capability,
            payload=payload,
            raw_text=request.text,
            sender_id=request.sender_id,
            source=request.source,
            metadata=request.metadata,
        )

    def run(self, request: RuntimeRequest | CapabilityRequest) -> Task:
        runtime_request = (
            self._from_capability_request(request)
            if isinstance(request, CapabilityRequest)
            else self._with_inferred_capability(request)
        )
        return self._runtime.submit(runtime_request)

    def _from_capability_request(self, request: CapabilityRequest) -> RuntimeRequest:
        text = request.raw_text or str(request.payload.get("text") or "")
        urls = request.payload.get("urls") or ()
        if isinstance(urls, str):
            urls = (urls,)
        if not urls and request.payload.get("url"):
            urls = (str(request.payload["url"]),)
        return RuntimeRequest(
            capability_id=request.capability,
            command=str(request.payload.get("command") or request.capability),
            text=text,
            urls=tuple(str(item) for item in urls),
            actor_id=request.sender_id,
            source=request.source,
            metadata=request.metadata,
        )

    def _with_inferred_capability(self, request: RuntimeRequest) -> RuntimeRequest:
        if request.capability_id or request.capability_hint:
            return request
        capability = self._infer_capability(request.text)
        urls = request.urls or ((_first_url(request.text),) if capability == "url_ingest" else ())
        return RuntimeRequest(
            capability_id=capability,
            command=request.command or ("url" if capability == "url_ingest" else capability),
            text=request.text,
            urls=tuple(url for url in urls if url),
            actor_id=request.actor_id,
            tenant_id=request.tenant_id,
            permissions=request.permissions,
            raw_event=request.raw_event,
            sender_id=request.sender_id,
            source=request.source,
            metadata=request.metadata,
        )

    def _infer_capability(self, text: str) -> str:
        if _first_url(text):
            return "url_ingest"
        raise ValueError("could not infer capability from request")


def _first_url(text: str) -> str:
    for token in text.split():
        if token.startswith(("http://", "https://")):
            return token.strip("，,。)）]")
    return ""


def failure_result(code: str, message: str) -> CapabilityResult:
    return CapabilityResult(
        status="failed",
        summary=message,
        limitations=(Limitation(code=code, message=message),),
    )


def _status_from_result(result: Any) -> TaskStatus:
    if _requires_auth(result):
        return TaskStatus.NEEDS_AUTH
    if isinstance(result, CapabilityResult) and result.status in {"failed", "error"}:
        return TaskStatus.FAILED
    if isinstance(result, dict) and result.get("status") in {"failed", "error"}:
        return TaskStatus.FAILED
    return TaskStatus.SUCCEEDED


def _requires_auth(result: Any) -> bool:
    if isinstance(result, CapabilityResult):
        return result.requires_auth
    if isinstance(result, dict):
        return result.get("status") == "needs_browser_auth"
    return False
