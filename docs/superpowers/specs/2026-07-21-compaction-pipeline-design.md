# Compaction pipeline upgrade — design

**Date:** 2026-07-21
**Status:** approved (brainstorm), pending implementation plan

## Goal

Close the gaps between marim's compaction and Claude Code's, keeping what marim
already does better. Scope, as agreed:

1. Structured summarizer prompt (schema, security-constraints-verbatim, next-step quote)
2. Manual `/compact [instructions]`
3. Hook parity: PreCompact `manual`/`auto` trigger + blocking (manual only), new PostCompact
4. Rapid-refill thrash breaker
5. Standalone microcompaction (mask stale tool observations *before* summarizing,
   skip the summarizer when masking alone suffices)
6. Persisted payloads: masked tool outputs are recoverable from the session scratchpad

**Out of scope** (deliberately deferred): speculative/precomputed background
compaction; a consecutive-failure breaker (the summarizer already falls back to
truncation, so failures cannot loop).

## What marim keeps (already better than Claude Code)

- `ContextLimits`' window-vs-budget split (`min(budget, 0.8 × window)`).
- The verbatim tail: `head + summary + last-N-messages` cut only at a user-turn
  boundary, so tool-call/return pairing is structurally unbreakable.
- Forced post-overflow recovery with masking as the last-resort lever.
- Truncation fallback when the summarizer fails.

## Architecture: staged pipeline in `SessionController.maybe_compact`

Chosen over (B) extracting a `ContextReducer` collaborator — deferred until a
third stage exists — and (C) Claude-Code-style always-on microcompaction, which
reintroduces the per-turn cache-bust the current code deliberately avoids.

```
warm discovery → threshold → size = max(chars/4 estimate, measured last-request tokens)
│
├─ breaker open? → skip (auto only; one-time notice)                     [NEW]
├─ gate: size > threshold, or force=True, or trigger="manual"
│
├─ PreCompact hook   trigger="auto"|"manual", custom_instructions
│     manual + block verdict → abort with notice                        [NEW]
│     auto   + block verdict → log at INFO, proceed anyway
│
├─ STAGE 1 · microcompact                                               [NEW ORDER]
│     mask ToolReturnParts older than mask_keep_recent (4), ≥ mask_min_chars (200)
│     payload → scratchpad file first; placeholder carries the path
│     re-estimate: now ≤ threshold? → done, summarizer never called
│
├─ STAGE 2 · summarize-compact (only if still over threshold)
│     _plan_tail_start → head + summary + verbatim tail   (unchanged)
│     summarizer receives custom instructions when manual
│
├─ force-fallback: the existing ctrl.py post-overflow mask-recovery branch
│     collapses into stage 1 (stage 1 runs unconditionally on force)
│
└─ persist → PostCompact hook (trigger, pre/post tokens, stage) → on_compact
```

