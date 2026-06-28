# Nested Sub-Agents with Depth Limit — Design Spec

## Goal

Allow sub-agents to spawn child sub-agents (grandchildren), with a hard depth limit of 3 levels: main agent (depth 0) → sub-agent (depth 1) → grandchild (depth 2). Depth-2 agents are leaves and cannot spawn further.

## Background

Today, `spawn_agent` is registered only on the main agent. `register_subagent()` explicitly excludes it — the comment at `tools/provider.py:777` says "spawn_agent is never among them, so sub-agents can't recurse." This change removes that restriction in a controlled, depth-limited way.

## Depth Model

| Depth | Role | Can spawn? | Produced by |
|-------|------|-----------|-------------|
| 0 | Main agent | ✅ produces depth-1 | — |
| 1 | Sub-agent | ✅ produces depth-2 | main agent |
| 2 | Grandchild | ❌ leaf | sub-agent |

Max depth = 3 (hardcoded constant `SUBAGENT_MAX_DEPTH = 3`). A spawn is refused when `resulting_depth >= max_depth`.

## Architecture

### 1. SubagentRunner gains max_depth

`SubagentRunner.__init__` gains a `max_depth: int = 3` parameter. This is the depth ceiling — spawns that would produce a sub-agent at `depth >= max_depth` are refused.

### 2. build() tracks depth

`SubagentRunner.build()` gains a `depth: int = 0` parameter indicating the depth of the sub-agent being built. When `depth + 1 >= max_depth`, the resulting sub-agent is a leaf — `spawn_agent` is not registered on it.

### 3. Conditional spawn_agent registration

`register_subagent()` (in `tools/provider.py`) currently registers a fixed set of tools. After this change, it conditionally includes `spawn_agent` when the sub-agent's depth allows further nesting:

```python
# In register_subagent, after registering the standard tools:
if depth + 1 < max_depth:
    agent.tool(spawn_agent)
```

This means:
- Depth-1 sub-agents (1+1=2 < 3) → get `spawn_agent`
- Depth-2 grandchildren (2+1=3 ≥ 3) → do NOT get `spawn_agent`

### 4. spawn_agent tool gains max_depth parameter

The tool signature gains `max_depth: int | None = None`:

```python
async def spawn_agent(
    ctx: RunContext[Deps],
    type: str,
    task: str,
    *,
    max_depth: int | None = None,
    ...
) -> str:
```

- When `None` (main agent registration), defaults to `SUBAGENT_MAX_DEPTH` (3) inside the tool body.
- When registered on a sub-agent, the runner binds `max_depth` via `functools.partial`.

The tool checks:
```python
effective_max = max_depth if max_depth is not None else SUBAGENT_MAX_DEPTH
if ctx.deps.subagent_depth + 1 >= effective_max:
    return f"Cannot spawn: at depth {ctx.deps.subagent_depth}, max depth is {effective_max}."
```

### 4a. Binding max_depth on sub-agents

When `SubagentRunner.build()` registers `spawn_agent` on a sub-agent (because depth+1 < max_depth), it binds the remaining depth via `functools.partial`:

```python
from functools import partial

# In build(), when registering spawn_agent on the sub-agent:
child_spawn = partial(spawn_agent, max_depth=self._max_depth - 1)
child_spawn.__name__ = "spawn_agent"  # preserve tool name
agent.tool(child_spawn)
```

This way the tool's signature stays clean (same function, same name) while the max_depth is pre-filled. The sub-agent's `spawn_agent` call doesn't need to pass `max_depth` — it's already bound.

### 5. Deps gains subagent_depth

A new field `subagent_depth: int = 0` on `Deps`. The main agent runs at depth 0. When `SubagentRunner.build()` creates a sub-agent, it produces `deps.replace(subagent_depth=depth)` so the sub-agent knows its depth.

### 6. Depth surfaced in tool description

The `spawn_agent` tool's docstring includes remaining depth info so the model can reason about nesting:

```
"You may spawn sub-agents (N levels remaining)."
```

Where N = `max_depth - current_depth - 1`.

## Files Touched

| File | Change |
|------|--------|
| `src/marim_harness/runtime/deps.py` | Add `subagent_depth: int = 0` to `Deps` |
| `src/marim_harness/subagents/runner.py` | `max_depth` param on runner; `depth` param on `build()`; conditional `spawn_agent` registration |
| `src/marim_harness/tools/provider.py` | `spawn_agent` gains `max_depth` param, depth check, dynamic description; main-agent registration binds `max_depth=3` via `partial` |
| `src/marim_harness/runtime/harness.py` | Pass `max_depth` to `SubagentRunner` |

## Files NOT Touched

| File | Why unchanged |
|------|--------------|
| `tools/names.py` | `spawn_agent` is not in `SUBAGENT_FNS` — it's registered conditionally by the runner, not by the tool name sets |
| `runtime/bootstrap.py` | No new config knobs (max_depth is hardcoded) |
| `runtime/permissions.py` | No mode changes needed |
| `session/` | No session-level changes needed |

## What Stays the Same

- Tool reach (read + gated in auto mode) is identical at every depth
- Worktree isolation, MCP grants, hooks — unchanged
- Background spawns — unchanged (background spawns are still main-agent-only)
- CLI backend spawns — unchanged (external processes can't nest)
- `SUBAGENT_TOOLS` / `SUBAGENT_FNS` — unchanged (spawn_agent is registered outside this system)

## Edge Cases

1. **Background spawns from sub-agents:** Not supported. `background=True` on `spawn_agent` still routes through `run_background_agent`, which is only wired on the main harness. Sub-agents don't have it. This is fine — background nesting is a different feature.
2. **Isolated (worktree) grandchildren:** Worked — `_open_worktree` is called per-spawn regardless of depth.
3. **MCP grants on grandchildren:** Worked — `mcp.granted_servers()` is called per-spawn.
4. **Hooks for grandchildren:** Worked — `subagent_start`/`subagent_stop` fire per-spawn.
5. **Model override on nested spawns:** Worked — `build()` still accepts `model` override.
6. **Concurrency cap:** Shared across all depths (one semaphore per runner).

## Testing Plan

1. **Unit: depth tracking** — `SubagentRunner(max_depth=3).build(depth=1)` registers `spawn_agent`; `build(depth=2)` does not.
2. **Unit: tool guard** — `spawn_agent` refuses when `subagent_depth + 1 >= max_depth`.
3. **Unit: Deps propagation** — `deps.replace(subagent_depth=N)` flows through `build()`.
4. **Integration: 3-level chain** — Main → sub → grandchild completes; grandchild's `spawn_agent` is absent.
5. **Unit: tool description** — Depth info appears in registered tool description.
6. **Regression: existing sub-agent tests** — All current tests pass unchanged (depth=0, max_depth=3 → same behavior as today).
