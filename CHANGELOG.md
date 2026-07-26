# Changelog

All notable changes to marim-harness are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/) —
pre-1.0, minor versions may contain breaking changes.

## [0.2.0] - 2026-07-26

- `marim serve`: sessions can switch models — `GET /v1/models` lists the
  catalog, session create accepts a `model`, and
  `POST /v1/sessions/{sid}/model` switches an existing session (409 while a
  turn is running). Session payloads report the *effective* model, never null.
- `marim serve`: background jobs are visible over HTTP —
  `GET /v1/sessions/{sid}/jobs` returns the job snapshot and
  `GET /v1/sessions/{sid}/jobs/{job_id}` the detail (prompt + result), with
  `started_at` stamped on each job.
- Autonomous wake now works in serve mode: the wake policy moved into a shared
  `WakeDriver` orchestrator used by both the TUI and the HTTP daemon, so a
  scheduled wake fires exactly once per trigger in either front-end.
- `marim serve`: safe GET endpoints send explicit `Cache-Control` headers
  (session reads are `no-cache`, so clients never act on a stale snapshot).
- TUI tool summaries show workspace-relative paths instead of absolute ones —
  `src/app.py` rather than the full `/home/...` prefix.
- Error handling hardened across the codebase: streaming errors are classified
  transient and retried like other infra failures, and a sweep across 32
  modules replaced broad exception swallowing with precise handling.
- New bundled `marim-docs` skill: navigating marim-harness's own documentation
  (guides, reference, architecture) from inside a session.
- Large tool-output spills (sub-agent reports, workflow results, fetched
  bodies) now land in the session scratchpad instead of `.marim/output/`
  inside the workspace — intermediate files no longer clutter the project;
  the legacy directory is still read as a fallback.
- Resumed sessions now revalidate offloaded-output references at load: a
  handle whose scratchpad file was cleaned up (reboot, tmpfiles aging) gets
  an explicit "file no longer exists — re-run the tool" note appended, with
  the inline preview kept — instead of promising a `read_file` that would
  fail. Sub-agent and workflow spill notes now always carry absolute paths.
- New `zen-go` provider: OpenCode Go, Zen's flat-rate subscription plan, via
  its OpenAI-compatible endpoint — `MARIM_PROVIDER=zen-go` with the same
  `OPENCODE_API_KEY` as `zen`, default model `glm-5.2` (open coding models
  only). Catalog, settings card, and qualified `zen-go:<model>` ids included.
- Image reads hardened (follow-ups to the `read_file` image support below):
  files are now recognized by header magic, not extension alone — a text file
  named `diagram.png` reads as text and a corrupt/0-byte image gets a notice
  instead of failing the whole turn on a provider 400; a vision-gate-blocked
  read no longer counts as "file observed" for the read-before-edit guard;
  and sub-agent transcript sidecars externalize image bytes to the image
  cache instead of re-serializing inline base64 before every model request.
- `read_file` now returns image files (png/jpg/webp/gif, up to 5 MB) as
  model-visible images on vision-capable models — screenshots and diagrams can
  be inspected directly, including by sub-agents (gated per spawn's own model).
  Catalog-gated: a model the catalog marks text-only gets a text notice
  instead; unknown capability sends the image optimistically. Image tool
  results are cached content-addressed on disk (not inlined into session
  files) and masked like any other stale observation.
- New `zen` provider: OpenCode Zen (opencode's model gateway) via its
  OpenAI-compatible endpoint — `MARIM_PROVIDER=zen` + `OPENCODE_API_KEY`,
  default model `mimo-v2.5-free` (free tier). Catalog, settings card, and
  qualified `zen:<model>` ids included.
- `HarnessBuilder.with_capability(...)`: attach pydantic-ai
  `AbstractCapability` instances (e.g. Pydantic AI Harness modules) to the
  embedded agent, after marim's built-in capabilities.
- `marim_harness.capabilities.Advisor` — marim's advisor exported as a
  standard pydantic-ai capability, attachable to any pydantic-ai agent (or
  via `HarnessBuilder.with_capability`). Marim's own advisor now shares the
  same consult core, so the two cannot drift.

## [0.1.0.post1] - 2026-07-23

- Packaging only: the PyPI project page now renders the README's relative
  links and the demo GIF as absolute forge URLs (rewritten at build time via
  `hatch-fancy-pypi-readme`). No code changes.

## [0.1.0] - 2026-07-23

The first tagged release. Highlights of what exists today:

- **Interactive TUI** (Textual): streaming responses, tool-call cards, live
  token counter, inline approval / ask-user / plan panels, sub-agents screen,
  settings, model picker, themes.
- **Headless mode**: one-shot prompts with `text`, `json`, or `stream-json`
  output; `marim serve` HTTP daemon (REST + WebSocket) in the `[serve]` extra.
- **Providers**: OpenRouter (default), any local OpenAI-compatible server
  (Ollama, LM Studio), Google Gemini, and a `claude-cli` provider that
  delegates turns to Claude Code on a Claude subscription.
- **Permission modes** (`auto` / `ask` / `plan`), a configurable shell command
  policy, and workspace-confined file tools.
- **Sessions**: per-workspace persistence, resume, compaction with observation
  masking, checkpoints and `/rewind` backed by git snapshots.
- **Sub-agents**: parallel spawns with granted tool reach, model tiers
  (`cheap`/`med`/`high`), nesting with a depth ceiling, detached fan-out with
  live cards, and an optional `claude -p` backend.
- **Dynamic workflows**: a gated `run_workflow` tool executing model-authored
  scripts in a pydantic-monty sandbox (`[workflows]` extra).
- **Integrations**: MCP servers (global / project / plugin scope), LSP
  navigation + diagnostics via pluggable providers (Python, TypeScript, C++,
  Java bundled), Gitea/GitHub forge tools via the `tea` CLI, lifecycle hooks
  (Claude-Code-compatible), plugins, skills, and persistent memory.
- **Embeddable**: `HarnessBuilder` composes the same agent loop as a library
  with explicit config and no env reads (see `docs/embedding.md` and
  `docs/sdk/`).
- **Extras**: advisor model, thinking levels, desktop notifications, image
  input, background jobs with autonomous wake, session scratchpad.
