"""Admin API and dashboard server."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from html import escape
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
from agentfeishu.core import BackgroundTaskRuntime, CapabilityRegistry
from agentfeishu.core.auth_links import verify_task_token
from agentfeishu.core.models import (
    CapabilityRequest,
    RuntimeRequest,
    Task,
    TaskStatus,
    to_jsonable,
)
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
    try:
        httpd.serve_forever()
    finally:
        handler.shutdown_runtime()
        httpd.server_close()


def make_handler(settings: Settings, registry: CapabilityRegistry):
    task_store = TaskStore(settings.task_store_path)
    background_runtime = BackgroundTaskRuntime(
        registry=registry,
        task_store=task_store,
        settings=settings,
    )

    class AdminHandler(BaseHTTPRequestHandler):
        server_version = "AgentFeishuAdmin/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            if parsed.path == "/auth/start":
                if not self._allow_admin_request():
                    return
                self._handle_auth_start(parsed.query)
                return
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
                self._send_json(task_store.list(limit=limit))
                return
            if parsed.path in {"/admin/api/config", "/api/config"}:
                self._send_json(settings.masked_configuration())
                return
            if parsed.path in {"/api/auth/sessions", "/admin/api/auth/sessions"}:
                self._handle_auth_session(parsed.query)
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
            auth_task_id = _task_id_from_auth_open_path(parsed.path)
            if auth_task_id:
                if not self._allow_admin_request():
                    return
                self._handle_open_browser_auth_and_resume(auth_task_id)
                return
            task_id = _task_id_from_resume_path(parsed.path)
            if task_id:
                if not self._allow_admin_request():
                    return
                self._handle_resume_task(task_id)
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

        def _send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
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
            task = background_runtime.submit(runtime_request)
            self._send_json({"code": 0, "task": to_jsonable(task)}, HTTPStatus.ACCEPTED)

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

        def _handle_open_browser_auth_and_resume(self, task_id: str) -> None:
            payload = self._read_json_body()
            if payload is None:
                return
            token = str(payload.get("token") or "")
            if not verify_task_token(settings, task_id, token):
                self._send_json({"error": "invalid_auth_token"}, HTTPStatus.FORBIDDEN)
                return
            task = _task_row(task_store, task_id)
            if task is None:
                self._send_json({"error": "task_not_found"}, HTTPStatus.NOT_FOUND)
                return
            if task.get("status") not in {
                TaskStatus.NEEDS_AUTH.value,
                TaskStatus.WAITING_FOR_AUTH.value,
            }:
                self._send_json({
                    "error": "task_not_waiting_for_auth",
                    "status": task.get("status"),
                }, HTTPStatus.CONFLICT)
                return
            login_url = _login_url_for_task(task)
            if not has_python_package("playwright"):
                self._send_json({
                    "error": "playwright_unavailable",
                    "install_hint": (
                        "pip install -e '.[url-ingest]' && "
                        "python -m playwright install chromium"
                    ),
                }, HTTPStatus.CONFLICT)
                return
            waiting = task_store.update(
                _task_from_row(task).transition(TaskStatus.WAITING_FOR_AUTH)
            )
            thread = threading.Thread(
                target=_open_browser_auth_and_resume_background,
                args=(
                    settings,
                    login_url,
                    background_runtime,
                    waiting,
                    _runtime_request_from_task_row(task),
                ),
                daemon=True,
            )
            thread.start()
            self._send_json({
                "status": "opening",
                "url": login_url,
                "task_id": task_id,
                "profile_dir": str(profile_dir_for_url(settings, login_url)),
            }, HTTPStatus.ACCEPTED)

        def _handle_auth_start(self, query: str) -> None:
            task_id, token = _auth_query_parts(query)
            if not verify_task_token(settings, task_id, token):
                self._send_html(
                    _error_page("invalid_auth_token", "Invalid or expired auth link."),
                    HTTPStatus.FORBIDDEN,
                )
                return
            task = _task_row(task_store, task_id)
            if task is None:
                self._send_html(
                    _error_page("task_not_found", "The task no longer exists."),
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_html(_auth_start_page(task, token, _login_url_for_task(task)))

        def _handle_auth_session(self, query: str) -> None:
            task_id, token = _auth_query_parts(query)
            if not verify_task_token(settings, task_id, token):
                self._send_json({"error": "invalid_auth_token"}, HTTPStatus.FORBIDDEN)
                return
            task = _task_row(task_store, task_id)
            if task is None:
                self._send_json({"error": "task_not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "task": task,
                "login_url": _login_url_for_task(task),
                "can_resume": task.get("status") in {
                    TaskStatus.NEEDS_AUTH.value,
                    TaskStatus.WAITING_FOR_AUTH.value,
                },
            })

        def _handle_resume_task(self, task_id: str) -> None:
            payload = self._read_json_body()
            if payload is None:
                return
            token = str(payload.get("token") or "")
            if not verify_task_token(settings, task_id, token):
                self._send_json({"error": "invalid_auth_token"}, HTTPStatus.FORBIDDEN)
                return
            task = _task_row(task_store, task_id)
            if task is None:
                self._send_json({"error": "task_not_found"}, HTTPStatus.NOT_FOUND)
                return
            if task.get("status") not in {
                TaskStatus.NEEDS_AUTH.value,
                TaskStatus.WAITING_FOR_AUTH.value,
            }:
                self._send_json({
                    "error": "task_not_waiting_for_auth",
                    "status": task.get("status"),
                }, HTTPStatus.CONFLICT)
                return
            resumed = background_runtime.resume(
                _task_from_row(task),
                _runtime_request_from_task_row(task),
            )
            self._send_json({"code": 0, "task": to_jsonable(resumed)}, HTTPStatus.ACCEPTED)

        def _allow_admin_request(self) -> bool:
            if _remote_admin_allowed() or _is_loopback(self.client_address[0]):
                return True
            self._send_json({"error": "admin_api_requires_localhost"}, HTTPStatus.FORBIDDEN)
            return False

        @staticmethod
        def shutdown_runtime() -> None:
            background_runtime.shutdown(wait=False, cancel_futures=False)

    return AdminHandler


def _auth_query_parts(query: str) -> tuple[str, str]:
    parsed = parse_qs(query)
    return (
        str(parsed.get("task_id", [""])[0]),
        str(parsed.get("token", [""])[0]),
    )


def _task_id_from_resume_path(path: str) -> str:
    prefix = "/api/tasks/"
    suffix = "/resume"
    if path.startswith(prefix) and path.endswith(suffix):
        return path[len(prefix):-len(suffix)].strip("/")
    return ""


def _task_id_from_auth_open_path(path: str) -> str:
    prefix = "/api/tasks/"
    suffix = "/auth/open"
    if path.startswith(prefix) and path.endswith(suffix):
        return path[len(prefix):-len(suffix)].strip("/")
    return ""


def _task_row(task_store: TaskStore, task_id: str) -> dict[str, Any] | None:
    try:
        return task_store.get(task_id)
    except KeyError:
        return None


def _runtime_request_from_task_row(row: dict[str, Any]) -> RuntimeRequest:
    request = row.get("request") if isinstance(row.get("request"), dict) else {}
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    urls = payload.get("urls") or ()
    if isinstance(urls, str):
        urls = (urls,)
    return RuntimeRequest(
        capability_id=str(request.get("capability") or row.get("capability") or ""),
        command=str(payload.get("command") or request.get("capability") or ""),
        text=str(payload.get("text") or request.get("raw_text") or ""),
        urls=tuple(str(item) for item in urls),
        actor_id=str(request.get("sender_id") or ""),
        source=str(request.get("source") or "feishu"),
        metadata=(
            request.get("metadata")
            if isinstance(request.get("metadata"), dict)
            else {}
        ),
    )


def _task_from_row(row: dict[str, Any]) -> Task:
    request = row.get("request") if isinstance(row.get("request"), dict) else {}
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    return Task(
        id=str(row["id"]),
        capability=str(row.get("capability") or request.get("capability") or "unknown"),
        status=TaskStatus(str(row.get("status") or TaskStatus.QUEUED.value)),
        request=CapabilityRequest(
            capability=str(request.get("capability") or row.get("capability") or "unknown"),
            payload=dict(payload),
            raw_text=str(request.get("raw_text") or ""),
            sender_id=str(request.get("sender_id") or ""),
            source=str(request.get("source") or "feishu"),
            metadata=(
                request.get("metadata")
                if isinstance(request.get("metadata"), dict)
                else {}
            ),
        ),
        created_at=_parse_datetime(row.get("created_at")),
        updated_at=_parse_datetime(row.get("updated_at")),
        started_at=_parse_datetime(row.get("started_at")),
        finished_at=_parse_datetime(row.get("finished_at")),
        result=row.get("result"),
        error=row.get("error"),
    )


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _login_url_for_task(task: dict[str, Any]) -> str:
    url = _first_task_url(task)
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "douyin.com" in host:
        return "https://www.douyin.com/"
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return "https://www.douyin.com/"


def _first_task_url(task: dict[str, Any]) -> str:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    resolved = str(data.get("resolved_url") or "")
    if resolved:
        return resolved
    request = task.get("request") if isinstance(task.get("request"), dict) else {}
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    urls = payload.get("urls")
    if isinstance(urls, list) and urls:
        return str(urls[0])
    if isinstance(urls, str):
        return urls
    return ""


def _auth_start_page(task: dict[str, Any], token: str, login_url: str) -> str:
    task_id = str(task.get("id", ""))
    safe_task_id = escape(task_id)
    safe_token = escape(token)
    safe_login_url = escape(login_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AgentFeishu Auth</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2328;
      --muted: #667085;
      --line: #d9dee7;
      --accent: #1456d9;
      --ok: #137333;
      --warn: #b25e09;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    main {{
      width: min(680px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
    }}
    h1 {{ margin: 0 0 12px; font-size: 20px; }}
    p {{ color: var(--muted); line-height: 1.5; }}
    code {{ overflow-wrap: anywhere; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
    button {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      background: #fff;
      color: var(--accent);
      font-weight: 650;
      cursor: pointer;
    }}
    button.primary {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    #status {{ margin-top: 16px; color: var(--warn); }}
    #status.ok {{ color: var(--ok); }}
  </style>
</head>
<body>
  <main>
    <h1>AgentFeishu Auth</h1>
    <p>Task <code>{safe_task_id}</code> needs a user-authorized browser session.</p>
    <p>Login target: <code>{safe_login_url}</code></p>
    <div class="actions">
      <button id="open" type="button">Open Browser Login</button>
      <button id="resume" class="primary" type="button">Continue Parsing</button>
    </div>
    <div id="status">Waiting for authorization.</div>
  </main>
  <script>
    const taskId = {json.dumps(task_id)};
    const token = {json.dumps(token)};
    const loginUrl = {json.dumps(login_url)};
    const statusEl = document.getElementById('status');
    function setStatus(text, ok = false) {{
      statusEl.textContent = text;
      statusEl.className = ok ? 'ok' : '';
    }}
    document.getElementById('open').addEventListener('click', async () => {{
      setStatus('Opening browser login. The task will resume after the browser closes...');
      const response = await fetch(`/api/tasks/${{taskId}}/auth/open`, {{
        method: 'POST',
        headers: {{'content-type': 'application/json'}},
        body: JSON.stringify({{token}})
      }});
      setStatus(response.ok ? 'Browser login opened. Finish login and close the browser window to resume automatically.' : 'Unable to open browser login.', response.ok);
    }});
    document.getElementById('resume').addEventListener('click', async () => {{
      setStatus('Resuming task...');
      const response = await fetch(`/api/tasks/${{taskId}}/resume`, {{
        method: 'POST',
        headers: {{'content-type': 'application/json'}},
        body: JSON.stringify({{token}})
      }});
      if (response.ok) {{
        setStatus('Task queued again. You can return to Feishu or the admin console.', true);
      }} else {{
        const body = await response.text();
        setStatus('Resume failed: ' + body);
      }}
    }});
  </script>
</body>
</html>"""


def _error_page(code: str, message: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{escape(code)}</title></head><body>"
        f"<h1>{escape(code)}</h1><p>{escape(message)}</p>"
        "</body></html>"
    )


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


def _open_browser_auth_and_resume_background(
    settings: Settings,
    url: str,
    runtime: BackgroundTaskRuntime,
    task: Task,
    request: RuntimeRequest,
) -> None:
    try:
        open_browser_login(settings, url)
    except Exception as exc:
        print(f"AgentFeishu browser auth failed for {url}: {exc}")
    runtime.resume(task, request)


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
