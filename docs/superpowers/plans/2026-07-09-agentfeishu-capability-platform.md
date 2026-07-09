# AgentFeishu Capability Platform Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Feishu-controlled Codex agent framework with a capability registry, task runtime, URL ingestion capability, and admin UI.

**Architecture:** Python package with isolated `config`, `core`, `gateway`, `capabilities`, and `ui/server` modules. Capability implementations register descriptors and execute through a shared runtime that records task state and structured results.

**Tech Stack:** Python 3.10+, stdlib HTTP server for the first admin UI, optional `url-ingest` extras for yt-dlp/gallery-dl/trafilatura/playwright, pytest for tests.

---

## Chunk 1: Core Platform

### Task 1: Package And Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/agentfeishu/config/settings.py`
- Test: `tests/test_config.py`

- [ ] Define project metadata, extras, CLI entrypoint, and ignored runtime state.
- [ ] Implement `Settings` with state directory, browser profile directory, download directory, and capability settings.
- [ ] Validate directory creation and environment overrides.

### Task 2: Capability Registry And Runtime

**Files:**
- Create: `src/agentfeishu/core/models.py`
- Create: `src/agentfeishu/core/registry.py`
- Create: `src/agentfeishu/core/runtime.py`
- Create: `src/agentfeishu/core/task_store.py`
- Test: `tests/test_registry_runtime.py`

- [ ] Define capability descriptors, dependency checks, task statuses, task results, evidence, and limitations.
- [ ] Implement registry registration, duplicate detection, lookup, and health reporting.
- [ ] Implement runtime validation, task creation, execution, failure capture, and task persistence.

## Chunk 2: Gateway And URL Capability

### Task 3: Feishu Gateway Model

**Files:**
- Create: `src/agentfeishu/gateway/feishu.py`
- Test: `tests/test_gateway.py`

- [ ] Normalize Feishu text/image/video events into runtime requests.
- [ ] Route URL-containing messages to `url_ingest`.
- [ ] Preserve raw text and sender metadata for audit.

### Task 4: URL Ingestion Capability

**Files:**
- Create: `src/agentfeishu/capabilities/url_ingest/*`
- Test: `tests/capabilities/test_url_ingest.py`

- [ ] Extract and resolve URLs.
- [ ] Detect optional dependencies without importing them at module import time.
- [ ] Try yt-dlp/gallery-dl/trafilatura/browser extractors in order.
- [ ] Return `needs_browser_auth` rather than bypassing login.
- [ ] Include evidence, limitations, and dependency health in every result.

## Chunk 3: Operator Surfaces

### Task 5: CLI

**Files:**
- Create: `src/agentfeishu/cli.py`
- Test: `tests/test_cli.py`

- [ ] Implement `agentfeishu capabilities`, `agentfeishu url parse`, and `agentfeishu serve`.
- [ ] Output JSON for automation and readable summaries for humans.

### Task 6: Admin API And UI

**Files:**
- Create: `src/agentfeishu/server.py`
- Create: `src/agentfeishu/ui/dashboard.py`
- Test: `tests/test_admin_api.py`

- [ ] Serve `/`, `/api/health`, `/api/capabilities`, `/api/tasks`, and `/api/config`.
- [ ] Render a dense operations dashboard with capability status, dependencies, config paths, and recent tasks.
- [ ] Avoid frontend build tooling in the first version.

## Chunk 4: Documentation And Verification

### Task 7: Documentation

**Files:**
- Modify: `README.md`

- [ ] Explain business goal, architecture, installation, optional URL ingest dependencies, browser login profile behavior, and safety boundaries.

### Task 8: Verification

**Files:**
- All tests

- [ ] Run unit tests.
- [ ] Run CLI smoke commands.
- [ ] Inspect dashboard endpoint.
- [ ] Commit and push branch.
