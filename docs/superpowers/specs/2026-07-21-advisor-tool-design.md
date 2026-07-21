# Advisor Tool — Design

**Date:** 2026-07-21
**Status:** Approved (brainstorm), pending implementation plan

## What this is

A client-side replica of Anthropic's advisor tool ([Claude Code docs](https://code.claude.com/docs/en/advisor),
[API docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool)) for marim-harness:
the main model gets an `advisor()` tool that, when called, forwards the full conversation
transcript to a separately-configured, typically stronger model and returns its strategic
guidance as the tool result. The main model decides *when* to consult; soft system-prompt
guidance steers it toward consulting before substantive work, when stuck, and before
declaring done.

Anthropic's version is a server-side tool (`advisor_20260301`); ours is a plain client-side
tool because marim runs on arbitrary providers (OpenRouter, local, Google). The observable
behavior matches: empty-input tool, full-transcript advisor view, tool-free one-shot advisor
run, advice text back, errors inside the tool result (the turn never fails on advisor failure).

## Scope decisions

- **Full parity target:** model-invoked tool, `/advisor` mid-session command, Settings-screen
  configuration, per-turn call cap, usage visibility in the transcript card.
- **Main loop only** in v1 — sub-agents do not inherit the advisor (they have model tiering);
  inheritance is a possible follow-up.
- **Advisor is a provider+model pair, independent of the main model** — a qualified
  `provider:slug` (e.g. `openrouter:anthropic/claude-opus-4.8`), so a local main model can
  pair with a cloud advisor. Bare slugs resolve against the default provider.
- **Soft steering only** — Anthropic's timing + weigh-the-advice system-prompt blocks; no
  "hard rule" enforcement, no nudge injection.
- **No capability-pairing enforcement** — arbitrary slugs can't be ranked; the user's pick
  is trusted.
- **Ungated tool** — configuring an advisor is consent to send the transcript to that
  provider (same reasoning as the main model itself).
- **claude-cli main-loop provider excluded** — marim's tools don't apply in that provider
  (marim is a launcher there), so the advisor simply doesn't exist for it. A `claude-cli`
  *advisor* model is allowed and gets the `aux_model_for` stateless-clone treatment.

## Core mechanism

### The tool

`advisor(ctx: RunContext[Deps])` — no parameters, module-level in `tools/provider.py`,
registered plain (ungated) on the main agent only (not in `_SUBAGENT_FNS`). The docstring is
the model-facing description: calling it forwards the entire conversation — task, every tool
call and result — to a stronger reviewer model, which returns strategic guidance.

**Live toggling:** the tool is registered with a pydantic-ai `prepare` hook that returns
`None` (omitting the tool from the run) when `deps.services.advise is None`. The tool exists
in the schema only when an advisor is configured; `/advisor off` / `/advisor <model>` take
effect on the next run with no agent rebuild.

### The advice call

New module `advisor.py` (sibling of `compaction.py`) owning `make_advisor(...)` and the
instructions text, cloned from the `make_summarizer`/`make_titler` pattern. Per call:

1. Resolve the current advisor slug (session override → env default) and build the model via
   `model_source.build("provider:slug")`. Per-call resolution is what makes mid-session
   switches live. `claude-cli` advisors get `aux_model_for` cloning.
2. Serialize `ctx.messages` (the full in-flight run history) with
   `compaction.render_transcript`.
3. Run a tool-free one-shot `Agent(advisor_model, instructions=_ADVISOR_INSTRUCTIONS)` with
   the transcript in a `=== TRANSCRIPT ===` block, restating the ask in the user turn (the
   claude-cli append-only-instructions gotcha). Output capped at `MARIM_ADVISOR_MAX_TOKENS`
   (default 2048) via model settings.
4. Return the advice text. On any failure, return a short actionable error string — the main
   model continues without advice; the run never fails.

`_ADVISOR_INSTRUCTIONS`: you are a senior reviewer advising a coding agent mid-task; read
the transcript; give focused strategic guidance — approach, risks, what to check before
proceeding; be concise; don't restate the transcript.

### Timing steering

When an advisor is configured, Anthropic's two soft-guidance blocks (call-before-substantive-
work timing; how to weigh advice vs own evidence) are appended via a dynamic `instructions`
function checking the same `services.advise is not None` condition — prompt and tool
availability cannot drift. Toggling the advisor mid-session breaks the prompt cache once
(instructions + tool schema change); inherent to a client-side implementation, documented.

### Call cap

`MARIM_ADVISOR_MAX_USES` (default unset = unlimited): a per-turn counter on `Deps`; when
exceeded the tool returns a "max uses exceeded, continue without advice" error string.

## Config flow

