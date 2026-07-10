"""URL ingestion capability implementation."""

from __future__ import annotations

from agentfeishu.config import Settings
from agentfeishu.core.models import (
    CapabilityDescriptor,
    CapabilityHealth,
    CapabilityRequest,
    CapabilityResult,
    Evidence,
    Limitation,
    RuntimeRequest,
)
from agentfeishu.core.runtime import RuntimeContext

from .dependencies import dependency_checks
from .browser_auth import aggregate_auth_state, auth_targets
from .extractors import ExtractionContext, extract_urls, ingest_url


class UrlIngestCapability:
    descriptor = CapabilityDescriptor(
        name="url_ingest",
        title="URL Content Ingestion",
        description=(
            "Parse video, image, gallery, and article URLs into structured "
            "evidence. Uses public extractors first and browser authorization "
            "when required."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["url"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "content_type": {"type": "string"},
                "resolved_url": {"type": "string"},
                "evidence": {"type": "array"},
                "limitations": {"type": "array"},
            },
        },
        tags=("url", "video", "image", "web"),
        requires_auth=True,
    )

    def health(self, settings: Settings) -> CapabilityHealth:
        checks = dependency_checks(include_vision=True)
        unavailable_required = [
            check.name for check in checks
            if check.required and not check.ok
        ]
        missing_optional = [check.name for check in checks if not check.required and not check.ok]
        enabled = settings.capability_enabled(self.descriptor.capability_id)
        targets = auth_targets(settings)
        warnings = tuple(
            f"optional dependency unavailable: {name}"
            for name in missing_optional
        )
        status = "disabled" if not enabled else "unavailable" if unavailable_required else (
            "degraded" if missing_optional else "ready"
        )
        return CapabilityHealth(
            name=self.descriptor.name,
            enabled=enabled,
            status=status,
            dependencies=checks,
            auth_state=aggregate_auth_state(settings),
            configuration={
                "browser_profiles_dir": str(settings.browser_profiles_dir),
                "downloads_dir": str(settings.downloads_dir),
                "artifacts_dir": str(settings.artifacts_dir),
                "auth_targets": [
                    {
                        "site": target.site,
                        "login_url": target.login_url,
                        "profile_dir": str(target.profile_dir),
                        "state": target.state.value,
                    }
                    for target in targets
                ],
            },
            warnings=warnings,
        )

    def execute(self, request: RuntimeRequest | CapabilityRequest,
                context: RuntimeContext | Settings) -> CapabilityResult:
        settings = context.settings if isinstance(context, RuntimeContext) else context
        url, raw_text = _request_url_and_text(request)
        if not url:
            urls = extract_urls(raw_text)
            url = urls[0] if urls else ""
        if not url:
            return CapabilityResult(
                status="failed",
                summary="No URL was found in the request.",
                limitations=(Limitation(
                    code="missing_url",
                    message="Provide at least one http:// or https:// URL.",
                ),),
            )
        report = ingest_url(url, ExtractionContext(settings=settings))
        status = "ok"
        next_action = ""
        if any(item.code == "needs_browser_auth" for item in report.limitations):
            status = "needs_browser_auth"
            next_action = "open_browser_login"
        elif report.limitations:
            status = "partial"
        summary = _summary_from_report(report)
        return CapabilityResult(
            status=status,
            summary=summary,
            data=report.to_dict(),
            evidence=report.evidence,
            limitations=report.limitations,
            next_action=next_action,
        )


def _request_url_and_text(request: RuntimeRequest | CapabilityRequest) -> tuple[str, str]:
    if isinstance(request, RuntimeRequest):
        url = request.urls[0] if request.urls else ""
        return url, request.text
    url = str(request.payload.get("url") or "")
    urls = request.payload.get("urls")
    if not url and isinstance(urls, (list, tuple)) and urls:
        url = str(urls[0])
    raw_text = request.raw_text or str(request.payload.get("text") or "")
    return url, raw_text


def _summary_from_report(report) -> str:
    auth_limitation = next(
        (item for item in report.limitations if item.code == "needs_browser_auth"),
        None,
    )
    if auth_limitation:
        return auth_limitation.message
    if report.limitations:
        return report.limitations[0].message
    if report.title:
        return f"{report.content_type}: {report.title}"
    if report.text:
        return f"{report.content_type}: {report.text[:160]}"
    return f"{report.content_type}: {report.resolved_url}"
