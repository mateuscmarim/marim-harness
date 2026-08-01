# Changelog

All notable changes to marim-harness are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/) —
pre-1.0, minor versions may contain breaking changes.

## [0.3.0] - 2026-07-31

- The approval panel now shows what you are approving. A long `write_file`
  preview rendered 18 rows in a non-scrolling box with no scrollbar and no
  marker, so a 101-line write looked like an 18-line one — you consented to a
  file you could not see. The detail pane scrolls, and a live "+N more lines"
  hint counts everything below the fold, including the rows the hosting panel
  itself clips; it recomputes on resize instead of measuring once at mount.
  The title, the preview, and the sudo passthrough modal's command line all
  run through a sanitizer that neutralizes terminal control sequences, so a
  tool argument cannot repaint or relocate the text a consent decision is
  read from.

- Four crash and hang paths around that panel are closed. A pending
  interaction panel that lost focus could never get it back — `a`/`d` typed
  into the prompt and Esc cancelled the turn — so teardown now prefers a
  still-pending sibling and the sub-agents view refuses to open over one. A
  queued message containing an unbalanced `[` raised `MarkupError` mid-render
  and took the app down with the in-flight turn; queue rows compose as
  `Content` (never parsed) instead of escaping to markup. A read-only data
  dir or a non-UTF-8 prompt-history file killed the app on write and on
  launch respectively; both are best-effort now, matching `prefs.py`.
  Finished sub-agent cards stop their spinner timers instead of waking the
  app 10×/s for the rest of the session, and a turn ending by cancel or
  provider error settles its in-flight rows and cards rather than leaving
  them pending forever.

- A run of consecutive tool calls folds into its group when the results
  interleave with the calls (call → result → call → result) — the common
  sequential case, where the group used to stay expanded forever with no
  duration in its header. `/exit` and `/quit` no longer discard queued
  messages in silence; they warn when there is something to lose. The
  slash-menu no longer covers a multi-line draft: its offset is recomputed
  from the prompt's real height on every filter and re-wrap.

- A durable usage ledger records per-turn token counts and cost deltas to a
  pair of JSONL files under the sessions base, queryable through
  `load_overview()` / `load_models()` (`marim_harness.stats`) for spend and
  model mix over time. Writes ride on the existing per-turn usage banking and
  are best-effort — a ledger failure never fails a turn. There is no backfill
  from sessions recorded before this landed. Opt out with `stats=False` on
  the builder or `MARIM_STATS=0`; see `docs/sdk/sessions-and-state.md`.

- An LSP server whose process died is evicted and cold-started instead of
  being trusted as alive. Eviction keyed on multilspy's `server_started`
  flag, which only flips when the `start_server` context exits — so a server
  killed by the OOM killer or a segfault left that context suspended and the
  flag reporting a corpse as alive. Nothing else caught it either: the RPC
  read loop exits quietly on EOF without failing pending requests, so every
  later request for that language stalled to the full 15s timeout, for the
  rest of the session, with no restart path. The subprocess's returncode is
  now a second, authoritative signal; an uninspectable server still reads as
  alive, so only a *known*-dead one is ever evicted.

- The model is told the current date. It rides in the per-turn user message
  rather than the system prompt, so the cached prefix stays byte-stable
  across turns and across day boundaries.

- `/sessions` now opens an interactive picker instead of printing a text
  list: type to filter by name, Tab into the list to navigate, Enter to
  switch. Press `d` twice on a highlighted (non-active) session to delete it
  — the same teardown `marim sessions delete` already performs. `/switch
  <number|name>` is unchanged.

