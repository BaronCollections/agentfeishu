"""Project-local browser authorization helpers for URL ingestion."""

from __future__ import annotations

import time
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentfeishu.config import Settings
from agentfeishu.core.models import AuthState, Evidence, Limitation

from .dependencies import has_python_package
from .models import UrlArtifact, UrlIngestReport


DEFAULT_AUTH_TARGETS = {
    "douyin": "https://www.douyin.com/",
}

DEFAULT_AUTH_COOKIE_NAMES = {
    "douyin.com": (
        "sessionid",
        "sessionid_ss",
        "sid_tt",
        "uid_tt",
        "uid_tt_ss",
    ),
}
DEFAULT_VISIBLE_FALLBACK_SITES = ("douyin.com",)

_LOCKS_GUARD = threading.Lock()
_PROFILE_LOCKS: dict[Path, threading.RLock] = {}


@dataclass(frozen=True)
class BrowserAuthTarget:
    site: str
    login_url: str
    profile_dir: Path
    state: AuthState


def site_key_from_url(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower() or "default"
    if _host_matches(host, "douyin.com"):
        return "www_douyin_com"
    return host.replace(".", "_").replace(":", "_")


def profile_dir_for_url(settings: Settings, url: str) -> Path:
    return settings.site_profile_dir(site_key_from_url(url))


def _profile_lock(profile_dir: Path) -> threading.RLock:
    resolved = profile_dir.expanduser().resolve()
    with _LOCKS_GUARD:
        lock = _PROFILE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PROFILE_LOCKS[resolved] = lock
        return lock


def profile_state(profile_dir: Path) -> AuthState:
    if not profile_dir.exists():
        return AuthState.REQUIRED
    try:
        if any(profile_dir.iterdir()):
            return AuthState.READY
    except OSError:
        return AuthState.UNKNOWN
    return AuthState.REQUIRED


def authorization_state_for_url(settings: Settings, url: str) -> AuthState:
    """Return whether a project-local profile has proven login cookies."""

    if _cookiefile_has_authorization(settings, url):
        return AuthState.READY
    if profile_state(profile_dir_for_url(settings, url)) is AuthState.READY:
        return AuthState.UNKNOWN
    return AuthState.REQUIRED


def auth_targets(settings: Settings) -> tuple[BrowserAuthTarget, ...]:
    targets = []
    configured = settings.capability_settings.get("url_ingest", {}).get("auth_targets")
    raw_targets = configured if isinstance(configured, dict) else DEFAULT_AUTH_TARGETS
    for site, login_url in raw_targets.items():
        if not isinstance(login_url, str) or not login_url:
            continue
        profile_dir = profile_dir_for_url(settings, login_url)
        targets.append(BrowserAuthTarget(
            site=str(site),
            login_url=login_url,
            profile_dir=profile_dir,
            state=authorization_state_for_url(settings, login_url),
        ))
    return tuple(targets)


def aggregate_auth_state(settings: Settings) -> AuthState:
    states = {target.state for target in auth_targets(settings)}
    if AuthState.READY in states:
        return AuthState.READY
    if AuthState.REQUIRED in states:
        return AuthState.REQUIRED
    return AuthState.UNKNOWN


def open_browser_login(
    settings: Settings,
    url: str,
    *,
    wait_for_authorization: bool = False,
    max_wait_s: int = 300,
    poll_interval_s: float = 1.0,
) -> dict[str, str]:
    """Open a headed persistent browser profile for user login.

    The call blocks until the browser window is closed. This is intentional for
    the CLI command; the HTTP API starts it in a background thread. When
    `wait_for_authorization` is enabled, known site-specific login cookies can
    close the browser and release the profile before the user closes the window.
    """

    if not has_python_package("playwright"):
        raise RuntimeError(
            "playwright is not installed; run: pip install -e '.[url-ingest]' "
            "&& python -m playwright install chromium"
        )
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(str(exc)) from exc

    profile_dir = profile_dir_for_url(settings, url)
    profile_dir.mkdir(parents=True, exist_ok=True)
    state = "closed"
    with _profile_lock(profile_dir):
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1_000)
                deadline = time.monotonic() + max_wait_s
                while True:
                    pages = [item for item in context.pages if not item.is_closed()]
                    if not pages:
                        state = "closed"
                        break
                    if wait_for_authorization and _context_has_authorization(
                        context, settings, url
                    ):
                        state = "authorized"
                        break
                    if wait_for_authorization and max_wait_s > 0 and time.monotonic() >= deadline:
                        state = "timeout"
                        break
                    try:
                        pages[0].wait_for_timeout(int(max(0.2, poll_interval_s) * 1000))
                    except Exception:
                        state = "closed"
                        break
            finally:
                try:
                    cookies = context.cookies()
                    if _cookies_indicate_authorized(
                        url, cookies, _authorization_cookie_names_for_url(settings, url)
                    ):
                        state = "authorized"
                        _write_cookiefile(settings, url, cookies)
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
    return {"url": url, "profile_dir": str(profile_dir), "state": state}


