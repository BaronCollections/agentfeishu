"""URL ingestion-specific models."""

from __future__ import annotations

from dataclasses import dataclass, field

from agentfeishu.core.models import Evidence, Limitation


@dataclass(frozen=True)
class UrlArtifact:
    kind: str
    path: str
    mime_type: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UrlIngestReport:
    input_url: str
    resolved_url: str
    content_type: str
    title: str = ""
    text: str = ""
    author: str = ""
    published_at: str = ""
    artifacts: tuple[UrlArtifact, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    limitations: tuple[Limitation, ...] = ()

    def to_dict(self) -> dict:
        return {
            "input_url": self.input_url,
            "resolved_url": self.resolved_url,
            "content_type": self.content_type,
            "title": self.title,
            "text": self.text,
            "author": self.author,
            "published_at": self.published_at,
            "artifacts": [artifact.__dict__ for artifact in self.artifacts],
            "evidence": [evidence.__dict__ for evidence in self.evidence],
            "limitations": [limitation.__dict__ for limitation in self.limitations],
        }
