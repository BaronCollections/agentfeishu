from __future__ import annotations

import sys
import types

from agentfeishu.capabilities.url_ingest import UrlIngestCapability
from agentfeishu.capabilities.url_ingest import browser_auth
from agentfeishu.capabilities.url_ingest import capability as capability_module
from agentfeishu.capabilities.url_ingest import extractors
from agentfeishu.capabilities.url_ingest.extractors import ExtractionContext, extract_urls, ingest_url
from agentfeishu.capabilities.url_ingest.models import UrlIngestReport
from agentfeishu.config import load_settings
from agentfeishu.core import AuthState, CapabilityRequest, Limitation


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


def test_url_ingest_marks_limited_reports_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(
        capability_module,
        "ingest_url",
        lambda url, context: UrlIngestReport(
            input_url=url,
            resolved_url=url,
            content_type="browser_page",
            text="视频数据加载中",
            limitations=(Limitation(
                code="browser_content_loading",
                message="still loading",
                recoverable=True,
            ),),
        ),
    )
    capability = UrlIngestCapability()

    result = capability.execute(
        CapabilityRequest(
            capability="url_ingest",
            payload={"url": "https://www.douyin.com/video/1"},
            raw_text="https://www.douyin.com/video/1",
        ),
        load_settings(tmp_path),
    )

    assert result.status == "partial"
    assert result.summary == "still loading"
    assert result.limitations[0].code == "browser_content_loading"


def test_url_ingest_health_reports_dependencies(tmp_path):
    capability = UrlIngestCapability()
    health = capability.health(load_settings(tmp_path))
    names = {dep.name for dep in health.dependencies}
    assert "yt_dlp" in names
    assert "playwright" in names
    assert health.configuration["browser_profiles_dir"].endswith("browser_profiles")
    assert health.auth_state.value == "required"
    assert health.configuration["auth_targets"][0]["login_url"] == "https://www.douyin.com/"


def test_douyin_short_and_www_hosts_share_authorized_profile_key(tmp_path):
    settings = load_settings(tmp_path)

    assert browser_auth.site_key_from_url("https://v.douyin.com/example/") == "www_douyin_com"
    assert (
        browser_auth.profile_dir_for_url(settings, "https://v.douyin.com/example/")
        == browser_auth.profile_dir_for_url(settings, "https://www.douyin.com/")
    )


def test_nonempty_profile_without_login_cookie_is_unknown_auth_state(tmp_path):
    settings = load_settings(tmp_path)
    profile_dir = browser_auth.profile_dir_for_url(settings, "https://www.douyin.com/")
    profile_dir.mkdir(parents=True)
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")

    assert browser_auth.profile_state(profile_dir) is AuthState.READY
    assert (
        browser_auth.authorization_state_for_url(settings, "https://www.douyin.com/")
        is AuthState.UNKNOWN
    )


def test_cookiefile_with_login_cookie_marks_authorization_ready(tmp_path):
    settings = load_settings(tmp_path)
    cookies_dir = settings.state_dir / "cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "www_douyin_com.txt").write_text(
        "\n".join([
            "# Netscape HTTP Cookie File",
            ".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc",
        ]) + "\n",
        encoding="utf-8",
    )

    assert (
        browser_auth.authorization_state_for_url(settings, "https://v.douyin.com/example/")
        is AuthState.READY
    )


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


def test_ytdlp_uses_exported_browser_profile_cookies(monkeypatch, tmp_path):
    settings = load_settings(tmp_path)
    cookiefile = tmp_path / "cookies.txt"
    captured_options = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured_options.update(options)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {
                "id": "video_1",
                "title": "video title",
                "description": "video text",
                "webpage_url": url,
            }

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(extractors, "has_python_package", lambda package: True)
    monkeypatch.setattr(
        extractors,
        "export_profile_cookies",
        lambda settings, url: cookiefile,
    )

    report = extractors._try_ytdlp(
        "https://v.douyin.com/example/",
        "https://www.douyin.com/video/1",
        ExtractionContext(settings=settings),
    )

    assert report is not None
    assert report.title == "video title"
    assert captured_options["cookiefile"] == str(cookiefile)
    assert captured_options["no_warnings"] is True
    assert hasattr(captured_options["logger"], "error")


def test_ytdlp_auth_error_after_cookie_export_is_not_reported_as_missing_auth(
    monkeypatch,
    tmp_path,
):
    settings = load_settings(tmp_path)
    cookiefile = tmp_path / "cookies.txt"

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("Fresh cookies (not necessarily logged in) are needed")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(extractors, "has_python_package", lambda package: True)
    monkeypatch.setattr(
        extractors,
        "export_profile_cookies",
        lambda settings, url: cookiefile,
    )

    report = extractors._try_ytdlp(
        "https://v.douyin.com/example/",
        "https://www.douyin.com/video/1",
        ExtractionContext(settings=settings),
    )

    assert report is not None
    assert report.limitations[0].code == "extractor_auth_rejected"
    assert "needs_browser_auth" not in {item.code for item in report.limitations}


def test_browser_loading_state_is_not_treated_as_extracted_content():
    assert browser_auth._looks_like_loading_state(
        "https://www.douyin.com/video/1",
        "",
        "精选\\n推荐\\n视频数据加载中",
    )


def test_browser_extract_falls_back_to_visible_capture_on_loading(monkeypatch, tmp_path):
    settings = load_settings(tmp_path)
    profile_dir = browser_auth.profile_dir_for_url(settings, "https://www.douyin.com/video/1")
    profile_dir.mkdir(parents=True)
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_extract_once(
        url, settings, sync_playwright, *, timeout_s, headless, wait_after_load_s
    ):
        calls.append({
            "headless": headless,
            "wait_after_load_s": wait_after_load_s,
        })
        if headless:
            return UrlIngestReport(
                input_url=url,
                resolved_url=url,
                content_type="browser_page",
                text="视频数据加载中",
                limitations=(Limitation(
                    code="browser_content_loading",
                    message="loading",
                    recoverable=True,
                ),),
            )
        return UrlIngestReport(
            input_url=url,
            resolved_url=url,
            content_type="video_page",
            title="rendered video title",
            text="章节要点\\n真实页面内容",
        )

    monkeypatch.setattr(browser_auth, "has_python_package", lambda package: True)
    monkeypatch.setattr(browser_auth, "_browser_extract_once", fake_extract_once)
    fake_playwright = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: None
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    report = browser_auth.try_browser_extract("https://www.douyin.com/video/1", settings)

    assert report is not None
    assert report.content_type == "video_page"
    assert report.title == "rendered video title"
    assert report.limitations == ()
    assert calls == [
        {"headless": True, "wait_after_load_s": 1},
        {"headless": False, "wait_after_load_s": 8},
    ]


def test_douyin_browser_text_is_cleaned_and_title_can_be_inferred():
    raw = "\n".join([
        "精选",
        "推荐",
        "2026 © 抖音",
        "京ICP备16016397号-3",
        "章节要点：共10个",
        "作者声明：内容由 AI 生成",
        "章节要点",
        "超强台风巴威从8级迅速增强至17级。",
        "第185集 | 台风巴威超17级，强度堪比摩羯",
    ])

    cleaned = browser_auth._clean_browser_text("https://www.douyin.com/video/1", raw)

    assert cleaned.startswith("章节要点：共10个")
    assert "京ICP备" not in cleaned
    assert browser_auth._infer_title_from_text("", cleaned) == "台风巴威超17级，强度堪比摩羯"


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