def _context_has_authorization(context: Any, settings: Settings, url: str) -> bool:
    cookie_names = _authorization_cookie_names_for_url(settings, url)
    if not cookie_names:
        return False
    try:
        cookies = context.cookies()
    except Exception:
        return False
    return _cookies_indicate_authorized(url, cookies, cookie_names)


def _authorization_cookie_names_for_url(settings: Settings, url: str) -> tuple[str, ...]:
    configured = settings.capability_settings.get("url_ingest", {}).get("auth_cookie_names")
    sources: list[dict[str, Any]] = [DEFAULT_AUTH_COOKIE_NAMES]
    if isinstance(configured, dict):
        sources.append(configured)
    host = urllib.parse.urlparse(url).netloc.lower()
    names: list[str] = []
    for source in sources:
        for host_pattern, raw_names in source.items():
            if not _host_matches(host, str(host_pattern).lower()):
                continue
            if isinstance(raw_names, str):
                raw_iterable = (raw_names,)
            elif isinstance(raw_names, (list, tuple, set)):
                raw_iterable = raw_names
            else:
                continue
            for name in raw_iterable:
                name = str(name).strip()
                if name and name not in names:
                    names.append(name)
    return tuple(names)


def _cookies_indicate_authorized(
    url: str,
    cookies: list[dict[str, Any]],
    cookie_names: tuple[str, ...],
) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    wanted = set(cookie_names)
    now = time.time()
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").lower()
        if name not in wanted or not value or not _cookie_applies_to_host(domain, host):
            continue
        try:
            expires = float(cookie.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0
        if expires > 0 and expires < now:
            continue
        return True
    return False


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.lstrip(".")
    return host == pattern or host.endswith(f".{pattern}")


def _cookie_applies_to_host(domain: str, host: str) -> bool:
    if not domain:
        return False
    domain = domain.lstrip(".")
    return host == domain or host.endswith(f".{domain}")


def export_profile_cookies(settings: Settings, url: str) -> Path | None:
    """Export project browser-profile cookies to a Netscape cookie file."""

    if not has_python_package("playwright"):
        return None
    profile_dir = profile_dir_for_url(settings, url)
    if profile_state(profile_dir) is not AuthState.READY:
        return None
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return None

    with _profile_lock(profile_dir):
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=True,
                )
                cookies = context.cookies()
                context.close()
        except Exception:
            return None

    return _write_cookiefile(settings, url, cookies)


def _write_cookiefile(settings: Settings, url: str,
                      cookies: list[dict[str, Any]]) -> Path | None:
    if not cookies:
        return None
    cookies_dir = settings.state_dir / "cookies"
    cookies_dir.mkdir(parents=True, exist_ok=True)
    cookiefile = cookies_dir / f"{site_key_from_url(url)}.txt"
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by AgentFeishu from a user-authorized browser profile.",
    ]
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "")
        if not name or not domain:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = int(cookie.get("expires") or 0)
        lines.append(
            "\t".join([domain, include_subdomains, path, secure, str(expires), name, value])
        )
    if len(lines) <= 2:
        return None
    cookiefile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        cookiefile.chmod(0o600)
    except OSError:
        pass
    return cookiefile


