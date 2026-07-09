# Sub-agents

Sub-agents are isolated agents the main agent can spawn via the
`spawn_agent` tool: fresh context, an explicit tool grant, and a final
report returned to the parent. The SDK surface is `AgentDef` +
`with_subagent`.

## `AgentDef`

```python
from marim_harness.workspace.agents import AgentDef

reviewer = AgentDef(
    name="reviewer",
    description="reviews diffs",          # shown to the model in the roster
    prompt="You review diffs.",           # the sub-agent's system prompt
    tools=frozenset({"read_file", "grep"}),
    source="programmatic",
)

builder.with_subagent(reviewer)
```

Fields:

| Field | Meaning |
| --- | --- |
| `name` | Spawn handle (`spawn_agent(type="reviewer", ...)`). |
| `description` | One-liner in the roster the model sees — write it as "use when …" guidance. |
| `prompt` | The sub-agent's system prompt. |
| `tools` | `frozenset` of tool names it may use (see grantable set below). |
| `source` | Provenance label (`"programmatic"` is fine for SDK use; the CLI uses `built-in`/discovery roots). |
| `plugin` | Owning plugin name, or `None`. Namespaces the agent as `plugin:name`. |
| `backend` | `"native"` (in-process pydantic-ai loop, default) or `"claude-cli"` (spawns the Claude Code CLI — requires a `claude` binary and subscription; CLI-oriented). |
| `model` | Backend-specific model override; `None` tracks the harness model (native backend). |

`with_subagent(defn)` registers the spec **and implies the `spawn` group** —
a spec nobody can reach is dead weight, so `spawn_agent` is granted to the
main agent automatically.

## What a sub-agent may be granted

The grantable universe (`tools/names.py:SUBAGENT_TOOLS`) is split by trust
boundary:

- **Read tools** — `read_file`, `glob`, `tree`, `grep`, plus the six LSP
  navigation tools. Local, side-effect-free.
- **Net tools** — `web_search`, `fetch_url`. Not workspace-mutating, but a
  distinct exfiltration boundary: grant deliberately per role, never bundle
  into "read".
- **Gated tools** — `write_file`, `edit_file`, `bash`. Mutate the workspace;
  a sub-agent only receives them in `auto` mode (where they run unprompted).

Memory, skills, tasks, and forge tools are main-agent only — a sub-agent's
job is its task, not the session's bookkeeping.

`build()` validates every grant up front:

- An unknown tool name fails `build()`.
- A tool from a group you didn't enable fails `build()` — including LSP
  names without `with_lsp(tools=True)` (the error message hints at the fix).
  This is deliberate: the alternative was silently dropping the tool at
  spawn time, leaving the sub-agent missing a tool its own spec promised
  with no error anywhere.

Reach is decided **up front** by which names are granted — sub-agent tools
are registered plain (no mid-run approval prompting inside a sub-agent).

## Nesting and the depth ceiling

`SUBAGENT_MAX_DEPTH = 3`. The main agent is depth 0, its spawns depth 1,
grandchildren depth 2. `spawn_agent` is granted to a sub-agent only when a
child could still fit under the ceiling (`depth + 1 < 3`); at the leaf depth
the tool is simply absent. Nesting is bounded, not forbidden.

## Runtime behavior

`spawn_agent` accepts the spec name, the task text, and optional knobs
(model override, MCP server grants, output budget, worktree isolation).
Each spawn gets fresh context — it inherits nothing from the parent's
conversation except what the parent puts in the task text. Sub-agent
progress is observable via the `bind_ui` sub-agent callbacks if you're
building an interactive front-end; headless embedders just get the final
report as the tool result.
