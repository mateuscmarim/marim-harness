# `marim mcp` CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `marim mcp` CLI subcommand (`add`/`list`/`get`/`remove`) that reads and writes `mcp.json`, mirroring the `claude mcp add` flag syntax.

**Architecture:** Three new persistence helpers in `mcp/config.py` (pure file I/O over the existing schema, atomic writes), a new `interfaces/cli/mcp.py` command module mirroring `cli/config.py`, and a one-line keyword registration in `cli/router.py`. The CLI parses claude-compatible flags, builds a server spec dict, and persists it via the helpers.

**Tech Stack:** Python ≥3.10, argparse, the existing `atomic_write_text` writer, pytest with `tmp_path`/`monkeypatch`.

## Global Constraints

- `requires-python >=3.10` — no 3.11+-only syntax (e.g. no `tomllib`, no `X | Y` only where a 3.10 runtime would reject; `dict[str, tuple[dict, str]]` annotations are fine as they're PEP 585 and 3.10-OK).
- Ruff line length **100**; lint set `E,F,I` (import sorting enforced). Run `uv run ruff check src tests`.
- Type-check with `uv run pyright` (basic mode, src only).
- All commands via `uv` (`uv run pytest`, never bare `pytest`/`pip`).
- CI order is ruff → pyright → pytest; match it locally before claiming done.
- All config writes go through `atomic_write_text(path, json.dumps(data, indent=2) + "\n")` — never a bare `write_text` (a crash mid-write must not truncate the user's `mcp.json`).
- MCP server schema (existing, do not change): stdio = `{"command": str, "args": [str], "env": {str: str}, "cwd": str}`; http = `{"url": str, "headers": {str: str}}` (no `type`); sse = same plus `"type": "sse"`. Optional on any: `"trust": bool`, `"enabled": bool`.

---

### Task 1: Persistence helpers in `mcp/config.py`

**Files:**
- Modify: `src/marim_harness/mcp/config.py` (add three functions after `persist_server_enabled`, ~line 174)
- Test: `tests/test_mcp_cli.py` (new)

**Interfaces:**
- Consumes: existing `global_mcp_config_path()`, `project_mcp_config_path(workspace_root)`, `_read_servers(path)`, `atomic_write_text` (all already imported in the module).
- Produces:
  - `add_server(path: Path, name: str, spec: dict, *, overwrite: bool = False) -> bool`
  - `remove_server(path: Path, name: str) -> bool`
  - `read_servers_with_source(workspace_root: Path) -> dict[str, tuple[dict, str]]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_cli.py`:

```python
import json
from pathlib import Path

from marim_harness.mcp import config as mcp_config


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_add_server_creates_file(tmp_path):
    path = tmp_path / "sub" / "mcp.json"
    ok = mcp_config.add_server(path, "web", {"url": "https://x/mcp"})
    assert ok is True
    assert _read(path) == {"web": {"url": "https://x/mcp"}}
    # trailing newline + 2-space indent (matches persist_server_enabled output)
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_add_server_rejects_duplicate(tmp_path):
    path = tmp_path / "mcp.json"
    assert mcp_config.add_server(path, "web", {"url": "https://x/mcp"}) is True
    assert mcp_config.add_server(path, "web", {"url": "https://y/mcp"}) is False
    assert _read(path) == {"web": {"url": "https://x/mcp"}}  # unchanged


def test_add_server_overwrite(tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.add_server(path, "web", {"url": "https://x/mcp"})
    assert mcp_config.add_server(path, "web", {"url": "https://y/mcp"}, overwrite=True) is True
    assert _read(path) == {"web": {"url": "https://y/mcp"}}


def test_add_server_preserves_existing_servers(tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.add_server(path, "a", {"command": "x"})
    mcp_config.add_server(path, "b", {"command": "y"})
    assert set(_read(path)) == {"a", "b"}


def test_add_server_tolerates_malformed_file(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("not json", encoding="utf-8")
    assert mcp_config.add_server(path, "web", {"url": "https://x/mcp"}) is True
    assert _read(path) == {"web": {"url": "https://x/mcp"}}


def test_remove_server_present_and_absent(tmp_path):
    path = tmp_path / "mcp.json"
    mcp_config.add_server(path, "a", {"command": "x"})
    mcp_config.add_server(path, "b", {"command": "y"})
    assert mcp_config.remove_server(path, "a") is True
    assert set(_read(path)) == {"b"}
    assert mcp_config.remove_server(path, "missing") is False


def test_remove_server_missing_file(tmp_path):
    assert mcp_config.remove_server(tmp_path / "nope.json", "a") is False


def test_read_servers_with_source_project_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    mcp_config.add_server(mcp_config.global_mcp_config_path(), "g", {"command": "x"})
    mcp_config.add_server(mcp_config.global_mcp_config_path(), "shared", {"command": "global"})
    mcp_config.add_server(mcp_config.project_mcp_config_path(ws), "p", {"command": "y"})
    mcp_config.add_server(mcp_config.project_mcp_config_path(ws), "shared", {"command": "proj"})
    result = mcp_config.read_servers_with_source(ws)
    assert result["g"] == ({"command": "x"}, "user")
    assert result["p"] == ({"command": "y"}, "project")
    assert result["shared"] == ({"command": "proj"}, "project")  # project wins
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_mcp_cli.py -q`
Expected: FAIL — `AttributeError: module 'marim_harness.mcp.config' has no attribute 'add_server'`.

- [ ] **Step 3: Implement the three helpers**

Append to `src/marim_harness/mcp/config.py` immediately after `persist_server_enabled` (after line 174):

```python
def add_server(path: Path, name: str, spec: dict, *, overwrite: bool = False) -> bool:
    """Write ``spec`` under ``name`` into the ``mcpServers`` map at ``path``,
    creating the file (and parent dir) if absent. A missing or malformed file is
    treated as empty. Returns False without writing if ``name`` already exists and
    ``overwrite`` is not set; otherwise writes atomically and returns True."""
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if name in servers and not overwrite:
        return False
    servers[name] = spec
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return True


def remove_server(path: Path, name: str) -> bool:
    """Delete ``name`` from the ``mcpServers`` map at ``path`` and rewrite
    atomically. Returns False if the file is missing/malformed or has no such
    server (nothing removed)."""
    if name not in _read_servers(path):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["mcpServers"][name]
        atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError, TypeError):
        return False
    return True


def read_servers_with_source(workspace_root: Path) -> dict[str, tuple[dict, str]]:
    """Map each user-editable server to ``(spec, source)`` where ``source`` is
    ``"user"`` (global file) or ``"project"`` (``.marim/mcp.json``). Project wins
    on a name clash, matching ``load_mcp_config`` precedence. Plugin-provided
    servers are not included — only the two files the CLI can edit."""
    result: dict[str, tuple[dict, str]] = {}
    for spec_name, spec in _read_servers(global_mcp_config_path()).items():
        result[spec_name] = (spec, "user")
    for spec_name, spec in _read_servers(project_mcp_config_path(workspace_root)).items():
        result[spec_name] = (spec, "project")
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_mcp_cli.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src tests && uv run pyright src/marim_harness/mcp/config.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/mcp/config.py tests/test_mcp_cli.py
git commit -m "feat(mcp): add/remove/list persistence helpers for mcp.json"
```

---

### Task 2: Spec-building + validation in `cli/mcp.py`

**Files:**
- Create: `src/marim_harness/interfaces/cli/mcp.py`
- Test: `tests/test_mcp_cli.py` (extend)

**Interfaces:**
- Consumes: nothing from Task 1 yet (pure functions).
- Produces:
  - `class SpecError(ValueError)` — raised with a user-facing message on bad input.
  - `_parse_pairs(items: list[str], sep: str, what: str) -> dict[str, str]` — splits `["K=V", ...]` (sep `"="`) or `["Name: Value", ...]` (sep `":"`); raises `SpecError` on a token missing the separator.
  - `_build_spec(*, transport: str, rest: list[str], headers: list[str], envs: list[str], trust: bool) -> dict` — returns the server spec dict; raises `SpecError` on invalid combinations. `rest` is the positional remainder ([command, *args] for stdio; [url] for http/sse).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_cli.py`:

```python
import pytest

from marim_harness.interfaces.cli import mcp as mcp_cmd


def test_build_spec_stdio():
    spec = mcp_cmd._build_spec(
        transport="stdio", rest=["node", "x.js", "--port"],
        headers=[], envs=["A=1", "B=2"], trust=False,
    )
    assert spec == {"command": "node", "args": ["x.js", "--port"], "env": {"A": "1", "B": "2"}}


def test_build_spec_stdio_minimal():
    spec = mcp_cmd._build_spec(
        transport="stdio", rest=["mddocs-mcp"], headers=[], envs=[], trust=False,
    )
    assert spec == {"command": "mddocs-mcp"}


def test_build_spec_http_with_header_and_trust():
    spec = mcp_cmd._build_spec(
        transport="http", rest=["https://x/mcp"],
        headers=["Authorization: Bearer t"], envs=[], trust=True,
    )
    assert spec == {"url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}, "trust": True}


def test_build_spec_sse_sets_type():
    spec = mcp_cmd._build_spec(
        transport="sse", rest=["https://x/sse"], headers=[], envs=[], trust=False,
    )
    assert spec == {"url": "https://x/sse", "type": "sse"}


def test_build_spec_rejects_header_on_stdio():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(
            transport="stdio", rest=["node"], headers=["A: b"], envs=[], trust=False,
        )


def test_build_spec_rejects_env_on_http():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(
            transport="http", rest=["https://x/mcp"], headers=[], envs=["A=1"], trust=False,
        )


def test_build_spec_rejects_empty_rest():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(transport="stdio", rest=[], headers=[], envs=[], trust=False)


def test_build_spec_rejects_extra_url_positionals():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._build_spec(
            transport="http", rest=["https://x/mcp", "junk"], headers=[], envs=[], trust=False,
        )


def test_parse_pairs_bad_token():
    with pytest.raises(mcp_cmd.SpecError):
        mcp_cmd._parse_pairs(["noequals"], "=", "env")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_mcp_cli.py -k "build_spec or parse_pairs" -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.cli.mcp'`.

- [ ] **Step 3: Create the module with the pure helpers**

Create `src/marim_harness/interfaces/cli/mcp.py`:

```python
"""``marim mcp ...`` — add, list, inspect, and remove MCP servers in mcp.json.

Mirrors the ``claude mcp add`` flag surface so docs and muscle memory transfer:
``marim mcp add <name> <command> [args...]`` for stdio, and
``marim mcp add --transport http|sse <name> <url> -H "K: V"`` for remote servers.
"""

import argparse
import json
import sys
from pathlib import Path

from ...mcp.config import (
    add_server,
    global_mcp_config_path,
    project_mcp_config_path,
    read_servers_with_source,
    remove_server,
)


class SpecError(ValueError):
    """A user-facing validation failure while building a server spec."""


def _parse_pairs(items: list[str], sep: str, what: str) -> dict[str, str]:
    """Split ``["K<sep>V", ...]`` into a dict, trimming whitespace around the value.
    Raises :class:`SpecError` naming ``what`` if any token lacks ``sep``."""
    out: dict[str, str] = {}
    for item in items:
        if sep not in item:
            raise SpecError(f"invalid {what} {item!r}: expected 'KEY{sep}VALUE'")
        key, value = item.split(sep, 1)
        out[key.strip()] = value.strip()
    return out


def _build_spec(*, transport: str, rest: list[str], headers: list[str],
                envs: list[str], trust: bool) -> dict:
    """Build a server spec dict from parsed CLI pieces. ``rest`` is the positional
    remainder after the name: ``[command, *args]`` for stdio, ``[url]`` for remote.
    Raises :class:`SpecError` on invalid flag/transport combinations."""
    if not rest:
        need = "a command" if transport == "stdio" else "a url"
        raise SpecError(f"missing {need} for transport {transport!r}")
    spec: dict = {}
    if transport == "stdio":
        if headers:
            raise SpecError("--header is only valid for http/sse transports")
        spec["command"] = rest[0]
        if rest[1:]:
            spec["args"] = rest[1:]
        if envs:
            spec["env"] = _parse_pairs(envs, "=", "env")
    else:  # http or sse
        if envs:
            raise SpecError("--env is only valid for the stdio transport")
        if len(rest) > 1:
            raise SpecError(f"unexpected extra arguments after url: {rest[1:]}")
        spec["url"] = rest[0]
        if headers:
            spec["headers"] = _parse_pairs(headers, ":", "header")
        if transport == "sse":
            spec["type"] = "sse"
    if trust:
        spec["trust"] = True
    return spec
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_mcp_cli.py -k "build_spec or parse_pairs" -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src tests && uv run pyright src/marim_harness/interfaces/cli/mcp.py`
Expected: no errors. (Note: `add_server`, `global_mcp_config_path`, `project_mcp_config_path`, `read_servers_with_source`, `remove_server` are imported now but used in Task 3 — if pyright/ruff flags them as unused, that resolves in Task 3; if ruff F401 fails here, proceed to Task 3 before committing, or add the command handlers in this same commit. Prefer committing Tasks 2+3 together if the unused-import lint blocks.)

- [ ] **Step 6: Commit (only if lint is clean; otherwise fold into Task 3's commit)**

```bash
git add src/marim_harness/interfaces/cli/mcp.py tests/test_mcp_cli.py
git commit -m "feat(mcp): spec-building and validation for marim mcp add"
```

---

### Task 3: Command handlers + router registration

**Files:**
- Modify: `src/marim_harness/interfaces/cli/mcp.py` (add parser, `_cmd_*`, `main`)
- Modify: `src/marim_harness/interfaces/cli/router.py:13` (add `"mcp"` to `_MANAGEMENT`)
- Test: `tests/test_mcp_cli.py` (extend with `main()`-level tests)

**Interfaces:**
- Consumes: `_build_spec`, `SpecError`, and the Task 1 helpers (`add_server`, `remove_server`, `read_servers_with_source`, `global_mcp_config_path`, `project_mcp_config_path`).
- Produces: `main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_cli.py`:

```python
import io


def _run(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = mcp_cmd.main(argv, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def test_main_add_stdio_writes_project_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run(["add", "mddocs", "node", "x.js", "-e", "K=v"])
    assert code == 0, err
    data = json.loads((tmp_path / ".marim" / "mcp.json").read_text())["mcpServers"]
    assert data["mddocs"] == {"command": "node", "args": ["x.js"], "env": {"K": "v"}}
    # project-scope trust caveat surfaced on stderr
    assert "trust" in err.lower()


def test_main_add_http_user_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run([
        "add", "--transport", "http", "--scope", "user", "remote",
        "https://x/mcp", "-H", "Authorization: Bearer t",
    ])
    assert code == 0, err
    from marim_harness.mcp.config import global_mcp_config_path
    data = json.loads(global_mcp_config_path().read_text())["mcpServers"]
    assert data["remote"] == {"url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}}


def test_main_add_duplicate_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    assert _run(["add", "a", "x"])[0] == 0
    code, out, err = _run(["add", "a", "y"])
    assert code == 1
    assert "already" in err.lower()


def test_main_add_validation_error_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run(["add", "a", "x", "-H", "K: v"])  # header on stdio
    assert code == 2
    assert "http/sse" in err


def test_main_list_shows_source(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    _run(["add", "--scope", "user", "g", "x"])
    _run(["add", "--scope", "project", "p", "y"])
    code, out, err = _run(["list"])
    assert code == 0
    assert "g" in out and "user" in out
    assert "p" in out and "project" in out


def test_main_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    code, out, err = _run(["list"])
    assert code == 0
    assert "no" in out.lower()


def test_main_get_known_and_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    _run(["add", "--scope", "user", "g", "node", "x.js"])
    code, out, err = _run(["get", "g"])
    assert code == 0
    assert "node" in out and "user" in out
    code, out, err = _run(["get", "nope"])
    assert code == 1


def test_main_remove_present_and_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    _run(["add", "--scope", "user", "g", "x"])
    assert _run(["remove", "g"])[0] == 0
    code, out, err = _run(["remove", "g"])
    assert code == 1


def test_main_no_subcommand_prints_help(tmp_path, monkeypatch):
    code, out, err = _run([])
    assert code == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_mcp_cli.py -k main -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'main'`.

- [ ] **Step 3: Add the parser, handlers, and `main` to `cli/mcp.py`**

Append to `src/marim_harness/interfaces/cli/mcp.py`:

```python
def _scope_path(scope: str, workspace_root: Path) -> Path:
    """Map a ``--scope`` value to its config file. ``user`` -> global; ``project``
    -> the workspace's ``.marim/mcp.json``."""
    if scope == "user":
        return global_mcp_config_path()
    return project_mcp_config_path(workspace_root)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marim mcp", add_help=True)
    # Like ``git -C``: choose the workspace root for project-scoped servers.
    parser.add_argument(
        "-C", "--workspace", default=None, metavar="DIR",
        help="Workspace root for project-scoped servers (default: current directory).",
    )
    sub = parser.add_subparsers(dest="cmd")

    add = sub.add_parser("add", help="Add an MCP server.")
    add.add_argument("name")
    add.add_argument(
        "-t", "--transport", choices=("stdio", "http", "sse"), default="stdio",
        help="Transport (default: stdio).",
    )
    add.add_argument(
        "-s", "--scope", choices=("user", "project"), default="project",
        help="user = global config; project = .marim/mcp.json (default: project).",
    )
    add.add_argument("-H", "--header", action="append", default=[], metavar="NAME: VALUE",
                     help="HTTP header (repeatable; http/sse only).")
    add.add_argument("-e", "--env", action="append", default=[], metavar="KEY=VALUE",
                     help="Environment variable (repeatable; stdio only).")
    add.add_argument("--trust", action="store_true",
                     help="Bypass tool-call approval for this server.")

    lst = sub.add_parser("list", help="List configured MCP servers.")
    lst.add_argument("--json", action="store_true", help="Emit JSON.")

    get = sub.add_parser("get", help="Show one server's configuration.")
    get.add_argument("name")

    rm = sub.add_parser("remove", help="Remove an MCP server.")
    rm.add_argument("name")
    rm.add_argument("-s", "--scope", choices=("user", "project"), default=None,
                    help="Limit removal to one scope (default: search project then user).")
    return parser


def _workspace_root(args) -> Path:
    return Path(args.workspace) if args.workspace else Path.cwd()


def _cmd_add(args, rest, *, out, err) -> int:
    try:
        spec = _build_spec(
            transport=args.transport, rest=rest, headers=args.header,
            envs=args.env, trust=args.trust,
        )
    except SpecError as exc:
        print(f"error: {exc}", file=err)
        return 2
    path = _scope_path(args.scope, _workspace_root(args))
    if not add_server(path, args.name, spec):
        print(f"error: server {args.name!r} already exists in {path} "
              f"(remove it first, or pick another name)", file=err)
        return 1
    print(f"Added MCP server {args.name!r} ({args.transport}) to {path}", file=out)
    if args.scope == "project":
        print("note: project servers in .marim/mcp.json load only when project "
              "trust is enabled (MARIM_TRUST_PROJECT_HOOKS).", file=err)
    return 0


def _cmd_list(args, *, out, err) -> int:
    servers = read_servers_with_source(_workspace_root(args))
    if args.json:
        print(json.dumps({n: {"source": s, **spec} for n, (spec, s) in servers.items()}), file=out)
        return 0
    if not servers:
        print("No MCP servers configured.", file=out)
        return 0
    for name, (spec, source) in sorted(servers.items()):
        target = spec.get("command") or spec.get("url") or "?"
        print(f"{name}  [{source}]  {target}", file=out)
    return 0


def _cmd_get(args, *, out, err) -> int:
    servers = read_servers_with_source(_workspace_root(args))
    entry = servers.get(args.name)
    if entry is None:
        print(f"error: no MCP server named {args.name!r}", file=err)
        return 1
    spec, source = entry
    print(f"{args.name}  [{source}]", file=out)
    print(json.dumps(spec, indent=2), file=out)
    return 0


def _cmd_remove(args, *, out, err) -> int:
    workspace_root = _workspace_root(args)
    if args.scope:
        scopes = [args.scope]
    else:
        scopes = ["project", "user"]
    for scope in scopes:
        path = _scope_path(scope, workspace_root)
        if remove_server(path, args.name):
            print(f"Removed MCP server {args.name!r} from {path}", file=out)
            return 0
    print(f"error: no MCP server named {args.name!r} to remove", file=err)
    return 1


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = _build_parser()
    # ``add`` accepts a positional remainder (command + args, or url) that may
    # contain dashes; parse_known_args pulls the recognized options out wherever
    # they appear and leaves the rest in order. The first leftover is consumed as
    # ``name`` by the parser; the remaining leftovers are the spec positionals.
    args, rest = parser.parse_known_args(argv)
    if args.cmd == "add":
        return _cmd_add(args, rest, out=out, err=err)
    if rest:
        print(f"error: unexpected arguments: {rest}", file=err)
        return 2
    if args.cmd == "list":
        return _cmd_list(args, out=out, err=err)
    if args.cmd == "get":
        return _cmd_get(args, out=out, err=err)
    if args.cmd == "remove":
        return _cmd_remove(args, out=out, err=err)
    parser.print_help(err)
    return 2
```

- [ ] **Step 4: Register the keyword in the router**

In `src/marim_harness/interfaces/cli/router.py:13`, change:

```python
_MANAGEMENT = {"sessions", "config", "models", "plugin"}
```

to:

```python
_MANAGEMENT = {"sessions", "config", "models", "plugin", "mcp"}
```

- [ ] **Step 5: Run the full new test file**

Run: `uv run pytest --no-cov tests/test_mcp_cli.py -q`
Expected: PASS (all tests from Tasks 1–3).

- [ ] **Step 6: Lint + type-check + router smoke**

Run: `uv run ruff check src tests && uv run pyright`
Expected: no errors.

Then a manual smoke test through the real entry point:

Run: `uv run marim mcp add --transport http demo https://example.com/mcp -H "Authorization: Bearer x" --scope user && uv run marim mcp list && uv run marim mcp remove demo`
Expected: "Added MCP server 'demo' (http) ...", a list line `demo  [user]  https://example.com/mcp`, then "Removed MCP server 'demo' ...". (Writes to the real global `mcp.json`; the remove cleans it up.)

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/cli/mcp.py src/marim_harness/interfaces/cli/router.py tests/test_mcp_cli.py
git commit -m "feat(mcp): marim mcp add/list/get/remove CLI command"
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md` and/or `docs/` (whichever documents MCP config today — grep for `mcp.json` first)

**Interfaces:** none (docs only).

- [ ] **Step 1: Find where MCP config is documented**

Run: `grep -rn "mcp.json\|mcpServers" README.md docs/ 2>/dev/null`
Read the matching section(s) to match tone/structure.

- [ ] **Step 2: Add a short `marim mcp` usage section**

Document the four subcommands with the two canonical examples (stdio + http), the `--scope user|project` mapping (and the project-trust caveat), and that it edits the same `mcp.json` files that can be hand-edited. Keep it to the style of the surrounding docs. If no MCP doc section exists, add a brief one near the existing config/CLI documentation.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/
git commit -m "docs: document marim mcp CLI command"
```

---

## Self-Review

**Spec coverage:**
- Full parity add/list/remove/get → Task 3 ✓
- claude-exact add syntax (`--transport`, `-H`, `-e`, positional cmd/url, default stdio) → Task 2 (`_build_spec`) + Task 3 (parser, `parse_known_args` for trailing args) ✓
- `--scope user|project`, default project → Task 3 parser + `_scope_path` ✓
- Persistence helpers (add/remove/read-with-source), atomic writes → Task 1 ✓
- Validation loud at add-time (one of command/url; type ∈ {unset, sse}; KEY=value / Name: Value parsing) → Task 2 ✓
- Project-scope trust warning → Task 3 `_cmd_add` ✓
- Exit codes 0/1/2 → Tasks 2–3, asserted in tests ✓
- `--trust` flag → Task 2 + Task 3 ✓
- Testing per `test_bootstrap.py` style (StringIO, tmp_path, XDG_CONFIG_HOME) → all tasks ✓
- Files touched list (router, cli/mcp.py, mcp/config.py, tests) → matches spec ✓

**Placeholder scan:** none — every step has concrete code or commands.

**Type consistency:** `add_server`/`remove_server`/`read_servers_with_source` signatures are identical across Task 1 definition, Task 2 imports, and Task 3 usage. `_build_spec` keyword args (`transport`, `rest`, `headers`, `envs`, `trust`) match between Task 2 definition and Task 3 `_cmd_add` call site. `_parse_pairs(items, sep, what)` consistent. `main(argv, *, out, err)` matches the `config.py` convention and the test `_run` helper.

**Note on Task 2 lint:** the helper imports in Task 2 Step 3 are consumed in Task 3. If F401 (unused import) blocks the Task 2 commit, fold Tasks 2 and 3 into a single commit — flagged inline at Task 2 Step 5/6.