def _cookiefile_has_authorization(settings: Settings, url: str) -> bool:
    cookie_names = _authorization_cookie_names_for_url(settings, url)
    if not cookie_names:
        return False
    cookiefile = settings.state_dir / "cookies" / f"{site_key_from_url(url)}.txt"
    if not cookiefile.exists():
        return False
    cookies: list[dict[str, Any]] = []
    try:
        for line in cookiefile.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, _, _, expires, name, value = parts[:7]
            cookies.append({
                "domain": domain,
                "expires": expires,
                "name": name,
                "value": value,
            })
    except OSError:
        return False
    return _cookies_indicate_authorized(url, cookies, cookie_names)


def try_browser_extract(url: str, settings: Settings,
                        timeout_s: int = 20) -> UrlIngestReport | None:
    """Try reading a page with an existing persistent browser profile."""

    if not has_python_package("playwright"):
        return None
    profile_dir = profile_dir_for_url(settings, url)
    if profile_state(profile_dir) is not AuthState.READY:
        return None
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return None

    report = _browser_extract_once(
        url,
        settings,
        sync_playwright,
        timeout_s=timeout_s,
        headless=True,
        wait_after_load_s=1,
    )
    if (
        report
        and _report_has_limitation(report, "browser_content_loading")
        and _visible_browser_fallback_enabled(settings, url)
    ):
        visible_report = _browser_extract_once(
            url,
            settings,
            sync_playwright,
            timeout_s=timeout_s,
            headless=False,
            wait_after_load_s=_visible_browser_wait_s(settings),
        )
        if visible_report and not _report_has_limitation(
            visible_report, "browser_content_loading"
        ):
            return visible_report
    return report


def _browser_extract_once(
    url: str,
    settings: Settings,
    sync_playwright,
    *,
    timeout_s: int,
    headless: bool,
    wait_after_load_s: int,
) -> UrlIngestReport:
    profile_dir = profile_dir_for_url(settings, url)
    try:
        with _profile_lock(profile_dir):
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=headless,
                    viewport={"width": 1280, "height": 900},
                    args=(
                        ["--disable-blink-features=AutomationControlled"]
                        if not headless else []
                    ),
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                    if wait_after_load_s > 0:
                        page.wait_for_timeout(wait_after_load_s * 1000)
                    title = page.title()
                    body_text = ""
                    try:
                        body_text = page.locator("body").inner_text(timeout=3_000)
                    except Exception:
                        body_text = ""
                    current_url = page.url
                    video_count = page.locator("video").count()
                    artifacts = _capture_browser_artifacts(
                        page,
                        settings,
                        current_url,
                        enabled=not headless and bool(body_text),
                    )
                finally:
                    context.close()
    except Exception as exc:
        return UrlIngestReport(
            input_url=url,
            resolved_url=url,
            content_type="dynamic_page",
            evidence=(Evidence(
                kind="extractor_error",
                value=str(exc),
                source="playwright",
            ),),
            limitations=(Limitation(
                code="browser_extract_failed",
                message=str(exc),
                recoverable=True,
                next_action="open_browser_login",
            ),),
        )

    evidence = (
        Evidence(
            kind="extractor",
            value="playwright_headless" if headless else "playwright_visible",
            source=current_url,
        ),
        Evidence(
            kind="browser_profile",
            value=str(profile_dir),
            source=site_key_from_url(url),
            confidence="high",
        ),
        Evidence(
            kind="browser_mode",
            value="headless" if headless else "visible",
            source=current_url,
            confidence="high",
        ),
    )
    if _looks_like_login_state(current_url, title, body_text):
        return UrlIngestReport(
            input_url=url,
            resolved_url=current_url,
            content_type="dynamic_page",
            title=title,
            text=body_text[:2000],
            evidence=evidence,
            limitations=(Limitation(
                code="needs_browser_auth",
                message="Stored browser profile is missing or has expired login state.",
                recoverable=True,
                next_action="open_browser_login",
            ),),
        )
    if _looks_like_loading_state(current_url, title, body_text):
        return UrlIngestReport(
            input_url=url,
            resolved_url=current_url,
            content_type="browser_page",
            title=title,
            text=body_text[:2000],
            evidence=evidence,
            limitations=(Limitation(
                code="browser_content_loading",
                message=(
                    "The authorized browser profile opened the page, but the "
                    "headless browser only saw a loading shell."
                ),
                recoverable=True,
                next_action="retry_with_visible_browser",
            ),),
        )
    cleaned_text = _clean_browser_text(current_url, body_text)
    inferred_title = _infer_title_from_text(title, cleaned_text)
    return UrlIngestReport(
        input_url=url,
        resolved_url=current_url,
        content_type="video_page" if video_count > 0 else "browser_page",
        title=inferred_title,
        text=cleaned_text[:8000],
        artifacts=artifacts,
        evidence=evidence,
    )