**Env layer** (`config/model.py`, provider-independent knobs in `_common_kwargs` →
`ModelConfig` fields):

- `advisor_model: str | None` from `MARIM_ADVISOR_MODEL` (qualified `provider:slug` or bare)
- `advisor_max_tokens: int` from `MARIM_ADVISOR_MAX_TOKENS` (default 2048)
- `advisor_max_uses: int | None` from `MARIM_ADVISOR_MAX_USES` (default unlimited)

All documented in `.env.example` next to the tier block.

**Builder layer:** `HarnessConfig` gains the three fields; `HarnessBuilder` gains
`with_advisor(model, *, max_tokens=2048, max_uses=None)` (no env reads, per the
builder/bootstrap split). `build_collaborators` constructs the advise callable when
configured; `build_services` binds it as `HarnessServices.advise` (the `run_workflow`
precedent).

**Bootstrap layer:** passes the three values through `with_config_overrides`, next to where
`subagent_tiers`/`summarizer`/`titler` are injected.

**Session persistence:** `SessionStore.advisor_model` mirrors `model` exactly
(save/save_meta/load/create-inherits-latest). `Harness.set_advisor_model(model_id | None,
persist=True)` mirrors `set_model`. Precedence: session override → `MARIM_ADVISOR_MODEL` →
none (off). `/advisor off` persists the string sentinel `"off"` so "explicitly off" survives
restarts and is distinguishable from "unset, inherit env".

**Validation:** if the slug's provider isn't in the active `MultiModelSource` (no
credentials), the advise callable returns an actionable error string and the settings row
surfaces the problem — no crash.

## TUI

**Settings row** (`interfaces/tui/settings.py`, Tools section): an "Advisor model" row
cloning the tier-row pattern (`_TIER_ROWS` → `_open_tier_picker` → `_on_tier_chosen`):
Static value ("off" when unset) + "change" button → `ModelPickerModal` over
`model_source.list_models` with an "off" affordance. Persists the *global default* via
`save_env_settings({"MARIM_ADVISOR_MODEL": ...})` + `refresh_from_env()`. Max-tokens and
max-uses as compact inputs in the `_ENV_INT_INPUTS` auto-save registry.

**`/advisor` command** (`interfaces/tui/commands.py`, modeled on `_cmd_model`):

- `/advisor` → model picker → `harness.set_advisor_model(chosen)` (session-persisted)
- `/advisor <slug>` → set directly
- `/advisor off` → disable for this session (persists sentinel)
- No mid-turn refusal — resolution is per-call, a switch applies to the next consultation
- Confirmation echoes resolved provider:model, or warns on missing credentials

**Transcript rendering** (`stream_render.py`): intercept `tool_name == "advisor"` in
`_TopLevelSink.intercept_tool` like `ask_user` — standalone `ToolCallWidget` outside the
collapsed tool-run group, run broken on both sides. Pending: "Advising… (provider:model)".
Result: "Advisor reviewed the conversation" with advice text in the existing reveal/collapse
body; advisor-run token usage in the card subtitle when available. No new widget class in v1.

**Status surface:** session start with an advisor active shows one line:
"Advisor: provider:model · /advisor".

## Error handling

- Provider/model build failure, HTTP errors → caught in the advise callable, returned as
  "Advisor unavailable: …. Continue without advice."
- Abort/Ctrl-C during a consult → cancelled with the turn like any tool;
  `_repair_unanswered_tool_calls` already handles the dangling-call shape.
- Transcript overflow → one retry with tightened `render_transcript` `max_part_chars`; then
  error string.

## Testing

- *Pure helpers, direct unit tests:* slug precedence (sentinel vs env vs unset), max-uses
  counter, prompt assembly.
- *Tool behavior (`TestModel`/`FunctionModel`):* tool present iff advisor configured (both
  directions of the `prepare` hook — the one new mechanism); advice text from a stubbed
  callable; error string on failure; max-uses exhaustion.
- *Config flow:* env → `ModelConfig`; `with_advisor` → `HarnessConfig`.
- *Session persistence:* round-trip; `"off"` sentinel beats env; `create()` inherits latest.
- *TUI:* `/advisor` dispatch variants (Pilot); settings-row save writes the env key.
- No live-model tests by default; manual smoke recipe vs local LM Studio in PR notes.

## Docs

- `.env.example`: the three vars.
- `docs/embedding.md`: `with_advisor`.
- `CLAUDE.md`: one paragraph — what it is, where it lives, cache-break-on-toggle caveat,
  claude-cli main-loop exclusion.

## Out of scope (follow-ups)

- Sub-agent advisor inheritance
- Advisor-side prompt caching across consults
- Nudge injection for under-calling models
- Hard-rule enforcement (first mutating action requires a prior consult)
