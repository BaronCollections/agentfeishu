"""Append-only task store for local runtime state."""

from __future__ import annotations

import json
from pathlib import Path
import threading

from .models import Task, TaskStatus, to_jsonable

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _lock_for_path(path: Path) -> threading.RLock:
    resolved = path.expanduser().resolve()
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved] = lock
        return lock


class InMemoryTaskStore:
    """Small local store used by tests and embedders.

    The runtime only depends on create/update/get/list semantics, so this can
    later be replaced by a queue-backed worker store without changing gateways.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()

    def create(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def update(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Task:
        with self._lock:
            return self._tasks[task_id]

    def list(self, limit: int = 50) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        if limit <= 0:
            return tasks
        return tasks[-limit:]


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_for_path(path)

    def create(self, task: Task) -> Task:
        self.append(task)
        return task

    def update(self, task: Task) -> Task:
        self.append(task)
        return task

    def append(self, task: Task) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(to_jsonable(task), ensure_ascii=False) + "\n")

    def events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            rows: list[dict] = []
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rows.append(json.loads(line))
        if limit <= 0:
            return rows
        return rows[-limit:]

    def list(self, limit: int = 50) -> list[dict]:
        latest = list(self.latest_by_id().values())
        if limit <= 0:
            return latest
        return latest[-limit:]

    def latest_by_id(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for row in self.events(limit=0):
            latest[row["id"]] = row
        return latest

    def recent_errors(self, limit: int = 20) -> list[dict]:
        errors = [
            row for row in self.list(limit=0)
            if row.get("status") == TaskStatus.FAILED.value or row.get("error")
        ]
        return errors[-limit:]
