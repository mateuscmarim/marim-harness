# Token efficiency via OpenRouter prompt caching

**Date:** 2026-06-22
**Status:** Approved design, pending implementation plan

## Problem

marim spends input tokens redundantly. Every turn re-bills the entire stable
request prefix — base instructions, ~2.4k tokens of tool schemas, and the whole
compacted history — at full input price. No prompt caching is configured
anywhere: `rg cache_control` returns nothing, and the OpenRouter model is built
with only `extra_body={"usage": {"include": True}}` (`config/openrouter_cost.py:82`).
The accounting layer already *tracks* `cache_read_tokens` / `cache_write_tokens`
(`usage.py`), but nothing ever populates them because no breakpoints are set.

On a long agentic session the unchanged prefix is the dominant input cost, and
caching it (reads at ~0.1×) is realistically a 5–10× input-cost reduction with no
behavioral change.

## Key insight: prefix stability decides whether caching hits

Anthropic prefix caching caches a request *prefix* up to a breakpoint. The first
changed byte invalidates everything downstream. marim's system prompt is
assembled (in `instructions.py:77-165`) as:

```
base INSTRUCTIONS
  → global / project / plugin AGENTS.md
  → memory index → skill index → agent index → MCP index
  → _task_state            ← VOLATILE
  → memory policy
```

…followed by tool definitions, then history.

Almost all of this is stable mid-session. The exception is **`_task_state`**
(`instructions.py:149-159`): the task checklist the agent is explicitly told to
mutate most turns ("keep one item in progress, mark items done as you complete
them"). It lives in the system prompt, *ahead of* the tool definitions and
history. During active multi-step work it changes every turn and invalidates the
cache for the tool defs and the entire history behind it.

Consequence: enabling caching *without* fixing this would pay the 1.25× cache
**write** premium every turn while rarely earning the 0.1× **read** — strictly
worse than doing nothing during active execution.

Therefore the design has two parts: the caching mechanism (Part A) and the
prefix stabilization that makes it hit (Part B).

## Scope

- **In scope:** the OpenRouter provider path only (the default — e.g.
  `anthropic/claude-sonnet-4-6`). Highest ROI.
- **Out of scope:** local/Ollama (cannot cache) and Google-direct (separate,
  lower-traffic path). The `google` and `local` branches of `build_model` are
  untouched.

## Part A — Adopt the official `OpenRouterModel` and enable caching

pydantic-ai 1.107 (already installed) ships a purpose-built
`pydantic_ai.models.openrouter.OpenRouterModel` that subclasses
`OpenAIChatModel`, so downstream structural assumptions hold. It provides
first-class cache settings and native OpenRouter usage/cost parsing.

Replace the `OpenAIChatModel`-based construction in
`config/openrouter_cost.py:build_openrouter_model` with `OpenRouterModel`
configured via `OpenRouterModelSettings`:

| Setting | Value | Effect |
|---|---|---|
| `openrouter_cache_tool_definitions` | `'5m'` | `cache_control` on the last tool def → caches the tool schema block |
| `openrouter_cache_messages` | `'5m'` | `cache_control` on the last message → rolling prefix cache across system + tools + history |
| `openrouter_cache_instructions` | `'5m'` | `cache_control` on the static instruction prefix |
| `openrouter_usage` | `{'include': True}` | native usage accounting (replaces the manual `extra_body`) |

That is 3 cache breakpoints, under Anthropic's max of 4 (gated by the model
profile's `openrouter_max_cache_points`; cache behavior is also gated on
`openrouter_supports_cache_control` / `openrouter_supports_tool_cache`, which the
`anthropic/claude-*` profile satisfies).

Cache read/write tokens then flow natively into `RunUsage` via
`_map_openrouter_usage`, so the existing `usage.py` split display
(`split_tokens`) lights up with no change.

### Cost capture — thin re-inject

