"""Shared domain models for capabilities and runtime tasks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_AUTH = "needs_auth"
    WAITING_FOR_AUTH = "waiting_for_auth"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DependencyStatus(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    OPTIONAL = "optional"
    ERROR = "error"


class AuthState(str, Enum):
    NOT_REQUIRED = "not_required"
    READY = "ready"
    REQUIRED = "required"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    kind: str
    value: str
    source: str = ""
    confidence: str = "medium"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Limitation:
    code: str
    message: str
    recoverable: bool = False
    next_action: str = ""


@dataclass(frozen=True)
class DependencyCheck:
    name: str
    ok: bool = True
    required: bool = True
    status: DependencyStatus | None = None
    message: str = ""
    install_hint: str = ""
    version: str = ""

    def __post_init__(self) -> None:
        if self.status is None:
            status = DependencyStatus.AVAILABLE if self.ok else (
                DependencyStatus.MISSING if self.required else DependencyStatus.OPTIONAL)
            object.__setattr__(self, "status", status)
        else:
            object.__setattr__(self, "ok", self.status == DependencyStatus.AVAILABLE)
            if self.status == DependencyStatus.OPTIONAL:
                object.__setattr__(self, "required", False)


@dataclass(frozen=True)
class CapabilityDescriptor:
    name: str = ""
    title: str = ""
    description: str = ""
    capability_id: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    enabled_by_default: bool = True
    requires_auth: bool = False
    required_permissions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.capability_id and self.name:
            object.__setattr__(self, "capability_id", self.name)
        if not self.name and self.capability_id:
            object.__setattr__(self, "name", self.capability_id)
        if not self.title and self.name:
            object.__setattr__(self, "title", self.name.replace("_", " ").title())


@dataclass(frozen=True)
class CapabilityHealth:
    name: str
    enabled: bool
    status: str
    dependencies: tuple[DependencyCheck, ...] = ()
    auth_state: AuthState = AuthState.NOT_REQUIRED
    configuration: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityRequest:
    capability: str
    payload: dict[str, Any]
    raw_text: str = ""
    sender_id: str = ""
    source: str = "cli"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeRequest:
    text: str
    capability_id: str = ""
    command: str = ""
    urls: tuple[str, ...] = ()
    actor_id: str = ""
    tenant_id: str = ""
    permissions: frozenset[str] = field(default_factory=frozenset)
    raw_event: dict[str, Any] = field(default_factory=dict)
    sender_id: str = ""
    source: str = "cli"
    capability_hint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.actor_id and self.sender_id:
            object.__setattr__(self, "actor_id", self.sender_id)
        if not self.sender_id and self.actor_id:
            object.__setattr__(self, "sender_id", self.actor_id)
        if not self.capability_hint and self.capability_id:
            object.__setattr__(self, "capability_hint", self.capability_id)


@dataclass(frozen=True)
class CapabilityResult:
    status: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    next_action: str = ""

    @property
    def requires_auth(self) -> bool:
        return self.status == "needs_browser_auth"


@dataclass(frozen=True)
class RuntimeErrorInfo:
    code: str
    message: str
    recoverable: bool = False


@dataclass(frozen=True)
class Task:
    id: str
    capability: str
    status: TaskStatus
    request: CapabilityRequest
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: RuntimeErrorInfo | str | None = None
    dependency_checks: tuple[DependencyCheck, ...] = ()

    @property
    def task_id(self) -> str:
        return self.id

    @classmethod
    def create(cls, request: CapabilityRequest, *, status: TaskStatus = TaskStatus.QUEUED) -> "Task":
        return cls(
            id=new_id("task"),
            capability=request.capability,
            status=status,
            request=request,
        )

    def transition(self, status: TaskStatus, *,
                   result: Any = None,
                   error: RuntimeErrorInfo | str | None = None,
                   dependency_checks: tuple[DependencyCheck, ...] | None = None) -> "Task":
        now = utc_now()
        started = self.started_at
        finished = self.finished_at
        if status in {TaskStatus.PENDING, TaskStatus.QUEUED}:
            started = None
            finished = None
        if status == TaskStatus.RUNNING and started is None:
            started = now
        if status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.REJECTED,
            TaskStatus.NEEDS_AUTH,
            TaskStatus.WAITING_FOR_AUTH,
        }:
            finished = now if status not in {TaskStatus.WAITING_FOR_AUTH, TaskStatus.NEEDS_AUTH} else None
        return Task(
            id=self.id,
            capability=self.capability,
            status=status,
            request=self.request,
            created_at=self.created_at,
            updated_at=now,
            started_at=started,
            finished_at=finished,
            result=result,
            error=error,
            dependency_checks=(
                self.dependency_checks if dependency_checks is None else dependency_checks
            ),
        )


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value
