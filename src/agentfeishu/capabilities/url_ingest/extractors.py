"""Extractor pipeline for URL ingestion."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from agentfeishu.config import Settings
from agentfeishu.core.models import Evidence, Limitation

from .browser_auth import export_profile_cookies, profile_dir_for_url, try_browser_extract
from .dependencies import has_python_package
from .models import UrlArtifact, UrlIngestReport

URL_RE = re.compile(r"https?://[^\s，。)）\]]+")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


class UrlIngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractionContext:
    settings: Settings
    timeout_s: int = 20


def extract_urls(text: str) -> list[str]:
    return [match.group(0).rstrip("，,。") for match in URL_RE.finditer(text)]


def resolve_url(url: str, timeout_s: int = 20) -> tuple[str, list[str], str, int]:
    """Resolve redirects with a browser-like user agent.

    Returns `(final_url, chain, body_preview, status_code)`. A 404 after redirects still
    returns the last URL in the chain and an empty preview so callers can report
    evidence without pretending extraction succeeded.
    """

    opener = urllib.request.build_opener(_TrackingRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    chain = [url]
    try:
        with opener.open(request, timeout=timeout_s) as response:
            final_url = response.geturl()
            chain.extend(getattr(opener, "redirect_chain", []))
            body = response.read(4096).decode("utf-8", "replace")
            return final_url, _dedupe_chain(chain), body, response.status
    except urllib.error.HTTPError as exc:
        chain.extend(getattr(opener, "redirect_chain", []))
        final_url = exc.geturl() or (chain[-1] if chain else url)
        body = ""
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            body = ""
        return final_url, _dedupe_chain(chain), body, exc.code
    except urllib.error.URLError as exc:
        raise UrlIngestError(str(exc)) from exc


class _TrackingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        chain = getattr(self.parent, "redirect_chain", [])  # type: ignore[attr-defined]
        chain.append(newurl)
        setattr(self.parent, "redirect_chain", chain)  # type: ignore[attr-defined]
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _dedupe_chain(chain: list[str]) -> list[str]:
    result: list[str] = []
    for item in chain:
        if item and (not result or result[-1] != item):
            result.append(item)
    return result


def ingest_url(url: str, context: ExtractionContext) -> UrlIngestReport:
    try:
        final_url, chain, preview, status_code = _resolve_url_compat(url, context.timeout_s)
    except UrlIngestError as exc:
        return UrlIngestReport(
            input_url=url,
            resolved_url=url,
            content_type="network_error",
            evidence=(Evidence(kind="resolution_error", value=str(exc), source=url),),
            limitations=(Limitation(
                code="url_resolution_failed",
                message=str(exc),
                recoverable=True,
                next_action="check_url_or_retry",
            ),),
        )
    evidence = [Evidence(kind="redirect_chain", value=" -> ".join(chain), source=url)]

    if status_code in {401, 403, 407}:
        return _browser_auth_report(
            url,
            final_url,
            evidence,
            f"HTTP {status_code} indicates the page requires authorization.",
            settings=context.settings,
        )
    if status_code == 429:
        return UrlIngestReport(
            input_url=url,
            resolved_url=final_url,
            content_type="access_limited",
            evidence=tuple(evidence),
            limitations=(Limitation(
                code="rate_limited_or_anti_automation",
                message="HTTP 429 indicates rate limiting or anti-automation access control.",
                recoverable=True,
                next_action="retry_later_or_open_browser_login",
            ),),
        )

    ytdlp_failure_report: UrlIngestReport | None = None
    ytdlp_report = _try_ytdlp(url, final_url, context)
    if ytdlp_report and any(item.code == "needs_browser_auth" for item in ytdlp_report.limitations):
        return _merge_report(ytdlp_report, evidence)
    if ytdlp_report and not ytdlp_report.limitations:
        return _merge_report(ytdlp_report, evidence)
    if ytdlp_report:
        ytdlp_failure_report = ytdlp_report

    gallery_report = _try_gallery_dl(url, final_url, context)
    if gallery_report:
        return _merge_report(gallery_report, evidence)

    if _looks_like_image_url(final_url):
        return UrlIngestReport(
            input_url=url,
            resolved_url=final_url,
            content_type="image",
            artifacts=(UrlArtifact(kind="image", path=final_url),),
            evidence=tuple(evidence) + (
                Evidence(kind="url_classification", value="image", source=final_url),
            ),
            limitations=(Limitation(
                code="ocr_not_run",
                message="Image OCR requires the optional vision adapter.",
                recoverable=True,
                next_action="install_url_ingest_vision",
            ),),
        )

    if _looks_like_video_url(final_url):
        ytdlp_limitations = ytdlp_failure_report.limitations if ytdlp_failure_report else (
            Limitation(
                code="yt_dlp_unavailable",
                message="Video metadata extraction requires the optional yt-dlp adapter.",
                recoverable=True,
                next_action="install_url_ingest",
            ),
        )
        ytdlp_evidence = ytdlp_failure_report.evidence if ytdlp_failure_report else ()
        return UrlIngestReport(
            input_url=url,
            resolved_url=final_url,
            content_type="video",
            evidence=tuple(evidence) + (
                Evidence(kind="url_classification", value="video", source=final_url),
            ) + ytdlp_evidence,
            limitations=ytdlp_limitations,
        )

    article_report = _try_trafilatura(url, final_url, preview, context)
    if article_report:
        return _merge_report(article_report, evidence)

    browser_report = try_browser_extract(final_url or url, context.settings, context.timeout_s)
    if browser_report:
        return _merge_report(browser_report, evidence)

    if _looks_like_dynamic_or_restricted(final_url, preview):
        return _browser_auth_report(
            url,
            final_url,
            evidence,
            "Page requires browser execution or user-authorized login state.",
            settings=context.settings,
            text=_safe_preview_text(preview),
        )

    return UrlIngestReport(
        input_url=url,
        resolved_url=final_url,
        content_type="unknown",
        text=_safe_preview_text(preview),
        evidence=tuple(evidence),
        limitations=(ytdlp_failure_report.limitations if ytdlp_failure_report else ()) + (Limitation(
            code="unsupported_or_empty",
            message="No supported extractor returned content for this URL.",
            recoverable=False,
        ),),
    )


def _resolve_url_compat(url: str, timeout_s: int) -> tuple[str, list[str], str, int]:
    resolved = resolve_url(url, timeout_s)
    if len(resolved) == 3:
        final_url, chain, preview = resolved
        return final_url, chain, preview, 200
    final_url, chain, preview, status_code = resolved
    return final_url, chain, preview, status_code


def _browser_auth_report(input_url: str,
                         final_url: str,
                         evidence: list[Evidence],
                         message: str,
                         *,
                         settings: Settings,
                         text: str = "") -> UrlIngestReport:
    profile_dir = profile_dir_for_url(settings, final_url or input_url)
    site = urllib.parse.urlparse(final_url or input_url).netloc or "default"
    return UrlIngestReport(
        input_url=input_url,
        resolved_url=final_url,
        content_type="dynamic_page",
        text=text,
        evidence=tuple(evidence) + (
            Evidence(
                kind="browser_profile",
                value=str(profile_dir),
                source=site,
                confidence="high",
            ),
        ),
        limitations=(Limitation(
            code="needs_browser_auth",
            message=message,
            recoverable=True,
            next_action="open_browser_login",
        ),),
    )


def _try_ytdlp(input_url: str, final_url: str,
               context: ExtractionContext) -> UrlIngestReport | None:
    if not has_python_package("yt_dlp"):
        return None
    try:
        import yt_dlp  # type: ignore
    except Exception:
        return None
    output_template = str(context.settings.downloads_dir / "%(id)s.%(ext)s")
    options = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "outtmpl": output_template,
    }
    cookiefile = export_profile_cookies(context.settings, final_url or input_url)
    if cookiefile:
        options["cookiefile"] = str(cookiefile)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(final_url or input_url, download=False)
    except Exception as exc:
        message = str(exc)
        if _looks_like_auth_error(message):
            site = urllib.parse.urlparse(final_url or input_url).netloc or "default"
            if cookiefile:
                return UrlIngestReport(
                    input_url=input_url,
                    resolved_url=final_url,
                    content_type="video",
                    evidence=(
                        Evidence(kind="extractor_error", value=message, source="yt-dlp"),
                        Evidence(
                            kind="browser_cookies",
                            value=str(cookiefile),
                            source=site,
                            confidence="high",
                        ),
                    ),
                    limitations=(Limitation(
                        code="extractor_auth_rejected",
                        message=(
                            "A user-authorized browser cookie file was supplied, "
                            "but yt-dlp still could not read the video metadata."
                        ),
                        recoverable=True,
                        next_action="try_browser_visible_capture_or_retry_later",
                    ),),
                )
            return UrlIngestReport(
                input_url=input_url,
                resolved_url=final_url,
                content_type="dynamic_page",
                evidence=(
                    Evidence(kind="extractor_error", value=message, source="yt-dlp"),
                    Evidence(
                        kind="browser_profile",
                        value=str(context.settings.site_profile_dir(site)),
                        source=site,
                        confidence="high",
                    ),
                ),
                limitations=(Limitation(
                    code="needs_browser_auth",
                    message="Video page requires a user-authorized browser session.",
                    recoverable=True,
                    next_action="open_browser_login",
                ),),
            )
        return UrlIngestReport(
            input_url=input_url,
            resolved_url=final_url,
            content_type="video",
            evidence=(Evidence(kind="extractor_error", value=message, source="yt-dlp"),),
            limitations=(Limitation(
                code="yt_dlp_failed",
                message=message,
                recoverable=True,
                next_action="try_browser_or_cookies",
            ),),
        )
    if not isinstance(info, dict):
        return None
    title = str(info.get("title") or "")
    description = str(info.get("description") or "")
    webpage_url = str(info.get("webpage_url") or final_url)
    evidence = (
        Evidence(kind="extractor", value="yt-dlp", source=webpage_url, confidence="high"),
        Evidence(kind="metadata", value=json.dumps({
            "id": info.get("id"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "upload_date": info.get("upload_date"),
        }, ensure_ascii=False), source="yt-dlp"),
    )
    return UrlIngestReport(
        input_url=input_url,
        resolved_url=webpage_url,
        content_type="video",
        title=title,
        text=description,
        author=str(info.get("uploader") or ""),
        published_at=str(info.get("upload_date") or ""),
        evidence=evidence,
    )


def _try_gallery_dl(input_url: str, final_url: str,
                    context: ExtractionContext) -> UrlIngestReport | None:
    if not has_python_package("gallery_dl"):
        return None
    try:
        from gallery_dl import job  # type: ignore
    except Exception:
        return None
    try:
        extractor = job.DataJob(final_url or input_url)
        rows = list(extractor.run())
    except Exception:
        return None
    if not rows:
        return None
    return UrlIngestReport(
        input_url=input_url,
        resolved_url=final_url,
        content_type="gallery",
        text=f"gallery-dl extracted {len(rows)} metadata row(s).",
        evidence=(Evidence(kind="extractor", value="gallery-dl", source=final_url),),
    )


def _try_trafilatura(input_url: str, final_url: str, preview: str,
                     context: ExtractionContext) -> UrlIngestReport | None:
    if not has_python_package("trafilatura"):
        return None
    try:
        import trafilatura  # type: ignore
    except Exception:
        return None
    try:
        downloaded = trafilatura.fetch_url(final_url or input_url)
        extracted = trafilatura.extract(downloaded or preview, include_comments=False)
    except Exception:
        return None
    if not extracted:
        return None
    return UrlIngestReport(
        input_url=input_url,
        resolved_url=final_url,
        content_type="article",
        text=extracted,
        evidence=(Evidence(kind="extractor", value="trafilatura", source=final_url),),
    )


def _merge_report(report: UrlIngestReport,
                  evidence: list[Evidence]) -> UrlIngestReport:
    return UrlIngestReport(
        input_url=report.input_url,
        resolved_url=report.resolved_url,
        content_type=report.content_type,
        title=report.title,
        text=report.text,
        author=report.author,
        published_at=report.published_at,
        artifacts=report.artifacts,
        evidence=tuple(evidence) + report.evidence,
        limitations=report.limitations,
    )


def _looks_like_dynamic_or_restricted(final_url: str, preview: str) -> bool:
    parsed = urllib.parse.urlparse(final_url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    query = parsed.query.lower()
    if "login" in path or "signin" in path or "login" in query:
        return True
    if any(domain in host for domain in ("douyin.com", "iesdouyin.com", "tiktok.com")):
        return True
    low = preview.lower()
    if "type='password'" in low or 'type="password"' in low:
        return True
    return "<script" in low and len(_safe_preview_text(preview)) < 200


def _looks_like_auth_error(message: str) -> bool:
    low = message.lower()
    return any(
        marker in low
        for marker in (
            "login",
            "sign in",
            "cookies",
            "cookie",
            "forbidden",
            "unauthorized",
            "captcha",
            "verification",
        )
    )


def _looks_like_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"))


def _looks_like_video_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith((".mp4", ".mov", ".m4v", ".webm", ".mkv"))


def _safe_preview_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:2000]
