# Review Follow-ups (round 2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the findings from the second full-codebase review (2026-06-23). The
overall codebase grades **8.5/10**; these items are the concrete, fixable gaps that
hold it back from 9+. Most are "reports success when it didn't" correctness bugs plus
a fragile token heuristic.

**Architecture:** Surgical changes — no refactors, no new dependencies. Each task
touches 1–2 production files + tests and runs TDD (red → green → commit). No changes to
session file format, public CLI surface, or `MARIM_*` env vars.

**Tech Stack:** Python 3.10+ floor (CI on 3.10/3.12/3.14), pydantic-ai 1.107,
pytest + anyio, ruff (E,F,I,UP,B,SIM), pyright standard.

## Global Constraints

- One commit per task. Message format: `fix(area): …` / `test(area): …` / `docs(area): …`.
- TDD: failing test first, then impl, then green, then commit.
- No new dependencies; no public CLI / env-var / session-format changes.
- Run `uv run ruff check src tests` && `uv run pyright` && `uv run pytest` before claiming a task done (match CI order).

---

## P1 — Correctness / "looks like it worked but didn't"

### Task 1: Render denied/failed tool status in the live stream
**Severity:** High (UX correctness) · **Files:** `interfaces/tui/stream_render.py:427`, `interfaces/tui/session_view.py:98`, `interfaces/tui/widgets/tools.py:66-68,202-205`, `interfaces/tui/app.py:587-596`
- A user-denied `write_file`/`edit_file` renders a green ✓. `ToolCallWidget` already supports `failed`/`denied` glyphs, but `widget.finish(content)` defaults `status="done"` and the only non-done path is the bash-nonzero-exit heuristic.
- [x] Thread the `DeferredToolApprovalResult` / `ToolDenied` outcome from `_request_approval` back into `finish(status=...)`.
- [x] Test: a denied tool renders the denied glyph (not ✓).

### Task 2: Propagate git restore failure into `RewindResult`
**Severity:** High · **Files:** `workspace/snapshot.py:123-133`, `session/checkpoints.py:177-192`
- `restore` swallows `CalledProcessError` at debug level and returns normally; `rewind` then reports `restored_files=True` unconditionally — a partial/failed rewind looks clean.
- [x] Have `restore` signal failure; reflect it in `RewindResult`.
- [x] Test: failed-restore path reports failure to the caller.

### Task 3: Make the `_pre_restore` safety snapshot reachable / per-session
**Severity:** High (data-recovery) · **Files:** `workspace/snapshot.py:110`
- Pre-restore snapshot writes a fixed ref `refs/marim/checkpoints/_pre_restore` (no session id, never surfaced) — the "undoable rewind" net can't be pulled, and a rewind in one session clobbers another's recovery point.
- [x] Either namespace by session id + wire an "undo rewind" UI path, **or** document it as forensic-only and stop implying user-recoverability.
- [x] Test accordingly.

## P2 — Robustness / contract

### Task 4: Context-overflow fallback for compaction
**Severity:** Medium · **Files:** `compaction.py:42-64`, `agent.py` (turn loop)
- `char/4` (images flat 1500) is the SOLE gate on context safety; can be off by a large factor.
- [x] On a provider context-length error, force a compaction and retry once.
- [x] (Optional) swap in a real tokenizer for the estimate.
- [x] Tests for the retry path and the estimate.

### Task 5: Document `command_policy` as defense-in-depth, not a sandbox
**Severity:** Medium (security expectation) · **Files:** `command_policy.py:42-68`, `tools/shell.py:37-43`
- Regex-on-shell-string, trivially evadable (`rm  -rf`, `$(echo rm)`, `eval`, base64-pipe); `bash` runs full shell interpretation.
- [x] Docstring/comment stating it is NOT a security boundary so no caller relies on it as a sandbox.

### Task 6: Persist history after an in-turn compaction fires
**Severity:** Medium · **Files:** `agent.py:682-689`, `session/ctrl.py:96`
- After the clean `persist()` + `resumable = list(history)`, `_maybe_compact()` may replace history without persisting; process death between turns loses the compaction and diverges `resumable`.
- [x] Persist again when a compaction actually fired; verify version-cache interaction.
- [x] Test.

### Task 7: Fix `HarnessConfig(**kwargs)` contract + docs
**Severity:** Medium (misleading API) · **Files:** `agent.py:288`, `CLAUDE.md`
- `config or HarnessConfig(**kwargs)` silently ignores kwargs when both are passed, but docstring + CLAUDE.md claim a "merge".
- [x] Implement the merge **or** reject the mixed call; fix docstring and CLAUDE.md to match.

## P3 — Test hygiene & cleanups

### Task 8: De-flake job/steering timing tests
**Severity:** Low (CI flakiness) · **Files:** `tests/test_jobs.py:154,182,195,211,324`, `tests/test_steering.py:297-319`
- Fixed `asyncio.sleep` to await background completion can assert too early under load. `tests/test_shell.py:90-94` has the correct poll-loop pattern.
- [x] Replace sleeps with poll/`asyncio.Event` handshakes.

### Task 9: Remove leaf-widget layering violation
**Severity:** Low · **Files:** `interfaces/tui/widgets/tools.py:151-161`
- A display widget imports private `tools.fs._safe` and reads `self.app.harness.deps.workspace_root` (with a `type: ignore[attr-defined]` smell).
- [x] Pass resolved old/new text in via the result event instead.

### Task 10: Minor cleanups (batch)
**Severity:** Low · **Files:** `agent.py:339-346`, `tools/web.py:30-31`, `workspace/memory.py:100-106`, `interfaces/tui/widgets/autocomplete.py:25-29,83-86`, `tools/offload.py:13-14`
- [x] Correct `bind_ui` docstring (TUI *does* read `deps.tasks.items`/`deps.jobs.list()` — app.py:245,261).
- [x] Comment that `web_search` egress is intentionally unpinned and returns attacker-controlled URLs (prompt-injection boundary into `fetch_url`).
- [x] Anchor `memory._upsert_index_line` match so suffix-sharing slugs (`auth.md`/`oauth.md`) don't collide.
- [x] Remove dead `CommandAutocomplete.Dismissed` + `dismiss()`.
- [x] Rename `offload.MAX_OUTPUT_BYTES` → `MAX_OUTPUT_CHARS` (it measures chars).

---

## Out of scope (noted, not scheduled)

- SHA-pinning plugins against compromised upstream branches/tags on `update` (normal git-supply-chain tradeoff; `discovery.py`/`install.py`).
- Git-sourced executable plugins get no trust *preview* before clone (safe default — lands untrusted — but a UX gap; `interfaces/cli/plugin.py:77-91`).
- Real-LSP integration tests (all LSP tests use fakes; real servers are slow/flaky).
- Restore leaving empty directories behind (`snapshot.py:123-125`) — cosmetic.
