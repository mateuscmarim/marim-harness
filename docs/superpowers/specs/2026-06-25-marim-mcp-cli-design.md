# `marim mcp` CLI — Design

**Date:** 2026-06-25
**Status:** Approved (brainstorm), pending implementation plan

## Goal

Add a `marim mcp` CLI subcommand that lets users add, list, inspect, and remove
MCP servers from the command line — matching the `claude mcp add` flag syntax so
documentation and muscle memory transfer 1:1. Today MCP servers can only be
configured by hand-editing `~/.config/marim/mcp.json` (global) or
`.marim/mcp.json` (project). There is no write path from the CLI; only
`persist_server_enabled` exists, and it only flips the `enabled` flag.

## Non-goals

- `enable`/`disable` subcommands. Not part of claude's surface; `persist_server_enabled`
  already exists if we want them later.
- `add-json` / `add-from-claude-desktop` import commands. Out of scope for v1.
- Any change to how MCP config is *loaded* or to the on-disk schema.

## CLI surface

New management keyword `mcp` added to `_MANAGEMENT` in
`src/marim_harness/interfaces/cli/router.py:13`, plus a new module
`src/marim_harness/interfaces/cli/mcp.py` exposing
`main(argv, *, out=sys.stdout, err=sys.stderr) -> int`, mirroring the shape of
`cli/config.py`.

```
marim mcp add <name> [cmd [args...]]              # stdio (default transport)
marim mcp add --transport http|sse <name> <url>   # remote
    -H, --header   "Name: Value"   (repeatable)
    -e, --env      KEY=value       (repeatable; stdio only)
    --transport    stdio|http|sse  (default: stdio)
    --scope        user|project    (default: project)
    --trust                        (marim-specific: bypass tool approval)
    --                             (separator; stdio args containing dashes pass through)
marim mcp list                     # both scopes; marks source + enabled/disabled
marim mcp get <name>               # full detail of one server
marim mcp remove <name> [--scope user|project]
```

### Argument interpretation

- **stdio** (default): the first positional after `<name>` is `command`; the rest
  (or everything after `--`) are `args`. `-e/--env KEY=value` entries become the
  `env` map. `-H/--header` is rejected for stdio.
- **http**: positional after `<name>` is `url`; `type` is omitted in the written
  JSON (http is the schema default). `-H/--header "Name: Value"` entries become the
  `headers` map. `-e/--env` is rejected for remote transports.
- **sse**: same as http but the written JSON sets `"type": "sse"`.
- `--trust` writes `"trust": true` (default false). Servers are written with
  `enabled` defaulting to true (field omitted, matching current default behavior).

### Scope → path mapping

| `--scope` | File | Helper |
|-----------|------|--------|
| `user`    | `~/.config/marim/mcp.json` | `global_mcp_config_path()` |
| `project` | `<workspace>/.marim/mcp.json` | `project_mcp_config_path(workspace_root)` |

Default scope is `project`. `add`/`remove`/`get`/`list` reuse the existing path
helpers — no new file locations are introduced. The workspace root is resolved
the same way `plugin.py` does (`-C/--workspace`, else cwd).

## Persistence helpers (`src/marim_harness/mcp/config.py`)

Three new functions alongside the existing `persist_server_enabled`:

- `add_server(path: Path, name: str, spec: dict, *, overwrite: bool = False) -> bool`
  Read-or-initialize `{"mcpServers": {}}`; if `name` already exists and not
  `overwrite`, return `False` (caller reports the duplicate). Otherwise merge and
  `atomic_write_text(path, json.dumps(data, indent=2) + "\n")`. Returns `True` on write.
- `remove_server(path: Path, name: str) -> bool`
  Delete the key and write back atomically; return `False` if the server was absent.
- `read_servers_with_source(workspace_root: Path) -> dict[str, tuple[dict, str]]`
  For `list`/`get`: each server tagged `"user"` or `"project"`, with `project`
  winning on name clash (matching `load_mcp_config` precedence). Plugin-provided
  servers are out of scope for listing here — only the two user-editable files.

All writes go through `atomic_write_text` (`src/marim_harness/atomic_io.py:93`),
consistent with `persist_server_enabled`.

## Validation & behavior

- **Validate before write.** Exactly one of `command` / `url` must be present;
  `type` ∈ {unset, `"sse"`}; `-e` parsed as `KEY=value`, `-H` parsed as
  `Name: Value`, else a usage error. This mirrors what `build_mcp_servers`
  tolerates at load time, but fails *loudly* at add time instead of silently
  skipping a malformed entry during bootstrap.
- **Project-scope trust warning.** Project `mcp.json` is only loaded when
  `trust_project_hooks` is enabled. After a successful `add --scope project`,
  print a note to stderr: *"Added to .marim/mcp.json — only loaded when project
  trust is enabled."* This is the single real footgun and is worth surfacing.
- **Exit codes** (same convention as `cli/config.py`): `0` success; `1` runtime
  error (duplicate name on add, unknown name on get/remove); `2` argparse usage
  error. Unknown name on `get`/`remove` prints a clear message to stderr.
- `list` with no servers prints a friendly empty-state line, exit `0`.

## Testing

Follow the `test_bootstrap.py` pattern — write to a tmp `mcp.json`, call helpers
and `cli/mcp.py:main(argv, out=, err=)` with `StringIO` buffers, and assert both
the on-disk JSON and the captured stdout/stderr.

Cases:
- stdio add (command + args + env) writes expected JSON
- http add with `-H` header writes `url` + `headers`, no `type`
- sse add writes `"type": "sse"`
- duplicate-name add rejected (exit 1), file unchanged
- `--trust` writes `"trust": true`
- `-H` on stdio / `-e` on remote → usage error (exit 2)
- malformed `-e` / `-H` value → usage error
- remove present (True) and absent (exit 1) cases
- `list` across both scopes shows source tags and project-wins precedence
- `get` known vs unknown name
- round-trip: `add` then `load_mcp_config(workspace, trust_project=True)` sees the server
- project-scope add emits the trust warning to stderr

## Files touched

- `src/marim_harness/interfaces/cli/router.py` — add `"mcp"` to `_MANAGEMENT`
- `src/marim_harness/interfaces/cli/mcp.py` — new module (parser + `_cmd_*` handlers)
- `src/marim_harness/mcp/config.py` — `add_server`, `remove_server`, `read_servers_with_source`
- `tests/` — new `test_cli_mcp.py` (CLI + helper coverage)
