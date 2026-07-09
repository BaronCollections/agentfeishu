"""Admin API and dashboard server."""

from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from agentfeishu.capabilities import default_registry
from agentfeishu.capabilities.url_ingest.browser_auth import (
    open_browser_login,
    profile_dir_for_url,
)
from agentfeishu.capabilities.url_ingest.dependencies import has_python_package
from agentfeishu.config import Settings, load_settings
from agentfeishu.core import CapabilityRegistry, TaskRuntime
from agentfeishu.core.models import to_jsonable
from agentfeishu.core.task_store import TaskStore
from agentfeishu.gateway import FeishuEventNormalizer, GatewayCommandRouter
from agentfeishu.ui.dashboard import dashboard_html


def build_overview(settings: Settings, registry: CapabilityRegistry,
                   task_store: TaskStore | None = None) -> dict[str, Any]:
    store = task_store or TaskStore(settings.task_store_path)
    health = registry.health(settings)
    return {
        "service": {
            "name": "agentfeishu",
            "status": "running",
        },
        "capabilities": [to_jsonable(item) for item in health],
        "dependencies": _flatten_dependencies(health),
        "configuration": settings.masked_configuration(),
        "auth": _auth_summary(health),
        "recent_tasks": store.list(limit=20),
        "recent_errors": store.recent_errors(limit=20),
    }


def serve(*, settings: Settings | None = None,
          registry: CapabilityRegistry | None = None,
          host: str = "127.0.0.1",
          port: int = 8765) -> None:
    settings = settings or load_settings()
    registry = registry or default_registry()
    handler = make_handler(settings, registry)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"AgentFeishu admin UI: http://{host}:{port}/admin")
    httpd.serve_forever()


def make_handler(settings: Settings, registry: CapabilityRegistry):
    class AdminHandler(BaseHTTPRequestHandler):
        server_version = "AgentFeishuAdmin/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            if _is_admin_path(parsed.path) and not self._allow_admin_request():
                return
            if parsed.path in {"/", "/admin"}:
                self._send_html(dashboard_html())
                return
            if parsed.path in {"/admin/api/overview", "/api/health"}:
                self._send_json(build_overview(settings, registry))
                return
            if parsed.path in {"/admin/api/capabilities", "/api/capabilities"}:
                self._send_json([to_jsonable(item) for item in registry.health(settings)])
                return
            if parsed.path in {"/admin/api/tasks", "/api/tasks"}:
                limit = _limit_from_query(parsed.query, 20)
                self._send_json(TaskStore(settings.task_store_path).list(limit=limit))
                return
            if parsed.path in {"/admin/api/config", "/api/config"}:
                self._send_json(settings.masked_configuration())
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            if _is_admin_path(parsed.path) and not self._allow_admin_request():
                return
            if parsed.path in {
                "/",
                "/admin",
                "/admin/api/overview",
                "/admin/api/capabilities",
                "/admin/api/tasks",
                "/admin/api/config",
                "/api/health",
                "/api/capabilities",
                "/api/tasks",
                "/api/config",
            }:
                content_type = (
                    "text/html; charset=utf-8"
                    if parsed.path in {"/", "/admin"}
                    else "application/json; charset=utf-8"
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            if parsed.path in {"/feishu/events", "/api/feishu/events"}:
                self._handle_feishu_event()
                return
            if parsed.path in {
                "/api/auth/browser/open",
                "/admin/api/auth/browser/open",
            }:
                if not self._allow_admin_request():
                    return
                self._handle_open_browser_auth()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json({"error": "invalid_content_length"}, HTTPStatus.BAD_REQUEST)
                return None
            if length <= 0 or length > 1_048_576:
                self._send_json({"error": "invalid_body_size"}, HTTPStatus.BAD_REQUEST)
                return None
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
                return None
            if not isinstance(payload, dict):
                self._send_json({"error": "json_body_must_be_object"}, HTTPStatus.BAD_REQUEST)
                return None
            return payload

        def _handle_feishu_event(self) -> None:
            payload = self._read_json_body()
            if payload is None:
                return
            if not _verify_feishu_token(payload, settings):
                self._send_json({"error": "invalid_feishu_token"}, HTTPStatus.FORBIDDEN)
                return
            if payload.get("challenge"):
                self._send_json({"challenge": payload["challenge"]})
                return

            message = FeishuEventNormalizer().normalize(payload)
            runtime_request = GatewayCommandRouter().route(message)
            runtime = TaskRuntime(
                registry=registry,
                task_store=TaskStore(settings.task_store_path),
                settings=settings,
            )
            task = runtime.submit(runtime_request)
            self._send_json({"code": 0, "task": to_jsonable(task)})

        def _handle_open_browser_auth(self) -> None:
            payload = self._read_json_body()
            if payload is None:
                return
            url = str(payload.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                self._send_json({"error": "valid_url_required"}, HTTPStatus.BAD_REQUEST)
                return
            if not has_python_package("playwright"):
                self._send_json({
                    "error": "playwright_unavailable",
                    "install_hint": (
                        "pip install -e '.[url-ingest]' && "
                        "python -m playwright install chromium"
                    ),
                }, HTTPStatus.CONFLICT)
                return
            thread = threading.Thread(
                target=_open_browser_auth_background,
                args=(settings, url),
                daemon=True,
            )
            thread.start()
            self._send_json({
                "status": "opening",
                "url": url,
                "profile_dir": str(profile_dir_for_url(settings, url)),
            }, HTTPStatus.ACCEPTED)

        def _allow_admin_request(self) -> bool:
            if _remote_admin_allowed() or _is_loopback(self.client_address[0]):
                return True
            self._send_json({"error": "admin_api_requires_localhost"}, HTTPStatus.FORBIDDEN)
            return False

    return AdminHandler


def _flatten_dependencies(health) -> list[dict[str, Any]]:
    rows = []
    for item in health:
        for dep in item.dependencies:
            row = to_jsonable(dep)
            row["capability"] = item.name
            rows.append(row)
    return rows


def _auth_summary(health) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in health:
        targets = item.configuration.get("auth_targets")
        if isinstance(targets, list) and targets:
            for target in targets:
                if not isinstance(target, dict):
                    continue
                rows.append({
                    "capability": item.name,
                    "site": str(target.get("site", "")),
                    "login_url": str(target.get("login_url", "")),
                    "profile_dir": str(target.get("profile_dir", "")),
                    "state": str(target.get("state", item.auth_state.value)),
                })
        else:
            rows.append({"capability": item.name, "state": item.auth_state.value})
    return rows


def _limit_from_query(query: str, default: int) -> int:
    raw = parse_qs(query).get("limit", [str(default)])[0]
    try:
        return max(1, min(int(raw), 200))
    except ValueError:
        return default


def _verify_feishu_token(payload: dict[str, Any], settings: Settings) -> bool:
    expected = settings.feishu_verification_token
    if not expected:
        return True
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    token = str(payload.get("token") or header.get("token") or "")
    return token == expected


def _open_browser_auth_background(settings: Settings, url: str) -> None:
    try:
        open_browser_login(settings, url)
    except Exception as exc:
        print(f"AgentFeishu browser auth failed for {url}: {exc}")


def _is_admin_path(path: str) -> bool:
    return path in {"/", "/admin"} or path.startswith("/admin/api/") or (
        path.startswith("/api/") and not path.startswith("/api/feishu/")
    )


def _remote_admin_allowed() -> bool:
    return os.environ.get("AGENTFEISHU_ALLOW_REMOTE_ADMIN", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_loopback(host: str) -> bool:
    return host == "::1" or host.startswith("127.") or host == "localhost"
