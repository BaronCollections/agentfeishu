"""Project configuration and state-directory layout."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - Python 3.11+ path
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 path
    import tomli as tomllib  # type: ignore


SENSITIVE_KEY_PARTS = ("token", "secret", "password", "key")


def _default_project_root() -> Path:
    return Path.cwd()


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a TOML table: {path}")
    return data


def _path_from_env_or_config(name: str, config_value: str | None,
                             default: Path) -> Path:
    env_value = os.environ.get(name)
    raw = env_value or config_value
    if not raw:
        return default
    return Path(raw).expanduser()


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one AgentFeishu project."""

    project_root: Path = field(default_factory=_default_project_root)
    state_dir: Path | None = None
    browser_profiles_dir: Path | None = None
    downloads_dir: Path | None = None
    artifacts_dir: Path | None = None
    config_path: Path | None = None
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    codex_command: str = "codex"
    runtime_max_workers: int = 4
    enabled_capabilities: tuple[str, ...] = ()
    capability_settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        root = self.project_root.expanduser().resolve()
        object.__setattr__(self, "project_root", root)
        state = (self.state_dir or root / "state").expanduser()
        object.__setattr__(self, "state_dir", state)
        object.__setattr__(
            self, "browser_profiles_dir",
            (self.browser_profiles_dir or state / "browser_profiles").expanduser())
        object.__setattr__(
            self, "downloads_dir",
            (self.downloads_dir or state / "downloads").expanduser())
        object.__setattr__(
            self, "artifacts_dir",
            (self.artifacts_dir or state / "artifacts").expanduser())

    @property
    def task_store_path(self) -> Path:
        return self.state_dir / "tasks.jsonl"

    @property
    def error_log_path(self) -> Path:
        return self.state_dir / "errors.jsonl"

    def ensure_directories(self) -> None:
        for path in (
            self.state_dir,
            self.browser_profiles_dir,
            self.downloads_dir,
            self.artifacts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def site_profile_dir(self, site: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in site)
        return self.browser_profiles_dir / (safe or "default")

    def masked_configuration(self) -> dict[str, Any]:
        """Return configuration safe to expose in the admin UI."""

        result = {
            "project_root": str(self.project_root),
            "state_dir": str(self.state_dir),
            "browser_profiles_dir": str(self.browser_profiles_dir),
            "downloads_dir": str(self.downloads_dir),
            "artifacts_dir": str(self.artifacts_dir),
            "config_path": str(self.config_path) if self.config_path else "",
            "feishu_app_id": self.feishu_app_id,
            "feishu_app_secret": self.feishu_app_secret,
            "feishu_verification_token": self.feishu_verification_token,
            "codex_command": self.codex_command,
            "runtime_max_workers": self.runtime_max_workers,
            "enabled_capabilities": list(self.enabled_capabilities),
        }
        return {
            key: ("***" if _is_sensitive_key(key) and value else value)
            for key, value in result.items()
        }

    @classmethod
    def from_env(cls) -> "Settings":
        return load_settings(create_dirs=False)

    def capability_enabled(self, capability_id: str) -> bool:
        env_name = (
            "AGENTFEISHU_CAPABILITY_"
            + capability_id.upper().replace("-", "_")
            + "_ENABLED"
        )
        env_value = os.environ.get(env_name)
        if env_value is not None:
            return env_value.strip().lower() not in {"0", "false", "no", "off"}
        configured = self.capability_settings.get(capability_id, {})
        if "enabled" in configured:
            return bool(configured["enabled"])
        if self.enabled_capabilities:
            return capability_id in self.enabled_capabilities
        return True


def _is_sensitive_key(key: str) -> bool:
    low = key.lower()
    return any(part in low for part in SENSITIVE_KEY_PARTS)


def load_settings(project_root: Path | None = None,
                  config_path: Path | None = None,
                  *, create_dirs: bool = True) -> Settings:
    """Load settings from env and optional TOML.

    Priority is env > config file > defaults. The config file defaults to
    `<project_root>/agentfeishu.toml`.
    """

    root = (project_root or _default_project_root()).expanduser().resolve()
    env_config = os.environ.get("AGENTFEISHU_CONFIG")
    cfg_path = config_path or (Path(env_config).expanduser() if env_config else None)
    cfg_path = cfg_path or root / "agentfeishu.toml"
    raw = _read_toml(cfg_path)
    paths = raw.get("paths") or {}
    feishu = raw.get("feishu") or {}
    runtime = raw.get("runtime") or {}
    capabilities = raw.get("capabilities") or {}
    enabled = capabilities.get("enabled") or ()
    if isinstance(enabled, str):
        enabled_tuple = (enabled,)
    else:
        enabled_tuple = tuple(str(item) for item in enabled)
    settings = Settings(
        project_root=root,
        state_dir=_path_from_env_or_config(
            "AGENTFEISHU_STATE_DIR", paths.get("state_dir"), root / "state"),
        browser_profiles_dir=_path_from_env_or_config(
            "AGENTFEISHU_BROWSER_PROFILES_DIR",
            paths.get("browser_profiles_dir"), root / "state" / "browser_profiles"),
        downloads_dir=_path_from_env_or_config(
            "AGENTFEISHU_DOWNLOADS_DIR", paths.get("downloads_dir"),
            root / "state" / "downloads"),
        artifacts_dir=_path_from_env_or_config(
            "AGENTFEISHU_ARTIFACTS_DIR", paths.get("artifacts_dir"),
            root / "state" / "artifacts"),
        config_path=cfg_path,
        feishu_app_id=os.environ.get("FEISHU_APP_ID", str(feishu.get("app_id", ""))),
        feishu_app_secret=os.environ.get(
            "FEISHU_APP_SECRET", str(feishu.get("app_secret", ""))),
        feishu_verification_token=os.environ.get(
            "FEISHU_VERIFICATION_TOKEN",
            str(feishu.get("verification_token", ""))),
        codex_command=os.environ.get(
            "AGENTFEISHU_CODEX_COMMAND",
            str(runtime.get("codex_command", "codex"))),
        runtime_max_workers=_positive_int_from_env_or_config(
            "AGENTFEISHU_RUNTIME_MAX_WORKERS",
            runtime.get("max_workers"),
            4,
        ),
        enabled_capabilities=enabled_tuple,
        capability_settings={
            str(key): value
            for key, value in capabilities.items()
            if isinstance(value, dict)
        },
        raw_config=raw,
    )
    if create_dirs:
        settings.ensure_directories()
    return settings


def _positive_int_from_env_or_config(name: str, config_value: Any,
                                     default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        raw = config_value
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
