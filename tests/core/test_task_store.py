from __future__ import annotations

import json
import threading

from agentfeishu.core import CapabilityRequest, Task, TaskStatus
from agentfeishu.core.task_store import TaskStore


def test_task_store_serializes_concurrent_appends(tmp_path):
    store_path = tmp_path / "tasks.jsonl"
    task_count = 30

    def write_task(index: int) -> None:
        store = TaskStore(store_path)
        task = Task.create(
            CapabilityRequest(
                capability="echo",
                payload={"index": index},
                raw_text=f"task {index}",
            ),
            status=TaskStatus.QUEUED,
        )
        store.create(task)
        store.update(task.transition(TaskStatus.RUNNING))
        store.update(task.transition(TaskStatus.SUCCEEDED, result={"index": index}))

    threads = [
        threading.Thread(target=write_task, args=(index,))
        for index in range(task_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    lines = store_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == task_count * 3
    for line in lines:
        json.loads(line)

    latest = TaskStore(store_path).latest_by_id()
    assert len(latest) == task_count
    assert {row["status"] for row in latest.values()} == {"succeeded"}
