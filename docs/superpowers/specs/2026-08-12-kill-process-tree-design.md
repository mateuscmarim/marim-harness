# Design: `kill_process_tree` — deep descendant cleanup for subprocess kills

## Problem

When marim kills a spawned `claude` CLI process (via `_kill_process_group` in
`cli_backend.py`), the MCP servers that `claude` launched survive. Each MCP
server is in its own process group (the `mcp` library's `stdio_client` uses
`start_new_session=True`), so `os.killpg` on the parent's group doesn't reach
them. The orphaned servers are reparented to init/zsh and keep running,
consuming ~60 MB each (uv + python3 pair).

The same pattern affects `tools/impl/shell.py`'s `_kill_group` — any subprocess
tree with deep grandchildren (e.g. `bash → make → cc`) can leave orphans.

## Root cause

```
marim (PID 1857)
└─ claude -p (PGID = claude PID, start_new_session=True)
   └─ zsh (reparented after claude dies)
      └─ uv run twm-action-items-mcp (PGID = uv PID, start_new_session=True)
         └─ python3 twm-action-items-mcp (PGID = python PID)
```

`os.killpg(os.getpgid(claude_pid), SIGKILL)` kills only the process group
containing `claude` itself. The MCP servers are in independent process groups
and survive.

## Approach: Walk process tree before kill

Build a PPID→children map from `/proc`, find all descendants of the target PID,
collect their PGIDs, then SIGKILL every unique group. Enumeration happens
*before* any kill so the tree is still intact.

## New module: `tools/impl/process.py`

### `kill_process_tree(pid: int) -> None`

1. **Build PPID map** — single O(n) scan of `/proc/<pid>/stat` for every
   numeric entry. Extract `pid` (field 0) and `ppid` (field 3). ~200-500
   entries on a typical system, <1ms.

2. **Collect descendants** — BFS from the target `pid` through the PPID map.
   Produces a `set[int]` of all descendant PIDs.

3. **Group by PGID** — for each descendant, call `os.getpgid(pid)`. Group into
   `{pgid: [pids]}`. `ProcessLookupError` (already-exited race) is caught and
   skipped.

4. **Kill each group** — `os.killpg(pgid, SIGKILL)` for each unique PGID.
   The top process's own group is killed last as a safety net.

5. **Fallback** — if `os.killpg` fails for a group, fall back to individual
   `os.kill(pid, SIGKILL)` per PID.

### Error handling

| Failure | Response |
|---------|----------|
| `/proc` unreadable | Fall back to plain `os.killpg` on the top process |
| `ProcessLookupError` on `os.getpgid` | Skip the exited process |
| `PermissionError` on `os.killpg` | Log at debug, continue |
| `ProcessLookupError` on `os.killpg` | All members already dead — no-op |

## Changes to existing code

### `subagents/cli_backend.py`

`_kill_process_group` body replaced:

```python
def _kill_process_group(proc) -> None:
    if proc.returncode is not None:
        return
    from ..tools.impl.process import kill_process_tree
    kill_process_tree(proc.pid)
```

### `tools/impl/shell.py`

`_kill_group` body replaced with the same call. Signature and semantics unchanged.

## Testing

`tests/test_process.py`:

- Fork a tree of `sleep` processes (parent → child → grandchild), each in its
  own process group via `os.setsid()`.
- Call `kill_process_tree` on the root.
- Assert all descendants are gone (`os.waitpid` with `WNOHANG` returns the PID).
- Edge case: call on an already-dead PID (no crash).