**Ordering change.** Today masking runs *after* a compaction, on the surviving
tail. The new pipeline masks *first* (Claude Code's "clear tool outputs first,
then summarize if needed"). Cache-neutrality is preserved: stage 1 only runs
when the gate has already tripped, i.e. when a history rewrite (and its cache
miss) was about to happen anyway.

**Existing knobs keep their meaning:** `mask_keep_recent`, `mask_min_chars`,
`keep_last_messages`; `mask_observations` now gates stage 1 instead of the
post-step. Force-recovery still ignores `mask_observations` (recovery of last
resort, as today).

## Components

### 1. `CompactionBreaker` (pure, `compaction.py`)

Dataclass tracking `(turns_since_last_compact, consecutive_rapid_refills)`.
Claude Code's rule: if the context refills past the threshold within **3 turns**
of the previous compaction, **3 consecutive times**, the breaker opens.

- While open: auto-compaction is skipped; the user gets a one-time notice —
  "compaction is thrashing; a file read or tool output is likely too large for
  the context window — read in smaller chunks or /clear".
- Reset by: manual `/compact`, `/clear`, session switch, or any compaction whose
  result survives more than 3 turns.
- In-memory only; not persisted with the session. A resumed session gets a
  fresh breaker (correct: resume re-measures).
- Owned by `SessionController`; consulted at the gate.

### 2. Mask-with-persist (`compaction.py` + `workspace/` scratchpad)

`mask_stale_observations` grows an optional `persist: Callable[[str, str], str | None]`
(content, hint → path or None). For each masked return:

- persist succeeds → placeholder:
  `[output elided to save context; full content at <path> — read_file it if still needed]`
- persist unavailable (`MARIM_SCRATCHPAD` off) or fails → today's
  `MASKED_OBSERVATION` text. Persist failures are best-effort and never block
  compaction.

New scratchpad helper `persist_elided(content, hint) → path | None` writes
`elided/NNN-<toolname>.txt` under the session scratchpad — already a file-tool
guard root, advertised in the system prompt, auto-approved in ask mode: zero
new plumbing for recovery. Lifetime is /tmp; acceptable for content whose
contract is already "re-run the tool if you need it".

Idempotency: already-masked returns (either placeholder form) are skipped, as
today.

### 3. Structured summarizer (`compaction.py`)

`Summarizer` protocol becomes `summarize(messages, instructions: str | None = None)`.
The prompt upgrades from freeform "dense notes" to a fixed schema adapted from
Claude Code:

1. Primary request and intent
2. Key technical concepts
3. Files and code sections (with important snippets)
4. Errors and fixes (incl. user feedback)
5. All user messages (non-tool-result)
6. Pending tasks
7. Current work
8. Next step — **with a verbatim quote** from the recent conversation to prevent
   task drift
9. Security-relevant user constraints — **preserved verbatim**

Style stays "terse notes, not prose". Manual instructions are appended as a
`## Compact instructions` block the summarizer is told to honor.

### 4. Hook engine (`hooks/`)

Three surgical pieces:

- `events.py`: add `POST_COMPACT = "PostCompact"`. Observe-only. Payload:
  `trigger`, `pre_compact_tokens`, `post_compact_tokens`, `stage`
  (`"micro"` | `"summary"` | `"micro+summary"`).
- **Matcher**: for compact events the trigger string rides the existing
  `tool_name` matcher slot, so `"matcher": "manual"` / `"auto"` works exactly
  like Claude Code with zero new matcher code.
- **Blocking**: new `dispatch_verdict(event, payload) → HookVerdict(blocked, reason)`
  alongside the existing `dispatch`. Only `PRE_COMPACT` calls it, so every
  other event's observe-only contract is untouched. A hook blocks via either
  Claude Code contract: exit code 2 (stderr = reason) or exit 0 +
  `{"decision": "block", "reason": ...}`. The verdict is honored only when
  `trigger="manual"`; on auto it is logged and ignored (a hook must never be
  able to wedge a session into the hard context limit). A crashing/timing-out
  hook is **not** a block (keeps the swallow-and-log contract).
- `hooks/dispatch.py`: typed wrappers `pre_compact(trigger, instructions)` and
  `post_compact(trigger, pre_tokens, post_tokens, stage)`.
- CLAUDE.md's hooks line gains the third exception (PreCompact blocks manual
  compaction).

### 5. `/compact [instructions]` (`interfaces/tui/commands.py`)

New `Command`. Semantics:

- Refuses while `turn_busy` (same guard as `!`).
- Runs `maybe_compact(trigger="manual", instructions=...)` in a worker so the
  summarizer cannot freeze the UI.
- Bypasses the size gate: compacts whenever there is *anything* to drop
  (`_plan_tail_start` grows a `manual` flag that skips the token check).
- Bypasses and **resets** the breaker.
- Hook-blockable (the only blockable path).
- Nothing droppable → notice "nothing to compact".
- Headless mode: not exposed (no interactive commands there).

### 6. `SessionController.maybe_compact` signature

`maybe_compact(*, force: bool = False, trigger: str = "auto", instructions: str | None = None)`.
The `ctrl.py` force-recovery branch (mask-in-place when nothing is droppable)
collapses into stage 1, which now runs unconditionally on `force`.

## Error handling

| Failure | Behavior |
| --- | --- |
| Payload persist fails / scratchpad off | plain `MASKED_OBSERVATION`, compaction proceeds |
| Summarizer raises / returns empty | truncation fallback (unchanged) |
| Hook subprocess crashes or times out | logged, treated as non-block (unchanged contract) |
| Block verdict on auto | logged at INFO, compaction proceeds |
| Breaker open | auto-compact skipped, one-time notice; manual & force still work |

## Testing

Pure helpers tested directly (house convention):

- `CompactionBreaker`: trip/reset table tests (3-in-3 rule, reset paths).
- Mask-with-persist: path lands in placeholder; fallback on persist failure;
  idempotent over both placeholder forms; keep-recent honored.
- Stage-1-suffices: history over threshold whose bloat is old tool output →
  summarizer **not** called, history under threshold after.
- Manual: bypasses size gate; resets breaker; blockable.
- Verdict parsing: exit 2, JSON block, malformed JSON, crash ≠ block.
- Auto ignores block verdicts.
- PostCompact payload fields (trigger/pre/post/stage).
- Existing `maybe_compact` tests updated for the mask-first reorder.
- One integration test through `SessionController` with a stub summarizer and a
  recording hook engine asserting full pipeline order:
  PreCompact → mask → (summarize) → persist → PostCompact → on_compact.

## Reference: Claude Code behavior this mirrors

From the v2.1.216 binary: microcompact keeps the last 5 tool results and fires
only when it frees ≥20k tokens (marim reuses its existing `mask_keep_recent`/
threshold-gated design instead); the thrash breaker message and 3-refills-in-3-
turns rule; the 9-section summary schema with verbatim security constraints and
next-step quotes; PreCompact matchers `manual`/`auto` and both blocking
contracts; `## Compact Instructions` injection for `/compact <instructions>`.
