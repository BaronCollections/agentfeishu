# AgentFeishu Capability Platform Design

## Business Goal

AgentFeishu is an open-source business framework where Feishu is the operator surface and Codex-powered agents execute extensible capabilities. URL parsing is the first serious capability, not the product boundary. The framework must support future capabilities with consistent configuration, status, permission, execution, task history, and admin visibility.

## Product Scope

The first version must provide:

- A capability registry for business abilities such as URL ingestion, research, code work, document extraction, and future custom tools.
- A task runtime that accepts normalized requests, validates capability availability, tracks state transitions, records errors, and returns structured results.
- A Feishu gateway model that can normalize incoming messages into runtime requests.
- A URL ingestion capability that resolves URLs, attempts public extraction with project-local optional dependencies, uses browser authorization when required, and returns evidence and limitations rather than hallucinating inaccessible content.
- An admin UI showing capability inventory, dependency/config/login status, recent tasks, and operational errors.

## Architecture

The framework is split into small modules with stable contracts:

- `config`: Load and validate project settings, state directories, and capability options.
- `core`: Shared domain models, capability registry, runtime orchestration, and task store.
- `gateway`: Feishu event normalization and routing hints.
- `capabilities`: Pluggable business capabilities. Each capability owns its schema, dependency checks, execution, and user-facing metadata.
- `server/ui`: Lightweight admin API and static dashboard for operational visibility.

Capabilities must be isolated. Adding a future capability should not require changing URL parsing internals or Feishu event parsing.

## URL Ingestion Rules

The URL capability performs a pipeline:

1. Extract URLs from free text.
2. Resolve short links without bypassing access controls.
3. Try specialized extractors: `yt-dlp` for video, `gallery-dl` for image/gallery, `trafilatura` for article text.
4. Fall back to Playwright browser capture with a persistent per-site profile under `state/browser_profiles/<site>`.
5. If login is required, return `needs_browser_auth` with a safe browser action instead of bypassing login.
6. If media is obtained, extract metadata and prepare hooks for audio transcription and frame OCR.
7. If media is not obtained, return visible text, screenshots when available, evidence, and limitations.

The capability must never claim it watched or heard a video unless the extraction evidence shows media, transcript, frames, or page content was actually obtained.

## Admin UI

The admin UI is an operations console, not a landing page. It should show:

- Capability cards with status, dependency health, required config, and login state.
- Recent tasks with status, capability, timestamps, errors, and result summary.
- Configuration paths and state directory.
- Actionable warnings for missing optional dependencies or login profiles.

The first implementation uses a simple server-rendered HTML dashboard and JSON endpoints to avoid a heavy frontend build system.

## Safety And Compliance

AgentFeishu must not include code intended to bypass login, defeat paywalls, or evade access controls. It may use user-authorized browser profiles and public extractors. All inaccessible or private content paths must produce explicit limitations.
