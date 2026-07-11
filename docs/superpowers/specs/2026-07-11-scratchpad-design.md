# Scratchpad Directory — Design

**Date:** 2026-07-11
**Status:** Approved

## Problem

The agent currently has nowhere to put intermediate work — temp scripts, staged
outputs, analysis artifacts — except the workspace itself, which pollutes the
project tree and git status. Claude Code solves this with a session-specific
scratchpad directory advertised in the system prompt; marim should have the
same convention.

## Decisions (from brainstorming)

- **Motivation:** keep temp files out of the repo.
- **Location/lifetime:** `/tmp`, per session. A resumed session reuses the same
  path (recreated if the OS cleaned it). Not fresh-per-run, not XDG.
- **Approvals:** `write_file`/`edit_file` targeting the scratchpad are
  auto-approved in `ask` mode. `bash` stays gated. `plan` mode still denies.
- **Sub-agents:** share the session scratchpad (same path in their prompt),
  enabling file hand-off between parent and child.
- **Mechanism:** extend the workspace path guard with extra allowed roots
  (Approach 1), rather than an in-workspace `.marim/scratch/` dir (contradicts
  the motivation) or dedicated scratch tools (duplicated tool surface).

## Design

### Path scheme & lifecycle

- Path: `/tmp/marim-<uid>/<workspace-slug>/<session-id>/scratchpad`.
  - `<uid>` = `os.getuid()`, avoiding multi-user collisions on shared machines.
  - `<workspace-slug>` reuses the `{name}-{sha256(root)[:12]}` naming already
    used by `session/store.py:_workspace_dir`, so scratchpads key the same way
    session storage does.
  - The `scratchpad` leaf leaves room for future session sidecars in the same
    per-session dir.
- Pure helper `scratchpad_root(workspace_root, session_id) -> Path` in a new
  `workspace/scratchpad.py` — side-effect-free, unit-tested directly, per the
  repo's pure-helper convention.
- Created lazily (`mkdir(parents=True, exist_ok=True)`) the first time it is
  resolved in a turn. Deriving the path from the live session-id getter at
  turn time (never baked in at build time) handles mid-run session switches
  and recreates the dir if `/tmp` was cleaned under a resumed session.
- **Squatting guard:** `/tmp/marim-<uid>` is created mode `0700`. If it already
  exists but is a symlink or not owned by the current uid, the scratchpad
  disables itself (getter returns `None`) with a logged warning rather than
  proceed.
- **Cleanup:** `SessionManager.delete()` removes the session's scratchpad dir
  best-effort, alongside the existing transcript-dir and image-cache cleanup.
  Otherwise `/tmp` semantics apply (OS reclaims on reboot — the point of
  living there).

### Wiring & prompt injection

- `HarnessServices` gains `get_scratchpad: Callable[[], Path | None] | None`,
  wired in `build_collaborators` next to the existing `get_session_id` lambda.
  Returns `None` when there is no session store (embedding/tests) or when the
  feature is disabled; every consumer guards on `None`, matching the existing
  optional-callback convention in `Deps`.
- New `@agent.instructions` closure in `register_instructions`
  (`runtime/instructions.py`, alongside `_project_instructions`) renders a
  short "Scratchpad Directory" block: the path plus guidance to use it for
  intermediate files instead of the workspace or `/tmp`. Returns `""` when the
  getter is `None` or returns `None`, so headless embedders see no change.
- Config knob: `HarnessConfig.scratchpad_enabled: bool = True`, settable via
  `HarnessBuilder.with_config_overrides`. Bootstrap reads `MARIM_SCRATCHPAD=0`
  to disable — env-reading stays in bootstrap per the builder/bootstrap split.

### Workspace guard

- `resolve_in_workspace(root, path)` in `workspace/fs.py` gains an optional
  `extra_roots: Sequence[Path] = ()` parameter. Resolution is unchanged
  (symlinks still chased via `.resolve()`); the only addition: a candidate
  landing inside an extra root is accepted. Relative paths still resolve
  against the workspace root — the scratchpad is reached by absolute path,
  exactly as advertised in the prompt.
- The fs/edit tools pass `extra_roots=[scratchpad]` when
  `ctx.deps.services.get_scratchpad` yields a path (mkdir happens lazily at
  that point). No getter → empty tuple → today's behavior, bit-for-bit.
- `ReadLedger` (read-before-edit) applies to scratchpad files for free, since
  it is keyed on resolved paths downstream of the same guard.

### Approvals

- In the approval resolver (`runtime/permissions.py` / the controller's
  `resolve_approvals` path), a `write_file`/`edit_file` call whose **resolved**
  target is inside the scratchpad is auto-approved in `ask` mode.
- `bash` stays gated (a command's filesystem reach cannot be cheaply proven to
  stay inside the scratchpad).
- `plan` mode still denies scratchpad writes — plan mode's "no mutations"
  promise stays absolute.

### Sub-agents & providers

- Sub-agents share the session scratchpad: they inherit the same
  `Deps`/services graph, so the guard exception and the prompt block apply
  without extra plumbing. Sub-agent tools are registered plain (no approval
  gate), so no approval work is needed there.
- The `claude-cli` provider is unaffected: marim's tools and prompt assembly
  do not apply there (the CLI runs its own loop with its own scratchpad), so
  the feature is naturally scoped to the native providers.

## Error handling

- Squatting-check failure or any `OSError` on mkdir → getter returns `None`,
  a warning is logged once, and the system degrades to today's behavior (no
  prompt block, no extra root, normal gating).
- Session deleted while files remain → cleanup is best-effort
  (`ignore_errors`); a failed delete never blocks session deletion.

## Testing

- `scratchpad_root`: path shape, uid segment, workspace slug parity with
  `session/store.py`.
- `resolve_in_workspace` with `extra_roots`: inside root, inside extra root,
  outside both, symlink-escape from the extra root, relative paths still
  workspace-bound.
- Squatting guard: pre-existing symlink / wrong-owner dir disables the getter.
- Approval routing: scratchpad-targeted `write_file`/`edit_file` auto-approve
  in `ask` mode; non-scratchpad targets still prompt; `plan` mode still
  denies; `bash` still gated.
- Instructions: block renders with a session id, absent without one and when
  `scratchpad_enabled=False`.
- `SessionManager.delete` removes the scratchpad dir.

## Out of scope

- Making `bash` scratchpad-aware in the command policy.
- Any scratchpad-specific retention/GC policy beyond `/tmp` semantics and
  delete-on-session-delete.
- Server (`serve`) API surface for scratchpads — concurrent server sessions
  are already collision-free because the path is keyed by session id.
