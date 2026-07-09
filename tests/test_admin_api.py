from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer

from agentfeishu.capabilities import default_registry
from agentfeishu.capabilities.url_ingest import extractors
from agentfeishu.config import load_settings
from agentfeishu.core import (
    CapabilityDescriptor,
    CapabilityHealth,
    CapabilityRegistry,
    RuntimeContext,
    RuntimeRequest,
)
from agentfeishu.core.task_store import TaskStore
import agentfeishu.server as server_module
from agentfeishu.server import build_overview, make_handler


def test_build_overview_has_admin_sections(tmp_path):
    settings = load_settings(tmp_path)
    overview = build_overview(settings, default_registry())
    for key in (
        "capabilities",
        "dependencies",
        "configuration",
        "auth",
        "recent_tasks",
        "recent_errors",
    ):
        assert key in overview
    assert "secret" not in json.dumps(
        list(overview["configuration"].values())
    ).lower()


def test_admin_api_overview_endpoint(tmp_path):
    settings = load_settings(tmp_path)
    handler = make_handler(settings, default_registry())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/admin/api/overview",
            timeout=5,
        ) as response:
            assert response.status == 200
            assert response.headers["content-type"].startswith("application/json")
            data = json.loads(response.read().decode("utf-8"))
        assert "capabilities" in data
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_feishu_challenge_verifies_token(tmp_path):
    (tmp_path / "agentfeishu.toml").write_text(
        '[feishu]\nverification_token = "expected"\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    server, thread = _start_server(settings)
    try:
        port = server.server_address[1]
        response = _post_json(
            f"http://127.0.0.1:{port}/feishu/events",
            {"token": "expected", "challenge": "abc"},
        )
        assert response["status"] == HTTPStatus.OK
        assert response["data"]["challenge"] == "abc"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_feishu_rejects_invalid_token(tmp_path):
    (tmp_path / "agentfeishu.toml").write_text(
        '[feishu]\nverification_token = "expected"\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    server, thread = _start_server(settings)
    try:
        port = server.server_address[1]
        response = _post_json(
            f"http://127.0.0.1:{port}/feishu/events",
            {"token": "wrong", "challenge": "abc"},
            expect_error=True,
        )
        assert response["status"] == HTTPStatus.FORBIDDEN
        assert response["data"]["error"] == "invalid_feishu_token"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_feishu_message_routes_url_to_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(
        extractors,
        "resolve_url",
        lambda url, timeout_s=20: (url, [url], "", 200),
    )
    monkeypatch.setattr(extractors, "has_python_package", lambda package: False)
    settings = load_settings(tmp_path)
    server, thread = _start_server(settings)
    try:
        port = server.server_address[1]
        response = _post_json(
            f"http://127.0.0.1:{port}/feishu/events",
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_1"}},
                    "message": {
                        "message_id": "om_1",
                        "chat_id": "oc_1",
                        "message_type": "text",
                        "content": '{"text":"解析 https://cdn.example/photo.jpg"}',
                    },
                }
            },
        )
        task = response["data"]["task"]
        assert response["status"] == HTTPStatus.ACCEPTED
        assert task["capability"] == "url_ingest"
        assert task["status"] == "queued"
        assert task["request"]["sender_id"] == "ou_1"
        assert _wait_for_task_status(settings, task["id"], "succeeded")
    finally:
        server.RequestHandlerClass.shutdown_runtime()
        server.shutdown()
        thread.join(timeout=5)


def test_feishu_message_is_accepted_before_capability_finishes(tmp_path):
    capability = BlockingUrlCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    settings = load_settings(tmp_path)
    server, thread = _start_server(settings, registry)
    try:
        port = server.server_address[1]
        started = time.monotonic()
        response = _post_json(
            f"http://127.0.0.1:{port}/feishu/events",
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_1"}},
                    "message": {
                        "message_id": "om_1",
                        "chat_id": "oc_1",
                        "message_type": "text",
                        "content": '{"text":"解析 https://example.com/video"}',
                    },
                }
            },
        )
        elapsed = time.monotonic() - started
        task = response["data"]["task"]

        assert response["status"] == HTTPStatus.ACCEPTED
        assert elapsed < 0.5
        assert task["status"] == "queued"
        assert capability.started.wait(timeout=2)

        capability.release.set()
        assert _wait_for_task_status(settings, task["id"], "succeeded")
    finally:
        capability.release.set()
        server.RequestHandlerClass.shutdown_runtime()
        server.shutdown()
        thread.join(timeout=5)


def test_browser_auth_endpoint_reports_missing_optional_dependency(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "has_python_package", lambda package: False)
    settings = load_settings(tmp_path)
    server, thread = _start_server(settings)
    try:
        port = server.server_address[1]
        response = _post_json(
            f"http://127.0.0.1:{port}/admin/api/auth/browser/open",
            {"url": "https://www.douyin.com/"},
            expect_error=True,
        )
        assert response["status"] == HTTPStatus.CONFLICT
        assert response["data"]["error"] == "playwright_unavailable"
    finally:
        server.shutdown()
        thread.join(timeout=5)


class BlockingUrlCapability:
    descriptor = CapabilityDescriptor(
        capability_id="url_ingest",
        name="URL Ingest",
        description="Blocking URL ingest test double.",
    )

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def health(self, settings):
        return CapabilityHealth(name="url_ingest", enabled=True, status="ready")

    def execute(self, request: RuntimeRequest, context: RuntimeContext):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release blocking URL capability")
        return {"status": "ok", "url": request.urls[0]}


def _start_server(settings, registry=None):
    handler = make_handler(settings, registry or default_registry())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _wait_for_task_status(settings, task_id, status):
    deadline = time.monotonic() + 3
    store = TaskStore(settings.task_store_path)
    while time.monotonic() < deadline:
        tasks = {task["id"]: task for task in store.list(limit=0)}
        if tasks.get(task_id, {}).get("status") == status:
            return True
        time.sleep(0.01)
    return False


def _post_json(url, payload, *, expect_error=False):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {
                "status": response.status,
                "data": json.loads(response.read().decode("utf-8")),
            }
    except urllib.error.HTTPError as exc:
        if not expect_error:
            raise
        return {
            "status": exc.code,
            "data": json.loads(exc.read().decode("utf-8")),
        }
