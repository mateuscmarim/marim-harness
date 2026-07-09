# Integrations

MCP servers, LSP, forge (Gitea/GitHub), lifecycle hooks, and the bash
command policy.

## MCP servers

`with_mcp_server(server)` attaches a **ready pydantic-ai MCP server/toolset
object** — the SDK does not parse marim's JSON spec format (`.marim/mcp.json`
is a CLI concern; bootstrap converts specs before reaching the builder):

```python
from pydantic_ai.mcp import MCPServerStdio

builder.with_mcp_server(MCPServerStdio("npx", args=["-y", "some-mcp-server"]))
```

(`MCPServerStdio`/`MCPServerSSE`/`MCPServerStreamableHTTP` are deprecated in
pydantic-ai 2.x in favor of `MCPToolset`, but marim's own MCP config layer
still builds them — see `mcp/config.py` — so they're the tested path here
too.)

Servers connect lazily after `build()`. Two consequences:

- MCP tool names are not part of the custom-tool collision check — a clash
  surfaces at connect/run time, not `build()`.
- A server that launches a subprocess launches it when it connects; attach
  only servers you trust.

## LSP

`with_lsp(enabled=True, tools=True)` controls two independent switches:

- **The manager** (`enabled`) — a multilspy-backed language-server pool.
  With it on, diagnostics are appended to `write_file`/`edit_file` results
  best-effort, so the model sees type errors it just introduced.
- **The navigation tools** (`tools`) — `goto_definition`, `find_references`,
  `hover`, `document_symbols`, `workspace_symbols`, `diagnostics`. Only
  meaningful when the manager is on; `enabled=False` folds tools off too.

`with_lsp(enabled=False)` is the explicit off-switch after
`with_defaults()`. The tools' availability also determines whether LSP names
are grantable to [sub-agents](subagents.md) and count in the custom-tool
collision check.

## Forge

`with_forge(backend)` attaches five forge-agnostic PR tools — `list_prs`,
`view_pr`, `ci_status` (read-only) and `create_pr`, `checkout_pr` (gated for
approval) — against an explicit `ForgeBackend` implementation (a Protocol;
see `forge/backend.py`). The shipped backend is `TeaBackend`, which shells
out to the `tea` CLI for Gitea. Passing the backend explicitly bypasses the
CLI's `tea`-on-`PATH` auto-detection:

```python
from marim_harness.forge.tea_backend import TeaBackend

builder.with_forge(TeaBackend())
```

A custom tool named like a forge tool fails `build()` when `with_forge` is
on.

## Lifecycle hooks

`with_hooks(runner)` attaches a `HookRunner` — the Claude-Code-compatible
hook engine (session/prompt/tool/compaction events). Hooks are observe-only
except SessionStart/UserPromptSubmit, which may inject context into the
prompt assembly.

```python
from marim_harness.hooks import HookRunner

builder.with_hooks(HookRunner(hook_config))
```

Constraint: `with_hooks` cannot be combined with `with_deps` — the runner is
wired onto the builder-constructed `Deps`, so when you supply your own
`Deps` the combination is a `build()` error. Set `deps.hooks` on your own
`Deps` instead.

## Command policy (bash)

`with_bash(policy)` takes a `CommandPolicy` — regex-based deny/allow rules
checked before every `bash` execution, on top of (not instead of) the
mode-based gating:

```python
from marim_harness import CommandPolicy

policy = CommandPolicy(
    denylist=[r"rm\s+-rf", r"git\s+push"],
    allowlist=[],           # empty allowlist = everything not denied is allowed
)
builder.with_bash(policy)
```

Semantics (`command_policy.py`):

- Deny patterns are checked first; a match blocks with
  `command matches denylist pattern '...'`.
- If the allowlist is non-empty, a command must match at least one allow
  pattern or it is blocked. An empty allowlist allows everything not denied.
- `CommandPolicy.parse(deny=..., allow=...)` builds one from raw
  comma-/newline-separated config strings.

Remember this is policy, not a sandbox — `plan` mode's read-only bash
filtering is likewise best-effort classification. For hard isolation, run
the whole process in a container.
