"""Signed local auth links for user-authorized browser sessions."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import secrets
from urllib.parse import urlencode

from agentfeishu.config import Settings

from .models import CapabilityResult


def auth_url_for_task(settings: Settings, task_id: str) -> str:
    query = urlencode({
        "task_id": task_id,
        "token": task_token(settings, task_id),
    })
    return f"{settings.local_auth_base_url.rstrip('/')}/auth/start?{query}"


def task_token(settings: Settings, task_id: str) -> str:
    secret = _local_auth_secret(settings)
    digest = hmac.new(secret, task_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def verify_task_token(settings: Settings, task_id: str, token: str) -> bool:
    if not task_id or not token:
        return False
    return hmac.compare_digest(task_token(settings, task_id), token)


def attach_auth_url(settings: Settings, task_id: str, result):
    if isinstance(result, CapabilityResult):
        data = dict(result.data)
        data.setdefault("auth_url", auth_url_for_task(settings, task_id))
        data.setdefault("auth_task_id", task_id)
        return replace(result, data=data, next_action="open_auth_url")
    if isinstance(result, dict):
        data = dict(result)
        data.setdefault("auth_url", auth_url_for_task(settings, task_id))
        data.setdefault("auth_task_id", task_id)
        return data
    return result


def _local_auth_secret(settings: Settings) -> bytes:
    path = settings.local_auth_secret_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes().strip()
    secret = secrets.token_urlsafe(32).encode("utf-8")
    path.write_bytes(secret)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret
