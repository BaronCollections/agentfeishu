from __future__ import annotations

from agentfeishu.capabilities.url_ingest import UrlIngestCapability
from agentfeishu.capabilities.url_ingest import extractors
from agentfeishu.capabilities.url_ingest.extractors import ExtractionContext, extract_urls, ingest_url
from agentfeishu.capabilities.url_ingest.models import UrlIngestReport
from agentfeishu.config import load_settings
from agentfeishu.core import CapabilityRequest, Limitation


def test_extract_urls_from_mixed_text():
    urls = extract_urls("看这个 https://v.douyin.com/abc/ ，还有 https://x.test/a)")
    assert urls == ["https://v.douyin.com/abc/", "https://x.test/a"]


def test_url_ingest_requires_url(tmp_path):
    capability = UrlIngestCapability()
    result = capability.execute(
        CapabilityRequest(capability="url_ingest", payload={}, raw_text="no url"),
        load_settings(tmp_path),
    )
    assert result.status == "failed"
    assert result.limitations[0].code == "missing_url"


def test_url_ingest_health_reports_dependencies(tmp_path):
    capability = UrlIngestCapability()
    health = capability.health(load_settings(tmp_path))
    names = {dep.name for dep in health.dependencies}
    assert "yt_dlp" in names
    assert "playwright" in names
    assert health.configuration["browser_profiles_dir"].endswith("browser_profiles")
    assert health.auth_state.value == "required"
    assert health.configuration["auth_targets"][0]["login_url"] == "https://www.douyin.com/"


def test_login_page_returns_browser_auth_limitation(monkeypatch, tmp_path):
    def fake_resolve(url, timeout_s=20):
        return (
            "https://site.example/login?next=/private",
            [url, "https://site.example/login?next=/private"],
            "<html><form><input type='password'></form></html>",
        )

    monkeypatch.setattr(extractors, "resolve_url", fake_resolve)
    monkeypatch.setattr(extractors, "has_python_package", lambda package: False)

    report = ingest_url(
        "https://site.example/private",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "dynamic_page"
    assert report.limitations[0].code == "needs_browser_auth"
    assert report.limitations[0].next_action == "open_browser_login"
    assert any(item.kind == "browser_profile" for item in report.evidence)


def test_direct_image_url_returns_artifact_and_ocr_limitation(monkeypatch, tmp_path):
    monkeypatch.setattr(extractors, "resolve_url", lambda url, timeout_s=20: (url, [url], ""))
    monkeypatch.setattr(extractors, "has_python_package", lambda package: False)

    report = ingest_url(
        "https://cdn.example/image.webp",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "image"
    assert report.artifacts[0].kind == "image"
    assert report.artifacts[0].path == "https://cdn.example/image.webp"
    assert report.limitations[0].code == "ocr_not_run"


def test_direct_video_url_reports_ytdlp_limitation(monkeypatch, tmp_path):
    monkeypatch.setattr(extractors, "resolve_url", lambda url, timeout_s=20: (url, [url], ""))
    monkeypatch.setattr(extractors, "has_python_package", lambda package: False)

    report = ingest_url(
        "https://cdn.example/video.mp4",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "video"
    assert report.limitations[0].code == "yt_dlp_unavailable"
    assert report.limitations[0].recoverable is True


def test_http_forbidden_returns_browser_auth_limitation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        extractors,
        "resolve_url",
        lambda url, timeout_s=20: (url, [url], "forbidden", 403),
    )
    monkeypatch.setattr(extractors, "has_python_package", lambda package: False)

    report = ingest_url(
        "https://private.example/page",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "dynamic_page"
    assert report.limitations[0].code == "needs_browser_auth"
    assert "HTTP 403" in report.limitations[0].message


def test_http_rate_limit_returns_access_limitation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        extractors,
        "resolve_url",
        lambda url, timeout_s=20: (url, [url], "rate limited", 429),
    )
    monkeypatch.setattr(extractors, "has_python_package", lambda package: False)

    report = ingest_url(
        "https://public.example/page",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "access_limited"
    assert report.limitations[0].code == "rate_limited_or_anti_automation"
    assert report.limitations[0].recoverable is True


def test_ytdlp_failure_does_not_block_article_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        extractors,
        "resolve_url",
        lambda url, timeout_s=20: (url, [url], "<html>article</html>", 200),
    )
    monkeypatch.setattr(
        extractors,
        "_try_ytdlp",
        lambda input_url, final_url, context: UrlIngestReport(
            input_url=input_url,
            resolved_url=final_url,
            content_type="video",
            limitations=(Limitation(
                code="yt_dlp_failed",
                message="not a supported video",
                recoverable=True,
            ),),
        ),
    )
    monkeypatch.setattr(
        extractors,
        "_try_trafilatura",
        lambda input_url, final_url, preview, context: UrlIngestReport(
            input_url=input_url,
            resolved_url=final_url,
            content_type="article",
            text="article text",
        ),
    )

    report = ingest_url(
        "https://news.example/story",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "article"
    assert report.text == "article text"


def test_browser_profile_extractor_can_complete_dynamic_page(monkeypatch, tmp_path):
    monkeypatch.setattr(
        extractors,
        "resolve_url",
        lambda url, timeout_s=20: (
            "https://app.example/private",
            [url, "https://app.example/private"],
            "<script>app()</script>",
            200,
        ),
    )
    monkeypatch.setattr(extractors, "has_python_package", lambda package: False)
    monkeypatch.setattr(
        extractors,
        "try_browser_extract",
        lambda url, settings, timeout_s=20: UrlIngestReport(
            input_url=url,
            resolved_url=url,
            content_type="browser_page",
            title="Private dashboard",
            text="authorized content",
        ),
    )

    report = ingest_url(
        "https://app.example/link",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "browser_page"
    assert report.title == "Private dashboard"
    assert report.text == "authorized content"


def test_url_resolution_error_returns_structured_limitation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        extractors,
        "resolve_url",
        lambda url, timeout_s=20: (_ for _ in ()).throw(
            extractors.UrlIngestError("dns failed")
        ),
    )

    report = ingest_url(
        "https://missing.example/item",
        ExtractionContext(settings=load_settings(tmp_path)),
    )

    assert report.content_type == "network_error"
    assert report.limitations[0].code == "url_resolution_failed"
    assert report.limitations[0].recoverable is True