def _capture_browser_artifacts(page, settings: Settings, url: str, *,
                               enabled: bool) -> tuple[UrlArtifact, ...]:
    if not enabled:
        return ()
    try:
        captures_dir = settings.artifacts_dir / "browser_captures"
        captures_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_artifact_stem(url)
        path = captures_dir / f"{stem}_{int(time.time())}.png"
        page.screenshot(path=str(path), full_page=False)
        return (UrlArtifact(
            kind="screenshot",
            path=str(path),
            mime_type="image/png",
            metadata={"source_url": url},
        ),)
    except Exception:
        return ()


def _safe_artifact_stem(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    raw = f"{parsed.netloc}_{parsed.path.strip('/')}".strip("_") or "capture"
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)
    return safe[:96] or "capture"


def _visible_browser_fallback_enabled(settings: Settings, url: str) -> bool:
    configured = settings.capability_settings.get("url_ingest", {})
    raw_enabled = configured.get("visible_browser_fallback")
    if raw_enabled is not None and not bool(raw_enabled):
        return False
    raw_sites = configured.get("visible_browser_fallback_sites")
    sites = (
        tuple(str(item) for item in raw_sites)
        if isinstance(raw_sites, (list, tuple, set))
        else DEFAULT_VISIBLE_FALLBACK_SITES
    )
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(_host_matches(host, site.lower()) for site in sites)


def _visible_browser_wait_s(settings: Settings) -> int:
    configured = settings.capability_settings.get("url_ingest", {})
    raw = configured.get("visible_browser_wait_s", 8)
    try:
        return max(1, min(int(raw), 30))
    except (TypeError, ValueError):
        return 8


def _report_has_limitation(report: UrlIngestReport, code: str) -> bool:
    return any(item.code == code for item in report.limitations)


def _clean_browser_text(url: str, body_text: str) -> str:
    if not _host_matches(urllib.parse.urlparse(url).netloc.lower(), "douyin.com"):
        return body_text
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    start_markers = (
        "章节要点",
        "作者声明",
    )
    start_index = None
    for index, line in enumerate(lines):
        if line.startswith(start_markers) or (line.startswith("第") and " | " in line):
            start_index = index
            break
    if start_index is not None:
        lines = lines[start_index:]
    return "\n".join(lines)


def _infer_title_from_text(title: str, text: str) -> str:
    if title:
        return title
    for line in text.splitlines():
        if " | " not in line:
            continue
        candidate = line.split(" | ", 1)[1].strip()
        if candidate:
            return candidate[:300]
    return ""


def _looks_like_login_state(url: str, title: str, body_text: str) -> bool:
    low = " ".join([url, title, body_text[:500]]).lower()
    return any(
        marker in low
        for marker in (
            "login",
            "sign in",
            "登录",
            "验证码",
            "captcha",
            "verify",
        )
    )


def _looks_like_loading_state(url: str, title: str, body_text: str) -> bool:
    low = " ".join([url, title, body_text[:1000]]).lower()
    return any(
        marker in low
        for marker in (
            "视频数据加载中",
            "data loading",
            "loading video",
        )
    )
