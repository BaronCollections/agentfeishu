"""Command-line interface for AgentFeishu."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agentfeishu.capabilities import default_registry
from agentfeishu.capabilities.url_ingest.browser_auth import open_browser_login
from agentfeishu.config import load_settings
from agentfeishu.core import RuntimeRequest, TaskRuntime
from agentfeishu.core.models import to_jsonable
from agentfeishu.core.task_store import TaskStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentfeishu")
    parser.add_argument("--project-root", default="", help="Project root directory")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("capabilities", help="List capability health")

    url = sub.add_parser("url", help="URL capability commands")
    url_sub = url.add_subparsers(dest="url_command")
    parse = url_sub.add_parser("parse", help="Parse a URL or text containing a URL")
    parse.add_argument("input", help="URL or text")
    parse.add_argument("--json", action="store_true", help="Print raw JSON")

    auth = sub.add_parser("auth", help="Authorization helpers")
    auth_sub = auth.add_subparsers(dest="auth_command")
    login = auth_sub.add_parser("login", help="Open a project-local browser login profile")
    login.add_argument("url", help="Login URL, for example https://www.douyin.com/")

    serve = sub.add_parser("serve", help="Start the admin UI/API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).expanduser() if args.project_root else None
    settings = load_settings(root)
    registry = default_registry()

    if args.command == "capabilities":
        print(json.dumps(
            [to_jsonable(item) for item in registry.health(settings)],
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    if args.command == "url" and args.url_command == "parse":
        runtime = TaskRuntime(
            registry=registry,
            task_store=TaskStore(settings.task_store_path),
            settings=settings,
        )
        task = runtime.submit(RuntimeRequest(
            capability_id="url_ingest",
            command="url",
            text=args.input,
            urls=(args.input,) if args.input.startswith(("http://", "https://")) else (),
            source="cli",
        ))
        if args.json:
            print(json.dumps(to_jsonable(task), ensure_ascii=False, indent=2))
        else:
            _print_task_summary(task)
        return 0 if task.status.value in {"succeeded", "needs_auth"} else 1

    if args.command == "auth" and args.auth_command == "login":
        try:
            result = open_browser_login(settings, args.url)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "serve":
        from agentfeishu.server import serve
        serve(settings=settings, registry=registry, host=args.host, port=args.port)
        return 0

    build_parser().print_help()
    return 2


def _print_task_summary(task) -> None:
    print(f"task: {task.id}")
    print(f"status: {task.status.value}")
    if task.result:
        print(f"summary: {task.result.summary}")
        if task.result.next_action:
            print(f"next_action: {task.result.next_action}")
        if task.result.limitations:
            print("limitations:")
            for item in task.result.limitations:
                print(f"- {item.code}: {item.message}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
