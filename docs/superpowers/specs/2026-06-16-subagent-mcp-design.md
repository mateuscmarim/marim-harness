# Sub-Agent MCP Access — Design

**Date:** 2026-06-16
**Status:** Approved (design)

## Problem

Sub-agents spawned via `spawn_agent` currently get **zero** MCP tools. The main
agent reaches MCP servers (passed as per-run `toolsets=` at `agent.py:565`), but
`_run_subagent` / `_run_background_subagent` call `sub.run(...)` with no
`toolsets`, so a delegated agent can never use an MCP server.

This blocks the most natural delegation: pointing a generic `explore` agent at an
MCP-backed source — "go read the docs in the `mddocs` server," "go look at what's
in this database." The built-in `explore` / `general` agents have no definition
file, so a static (frontmatter) grant cannot reach them at all.

## Goal

Let the **main agent assign**, per spawn, which MCP servers a sub-agent may use.
Least authority by default (none), reusing the existing approval machinery so a
granted sub-agent's MCP calls gate exactly like the main agent's.

## Decisions

- **Dynamic-only.** The main agent names servers at spawn time via a `spawn_agent`
  argument. No frontmatter `mcp:` field. One mechanism, identical for built-in and
  authored agents. (A frontmatter "ceiling" is a future "lock this role down"
  feature — YAGNI now, addable without breaking this API.)
- **Server-level grants.** Naming a server grants all of its tools, matching the
  `/mcp enable/disable <name>` granularity. (Tool-level filtering is YAGNI.)
- **Default none.** `spawn_agent` with no `mcp` argument grants no MCP tools —
  unchanged behavior for every existing call site.
- **Reuse live server objects.** A grant is a filtered sub-list of
  `self._live_servers`, handed to `sub.run(..., toolsets=...)`. No new permission
  code.

## Why gating comes for free

Each MCP server object is built in `mcp.py:build_mcp_servers` with a
`process_tool_call` hook from `make_approval_hook` (`mcp.py:109`). That hook reads
`mode` and `request_approval` from `ctx.deps` **at call time**:

- `plan` → deny (read-only),
- `auto` → run,
- `ask` → run if the server is `trust`-ed, else prompt via `deps.request_approval`
  (the prompt already names the server, e.g. `mddocs_search_docs`).

Sub-agents already run with `deps=self.deps` (`agent.py:443`, `agent.py:465`). So
passing the *same hooked server objects* as `toolsets` to a sub-agent's `run`
means its MCP calls gate identically to the main agent's — no bypass, no new code,
runtime mode switches honored. This is the core simplification of the design.

## API

`spawn_agent` (`tools/provider.py:132`) gains one optional parameter:

```python
async def spawn_agent(
    ctx: RunContext[Deps],
    type: str,
    task: str,
    background: bool = False,
    mcp: list[str] | None = None,
) -> str:
```

- `mcp=None` (default) → no MCP access.
- `mcp=["mddocs"]` → the sub-agent gets exactly the `mddocs` server.

The argument flows through the two deps-callbacks:

- `deps.run_subagent(type, task, tool_call_id, mcp_names)`
- `deps.run_background_agent(type, task, mcp_names)`

into `Harness._run_subagent` / `Harness._run_background_subagent`, which resolve
names to live servers and pass them as `toolsets`.

## Data flow

```
spawn_agent(mcp=["mddocs"])
  └─ ctx.deps.run_subagent(type, task, tool_call_id, ["mddocs"])
       └─ Harness._run_subagent(type, task, tool_call_id, mcp_names=["mddocs"])
            granted, unknown = self._granted_servers(["mddocs"])
            result = await sub.run(task, deps=self.deps, toolsets=granted, event_stream_handler=...)
            # prepend the unknown-names note to the report when `unknown` is non-empty
```

Resolution helper (new, on `Harness`):

```python
def _granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]:
    """Resolve requested MCP server names to live server objects.

    Returns (granted_servers, unknown_names). A name is unknown if no live
    server matches it or the server is currently disabled."""
    if not names:
        return [], []
    requested = list(dict.fromkeys(names))  # de-dupe, preserve order
    by_name = {self._server_name(s): s for s in self._live_servers}
    granted, unknown = [], []
    for name in requested:
        server = by_name.get(name)
        if server is None or name in self.disabled:
            unknown.append(name)
        else:
            granted.append(server)
    return granted, unknown
```

A disabled server is treated as unavailable (it isn't in `_live_servers` anyway
once disabled at connect time, but the `self.disabled` check also covers a server
disabled live).

## Error handling

Unknown or disabled names are **dropped** from the grant — never fatal — and
surfaced to the model as a one-line note prepended to the report so it can
self-correct:

```
(note: ignored unknown MCP server 'postgres'; enabled: mddocs, sentry)
```

The note is built only when `unknown` is non-empty. For background spawns the same
note is folded into the job-start string.

## Discoverability

The `spawn_agent` instructions already list sub-agent *types*. Add a line listing
the currently-**enabled** MCP server names (reusing the same enabled-set logic as
the main turn at `agent.py:565`: live servers whose name is not in
`self.disabled`), so the model knows what it can grant. When no servers are
enabled, the line is omitted.

This lives in the instruction-building closure in `Harness.__init__`
(`agent.py`, alongside the existing sub-agents-index line), so it reflects the
live enabled set each turn.

## Files

- **Modify** `src/marim_harness/tools/provider.py` — add `mcp` param to
  `spawn_agent`; thread to both deps-callbacks; update docstring.
- **Modify** `src/marim_harness/deps.py` — widen `run_subagent` /
  `run_background_agent` callback signatures to carry `mcp_names`.
- **Modify** `src/marim_harness/agent.py` — add `_granted_servers`; pass
  `toolsets=granted` in `_run_subagent` / `_run_background_subagent`; prepend the
  unknown-names note; add the enabled-servers line to spawn instructions.
- **Test** `tests/test_agent.py` (and/or `tests/test_provider.py`) — see below.

## Testing

1. **Default is none.** `spawn_agent` without `mcp` → `_run_subagent` passes
   `toolsets=[]` (or omits MCP servers) to `sub.run`. Regression guard.
2. **Named server resolves.** With a live server named `mddocs`,
   `mcp=["mddocs"]` → exactly that server object appears in the `toolsets` passed
   to `run`.
3. **Unknown name dropped + noted.** `mcp=["nope"]` → empty grant and the report
   carries the `(note: ignored unknown MCP server 'nope'; ...)` line.
4. **Disabled name dropped.** A server in `self.disabled` is excluded from the
   grant and reported unknown.
5. **De-dupe.** `mcp=["mddocs", "mddocs"]` grants the server once.
6. **Background path.** `_run_background_subagent` grants identically and folds the
   note into the job-start string.
7. **Gating preserved.** The granted server passed to a sub-agent is the same
   hooked object from `build_mcp_servers` — assert identity (the hook itself is
   already unit-tested in `tests/test_mcp.py`; here we only assert the wiring
   forwards the hooked server, not a copy).

`_live_servers` is populated by `connect()`; tests construct a `Harness` with
stub/live servers via the existing test fixtures and call the spawn paths directly,
asserting on the `toolsets` argument captured from a patched `sub.run`.

## Out of scope (YAGNI)

- Frontmatter `mcp:` ceilings on authored agents.
- Tool-level (sub-server) grants.
- Per-sub-agent `trust` overrides — a granted server keeps its configured trust.
- Letting sub-agents enable/disable servers or spawn further sub-agents (the
  latter is already disallowed).
