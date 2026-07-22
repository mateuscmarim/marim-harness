# Memory gaps: forget tool, wikilinks, guidance polish

**Date:** 2026-07-22
**Status:** Approved

## Background

Marim's native memory (`workspace/memory.py`) deliberately mirrors Claude Code's
auto-memory design: a `MEMORY.md` index injected every turn plus one
frontmattered `<slug>.md` file per fact, in two scopes (global under the config
dir, project under `.marim/memory`). Comparing the two systems surfaced three
gaps worth closing (a fourth — a personal-but-project "local" scope — was
considered and explicitly deferred until a real need shows up):

1. **No way to delete a memory.** Memory files are unreachable via the file
   tools (by design — global scope is outside the workspace sandbox), and the
   only tools are `remember`/`recall`. A wrong or stale memory keeps its index
   line injected into every future prompt forever; the only recourse is manual
   file surgery by the human.
2. **No linking convention between memories.** Claude Code links related
   memories with `[[name]]`; dangling links are legal and mark facts worth
   writing later. Marim has nothing like it.
3. **Missing store-hygiene guidance.** Claude Code's memory instructions bake
   in: convert relative dates to absolute when saving; don't save what the repo
   already records; treat recalled memories as possibly stale. Marim's
   docstrings and policy strings only gesture at these.

## Design

### 1. `forget` tool (gated hard delete)

**Alternatives considered:** soft delete to a `.trash/` subdir (recoverable, but
speculative complexity — YAGNI); a `delete=True` parameter on `remember` (fewer
tools, but `remember` is ungated, so the flag would silently bypass approval).
**Chosen:** hard delete behind approval gating.

- `workspace/memory.py` gains `delete_memory(scope: MemoryScope, name: str) ->
  bool`: slugify `name` (title or slug both resolve, as in `read_memory`),
  unlink `<slug>.md`, and remove its index line. The index rewrite happens under
  the same `file_lock` + `atomic_write_text` discipline as
  `_upsert_index_line`, matching the entry by its own link target (the existing
  `entry_link` regex), never by substring. Fail-soft per the module contract:
  returns `False` on OSError or a missing file, never raises into a turn.
- `tools/memory_tools.py` gains `forget(ctx, name, scope="project")` mirroring
  `recall`'s signature. Returns a plain confirmation ("Deleted project memory
  'x'") or a "no such memory" notice. Docstring instructs the model to check
  the index first and to prefer updating (via `remember` with the same title)
  over deleting unless the fact is actually wrong or obsolete.
- `tools/provider.py` registers `forget` on the main agent with
  `requires_approval=True` — it moves through the gated registration path, not
  the ungated read/query block that registers `remember`/`recall`.
- **Sub-agent reach:** sub-agents register tools plain (no mid-run approval
  exists there), so the `"memory"` group in `tools/names.py` stays
  `{remember, recall}`. `forget` is main-agent-only; a sub-agent granted memory
  can read and save but never delete. This keeps the gating decision meaningful.
- No new TUI command. Deletion is asked in natural language and flows through
  the gated tool; a `/forget` command is sugar we skip.

### 2. `[[wikilinks]]`

**Alternatives considered:** docstring-only convention; auto-inlining linked
bodies in `recall` (token bloat, cycle risk). **Chosen:** docstring convention
plus a link footer in `recall`.

- `remember`'s docstring gains the convention: link related memories with
  `[[name]]` in the body; link liberally; a `[[name]]` that doesn't match an
  existing memory is fine — it marks a fact worth writing later, not an error.
- `workspace/memory.py` gains a pure helper `extract_links(body: str) ->
  list[str]` (regex on `[[...]]`, deduplicated, order-preserving), unit-tested
  directly per the conventions.
- `recall` scans the returned body; when links are present it appends a one-line
  footer distinguishing saved linked entries from unwritten ones (e.g.
  `Linked memories — saved: a, b; not yet written: c`). Existence is checked by
  slugifying each link and testing for `<slug>.md` in the scope's directory (not
  the index, which could be stale). No bodies are inlined.

### 3. Guidance polish (text-only)

- `remember` docstring: convert relative dates to absolute when saving; don't
  save what the repo already records (git history, AGENTS.md, code structure).
- `recall` docstring: memories reflect when they were written — verify a named
  file/flag/function still exists before acting on it.
- The two proactive-policy strings in `runtime/instructions.py` (proactive ON /
  explicit-only) gain the repo-facts exclusion. These strings are static per
  session, so prompt caching is unaffected.

## Error handling

Everything follows `workspace/memory.py`'s existing contract: nothing raises
into a turn. `delete_memory` logs and returns `False`; `forget` turns that into
an ordinary tool-result string, exactly like `remember`'s `None` path.

## Testing

- `delete_memory`: deletes the file and only its index line (other lines
  preserved, including a hook that mentions another entry's filename); missing
  name returns `False`; unwritable dir returns `False`; title-vs-slug both
  resolve.
- `extract_links`: basic, duplicates, accents/odd characters, no links.
- Tool level: `forget` success and no-such-memory strings; `recall` footer for
  existing/dangling/mixed links and its absence when the body has none.
- Registration: `forget` is gated (`requires_approval=True`) on the main agent;
  the sub-agent `"memory"` name group still excludes it.

## Out of scope

- Local (personal, per-project, outside-repo) memory scope — deferred.
- `/forget` TUI command.
- Auto-inlining or recursive resolution of linked memories.
- Any change to the frontmatter schema (already matches Claude Code).
