from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from agentfeishu.capabilities import default_registry
from agentfeishu.capabilities.url_ingest import extractors
from agentfeishu.config import load_settings
from agentfeishu.core import (
    CapabilityDescriptor,
    CapabilityHealth,
    CapabilityResult,
    CapabilityRegistry,
    Limitation,
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


def test_needs_auth_task_link_can_resume_original_task(tmp_path):
    capability = NeedsAuthThenSuccessCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    settings = load_settings(tmp_path)
    server, thread = _start_server(settings, registry)
    try:
        port = server.server_address[1]
        response = _post_json(
            f"http://127.0.0.1:{port}/feishu/events",
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_1"}},
                    "message": {
                        "message_id": "om_auth",
                        "chat_id": "oc_1",
                        "message_type": "text",
                        "content": '{"text":"解析 https://v.douyin.com/example/"}',
                    },
                }
            },
        )
        task_id = response["data"]["task"]["id"]
        assert _wait_for_task_status(settings, task_id, "needs_auth")
        task = _latest_task(settings, task_id)
        auth_url = task["result"]["data"]["auth_url"]
        parsed_auth = urlparse(auth_url)
        token = parse_qs(parsed_auth.query)["token"][0]

        auth_page = _get_text(f"http://127.0.0.1:{port}{parsed_auth.path}?{parsed_auth.query}")
        assert task_id in auth_page
        assert "Continue Parsing" in auth_page

        session = _get_json(
            f"http://127.0.0.1:{port}/api/auth/sessions?task_id={task_id}&token={token}"
        )
        assert session["task"]["id"] == task_id
        assert session["login_url"] == "https://www.douyin.com/"
        assert session["can_resume"] is True

        tampered = _get_text(
            f"http://127.0.0.1:{port}{parsed_auth.path}?task_id={task_id}&token=wrong",
            expect_error=True,
        )
        assert "invalid_auth_token" in tampered

        resume = _post_json(
            f"http://127.0.0.1:{port}/api/tasks/{task_id}/resume",
            {"token": token},
        )
        assert resume["status"] == HTTPStatus.ACCEPTED
        assert resume["data"]["task"]["id"] == task_id
        assert resume["data"]["task"]["status"] == "queued"
        assert _wait_for_task_status(settings, task_id, "succeeded")
        assert capability.calls == 2
    finally:
        server.RequestHandlerClass.shutdown_runtime()
        server.shutdown()
        thread.join(timeout=5)


def test_auth_open_endpoint_resumes_after_browser_login(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "has_python_package", lambda package: True)
    opened_urls: list[str] = []

    def fake_open_browser_login(settings, url):
        opened_urls.append(url)
        return {"status": "closed"}

    monkeypatch.setattr(server_module, "open_browser_login", fake_open_browser_login)
    capability = NeedsAuthThenSuccessCapability()
    registry = CapabilityRegistry()
    registry.register(capability)
    settings = load_settings(tmp_path)
    server, thread = _start_server(settings, registry)
    try:
        port = server.server_address[1]
        response = _post_json(
            f"http://127.0.0.1:{port}/feishu/events",
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_1"}},
                    "message": {
                        "message_id": "om_auth_auto",
                        "chat_id": "oc_1",
                        "message_type": "text",
                        "content": '{"text":"解析 https://v.douyin.com/example/"}',
                    },
                }
            },
        )
        task_id = response["data"]["task"]["id"]
        assert _wait_for_task_status(settings, task_id, "needs_auth")
        task = _latest_task(settings, task_id)
        auth_url = task["result"]["data"]["auth_url"]
        token = parse_qs(urlparse(auth_url).query)["token"][0]

        opened = _post_json(
            f"http://127.0.0.1:{port}/api/tasks/{task_id}/auth/open",
            {"token": token},
        )

        assert opened["status"] == HTTPStatus.ACCEPTED
        assert opened["data"]["status"] == "opening"
        assert _wait_for_task_status(settings, task_id, "succeeded")
        assert opened_urls == ["https://www.douyin.com/"]
        assert capability.calls == 2
    finally:
        server.RequestHandlerClass.shutdown_runtime()
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


class NeedsAuthThenSuccessCapability:
    descriptor = CapabilityDescriptor(
        capability_id="url_ingest",
        name="URL Ingest",
        description="Needs auth once, then succeeds.",
    )

    def __init__(self) -> None:
        self.calls = 0

    def health(self, settings):
        return CapabilityHealth(name="url_ingest", enabled=True, status="ready")

    def execute(self, request: RuntimeRequest, context: RuntimeContext):
        self.calls += 1
        if self.calls == 1:
            return CapabilityResult(
                status="needs_browser_auth",
                summary="Login required.",
                data={"resolved_url": request.urls[0]},
                limitations=(Limitation(
                    code="needs_browser_auth",
                    message="Login required.",
                    recoverable=True,
                    next_action="open_browser_login",
                ),),
                next_action="open_browser_login",
            )
        return CapabilityResult(
            status="ok",
            summary="Parsed after auth.",
            data={"resolved_url": request.urls[0], "text": "done"},
        )


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


def _latest_task(settings, task_id):
    tasks = {task["id"]: task for task in TaskStore(settings.task_store_path).list(limit=0)}
    return tasks[task_id]


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


def _get_text(url, *, expect_error=False):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if not expect_error:
            raise
        return exc.read().decode("utf-8")


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))