The official model surfaces billed cost on `ModelResponse.provider_details['cost']`
(a per-response float, in dollars), **not** on `RunUsage`. marim's accounting
reads cost from `RunUsage.details[COST_DETAIL_KEY]` (int micro-USD), which
`RunUsage.incr` *sums* across the multiple requests in a turn. To preserve that
contract without rewriting the run loop or `usage.py`:

Keep a **thin** `OpenRouterModel` subclass (replacing today's `OpenAIChatModel`
subclass) that reuses the existing `read_cost_micro_usd` / `_with_cost` helpers
to override `_map_usage`, re-injecting the parsed `usage.cost` into
`RunUsage.details[COST_DETAIL_KEY]` as int micro-USD. The streamed path needs the
same re-inject; the implementation should confirm whether `OpenRouterModel`
streams via `OpenAIStreamedResponse` (in which case today's `request_stream`
class-swap carries over) or its own streamed-response class (which the override
must target instead).

Net effect on `config/openrouter_cost.py`: drop the manual `extra_body`, rebase
the subclass from `OpenAIChatModel` onto `OpenRouterModel`, keep the cost
helpers and the streaming hook. `usage.py` and all accounting/UI/tests are
untouched.

## Part B — Stabilize the system-prompt prefix

Relocate the volatile `_task_state` rendering out of `@agent.instructions`
(`instructions.py:149-159`) and into the per-turn `<turn-context>` user envelope
(`agent.py:_assemble_prompt`, where job digests, error notes, and hook context
already live). The task checklist is turn-state and belongs with the other
turn-state, at the *end* of the request where churn costs nothing.

After this move, the system prompt is stable across a session, so the tool-def
and message cache breakpoints actually hit on turn 2+.

The other instruction closures are effectively stable mid-session and stay in the
system prompt: the memory index changes only on `remember`/save; the skill,
agent, and MCP indexes are discovered once per session;
global/project/plugin/AGENTS.md and the memory policy are constant. `_task_state`
is the only frequent churner. The implementation should confirm this audit holds
before relying on it.

## Phasing

Parts A and B are separable:

- **A alone** wins on read-heavy / Q&A stretches and any turn where the checklist
  isn't mutating.
- **B** additionally captures active-execution turns by keeping the prefix stable
  while the checklist changes.

Ship both (recommended). A may be landed and measured first if desired.

## Data flow (after change)

1. `build_openrouter_model` returns the thin `OpenRouterModel` subclass with the
   four settings above.
2. Each turn, pydantic-ai assembles: stable system prompt → tool defs (last one
   carries `cache_control`) → history → last message (carries `cache_control`).
3. Anthropic (downstream of OpenRouter) reads the cached prefix and writes a new
   breakpoint at the current tail.
4. `_map_openrouter_usage` populates `cache_read_tokens` / `cache_write_tokens`
   on `RunUsage`; the thin subclass re-injects summed billed cost into
   `RunUsage.details[COST_DETAIL_KEY]`.
5. `usage.py` reports the split and exact cost exactly as today.

## Error handling

Caching is best-effort and must fail soft, matching the existing hook's posture:
if the model profile reports no cache support, the settings are ignored by
pydantic-ai and behavior is unchanged. The cost re-inject already fails soft (a
missing or non-numeric cost yields no detail and callers fall back to the
genai-prices estimate).

## Testing

- **Unit:** the rebased cost subclass still re-injects micro-USD into
  `RunUsage.details` (existing cost-capture tests should pass unchanged).
- **Unit:** `build_openrouter_model` constructs `OpenRouterModel` with the four
  expected settings.
- **Unit:** `_task_state` content now appears in the assembled `<turn-context>`,
  not in the system instructions (move/adjust the existing assertion).
- **Integration:** a recorded two-turn run reports non-zero `cache_read_tokens`
  on turn 2.

## Verification (proving the win)

`usage.py` already computes `cache_read` / `cache_write` and billed `cost_usd`.
Run a representative multi-turn session before and after, and compare
`cache_read_tokens` and `cost_usd`. Success criteria:

- Cache reads dominate input tokens on turn 2+.
- Per-turn billed cost drops materially on the stable-prefix turns.
