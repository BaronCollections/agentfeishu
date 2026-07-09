"""Dependency detection for URL ingestion."""

from __future__ import annotations

import importlib.util
import shutil
from agentfeishu.core.models import DependencyCheck, DependencyStatus


PYTHON_PACKAGES = {
    "yt_dlp": "pip install -e '.[url-ingest]'",
    "gallery_dl": "pip install -e '.[url-ingest]'",
    "trafilatura": "pip install -e '.[url-ingest]'",
    "playwright": "pip install -e '.[url-ingest]' && python -m playwright install chromium",
    "bs4": "pip install -e '.[url-ingest]'",
    "PIL": "pip install -e '.[url-ingest]'",
}

VISION_PACKAGES = {
    "rapidocr_onnxruntime": "pip install -e '.[url-ingest-vision]'",
    "faster_whisper": "pip install -e '.[url-ingest-vision]'",
}

COMMANDS = {
    "ffmpeg": "Install ffmpeg and ensure it is on PATH.",
}


def dependency_checks(include_vision: bool = True) -> tuple[DependencyCheck, ...]:
    checks: list[DependencyCheck] = []
    for package, hint in PYTHON_PACKAGES.items():
        checks.append(_python_check(package, hint))
    if include_vision:
        for package, hint in VISION_PACKAGES.items():
            check = _python_check(package, hint)
            if check.status == DependencyStatus.MISSING:
                check = DependencyCheck(
                    name=check.name,
                    status=DependencyStatus.OPTIONAL,
                    required=False,
                    message="optional vision/transcription dependency is not installed",
                    install_hint=check.install_hint,
                )
            checks.append(check)
    for command, hint in COMMANDS.items():
        checks.append(_command_check(command, hint))
    return tuple(checks)


def has_python_package(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


def _python_check(package: str, hint: str) -> DependencyCheck:
    if not has_python_package(package):
        return DependencyCheck(
            name=package,
            status=DependencyStatus.OPTIONAL,
            required=False,
            message="optional python package is not installed",
            install_hint=hint,
        )
    version = _package_version(package)
    return DependencyCheck(
        name=package,
        status=DependencyStatus.AVAILABLE,
        message="available",
        version=version,
    )


def _command_check(command: str, hint: str) -> DependencyCheck:
    path = shutil.which(command)
    if not path:
        return DependencyCheck(
            name=command,
            status=DependencyStatus.OPTIONAL,
            required=False,
            message="external command is not on PATH",
            install_hint=hint,
        )
    return DependencyCheck(
        name=command,
        status=DependencyStatus.AVAILABLE,
        message=path,
    )


def _package_version(package: str) -> str:
    try:
        import importlib.metadata as metadata
        return metadata.version(package.replace("_", "-"))
    except Exception:
        return ""
