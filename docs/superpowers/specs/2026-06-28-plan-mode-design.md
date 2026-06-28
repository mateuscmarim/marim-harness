# A real plan mode

**Status:** approved (design)
**Date:** 2026-06-28

## Problem

Plan mode today is the weakest of the three approval modes. Its entire behavior
is one branch in `runtime/permissions.py`:

```python
elif mode is Mode.plan:
    results.approvals[call.tool_call_id] = ToolDenied("read-only plan mode")
```

Every gated call (`write_file`/`edit_file`/`bash`) is denied with a fixed string.
Two consequences:

1. **The model is never told it is planning.** There is no mention of plan mode in
   `runtime/instructions.py` or `runtime/context.py`. The model doesn't plan
   deliberately — it tries to act, hits a denial, and reacts, one wasted tool call
   at a time.
2. **There is no exit ramp.** Plan mode is a dead end: nothing captures the plan and
   nothing transitions from planning to doing. The user must manually `/mode auto`
   (or `Ctrl+T`) and re-prompt.

So the three modes are really "approve-each / approve-all / refuse-all". Plan mode
is auto mode's denial twin, not a planning workflow.

## Goal

A layered plan mode that:

- **Posture** — the model knows it is planning and researches deliberately,
  ending by presenting a plan.
- **Handoff** — the user approves the plan and *chooses how to execute it* at that
  moment, reusing the existing three modes as targets.
- **Artifact** — the plan becomes a durable, structured thing: the live
  `update_tasks` checklist plus a markdown file under `.marim/plans/`.

Sub-agent *orchestration* is explicitly **out of scope** (it duplicates the
superpowers execution skills and would couple plan mode to the heaviest subsystem).
The plan file is the seam those skills consume. A thin "hand off to sub-agent"
affordance is included, but it only seeds a `spawn_agent` run — it owns no policy.

## Design

### 1. Planning posture (per-turn context)

When `workspace.mode is Mode.plan`, `_assemble_prompt` (`runtime/controller.py`)
prepends a planning instruction inside the existing `<turn-context>` envelope —
**not** the system prompt, so prompt caching is preserved (same mechanism already
used for error notes and hook context). The instruction tells the model: you are
planning, research read-only, do not attempt mutations, and end by calling
`present_plan` with a summary and ordered steps.

### 2. Read-only bash during planning

Today `bash` is gated, so in plan mode `resolve_approvals` denies it wholesale
*before* it reaches the bash tool's own command policy. Therefore the read-only
decision must live at the **approval layer**, not inside the tool.

- New leaf module `read_only_commands.py` — a curated allowlist of read-only
  commands (`git status|log|diff|show|branch`, `ls`, `cat`, `head`, `tail`, `rg`,
  `grep`, `find`, `tree`, `pwd`, `wc`, …), matched with `CommandPolicy`'s existing
  `re.search` semantics.
- Extend the plan branch of `resolve_approvals`:
  - `write_file` / `edit_file` → always deny ("read-only plan mode").
  - `bash` → approve **iff** the command is classified read-only; otherwise deny
    with `"plan mode: read-only commands only"`.
- **Not a sandbox.** This is the same best-effort nudge as `command_policy.py`
  (documented there as NOT A SANDBOX). A motivated model can evade a regex
  (`$(echo rm) …`). Acceptable: plan mode is a workflow aid, not a security
  boundary. This caveat is documented next to the allowlist.

### 3. `present_plan` tool + the handoff

New tool `present_plan(summary: str, steps: list[str])`, registered on the **main
agent only** (never granted to sub-agents). Built on the existing `ask_user` UI
primitive.

Behavior when called:

1. Render the plan (summary + steps) to the user.
2. Ask the user to choose an execution path:
   - **Execute hands-off (auto)** — set `workspace.mode = auto`.
   - **Execute step-by-step (ask)** — set `workspace.mode = ask`.
   - **Hand off to sub-agent** — seed a `spawn_agent` run with the plan file
     pre-loaded (thin sugar; no orchestration logic).
   - **Keep planning** — stay in plan mode; the model may refine.
3. Side effects via `ctx.deps`: write the plan file (§4) and populate
   `update_tasks` from `steps`.

The mode flip needs **no special engine**. `resolve_approvals` reads
`self.deps.workspace.mode` fresh on every approval round
(`runtime/controller.py:539`), so the next gated call in the same turn is resolved
under the newly set mode. The handoff falls out of the existing approval loop.

**Headless** (no UI callback wired): default to "keep planning" — emit the plan,
do not auto-execute, since no one is present to approve execution.

### 4. Plan artifact

- New `workspace/plans.py`: pure format/write/read helpers plus a thin IO wrapper,
  matching the codebase's pure-helper / thin-wiring split.
- Writes `.marim/plans/<slug>.md` containing: summary, steps as a markdown
  checklist, and metadata (created timestamp, session id, status). The `<slug>` is
  derived from the session id plus a short kebab-cased prefix of the summary's first
  line, keeping one stable file per plan per session (re-presenting overwrites it).
- The same `steps` populate `update_tasks`, so the task panel reflects the plan and
  execution checks items off live.
- The plan file is the **integration seam** for the superpowers execution skills
  (`subagent-driven-development`, `executing-plans`), which consume it directly.

### 5. Entry / exit & error handling

- **Entry** unchanged: `/mode plan`, `Ctrl+T`.
- **Exit**: via the `present_plan` handoff, or manual `/mode`.
- `present_plan` with empty `steps` → `ModelRetry` asking for steps.
- Plan-file write failure → best-effort warn, never crashes the turn (same pattern
  as memory / diagnostics).
- `present_plan` is a normal tool call with a normal return, so the "history must
  never end on an unanswered `ToolCallPart`" resumability invariant is untouched.
- "Keep planning" still writes the plan file as a draft so it survives the session.

## Files touched

- `runtime/permissions.py` — plan-branch read-only bash logic.
- `runtime/controller.py` — planning posture in `_assemble_prompt`.
- `tools/provider.py` + `tools/names.py` — new `present_plan` tool (main-agent only).
- `read_only_commands.py` *(new)* — read-only command classifier.
- `workspace/plans.py` *(new)* — plan artifact format/write/read.
- TUI approval/choice modal — the four-way execution choice.
- Tests (see below).

## Testing

- **Unit**
  - `read_only_commands` classifier: allow `git log`, `ls -la`, `rg foo`; deny
    `rm -rf`, `git push`, `echo x > file`, `pip install`.
  - `workspace/plans.py`: format → write → read round-trip; slug generation;
    metadata fields.
  - `resolve_approvals` plan branch: deny `write_file`/`edit_file`; approve
    read-only `bash`; deny mutating `bash`.
- **Integration**
  - `present_plan` flips `workspace.mode`, writes the plan file, and seeds
    `update_tasks`; the approval loop then continues under the new mode.
  - Headless: `present_plan` defaults to "keep planning" and does not execute.
- Gate on CI order: `ruff` → `pyright` → `pytest`.

## Out of scope (YAGNI)

- Deterministic sub-agent orchestration / dependency analysis of plan steps
  (owned by the superpowers execution skills).
- Cross-session plan resume UI, plan editing inside the harness.
- A dedicated `/plan` slash command (entry via `/mode plan` is sufficient).
