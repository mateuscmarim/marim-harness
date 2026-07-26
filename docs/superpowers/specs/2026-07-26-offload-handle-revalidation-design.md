# Offload-handle revalidation on session load — design

**Date:** 2026-07-26
**Status:** approved (discussed in conversation 2026-07-26)

## Problem

Large tool outputs are offloaded to the session scratchpad
(`/tmp/marim-<uid>/…/<session-id>/scratchpad`), and the persisted history
keeps only a *handle*: a line naming the file plus an inline preview. The
scratchpad can die (reboot, systemd-tmpfiles aging) while the session file in
`$XDG_DATA_HOME` lives on. A session resumed after that holds handles that
promise a `read_file` the model can no longer perform.

Compaction-elided payloads already solve this: `revalidate_elided_pointers`
runs at the load seam (`session/ctrl.py::_load_into_controller`) and degrades
dangling pointers to the honest "re-run the tool" placeholder. Offload
handles get no such pass — the model discovers the loss only by hitting a
file-not-found tool error.

The blocker to a simple fix is that handles come in **three formats**, none
designed for machine parsing:

1. `tools/impl/offload.py::_write_handle` — `` …full output saved to
   `path`. Read more with read_file… `` (bash, fs reads, grep, MCP, skills).
2. `tools/impl/fetch.py::_offload_body` — `` …full content saved to
   `path`. Read more with read_file… `` (already matches format 1's core).
3. `workspace/agents.py::cap_subagent_output` — `[output capped at N chars —
   full report at path]` (sub-agents and workflows; no backticks).

## Decision

Standardize the envelope first, then revalidate with a single matcher
("Option B" from the discussion). Rejected alternative ("Option A"): match
all three ad-hoc formats at the load seam — smaller diff, but a wording
tweak in any producer silently breaks revalidation with no test failure
unless every format is pinned individually.

## Design

### 1. One machine-recognizable envelope (`tools/impl/offload.py`)

The shared core is the phrase ``saved to `absolute-path` `` — backticked
path, preceded by the words "saved to". Formats 1 and 2 already conform.
Format 3 changes copy from `full report at {path}` to ``full report saved
to `{path}` ``.

`offload.py` exports the envelope contract:

- `OFFLOAD_HANDLE_RE` — compiled regex ``r"saved to `([^`\n]+)`"``.
- `find_offload_paths(content: str) -> list[str]` — pure helper returning
  every embedded path (usually 0 or 1).
- `OFFLOAD_GONE_NOTE` — the note revalidation appends (see §3). Lives here,
  next to the envelope, so producer copy and revalidation copy stay
  coherent in one module.

Producers do **not** import the regex to build their handles — they keep
their natural f-strings. Conformance is pinned by tripwire tests (§4), the
same pattern as `test_provider_prefixes_mirrors_known_providers`.

### 2. Producers emit absolute paths

`_write_handle` and fetch already emit absolute paths (scratchpad and
legacy dirs are both absolute at the call sites). `cap_subagent_output`'s
two callers pass a workspace-relative fallback path when no scratchpad is
available — `subagents/runner.py::_cap_output`
(`.marim/subagent-output/<ref>.md`) and `workflows/engine.py`
(`.marim/workflow-output/<name>.json`, engine.py:422). Both resolve the
fallback to an absolute path (joined to `deps.workspace.root`) before
formatting, so every new handle in history is absolute. Belt and
braces: revalidation still resolves non-absolute paths against the
workspace root (§3), so pre-existing histories with relative handles
revalidate correctly too.

### 3. Revalidation at the load seam (`compaction.py` + `session/ctrl.py`)

Extend the existing single history walk rather than adding a second one:
`_revalidate_parts` grows a second detector. For each string
`ToolReturnPart` content:

- **Elided pointer** (existing): dangling → replace with
  `MASKED_OBSERVATION`, unchanged behavior.
- **Offload handle** (new): extract paths via `find_offload_paths`;
  resolve non-absolute ones against a new `base: Path | None` parameter;
  if any path fails `exists()`, **append** `OFFLOAD_GONE_NOTE` to the
  content instead of replacing it — the inline preview is real information
  and must survive. Note text:

  ```
  \n\n⚠️ The offloaded file referenced above no longer exists (the
  scratchpad was cleaned since this session last ran) — re-run the tool if
  you need the full output.
  ```

- **Idempotency:** content already containing `OFFLOAD_GONE_NOTE` is
  skipped (one note per part, even with multiple dangling paths; the note
  deliberately says "referenced above" rather than naming one path).
- **Non-string content** (structured tool results): skipped, same guard as
  the pointer pass.

`revalidate_elided_pointers` keeps its name and contract (same-object
return when nothing changed; `(new_history, count)` otherwise) and gains
the `base` parameter; the count now covers both kinds. The call site in
`session/ctrl.py` passes `self.deps.workspace.root` and updates the debug
log wording to "dangling elided pointer(s)/offload handle(s)".

Cache note (unchanged rationale): this runs only at the load seam — a
cache-cold moment — so rewriting history there costs nothing, and live
handles are left byte-identical.

### 4. Tripwire tests

A test per producer renders a real handle and asserts `find_offload_paths`
extracts exactly the right absolute path:

- `_write_handle` via `offload_if_large` over the inline limit.
- fetch's `_offload_body`.
- `cap_subagent_output` over budget.

Any future copy edit that breaks the envelope now fails a named test
instead of silently disabling revalidation.

### Untouched by design

- Elided-pointer behavior (replace, not append) — nothing to preserve there.
- `offload_if_large`'s clip fallback and the legacy `.marim/output/`
  fallback dirs.
- Masking: an old handle still ages out as a stale observation like any
  other; revalidation and masking compose (a masked part no longer matches
  either detector).
- No per-turn checks — load seam only, per the existing cache rationale.

## Testing

Unit (offline), beyond the tripwires:

- Dangling handle → note appended once, preview retained; second
  revalidation is a no-op (idempotent).
- Live handle → same `history` object returned (`is`-check), content
  byte-identical.
- Relative path handle + `base` → resolved against base for the exists
  check.
- Mixed message: one part dangling-elided + one part dangling-handle in one
  history → pointer replaced, handle annotated, count = 2.
- Non-string content untouched.
- `cap_subagent_output` fallback path is absolute in the rendered note
  (runner resolution).

No live smoke needed — the change is pure history rewriting at load.

## Docs

- `CHANGELOG.md` Unreleased entry (resumed sessions now flag offloaded
  files lost to scratchpad cleanup instead of promising a dead
  `read_file`).