- `marim serve qr` prints a QR code that pairs a client with the daemon in one
  scan, encoding `marim://pair?v=1&url=…&token=…&name=…` — the URL, the bearer
  token, and a profile name (the machine's hostname by default), which is
  everything `marim-mobile` needs for a server profile. The address it encodes
  comes from the source address of the default route, so it's the one a phone
  on the same network should use rather than a `docker0` or bridge address, and
  it always prints in plain text under the code; `--advertise` overrides it for
  a tailnet name or a reverse proxy. `marim serve --qr` prints one at startup.
  Because the code carries a credential it is never part of normal startup
  output and is refused outright when stdout isn't a terminal. Needs `segno`
  (added to the `serve` extra); without it the pairing URI prints as text. The
  On a terminal that advertises sixel the code is drawn as an image — square,
  crisp, and free of any dependence on the font — and otherwise with Unicode 13
  sextants, six modules to a character cell, which puts a typical pairing
  payload in 27 columns by 18 rows and the whole block inside a 25-line
  terminal. `--sixel`/`--no-sixel` overrides the detection either way, and
  `--wide` falls back to half-blocks (square modules, five rows taller, drawn
  from a block every terminal font has had for decades) for fonts that predate
  Unicode 13 — an escape hatch the code prints under itself.

- `marim serve` startup now leads with the MARIM wordmark (the same art as the
  TUI intro header, shared from `interfaces/branding.py`) over an aligned block
  of the facts you need to drive the daemon — listen URL, bearer-token path,
  workspaces root, and idle TTL. The last two come from flags that were
  previously invisible at startup, so you couldn't tell from the terminal which
  workspaces root a running daemon had adopted. Only the token's path is
  printed, never its value. The art is skipped automatically when stdout isn't
  a terminal (systemd, Docker, a pipe), where the same facts print one per line
  led by the long-standing `marim serve … listening on …` line; new
  `--no-banner` flag and `MARIM_NO_BANNER=1` force that plain form on a
  terminal, and `NO_COLOR`/`TERM=dumb` drop the accent color.

- Model-catalog fetch failures no longer shout. An unreachable model server
  (LM Studio not running, the box offline, a slow upstream) used to spill a
  full httpx traceback per probe, which in `marim serve` — a daemon that
  re-fetches catalogs on every session build and model listing — buried the
  log. Transport failures now log a single line, and only the first time per
  endpoint until it answers again (repeats drop to DEBUG); HTTP status errors
  log one line every time (a 401 is actionable, its stack frames aren't);
  genuinely unexpected errors keep their traceback. Applies to the OpenRouter,
  Google, Zen, local (LM Studio/Ollama), and LM Studio context-window fetches.
- Interactive per-project trust: instead of a silent, undiscoverable
  `MARIM_TRUST_PROJECT_HOOKS` env var, marim now remembers a per-project trust
  decision in a persistent store (`$XDG_STATE_HOME/marim-harness/`), honored
  only while the project's gated surface (hooks/MCP/plugin executables)
  hasn't changed since the decision was made. First-open TUI dialog lists
  what a grant would enable and hot-applies it live (hooks reload, MCP
  connects, LSP registry rebuilds) — no restart needed; a decline persists
  too, with a one-line notice instead of re-prompting. New `/trust [on|off]`
  command and a live settings row; new `marim trust [status|grant|revoke]`
  CLI subcommand (headless `-p` prints a one-line stderr notice when
  untrusted, never re-prompting); `marim serve` gets
  `GET/POST /v1/workspaces/{ws}/trust` plus `trust_prompt_pending` on session
  payloads. `MARIM_TRUST_PROJECT_HOOKS` still works as a standalone override
  in both directions (explicit falsy now force-untrusts even over a trusting
  store). See `docs/guides/trust.md`.
- The TUI renders LaTeX math in replies (`$..$`, `$$..$$`, `\(..\)`, `\[..\]`)
  as Unicode approximations (`α² + √(β₁)`, `(-b±√(b²-4ac))/(2a)`) on every
  prose surface, including sub-agent transcripts. Streaming-safe by design
  (the parser converts a span once its closer arrives), falls back to literal
  LaTeX on anything unparsable, `MARIM_TUI_MATH=0` disables. flatlatex joins
  the `[tui]` extra.
- The test suite runs in parallel by default (pytest-xdist, `-n auto` with
  work-stealing): a ~5.5-minute serial run drops under a minute on a
  multi-core machine. `uv run pytest -n 0` restores the serial run for
  debugging.
- Live session mode switch: new `POST /v1/workspaces/{ws}/sessions/{sid}/mode`
  route lets a client change an existing session's approval mode
  (ask/auto/plan) after creation — same live-vs-persist shape as the existing
  `/model` route, 409 while a turn is running. The TUI's own mode
  toggle/cycle is unaffected (still a live, per-launch setting, not
  persisted).

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
