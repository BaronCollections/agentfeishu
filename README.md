# AgentFeishu

AgentFeishu is an open-source framework for controlling Codex-powered agents from Feishu. It is built as a capability platform: URL ingestion is the first built-in capability, and future business abilities can be added without changing the Feishu gateway or runtime contracts.

## What It Provides

- Feishu message normalization and routing contracts.
- A capability registry for business abilities.
- A runtime task state machine with durable local task logs.
- A URL ingestion capability for video, image, gallery, and article links.
- A lightweight admin console showing supported capabilities, dependencies, configuration, auth state, recent tasks, and errors.

## Architecture

```text
Feishu Gateway -> Runtime -> Capability Registry -> Capability Executor
                                |
                                +-> Admin UI/API
```

Main package areas:

- `agentfeishu.config`: project settings, state directories, masked configuration.
- `agentfeishu.core`: task models, runtime, registry, local task store.
- `agentfeishu.gateway`: Feishu event normalization.
- `agentfeishu.capabilities`: built-in and future capabilities.
- `agentfeishu.server`: admin API and dashboard.

## Installation

Core install:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

URL ingestion extras:

```bash
pip install -e ".[url-ingest]"
python -m playwright install chromium
```

Vision/transcription extras:

```bash
pip install -e ".[url-ingest-vision]"
```

Optional media processing may also need `ffmpeg` on PATH. AgentFeishu detects missing optional dependencies and exposes actionable warnings in the admin UI.

## URL Ingestion

Run:

```bash
agentfeishu url parse "https://v.douyin.com/Q92Ts3gmfb8/" --json
```

Pipeline:

1. Resolve short links and record redirect evidence.
2. Try `yt-dlp` for video metadata/subtitles/media.
3. Try `gallery-dl` for image and gallery URLs.
4. Try `trafilatura` for article text.
5. Reuse a project-local Playwright persistent profile when one exists.
6. Return a browser-auth required result for JS/login/anti-automation pages.

AgentFeishu does not bypass login, defeat paywalls, or evade access controls. If a page requires login, the capability returns `needs_browser_auth` and points to the project-local browser profile directory under `state/browser_profiles/<site>`.

To authorize a site with an installed `url-ingest` extra:

```bash
agentfeishu auth login "https://www.douyin.com/"
```

This opens a headed browser using a project-local persistent profile. After you login and close the browser window, later URL parsing can reuse that profile. Profiles live under `state/browser_profiles/` and are ignored by git.

HTTP `401`, `403`, and `407` are reported as authorization requirements. HTTP `429` is reported as a rate-limit or anti-automation limitation instead of guessed content.

## Feishu Callback

Start the server:

```bash
agentfeishu serve --host 127.0.0.1 --port 8765
```

Configure Feishu event callbacks to:

```text
POST /feishu/events
```

URL verification (`challenge`) and message events are supported. When `FEISHU_VERIFICATION_TOKEN` or `[feishu].verification_token` is configured, callback tokens are checked before challenge or task execution.

Example config:

```toml
[feishu]
verification_token = "your-callback-token"

[capabilities]
enabled = ["url_ingest"]
```

Text messages containing URLs are normalized, routed to `url_ingest`, submitted to the runtime, and persisted in the local task log.

## Admin UI

```bash
agentfeishu serve --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/admin
```

The UI is an operations console, not a landing page. It shows capability status, dependencies, configuration paths, auth state, recent tasks, and recent errors.

Automation-friendly API endpoints are also exposed:

- `GET /api/health`
- `GET /api/capabilities`
- `GET /api/tasks?limit=20`
- `GET /api/config`
- `POST /api/auth/browser/open`

Admin endpoints are intended for local operation. Requests from non-loopback clients are rejected unless `AGENTFEISHU_ALLOW_REMOTE_ADMIN=true` is explicitly set. Feishu callbacks are separate and protected by the Feishu verification token when configured.

## Safety Boundary

AgentFeishu may use public extractors and user-authorized browser sessions. It must not include code intended to bypass login, crack anti-bot protections, or access private content without authorization. Inaccessible content is reported as a limitation instead of being guessed.
