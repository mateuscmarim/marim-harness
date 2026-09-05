# Codex CLI Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `codex-cli` as both a main-loop provider (`MARIM_PROVIDER=codex-cli`) and a sub-agent backend (`backend: codex-cli`), driving OpenAI Codex over the long-lived `codex app-server` JSON-RPC transport with marim's Mode/approval panel, ask_user, steer and interrupt kept in the loop.

**Architecture:** A new `marim_harness/codex/` package owns the transport: `rpc.py` (framing), `server.py` (one shared `codex app-server` process, threads routed by id), `translate.py` (notifications → neutral events), `approvals.py` (server-side approval requests → marim's Mode + ApprovalPanel), `catalog.py`, `env.py`. A new shared base `config/external_cli.py::ExternalCliModel` carries the late-bound UI seams that `ClaudeCliModel` already has, so `Harness.wire_cli_model` and `aux_model_for` bind both providers through one isinstance. `config/codex_cli_model.py::CodexCliModel` is a Pydantic AI `Model` returning **text-only** `ModelResponse`s (tool activity goes out-of-band via `on_activity`, exactly like claude-cli). `subagents/codex_spawn.py::CodexSpawnOrchestrator` mirrors `CliSpawnOrchestrator` for `backend: codex-cli` spawns, sharing the app-server process with `ephemeral: true` threads.

**Tech Stack:** Python ≥ 3.10, pydantic-ai (`Model`/`StreamedResponse`), asyncio subprocess, Codex app-server JSON-RPC v2 (Codex CLI ≥ 0.152), pytest (+xdist, asyncio), a scripted fake app-server for tests.

**Spec:** `docs/superpowers/specs/2026-09-04-codex-cli-backend-design.md`

## Global Constraints

- `requires-python = ">=3.10"` — no `match`, no `ExceptionGroup`, no `Self`; `from __future__ import annotations` at the top of every new module.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity ≤ 10 per function (extract helpers, never `# noqa: C901`). `uv run ruff format` before every commit.
- `uv run pyright` must stay at zero errors (standard mode, `src` only).
- Use `uv run …` for everything. Never `pip`, bare `python`, or bare `pytest`.
- **Never run a paid or live model without the user's explicit OK.** The Codex subscription is the user's own. Task 0 and Task 13 are the only tasks that touch a real `codex`; both are gated on the user saying yes and on `MARIM_LIVE_CODEX=1`.
- Text-only `ModelResponse` invariant: `CodexCliModel` never returns a `ToolCallPart`; Codex's tool activity is display-only side-channel events (`on_activity`).
- `acceptForSession` is **never** sent as an approval decision; every decision is per-request (`accept`/`decline`/`cancel`).
- Mode mapping (spec §Mode mapping): `auto` → `approvalPolicy="on-request"` + `workspaceWrite`; `ask` → `"untrusted"` + `workspaceWrite`; `plan` → `"never"` + `readOnly`.
- Thinking → effort (spec §Thinking): `off`/unset → key omitted; `minimal`/`low` → `low`; `medium` → `medium`; `high` → `high`; `xhigh` → `xhigh` when the model lists it, else `high`.
- Every commit message ends with the trailer line `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Preserve the long "why" comments around resumability and the deps/services cycle when editing nearby code.

---

## File structure

**Create**

| File | Responsibility |
|---|---|
| `src/marim_harness/codex/__init__.py` | Empty package marker (re-exports nothing; keep import cycles out). |
| `src/marim_harness/codex/env.py` | Binary/timeout/version/login probes; `CodexUnavailable`. No I/O beyond `shutil.which` and a file existence check. |
| `src/marim_harness/codex/rpc.py` | `JsonRpcClient`: newline-delimited JSON-RPC over an `asyncio` reader/writer pair; request/response matching, notifications, server→client requests. Transport-agnostic (tests feed a `StreamReader`). |
| `src/marim_harness/codex/translate.py` | Pure: app-server notification → neutral event dataclasses (`TextDelta`, `ActivityStart`, …). |
| `src/marim_harness/codex/approvals.py` | Pure policy (`policy_for`, `sandbox_for`, `decide`) + `ApprovalBroker` that answers server requests via marim's approval panel / ask_user. |
| `src/marim_harness/codex/server.py` | `CodexServer`: spawns `codex app-server`, initializes, owns threads, routes notifications per thread into queues, respawns on crash. `shared_server()` singleton. |
| `src/marim_harness/codex/catalog.py` | `list_codex_models` → `ModelEntry` list; static fallback. |
| `src/marim_harness/config/external_cli.py` | `ExternalCliModel` base (late-bound seams) + `CliModelError` (moved here). |
| `src/marim_harness/config/codex_cli_model.py` | `CodexCliModel` + `CodexStreamedResponse` + `effort_for`. |
| `src/marim_harness/subagents/codex_spawn.py` | `CodexSpawnOrchestrator` (execute/resume for `backend: codex-cli`). |
| `tests/fakes/__init__.py`, `tests/fakes/codex_app_server.py` | Scripted fake app-server used by every non-live test. |
| `tests/test_codex_*.py`, `tests/test_external_cli_model.py`, `tests/test_subagent_codex_spawn.py` | Per-task tests (named in each task). |
| `docs/examples/agents/codex-worker.md` | Example `backend: codex-cli` agent spec. |

**Modify**

| File | Change |
|---|---|
| `src/marim_harness/config/env.py` | Add `MARIM_CODEX_CLI_BIN`, `MARIM_CODEX_CLI_TIMEOUT`, `CODEX_HOME` to the passthrough blocklist. |
| `src/marim_harness/subagents/cli_backend.py` | Public alias `iter_ndjson_lines = _iter_ndjson_lines`. |
| `src/marim_harness/config/claude_cli_model.py` | Inherit `ExternalCliModel`; `CliModelError` re-exported from `external_cli`; `_TextFolder` → public `TextFolder`. |
| `src/marim_harness/config/model.py` | `KNOWN_PROVIDERS`, `_provider_config`, `_provider_has_creds`, `build_model`, `ModelSource.list_models` codex arms. |
| `src/marim_harness/runtime/harness.py` | `wire_cli_model` binds the `ExternalCliModel` seams; `steer` / `manual_compact` / `aclose` hooks. |
| `src/marim_harness/session/ctrl.py`, `session/store.py` | `cli_thread_id` persistence; `aux_model_for` uses the base class. |
| `src/marim_harness/interfaces/tui/providers.py`, `settings.py` | `codex-cli` provider row. |
| `src/marim_harness/subagents/runner.py`, `cli_spawn.py`, `output_schema.py` | Backend dispatch table, uniform `execute` signature, codex output-schema arm. |
| `src/marim_harness/workspace/agents.py` | `backend:` docstring mentions `codex-cli`. |
| `.env.example`, `CHANGELOG.md`, `README.md`, `docs/reference/configuration.md`, `docs/guides/subagents.md`, `docs/guides/tui.md`, `docs/guides/sessions.md` | Docs. |

---

### Task 0: Live assumption checks (needs the user's OK; may be deferred)

**Files:** none committed. Scratch only.

**Interfaces:** Produces answers that Task 4 (`thread_config()`), Task 6 (`ApprovalBroker.handle` permissions arm) and Task 8 (`check_min_version`) consume. Each of those tasks carries a documented default so the plan is executable **before** this task runs.

The five open questions from the spec §Decisions log, with the default each later task assumes if unverified:

| # | Question | Default assumed | Consumer |
|---|---|---|---|
| 1 | Does `untrusted` + `workspaceWrite` prompt on every `fileChange`? | Yes (spec assumption). | Task 6 |
| 2 | Which `thread/start.config` keys clear inherited MCP servers/plugins? | `{"mcp_servers": {}}` (provisional). Fallback: marim-owned `CODEX_HOME`. | Task 4 `thread_config()` |
| 3 | Can one app-server process run turns on two threads concurrently? | Yes (needed for ephemeral clones + spawns sharing the process). | Task 4 |
| 4 | `initialize` result `userAgent` format? | `codex_cli_rs/0.152.1 (...)` — regex takes the first `x.y(.z)`. | Task 1 `parse_version` |
| 5 | Does echoing `params["permissions"]` back as `{"permissions": …}` satisfy `item/permissions/requestApproval`? | Yes. | Task 6 |

- [ ] **Step 1: Ask the user** (one message): "Task 0 needs a real `codex app-server` on your subscription for ~5 short probe turns (< 1k tokens each). OK to run now, or defer and implement on the defaults in the plan?" Do **not** run anything until they answer. If deferred, skip to Task 1 and leave the defaults; Task 13 revisits.

- [ ] **Step 2: If approved, write the probe script** to the scratchpad (not the repo), `probe_codex.py`:

```python
"""Probe the five plan assumptions against a real `codex app-server`."""

import json
import os
import subprocess
import sys
import threading
import time

proc = subprocess.Popen(
    ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
)
seen: list[dict] = []
next_id = 0


def send(method: str, params: dict | None = None, *, notify: bool = False) -> int:
    global next_id
    msg: dict = {"method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        next_id += 1
        msg["id"] = next_id
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return next_id


def reader() -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        obj = json.loads(line)
        seen.append(obj)
        print("<<", json.dumps(obj)[:300], flush=True)
        # Answer every server request with decline so nothing runs.
        if "id" in obj and "method" in obj:
            m = obj["method"]
            if m == "item/permissions/requestApproval":
                resp = {"permissions": obj["params"]["permissions"]}
            elif m == "item/tool/requestUserInput":
                resp = {"answers": {}}
            else:
                resp = {"decision": "decline"}
            proc.stdin.write(json.dumps({"id": obj["id"], "result": resp}) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]


threading.Thread(target=reader, daemon=True).start()
send("initialize", {"clientInfo": {"name": "marim-probe", "version": "0"}})
time.sleep(1)
send("initialized", notify=True)
cwd = os.getcwd()
ws = {"type": "workspaceWrite", "writableRoots": [cwd], "networkAccess": False,
      "excludeSlashTmp": False, "excludeTmpdirEnvVar": False}
# Q2: isolation keys — try the provisional override and read back the thread.
send("thread/start", {"cwd": cwd, "approvalPolicy": "untrusted", "sandbox": "workspace-write",
                      "config": {"mcp_servers": {}}, "ephemeral": True})
time.sleep(2)
tid = next(o["result"]["thread"]["id"] for o in seen if o.get("id") == 2)
# Q1: ask-mode file change → expect item/fileChange/requestApproval.
send("turn/start", {"threadId": tid, "approvalPolicy": "untrusted", "sandboxPolicy": ws,
                    "input": [{"type": "text", "text": "Create a file named probe.txt containing 'hi'.",
                               "text_elements": []}]})
time.sleep(25)
# Q3: a second thread turn while the first may still be running.
send("thread/start", {"cwd": cwd, "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only"})
time.sleep(2)
proc.stdin.close()  # type: ignore[union-attr]
time.sleep(1)
json.dump(seen, open("probe.jsonl", "w"), indent=1)
print("userAgent:", next((o["result"].get("userAgent") for o in seen if o.get("id") == 1), None))
print("permission prompts:", [o["method"] for o in seen if "id" in o and "method" in o])
print("errors:", [o for o in seen if "error" in o])
sys.exit(0)
```

- [ ] **Step 3: Run it** from a throwaway directory (`cd "$(mktemp -d)"`), `uv run --no-project python /path/to/probe_codex.py`, and read `probe.jsonl`.

- [ ] **Step 4: Record answers** in the spec's Decisions log (`docs/superpowers/specs/2026-09-04-codex-cli-backend-design.md`, section "Decisions log") as one bullet per question, and adjust these plan defaults:
  - Q2 answer → `thread_config()` in Task 4 (and if no key works: implement the `CODEX_HOME` fallback described in Task 4 Step 7).
  - Q4 answer → `parse_version` regex in Task 1 if the format differs.
  - Q5 answer → `ApprovalBroker._permissions_result` in Task 6.

- [ ] **Step 5: Commit** the spec update only:

```bash
git add docs/superpowers/specs/2026-09-04-codex-cli-backend-design.md
git commit -m "docs(spec): record codex app-server live probe answers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 1: `codex/env.py` + env blocklist

**Files:**
- Create: `src/marim_harness/codex/__init__.py` (empty), `src/marim_harness/codex/env.py`
- Modify: `src/marim_harness/config/env.py` (the blocklist that lists `MARIM_CLAUDE_CLI_BIN` at ~line 20 and `MARIM_CLAUDE_CLI_TIMEOUT` at ~line 35)
- Test: `tests/test_codex_env.py`
- Modify: `docs/reference/configuration.md` (two env-var rows — `tests/test_docs_reference.py::test_every_env_var_is_documented` rejects any `MARIM_*` name in `src/` that the table lacks)

**Interfaces:**
- Produces:
  - `CODEX_BINARY_ENV = "MARIM_CODEX_CLI_BIN"`, `CODEX_TIMEOUT_ENV = "MARIM_CODEX_CLI_TIMEOUT"`, `MIN_CODEX_VERSION = (0, 152)`, `INSTALL_HINT: str`
  - `class CodexUnavailable(Exception)`
  - `resolve_codex_binary() -> str | None`
  - `codex_home() -> Path`
  - `codex_logged_in() -> bool`
  - `codex_available() -> bool`
  - `codex_timeout() -> float`
  - `parse_version(user_agent: str) -> tuple[int, ...] | None`
  - `check_min_version(user_agent: str) -> None` (raises `CodexUnavailable`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codex_env.py
from __future__ import annotations

import os
import stat

import pytest

from marim_harness.codex import env as cenv


def _fake_bin(tmp_path, name="codex"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_resolve_binary_prefers_env_override(tmp_path, monkeypatch):
    p = _fake_bin(tmp_path, "my-codex")
    monkeypatch.setenv(cenv.CODEX_BINARY_ENV, str(p))
    assert cenv.resolve_codex_binary() == str(p)


def test_resolve_binary_falls_back_to_path(tmp_path, monkeypatch):
    _fake_bin(tmp_path)
    monkeypatch.delenv(cenv.CODEX_BINARY_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cenv.resolve_codex_binary() == str(tmp_path / "codex")


def test_resolve_binary_missing(monkeypatch, tmp_path):
    monkeypatch.delenv(cenv.CODEX_BINARY_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cenv.resolve_codex_binary() is None


def test_codex_home_default_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cenv.codex_home().name == ".codex"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cenv.codex_home() == tmp_path


def test_logged_in_requires_auth_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cenv.codex_logged_in() is False
    (tmp_path / "auth.json").write_text("{}")
    assert cenv.codex_logged_in() is True


def test_available_needs_binary_and_login(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(cenv.CODEX_BINARY_ENV, raising=False)
    assert cenv.codex_available() is False
    _fake_bin(tmp_path)
    assert cenv.codex_available() is False
    (tmp_path / "auth.json").write_text("{}")
    assert cenv.codex_available() is True


@pytest.mark.parametrize(
    "raw, expected",
    [("", 600.0), ("30", 30.0), ("1.5", 1.5), ("nope", 600.0), ("-1", 600.0)],
)
def test_timeout_parsing(monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv(cenv.CODEX_TIMEOUT_ENV, raw)
    else:
        monkeypatch.delenv(cenv.CODEX_TIMEOUT_ENV, raising=False)
    assert cenv.codex_timeout() == expected


@pytest.mark.parametrize(
    "ua, expected",
    [
        ("codex_cli_rs/0.152.1 (Arch Linux; x86_64)", (0, 152, 1)),
        ("codex_cli_rs/1.2 foo", (1, 2)),
        ("garbage", None),
    ],
)
def test_parse_version(ua, expected):
    assert cenv.parse_version(ua) == expected


def test_check_min_version_accepts_and_rejects():
    cenv.check_min_version("codex_cli_rs/0.152.1")
    cenv.check_min_version("codex_cli_rs/1.0.0")
    with pytest.raises(cenv.CodexUnavailable, match="0.152"):
        cenv.check_min_version("codex_cli_rs/0.140.0")
    with pytest.raises(cenv.CodexUnavailable, match="version"):
        cenv.check_min_version("weird")


def test_env_blocklist_hides_codex_knobs():
    from marim_harness.config.env import _PROJECT_ENV_BLOCKLIST

    assert "MARIM_CODEX_CLI_BIN" in _PROJECT_ENV_BLOCKLIST
    assert "MARIM_CODEX_CLI_TIMEOUT" in _PROJECT_ENV_BLOCKLIST
    assert "CODEX_HOME" in _PROJECT_ENV_BLOCKLIST
```


- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_codex_env.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.codex'`.

- [ ] **Step 3: Implement**

`src/marim_harness/codex/__init__.py`:

```python
"""Codex app-server transport: the `codex-cli` provider and sub-agent backend.

Deliberately re-exports nothing — import submodules directly (``from
.server import CodexServer``) so config/ and subagents/ can import pieces of
this package lazily without pulling the subprocess machinery at import time.
"""
```

`src/marim_harness/codex/env.py`:

```python
"""Environment probes for the Codex CLI: binary, login, timeout, version floor.

Pure lookups (``shutil.which``, one ``is_file``) so the settings screen and
provider detection can call these freely; nothing here spawns a process.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

CODEX_BINARY_ENV = "MARIM_CODEX_CLI_BIN"
CODEX_TIMEOUT_ENV = "MARIM_CODEX_CLI_TIMEOUT"
_DEFAULT_TIMEOUT = 600.0
# app-server v2 (thread/turn/item vocabulary, typed approval requests) landed
# in this line; older binaries speak a different protocol and are refused.
MIN_CODEX_VERSION = (0, 152)
INSTALL_HINT = (
    "Install the Codex CLI (npm i -g @openai/codex) and run `codex login`; "
    f"marim needs codex >= {MIN_CODEX_VERSION[0]}.{MIN_CODEX_VERSION[1]}."
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class CodexUnavailable(Exception):
    """The Codex CLI is missing, too old, or not logged in."""


def resolve_codex_binary() -> str | None:
    """Absolute path of the ``codex`` binary (``MARIM_CODEX_CLI_BIN`` override
    first, then PATH), or None."""
    return shutil.which(os.environ.get(CODEX_BINARY_ENV) or "codex")


def codex_home() -> Path:
    """``$CODEX_HOME`` or ``~/.codex`` — where the CLI keeps ``auth.json``."""
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def codex_logged_in() -> bool:
    """Whether ``codex login`` has run (auth.json exists). Existence only — the
    token's validity surfaces as the server's own error on the first turn."""
    return (codex_home() / "auth.json").is_file()


def codex_available() -> bool:
    return resolve_codex_binary() is not None and codex_logged_in()


def codex_timeout() -> float:
    """Idle timeout (seconds) for one turn; ``MARIM_CODEX_CLI_TIMEOUT`` or 600."""
    raw = os.environ.get(CODEX_TIMEOUT_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def parse_version(user_agent: str) -> tuple[int, ...] | None:
    """First ``x.y[.z]`` in an ``initialize`` ``userAgent`` string."""
    m = _VERSION_RE.search(user_agent)
    if m is None:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def check_min_version(user_agent: str) -> None:
    """Raise ``CodexUnavailable`` unless the reported version meets the floor."""
    parsed = parse_version(user_agent)
    if parsed is None:
        raise CodexUnavailable(f"Could not read the codex version from {user_agent!r}. {INSTALL_HINT}")
    if parsed[:2] < MIN_CODEX_VERSION:
        floor = ".".join(str(n) for n in MIN_CODEX_VERSION)
        raise CodexUnavailable(
            f"codex {'.'.join(str(n) for n in parsed)} is older than the {floor} minimum. {INSTALL_HINT}"
        )
```

Then in `src/marim_harness/config/env.py`, add three entries next to the existing `MARIM_CLAUDE_CLI_BIN` / `MARIM_CLAUDE_CLI_TIMEOUT` lines, keeping the list's alphabetical grouping:

```python
    "MARIM_CODEX_CLI_BIN",
    "MARIM_CODEX_CLI_TIMEOUT",
```

and `"CODEX_HOME",` beside `"XDG_CONFIG_HOME"`.

- [ ] **Step 3b: Document the two env vars** — in `docs/reference/configuration.md`,
  add two rows to the environment-variable table directly after the
  `MARIM_CLAUDE_CLI_TIMEOUT` row, in the same column format as the
  `MARIM_CLAUDE_CLI_BIN` / `MARIM_CLAUDE_CLI_TIMEOUT` rows:

  | `MARIM_CODEX_CLI_BIN` | Path to the `codex` binary when it is not on PATH (used by the `codex-cli` provider and `backend: codex-cli` sub-agents). |
  | `MARIM_CODEX_CLI_TIMEOUT` | Seconds a Codex turn may sit idle (no notification) before marim interrupts it. Default `600`. |

  Run: `uv run pytest --no-cov tests/test_docs_reference.py -v` — Expected: PASS.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_env.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/codex tests/test_codex_env.py src/marim_harness/config/env.py
git commit -m "feat(codex): env probes for the codex CLI (binary, login, timeout, version floor)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 2: `codex/rpc.py` — JSON-RPC framing client

**Files:**
- Create: `src/marim_harness/codex/rpc.py`
- Modify: `src/marim_harness/subagents/cli_backend.py` (add `iter_ndjson_lines = _iter_ndjson_lines` right after the `_iter_ndjson_lines` definition, ~line 471)
- Test: `tests/test_codex_rpc.py`

**Interfaces:**
- Consumes: `marim_harness.subagents.cli_backend.iter_ndjson_lines(stream) -> AsyncIterator[str]` (yields decoded lines without the trailing newline).
- Produces:
  - `class RpcError(Exception)` with `.code: int`, `.message: str`, `.data: object | None`
  - `NotificationHandler = Callable[[str, dict], Awaitable[None]]` — `(method, params)`
  - `ServerRequestHandler = Callable[[str, dict], Awaitable[dict]]` — `(method, params) -> result`
  - `class JsonRpcClient(reader: asyncio.StreamReader, writer: WriterLike, *, on_notification: NotificationHandler, on_server_request: ServerRequestHandler)` with
    - `async request(method: str, params: dict | None = None, *, timeout: float | None = None) -> dict`
    - `async notify(method: str, params: dict | None = None) -> None`
    - `async run() -> None` (read loop; returns on EOF)
    - `closed: asyncio.Event` (set on EOF; every pending request fails with `RpcError(-32000, "app-server closed")`)
  - Wire format: request `{"id": n, "method": m, "params": p}`, response `{"id": n, "result": r}` or `{"id": n, "error": {"code", "message", "data"}}`, notification `{"method": m, "params": p}`; **no** `jsonrpc` key (matches what `codex app-server` emits).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codex_rpc.py
from __future__ import annotations

import asyncio
import json

import pytest

from marim_harness.codex.rpc import JsonRpcClient, RpcError


class FakeWriter:
    def __init__(self) -> None:
        self.lines: list[dict] = []

    def write(self, data: bytes) -> None:
        for raw in data.decode().split("\n"):
            if raw:
                self.lines.append(json.loads(raw))

    async def drain(self) -> None:
        return None


async def _noop_notification(method: str, params: dict) -> None:
    return None


async def _noop_request(method: str, params: dict) -> dict:
    return {}


def _client(**kw):
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    client = JsonRpcClient(
        reader,
        writer,
        on_notification=kw.get("on_notification", _noop_notification),
        on_server_request=kw.get("on_server_request", _noop_request),
    )
    return reader, writer, client


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


@pytest.mark.anyio
async def test_request_matches_response_by_id():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    fut = asyncio.create_task(client.request("thread/start", {"cwd": "/x"}))
    await asyncio.sleep(0)
    sent = writer.lines[0]
    assert sent["method"] == "thread/start" and sent["params"] == {"cwd": "/x"}
    assert "jsonrpc" not in sent
    _feed(reader, {"id": sent["id"], "result": {"thread": {"id": "t1"}}})
    assert await fut == {"thread": {"id": "t1"}}
    reader.feed_eof()
    await loop


@pytest.mark.anyio
async def test_error_response_raises_rpc_error():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    fut = asyncio.create_task(client.request("thread/resume", {"threadId": "nope"}))
    await asyncio.sleep(0)
    _feed(reader, {"id": writer.lines[0]["id"], "error": {"code": -32000, "message": "no thread"}})
    with pytest.raises(RpcError) as exc:
        await fut
    assert exc.value.code == -32000 and "no thread" in str(exc.value)
    reader.feed_eof()
    await loop


@pytest.mark.anyio
async def test_notification_dispatch_and_params_default():
    got: list[tuple[str, dict]] = []

    async def on_notification(method: str, params: dict) -> None:
        got.append((method, params))

    reader, writer, client = _client(on_notification=on_notification)
    loop = asyncio.create_task(client.run())
    _feed(reader, {"method": "turn/started", "params": {"threadId": "t1"}})
    _feed(reader, {"method": "initialized"})
    reader.feed_eof()
    await loop
    assert got == [("turn/started", {"threadId": "t1"}), ("initialized", {})]


@pytest.mark.anyio
async def test_server_request_is_answered_with_handler_result():
    async def on_server_request(method: str, params: dict) -> dict:
        assert method == "item/commandExecution/requestApproval"
        return {"decision": "decline"}

    reader, writer, client = _client(on_server_request=on_server_request)
    loop = asyncio.create_task(client.run())
    _feed(reader, {"id": "srv-1", "method": "item/commandExecution/requestApproval", "params": {}})
    await asyncio.sleep(0.01)
    reader.feed_eof()
    await loop
    assert writer.lines == [{"id": "srv-1", "result": {"decision": "decline"}}]


@pytest.mark.anyio
async def test_server_request_handler_exception_becomes_error_reply():
    async def on_server_request(method: str, params: dict) -> dict:
        raise RpcError(-32601, "unknown method")

    reader, writer, client = _client(on_server_request=on_server_request)
    loop = asyncio.create_task(client.run())
    _feed(reader, {"id": 7, "method": "mystery", "params": {}})
    await asyncio.sleep(0.01)
    reader.feed_eof()
    await loop
    assert writer.lines == [{"id": 7, "error": {"code": -32601, "message": "unknown method"}}]


@pytest.mark.anyio
async def test_eof_fails_pending_requests_and_sets_closed():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    fut = asyncio.create_task(client.request("model/list"))
    await asyncio.sleep(0)
    reader.feed_eof()
    await loop
    assert client.closed.is_set()
    with pytest.raises(RpcError, match="closed"):
        await fut


@pytest.mark.anyio
async def test_request_timeout():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    with pytest.raises(asyncio.TimeoutError):
        await client.request("model/list", timeout=0.01)
    reader.feed_eof()
    await loop


@pytest.mark.anyio
async def test_malformed_line_is_skipped():
    reader, writer, client = _client()
    loop = asyncio.create_task(client.run())
    reader.feed_data(b"not json\n")
    _feed(reader, {"method": "warning", "params": {"message": "x"}})
    reader.feed_eof()
    await loop  # no exception
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_codex_rpc.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'marim_harness.codex.rpc'`.

- [ ] **Step 3: Implement**

First the alias in `src/marim_harness/subagents/cli_backend.py`, directly after `_iter_ndjson_lines`'s body ends:

```python
# Public name for the codex transport (codex/rpc.py) — same chunked reader,
# same "no line-length cap, trailing unterminated line still yielded" contract.
iter_ndjson_lines = _iter_ndjson_lines
```

Then `src/marim_harness/codex/rpc.py`:

```python
"""Newline-delimited JSON-RPC client for ``codex app-server``.

One reader task (``run``) demultiplexes three message kinds by shape:
``{"id", "result"|"error"}`` completes a pending request; ``{"id", "method"}``
is a server→client request answered through ``on_server_request`` (in its own
task, so a slow approval prompt never blocks the read loop); ``{"method"}``
alone is a notification. The wire carries no ``jsonrpc`` key — the server
omits it and tolerates its absence — so neither do we.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..subagents.cli_backend import iter_ndjson_lines

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[str, dict], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict], Awaitable[dict]]

CLOSED_CODE = -32000
INTERNAL_ERROR_CODE = -32603


class WriterLike(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class JsonRpcClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: WriterLike,
        *,
        on_notification: NotificationHandler,
        on_server_request: ServerRequestHandler,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    async def _send(self, obj: dict) -> None:
        # One writer at a time: two coroutines interleaving partial lines would
        # corrupt the NDJSON framing the server parses.
        async with self._write_lock:
            self._writer.write((json.dumps(obj) + "\n").encode())
            await self._writer.drain()

    async def request(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        if self.closed.is_set():
            raise RpcError(CLOSED_CODE, "app-server closed")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def run(self) -> None:
        """Read until EOF, dispatching each line. Always sets ``closed`` and
        fails every pending request on exit so callers never hang on a dead
        process."""
        try:
            async for line in iter_ndjson_lines(self._reader):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    logger.debug("codex rpc: skipping non-JSON line: %.200s", line)
                    continue
                if isinstance(obj, dict):
                    self._dispatch(obj)
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RpcError(CLOSED_CODE, "app-server closed"))
            self._pending.clear()

    def _dispatch(self, obj: dict) -> None:
        method = obj.get("method")
        if "id" in obj and method is None:
            self._complete(obj)
        elif "id" in obj:
            self._spawn(self._answer(obj["id"], str(method), obj.get("params") or {}))
        elif method is not None:
            self._spawn(self._on_notification(str(method), obj.get("params") or {}))

    def _complete(self, obj: dict) -> None:
        fut = self._pending.get(obj["id"]) if isinstance(obj["id"], int) else None
        if fut is None or fut.done():
            return
        if "error" in obj:
            err = obj["error"] or {}
            fut.set_exception(
                RpcError(int(err.get("code", -1)), str(err.get("message", "")), err.get("data"))
            )
        else:
            fut.set_result(obj.get("result") or {})

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, rid: object, method: str, params: dict) -> None:
        try:
            result = await self._on_server_request(method, params)
            reply: dict[str, Any] = {"id": rid, "result": result}
        except RpcError as exc:
            reply = {"id": rid, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the read loop
            logger.exception("codex rpc: server request %s failed", method)
            reply = {"id": rid, "error": {"code": INTERNAL_ERROR_CODE, "message": str(exc)}}
        await self._send(reply)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_rpc.py -v`
Expected: all PASS. Then `uv run pytest --no-cov tests/test_cli_backend.py -q` still passes.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/codex/rpc.py src/marim_harness/subagents/cli_backend.py tests/test_codex_rpc.py
git commit -m "feat(codex): JSON-RPC framing client over NDJSON

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Scripted fake app-server (test fixture)

**Files:**
- Create: `tests/fakes/__init__.py`, `tests/fakes/codex_app_server.py`
- Test: `tests/test_codex_fake_server.py` (smoke test of the fake itself)

**Interfaces:**
- Produces: `tests.fakes.fake_codex_bin(tmp_path: Path, scenario: dict) -> str` — writes the scenario JSON and an executable shell wrapper into `tmp_path`, returns the wrapper path (use it as `MARIM_CODEX_CLI_BIN`). The wrapper also records every client message to `tmp_path / "codex-requests.jsonl"`; `read_request_log(tmp_path) -> list[dict]` reads it back.
- Scenario schema (JSON):
  - `"userAgent"`: string, default `"codex_cli_rs/0.152.1 (fake)"`.
  - `"models"`: list of `model/list` entries, default two models (`gpt-5.6-sol` with `supportedReasoningEfforts` low/medium/high/xhigh, `gpt-5.4-mini` with low/medium).
  - `"resumable"`: list of thread ids `thread/resume` accepts (default `[]`; others get error `-32000 "thread not found"`).
  - `"turns"`: list of turn scripts, consumed in `turn/start` order; the **last** script repeats for further turns. Each script is a list of steps:
    - `{"notify": "<method>", "params": {...}}` — emit a notification; `$THREAD`/`$TURN` inside string values are substituted; `threadId`/`turnId` are injected into `params` when absent.
    - `{"request": "<method>", "params": {...}, "record_as": "<key>"}` — send a server→client request, block until the client's reply, store the reply under `record_as` in the request log.
    - `{"hang": true}` — block until `turn/interrupt` arrives (then the turn ends `interrupted`).
    - `{"sleep": <secs>}`.
    - `{"exit": <code>}` — write `"fake: dying"` to stderr and exit the process.
  - Every `turn/start` emits `turn/started` first and, after the script, `turn/completed` with `status` `"completed"` (or `"interrupted"`), unless a step set `"fail": "<message>"` (`{"fail": "boom"}` → `status: "failed"`, `error: {"message": "boom"}`).
- Handles: `initialize` (→ `{userAgent, codexHome, platformFamily, platformOs}`), `initialized`, `thread/start` (→ `thread-N`, emits `thread/started`), `thread/resume`, `turn/start` (→ `turn-N`), `turn/interrupt`, `turn/steer` (→ `{turnId}`), `thread/compact/start` (→ `{}` + `thread/compacted`), `model/list`. Anything else → error `-32601`.
- Concurrency model: **single-threaded blocking loop over stdin** — while a turn script runs, the only client messages it reads are replies to its own `request` steps and `turn/interrupt`; other requests are answered after the script finishes. Tests that need "two turns at once" are not supported by the fake (that is a Task 0 live check).

- [ ] **Step 1: Write the smoke test**

```python
# tests/test_codex_fake_server.py
from __future__ import annotations

import json
import subprocess
import sys

from tests.fakes import fake_codex_bin, read_request_log


def _run(binary: str, messages: list[dict]) -> list[dict]:
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [binary, "app-server"], input=payload, capture_output=True, text=True, timeout=10
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_fake_initialize_thread_and_turn(tmp_path):
    scenario = {
        "turns": [
            [
                {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "hel"}},
                {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "lo"}},
            ]
        ]
    }
    binary = fake_codex_bin(tmp_path, scenario)
    out = _run(
        binary,
        [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t", "version": "0"}}},
            {"method": "initialized"},
            {"id": 2, "method": "thread/start", "params": {"cwd": "/w"}},
            {
                "id": 3,
                "method": "turn/start",
                "params": {"threadId": "thread-1", "input": [{"type": "text", "text": "hi"}]},
            },
        ],
    )
    by_id = {o["id"]: o for o in out if "id" in o}
    assert by_id[1]["result"]["userAgent"].startswith("codex_cli_rs/0.152.1")
    assert by_id[2]["result"]["thread"]["id"] == "thread-1"
    assert by_id[3]["result"]["turn"]["id"] == "turn-1"
    methods = [o["method"] for o in out if "method" in o]
    assert methods == [
        "thread/started",
        "turn/started",
        "item/agentMessage/delta",
        "item/agentMessage/delta",
        "turn/completed",
    ]
    deltas = [o["params"] for o in out if o.get("method") == "item/agentMessage/delta"]
    assert deltas[0]["threadId"] == "thread-1" and deltas[0]["turnId"] == "turn-1"
    done = next(o for o in out if o.get("method") == "turn/completed")
    assert done["params"]["turn"]["status"] == "completed"
    log = read_request_log(tmp_path)
    assert [m["method"] for m in log] == ["initialize", "initialized", "thread/start", "turn/start"]


def test_fake_request_step_blocks_for_reply(tmp_path):
    scenario = {
        "turns": [
            [
                {
                    "request": "item/commandExecution/requestApproval",
                    "params": {"itemId": "c1", "command": "ls"},
                    "record_as": "approval",
                }
            ]
        ]
    }
    binary = fake_codex_bin(tmp_path, scenario)
    out = _run(
        binary,
        [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t", "version": "0"}}},
            {"id": 2, "method": "thread/start", "params": {}},
            {"id": 3, "method": "turn/start", "params": {"threadId": "thread-1", "input": []}},
            {"id": "srv-1", "result": {"decision": "accept"}},
        ],
    )
    req = next(o for o in out if o.get("method") == "item/commandExecution/requestApproval")
    assert req["id"] == "srv-1"
    log = read_request_log(tmp_path)
    assert any(m.get("approval") == {"decision": "accept"} for m in log)


def test_fake_resume_unknown_thread_errors(tmp_path):
    binary = fake_codex_bin(tmp_path, {"resumable": ["thread-9"]})
    out = _run(
        binary,
        [
            {"id": 1, "method": "thread/resume", "params": {"threadId": "thread-9"}},
            {"id": 2, "method": "thread/resume", "params": {"threadId": "thread-8"}},
        ],
    )
    by_id = {o["id"]: o for o in out if "id" in o}
    assert by_id[1]["result"]["thread"]["id"] == "thread-9"
    assert by_id[2]["error"]["code"] == -32000


def test_fake_exit_step_dies(tmp_path):
    binary = fake_codex_bin(tmp_path, {"turns": [[{"exit": 3}]]})
    proc = subprocess.run(
        [binary, "app-server"],
        input=json.dumps({"id": 1, "method": "thread/start", "params": {}})
        + "\n"
        + json.dumps({"id": 2, "method": "turn/start", "params": {"threadId": "thread-1", "input": []}})
        + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 3
    assert "fake: dying" in proc.stderr


def test_wrapper_uses_current_interpreter(tmp_path):
    binary = fake_codex_bin(tmp_path, {})
    assert sys.executable in open(binary).read()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_codex_fake_server.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'tests.fakes'`.

- [ ] **Step 3: Implement the fake**

`tests/fakes/__init__.py`:

```python
"""Test doubles for external processes."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

_FAKE = Path(__file__).with_name("codex_app_server.py")


def fake_codex_bin(tmp_path: Path, scenario: dict) -> str:
    """Write ``scenario`` and an executable ``codex`` wrapper into ``tmp_path``;
    return the wrapper path (set it as ``MARIM_CODEX_CLI_BIN``)."""
    scenario_path = tmp_path / "codex-scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    log_path = tmp_path / "codex-requests.jsonl"
    wrapper = tmp_path / "codex"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'MARIM_CODEX_FAKE_SCENARIO="{scenario_path}" '
        f'MARIM_CODEX_FAKE_LOG="{log_path}" '
        f'exec "{sys.executable}" "{_FAKE}" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def read_request_log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "codex-requests.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
```

`tests/fakes/codex_app_server.py`:

```python
"""A scripted stand-in for ``codex app-server`` (JSON-RPC over stdio).

Reads a scenario from ``$MARIM_CODEX_FAKE_SCENARIO`` (see Task 3 of the plan
for the schema) and logs every client message to ``$MARIM_CODEX_FAKE_LOG``.
Single-threaded: a turn script runs to completion on the stdin loop, reading
only the replies to its own server requests (and ``turn/interrupt``).
"""

from __future__ import annotations

import json
import os
import sys
import time

DEFAULT_MODELS = [
    {
        "id": "gpt-5.6-sol",
        "model": "gpt-5.6-sol",
        "displayName": "GPT-5.6 Sol",
        "hidden": False,
        "isDefault": True,
        "inputModalities": ["text", "image"],
        "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh")],
    },
    {
        "id": "gpt-5.4-mini",
        "model": "gpt-5.4-mini",
        "displayName": "GPT-5.4 mini",
        "hidden": False,
        "isDefault": False,
        "inputModalities": ["text"],
        "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium")],
    },
]


class Fake:
    def __init__(self, scenario: dict, log_path: str) -> None:
        self.scenario = scenario
        self.log = open(log_path, "a")  # noqa: SIM115 - lives for the process
        self.threads = 0
        self.turns = 0
        self.turn_scripts = scenario.get("turns") or [[]]
        self.server_req_n = 0
        self.pending_interrupt = False

    # --- wire helpers -------------------------------------------------------
    def send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def record(self, obj: dict) -> None:
        self.log.write(json.dumps(obj) + "\n")
        self.log.flush()

    def read(self) -> dict | None:
        line = sys.stdin.readline()
        if not line:
            return None
        obj = json.loads(line)
        self.record(obj)
        return obj

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    # --- dispatch -----------------------------------------------------------
    def serve(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                return
            self.handle(msg)

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        if method is None:
            return  # a stray reply outside a request step
        if method == "initialized":
            return
        handler = {
            "initialize": self.on_initialize,
            "thread/start": self.on_thread_start,
            "thread/resume": self.on_thread_resume,
            "turn/start": self.on_turn_start,
            "turn/interrupt": self.on_turn_interrupt,
            "turn/steer": self.on_turn_steer,
            "thread/compact/start": self.on_compact,
            "model/list": self.on_model_list,
        }.get(method)
        if handler is None:
            self.send({"id": rid, "error": {"code": -32601, "message": f"unknown {method}"}})
            return
        handler(rid, msg.get("params") or {})

    # --- methods ------------------------------------------------------------
    def on_initialize(self, rid, params) -> None:
        self.send(
            {
                "id": rid,
                "result": {
                    "userAgent": self.scenario.get("userAgent", "codex_cli_rs/0.152.1 (fake)"),
                    "codexHome": "/fake/.codex",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                },
            }
        )

    def _thread_obj(self, tid: str, params: dict) -> dict:
        return {
            "thread": {"id": tid, "cwd": params.get("cwd", "/"), "ephemeral": bool(params.get("ephemeral"))},
            "model": params.get("model") or "gpt-5.6-sol",
            "modelProvider": "openai",
            "cwd": params.get("cwd", "/"),
            "approvalPolicy": params.get("approvalPolicy", "on-request"),
            "approvalsReviewer": "user",
            "sandbox": params.get("sandbox", "workspace-write"),
        }

    def on_thread_start(self, rid, params) -> None:
        self.threads += 1
        tid = f"thread-{self.threads}"
        result = self._thread_obj(tid, params)
        self.send({"id": rid, "result": result})
        self.notify("thread/started", {"thread": result["thread"]})

    def on_thread_resume(self, rid, params) -> None:
        tid = params.get("threadId", "")
        if tid not in (self.scenario.get("resumable") or []):
            self.send({"id": rid, "error": {"code": -32000, "message": "thread not found"}})
            return
        self.send({"id": rid, "result": self._thread_obj(tid, params)})

    def on_model_list(self, rid, params) -> None:
        self.send({"id": rid, "result": {"data": self.scenario.get("models", DEFAULT_MODELS), "nextCursor": None}})

    def on_compact(self, rid, params) -> None:
        self.send({"id": rid, "result": {}})
        self.notify("thread/compacted", {"threadId": params.get("threadId"), "turnId": None})

    def on_turn_steer(self, rid, params) -> None:
        self.send({"id": rid, "result": {"turnId": params.get("expectedTurnId")}})

    def on_turn_interrupt(self, rid, params) -> None:
        # Outside a running script an interrupt is a no-op ack.
        self.pending_interrupt = True
        self.send({"id": rid, "result": {}})

    def on_turn_start(self, rid, params) -> None:
        self.turns += 1
        turn_id = f"turn-{self.turns}"
        tid = params.get("threadId", "thread-1")
        self.send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        self.notify("turn/started", {"threadId": tid, "turn": {"id": turn_id, "status": "inProgress"}})
        script = self.turn_scripts[min(self.turns - 1, len(self.turn_scripts) - 1)]
        status, error = self.run_script(script, tid, turn_id)
        turn = {"id": turn_id, "status": status, "items": [], "error": error}
        self.notify("turn/completed", {"threadId": tid, "turn": turn})

    # --- script execution ---------------------------------------------------
    def _subst(self, value, tid: str, turn_id: str):
        if isinstance(value, str):
            return value.replace("$THREAD", tid).replace("$TURN", turn_id)
        if isinstance(value, dict):
            return {k: self._subst(v, tid, turn_id) for k, v in value.items()}
        if isinstance(value, list):
            return [self._subst(v, tid, turn_id) for v in value]
        return value

    def _params(self, step: dict, tid: str, turn_id: str) -> dict:
        params = dict(self._subst(step.get("params") or {}, tid, turn_id))
        params.setdefault("threadId", tid)
        params.setdefault("turnId", turn_id)
        return params

    def run_script(self, script: list[dict], tid: str, turn_id: str) -> tuple[str, dict | None]:
        self.pending_interrupt = False
        for step in script:
            if "notify" in step:
                self.notify(step["notify"], self._params(step, tid, turn_id))
            elif "request" in step:
                self.server_request(step, tid, turn_id)
            elif step.get("hang"):
                self.wait_for_interrupt()
                return "interrupted", None
            elif "sleep" in step:
                time.sleep(float(step["sleep"]))
            elif "exit" in step:
                sys.stderr.write("fake: dying\n")
                sys.stderr.flush()
                os._exit(int(step["exit"]))
            elif "fail" in step:
                return "failed", {"message": str(step["fail"])}
            if self.pending_interrupt:
                return "interrupted", None
        return "completed", None

    def server_request(self, step: dict, tid: str, turn_id: str) -> None:
        self.server_req_n += 1
        rid = f"srv-{self.server_req_n}"
        self.send({"id": rid, "method": step["request"], "params": self._params(step, tid, turn_id)})
        while True:
            msg = self.read()
            if msg is None:
                os._exit(0)
            if msg.get("id") == rid and "method" not in msg:
                self.record({step.get("record_as", "reply"): msg.get("result", msg.get("error"))})
                return
            if msg.get("method") == "turn/interrupt":
                self.on_turn_interrupt(msg.get("id"), msg.get("params") or {})
                continue
            self.handle(msg)  # unrelated request (e.g. model/list) answered inline

    def wait_for_interrupt(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                os._exit(0)
            if msg.get("method") == "turn/interrupt":
                self.on_turn_interrupt(msg.get("id"), msg.get("params") or {})
                return
            self.handle(msg)


def main() -> None:
    scenario_path = os.environ.get("MARIM_CODEX_FAKE_SCENARIO")
    scenario = json.load(open(scenario_path)) if scenario_path else {}
    log_path = os.environ.get("MARIM_CODEX_FAKE_LOG") or os.devnull
    if sys.argv[1:] != ["app-server"]:
        sys.stderr.write(f"fake codex: unsupported argv {sys.argv[1:]!r}\n")
        sys.exit(2)
    Fake(scenario, log_path).serve()


if __name__ == "__main__":
    main()
```

Note the "wait for stdin" path in the test harness: the smoke test writes all messages up front and closes stdin, so `read()` returning `None` after the script is the normal end.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_fake_server.py -v`
Expected: all PASS. Also confirm `tests/fakes` is collected as a package: `uv run pytest --no-cov --collect-only -q tests/test_codex_fake_server.py | tail -3`.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests
git add tests/fakes tests/test_codex_fake_server.py
git commit -m "test(codex): scripted fake app-server fixture

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---
### Task 4: `codex/server.py` — process supervisor + thread router

**Files:**
- Create: `src/marim_harness/codex/server.py`
- Test: `tests/test_codex_server.py`

**Interfaces:**
- Consumes: `JsonRpcClient`, `RpcError`, `ServerRequestHandler` (Task 2); `resolve_codex_binary`, `codex_timeout`, `check_min_version`, `CodexUnavailable`, `INSTALL_HINT` (Task 1); `tests.fakes.fake_codex_bin` (Task 3).
- Produces:
  - `CLOSED = "__closed__"` — sentinel method name queued to every open thread when the process dies; its params are `{"stderr": "<last ~40 lines>"}`.
  - `@dataclass class ThreadHandle`: `thread_id: str`, `events: asyncio.Queue[tuple[str, dict]]`, `request_handler: ServerRequestHandler`, `usage_baseline: dict = field(default_factory=dict)`, `current_turn_id: str | None = None`.
  - `class CodexServer(*, binary: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None)`:
    - `async start() -> None` — idempotent; spawns, `initialize`, version check, `initialized`.
    - `async start_thread(*, cwd: str, developer_instructions: str | None, model: str | None, ephemeral: bool, sandbox: str, approval_policy: str, request_handler: ServerRequestHandler) -> ThreadHandle` (`sandbox` is a `SandboxMode` string: `"read-only"` / `"workspace-write"`).
    - `async resume_thread(thread_id: str, *, cwd: str, developer_instructions: str | None, model: str | None, sandbox: str, approval_policy: str, request_handler: ServerRequestHandler) -> ThreadHandle | None` (None when the server says the thread is gone).
    - `async start_turn(handle: ThreadHandle, *, inputs: list[dict], model: str | None, effort: str | None, approval_policy: str, sandbox_policy: dict, output_schema: dict | None = None) -> str` (returns the turn id; sets `handle.current_turn_id`).
    - `async interrupt(handle: ThreadHandle) -> None` (no-op without a current turn; swallows `RpcError`).
    - `async steer(handle: ThreadHandle, text: str) -> bool` (False without a current turn or on `RpcError`).
    - `async compact(handle: ThreadHandle) -> None` (best effort).
    - `async list_models() -> list[dict]` (the `data` array of `model/list`).
    - `def drop_thread(handle: ThreadHandle) -> None`.
    - `async aclose() -> None` — kills the process group; safe to call twice.
    - `@property alive -> bool`.
  - `thread_config() -> dict` — the `thread/start.config` isolation overrides (module-level so Task 0 can change one place).
  - `shared_server() -> CodexServer` (lazy module singleton, honours `MARIM_CODEX_CLI_BIN` at first use), `async close_shared_server() -> None`.
- Notification routing: `params["threadId"]` (or `params["thread"]["id"]` for `thread/started`) selects the handle; matching handles get `(method, params)` queued. Notifications without a thread id (`warning` without `threadId`, unknown) go to **every** open handle. Server requests route the same way to the handle's `request_handler`; an unroutable request is answered with `RpcError(-32601, ...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codex_server.py
from __future__ import annotations

import asyncio

import pytest

from marim_harness.codex.env import CodexUnavailable
from marim_harness.codex.server import CLOSED, CodexServer, ThreadHandle, thread_config
from tests.fakes import fake_codex_bin, read_request_log

pytestmark = pytest.mark.anyio


async def _decline(method: str, params: dict) -> dict:
    return {"decision": "decline"}


WS = {
    "type": "workspaceWrite",
    "writableRoots": ["/w"],
    "networkAccess": False,
    "excludeSlashTmp": False,
    "excludeTmpdirEnvVar": False,
}


async def _drain_until(handle: ThreadHandle, method: str, timeout: float = 5.0) -> list[tuple[str, dict]]:
    """Collect events until ``method`` arrives (per-event timeout; 3.10-safe)."""
    got: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(handle.events.get(), timeout)
        got.append(item)
        if item[0] == method:
            return got


async def test_start_initializes_and_checks_version(tmp_path):
    binary = fake_codex_bin(tmp_path, {})
    server = CodexServer(binary=binary)
    await server.start()
    try:
        assert server.alive
        log = read_request_log(tmp_path)
        assert log[0]["method"] == "initialize"
        assert log[0]["params"]["clientInfo"]["name"] == "marim-harness"
        assert log[1] == {"method": "initialized"}
    finally:
        await server.aclose()


async def test_start_rejects_old_version(tmp_path):
    binary = fake_codex_bin(tmp_path, {"userAgent": "codex_cli_rs/0.100.0"})
    server = CodexServer(binary=binary)
    with pytest.raises(CodexUnavailable, match="0.152"):
        await server.start()
    assert not server.alive


async def test_missing_binary_raises_unavailable(tmp_path):
    server = CodexServer(binary=str(tmp_path / "nope"))
    with pytest.raises(CodexUnavailable):
        await server.start()


async def test_thread_and_turn_events_route_to_handle(tmp_path):
    scenario = {
        "turns": [
            [
                {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "hi"}},
                {"notify": "warning", "params": {"message": "careful"}},
            ]
        ]
    }
    server = CodexServer(binary=fake_codex_bin(tmp_path, scenario))
    await server.start()
    try:
        handle = await server.start_thread(
            cwd="/w",
            developer_instructions="be brief",
            model="gpt-5.6-sol",
            ephemeral=False,
            sandbox="workspace-write",
            approval_policy="on-request",
            request_handler=_decline,
        )
        assert handle.thread_id == "thread-1"
        turn_id = await server.start_turn(
            handle,
            inputs=[{"type": "text", "text": "hello", "text_elements": []}],
            model="gpt-5.6-sol",
            effort="medium",
            approval_policy="on-request",
            sandbox_policy=WS,
        )
        assert turn_id == "turn-1" and handle.current_turn_id == "turn-1"
        events = await _drain_until(handle, "turn/completed")
        methods = [m for m, _ in events]
        assert "item/agentMessage/delta" in methods and "warning" in methods
        assert handle.current_turn_id is None  # cleared on turn/completed
        log = read_request_log(tmp_path)
        start = next(m for m in log if m.get("method") == "thread/start")
        assert start["params"]["developerInstructions"] == "be brief"
        assert start["params"]["config"] == thread_config()
        assert start["params"]["ephemeral"] is False
        turn = next(m for m in log if m.get("method") == "turn/start")
        assert turn["params"]["effort"] == "medium"
        assert turn["params"]["sandboxPolicy"] == WS
        assert "outputSchema" not in turn["params"]
    finally:
        await server.aclose()


async def test_effort_none_is_omitted_and_output_schema_forwarded(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    await server.start()
    try:
        handle = await server.start_thread(
            cwd="/w", developer_instructions=None, model=None, ephemeral=True,
            sandbox="read-only", approval_policy="never", request_handler=_decline,
        )
        await server.start_turn(
            handle, inputs=[], model=None, effort=None, approval_policy="never",
            sandbox_policy={"type": "readOnly", "networkAccess": False},
            output_schema={"type": "object"},
        )
        await _drain_until(handle, "turn/completed")
        turn = next(m for m in read_request_log(tmp_path) if m.get("method") == "turn/start")
        assert "effort" not in turn["params"] and "model" not in turn["params"]
        assert turn["params"]["outputSchema"] == {"type": "object"}
        start = next(m for m in read_request_log(tmp_path) if m.get("method") == "thread/start")
        assert "developerInstructions" not in start["params"] and "model" not in start["params"]
    finally:
        await server.aclose()


async def test_server_request_routes_to_thread_handler(tmp_path):
    scenario = {
        "turns": [[{"request": "item/commandExecution/requestApproval", "params": {"itemId": "c1"},
                    "record_as": "approval"}]]
    }
    seen: list[str] = []

    async def handler(method: str, params: dict) -> dict:
        seen.append(method)
        return {"decision": "accept"}

    server = CodexServer(binary=fake_codex_bin(tmp_path, scenario))
    await server.start()
    try:
        handle = await server.start_thread(
            cwd="/w", developer_instructions=None, model=None, ephemeral=True,
            sandbox="workspace-write", approval_policy="untrusted", request_handler=handler,
        )
        await server.start_turn(handle, inputs=[], model=None, effort=None,
                                approval_policy="untrusted", sandbox_policy=WS)
        await _drain_until(handle, "turn/completed")
        assert seen == ["item/commandExecution/requestApproval"]
        assert any(m.get("approval") == {"decision": "accept"} for m in read_request_log(tmp_path))
    finally:
        await server.aclose()


async def test_interrupt_ends_hanging_turn(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"turns": [[{"hang": True}]]}))
    await server.start()
    try:
        handle = await server.start_thread(
            cwd="/w", developer_instructions=None, model=None, ephemeral=True,
            sandbox="read-only", approval_policy="never", request_handler=_decline,
        )
        await server.start_turn(handle, inputs=[], model=None, effort=None,
                                approval_policy="never", sandbox_policy=WS)
        await asyncio.sleep(0.2)
        await server.interrupt(handle)
        events = await _drain_until(handle, "turn/completed")
        assert events[-1][1]["turn"]["status"] == "interrupted"
    finally:
        await server.aclose()


async def test_steer_requires_active_turn(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"turns": [[{"hang": True}]]}))
    await server.start()
    try:
        handle = await server.start_thread(
            cwd="/w", developer_instructions=None, model=None, ephemeral=True,
            sandbox="read-only", approval_policy="never", request_handler=_decline,
        )
        assert await server.steer(handle, "nope") is False
        await server.start_turn(handle, inputs=[], model=None, effort=None,
                                approval_policy="never", sandbox_policy=WS)
        await asyncio.sleep(0.2)
        assert await server.steer(handle, "also do X") is True
        steer = next(m for m in read_request_log(tmp_path) if m.get("method") == "turn/steer")
        assert steer["params"]["expectedTurnId"] == "turn-1"
        assert steer["params"]["input"][0]["text"] == "also do X"
        await server.interrupt(handle)
        await _drain_until(handle, "turn/completed")
    finally:
        await server.aclose()


async def test_resume_thread_unknown_returns_none(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"resumable": ["thread-42"]}))
    await server.start()
    try:
        ok = await server.resume_thread("thread-42", cwd="/w", developer_instructions=None, model=None,
                                        sandbox="read-only", approval_policy="never", request_handler=_decline)
        assert ok is not None and ok.thread_id == "thread-42"
        gone = await server.resume_thread("thread-7", cwd="/w", developer_instructions=None, model=None,
                                          sandbox="read-only", approval_policy="never", request_handler=_decline)
        assert gone is None
    finally:
        await server.aclose()


async def test_crash_delivers_closed_with_stderr_and_respawns(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {"turns": [[{"exit": 1}], []]}))
    await server.start()
    try:
        handle = await server.start_thread(
            cwd="/w", developer_instructions=None, model=None, ephemeral=True,
            sandbox="read-only", approval_policy="never", request_handler=_decline,
        )
        await server.start_turn(handle, inputs=[], model=None, effort=None,
                                approval_policy="never", sandbox_policy=WS)
        events = await _drain_until(handle, CLOSED)
        assert "fake: dying" in events[-1][1]["stderr"]
        assert not server.alive
        await server.start()  # respawn
        assert server.alive
        again = await server.start_thread(
            cwd="/w", developer_instructions=None, model=None, ephemeral=True,
            sandbox="read-only", approval_policy="never", request_handler=_decline,
        )
        assert again.thread_id == "thread-1"  # a fresh fake process
    finally:
        await server.aclose()


async def test_list_models_and_compact(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    await server.start()
    try:
        models = await server.list_models()
        assert [m["model"] for m in models] == ["gpt-5.6-sol", "gpt-5.4-mini"]
        handle = await server.start_thread(
            cwd="/w", developer_instructions=None, model=None, ephemeral=False,
            sandbox="read-only", approval_policy="never", request_handler=_decline,
        )
        await server.compact(handle)
        await _drain_until(handle, "thread/compacted")
    finally:
        await server.aclose()


async def test_aclose_is_idempotent(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    await server.start()
    await server.aclose()
    await server.aclose()
    assert not server.alive
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_codex_server.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'marim_harness.codex.server'`.

- [ ] **Step 3: Implement**

```python
# src/marim_harness/codex/server.py
"""One ``codex app-server`` process, shared by the main-loop model, its
ephemeral aux clones and every ``backend: codex-cli`` spawn.

Threads are the unit of isolation: each caller gets a ``ThreadHandle`` whose
``events`` queue receives only that thread's notifications, and whose
``request_handler`` answers only that thread's approval prompts. The process
is respawned lazily on the next ``start()`` after it dies; the death itself
is broadcast to every open handle as a ``CLOSED`` pseudo-notification so an
in-flight turn fails cleanly (the model layer turns it into ``CliModelError``).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import importlib.metadata
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any

from .env import (
    INSTALL_HINT,
    CodexUnavailable,
    check_min_version,
    codex_timeout,
    resolve_codex_binary,
)
from .rpc import JsonRpcClient, RpcError, ServerRequestHandler

logger = logging.getLogger(__name__)

CLOSED = "__closed__"
_STDERR_TAIL_LINES = 40
_INIT_TIMEOUT = 30.0
_KILL_GRACE = 2.0
# Methods whose params name the thread under ``thread.id`` instead of ``threadId``.
_THREAD_OBJ_METHODS = frozenset({"thread/started"})


def thread_config() -> dict:
    """``thread/start.config`` overrides that stop the thread from loading the
    user's global Codex MCP servers/plugins (marim owns tool reach; see spec
    §Isolation). PROVISIONAL until Task 0 confirms the key names — if no
    config key works, the fallback is a marim-owned CODEX_HOME (Step 7)."""
    return {"mcp_servers": {}}


@dataclass
class ThreadHandle:
    thread_id: str
    events: asyncio.Queue[tuple[str, dict]]
    request_handler: ServerRequestHandler
    usage_baseline: dict = field(default_factory=dict)
    current_turn_id: str | None = None


def _text_input(text: str) -> dict:
    return {"type": "text", "text": text, "text_elements": []}


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


class CodexServer:
    def __init__(
        self,
        *,
        binary: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self._binary = binary
        self._env = env
        self._timeout = timeout if timeout is not None else codex_timeout()
        self._proc: asyncio.subprocess.Process | None = None
        self._client: JsonRpcClient | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
        self._threads: dict[str, ThreadHandle] = {}
        self._start_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._client is not None
            and not self._client.closed.is_set()
        )

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        async with self._start_lock:
            if self.alive:
                return
            await self._reap()
            binary = self._binary or resolve_codex_binary()
            if binary is None or not os.path.exists(binary):
                raise CodexUnavailable(f"codex binary not found. {INSTALL_HINT}")
            await self._spawn(binary)
            await self._handshake()

    async def _spawn(self, binary: str) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                binary,
                "app-server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                # Own process group so aclose() can kill helpers codex forks.
                start_new_session=True,
            )
        except OSError as exc:
            raise CodexUnavailable(f"could not launch {binary}: {exc}. {INSTALL_HINT}") from exc
        assert self._proc.stdout is not None and self._proc.stdin is not None
        self._stderr_tail.clear()
        self._client = JsonRpcClient(
            self._proc.stdout,
            self._proc.stdin,
            on_notification=self._on_notification,
            on_server_request=self._on_server_request,
        )
        self._reader_task = asyncio.create_task(self._run_reader(self._client))
        self._stderr_task = asyncio.create_task(self._pump_stderr())

    async def _handshake(self) -> None:
        assert self._client is not None
        try:
            version = importlib.metadata.version("marim-harness")
        except importlib.metadata.PackageNotFoundError:
            version = "dev"
        try:
            result = await self._client.request(
                "initialize",
                {
                    "clientInfo": {"name": "marim-harness", "version": version},
                    "capabilities": {"experimentalApi": False},
                },
                timeout=_INIT_TIMEOUT,
            )
        except (RpcError, asyncio.TimeoutError) as exc:
            await self.aclose()
            raise CodexUnavailable(f"codex app-server initialize failed: {exc}") from exc
        try:
            check_min_version(str(result.get("userAgent", "")))
        except CodexUnavailable:
            await self.aclose()
            raise
        await self._client.notify("initialized")

    async def _run_reader(self, client: JsonRpcClient) -> None:
        try:
            await client.run()
        finally:
            # Process gone (or stdout closed): every open thread learns it once.
            tail = "\n".join(self._stderr_tail)
            for handle in list(self._threads.values()):
                handle.current_turn_id = None
                handle.events.put_nowait((CLOSED, {"stderr": tail}))

    async def _pump_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        async for raw in self._proc.stderr:
            self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip("\n"))

    async def _reap(self) -> None:
        """Forget a dead process (and its tasks) before spawning a new one."""
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = self._stderr_task = None
        self._client = None
        self._proc = None

    async def aclose(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), _KILL_GRACE)
            except (ProcessLookupError, PermissionError):
                pass
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        await self._reap()
        self._threads.clear()

    # --- routing ------------------------------------------------------------
    def _handle_for(self, method: str, params: dict) -> ThreadHandle | None:
        if method in _THREAD_OBJ_METHODS:
            tid = (params.get("thread") or {}).get("id")
        else:
            tid = params.get("threadId")
        return self._threads.get(str(tid)) if tid else None

    async def _on_notification(self, method: str, params: dict) -> None:
        handle = self._handle_for(method, params)
        targets = [handle] if handle is not None else list(self._threads.values())
        if method == "turn/completed" and handle is not None:
            handle.current_turn_id = None
        for h in targets:
            h.events.put_nowait((method, params))

    async def _on_server_request(self, method: str, params: dict) -> dict:
        handle = self._handle_for(method, params)
        if handle is None:
            raise RpcError(-32601, f"no thread for {method}")
        return await handle.request_handler(method, params)

    # --- threads ------------------------------------------------------------
    def _rpc(self) -> JsonRpcClient:
        if self._client is None or not self.alive:
            raise CodexUnavailable("codex app-server is not running")
        return self._client

    def _register(self, thread: dict, request_handler: ServerRequestHandler) -> ThreadHandle:
        handle = ThreadHandle(
            thread_id=str(thread["id"]), events=asyncio.Queue(), request_handler=request_handler
        )
        self._threads[handle.thread_id] = handle
        return handle

    async def start_thread(
        self,
        *,
        cwd: str,
        developer_instructions: str | None,
        model: str | None,
        ephemeral: bool,
        sandbox: str,
        approval_policy: str,
        request_handler: ServerRequestHandler,
    ) -> ThreadHandle:
        params = _drop_none(
            {
                "cwd": cwd,
                "developerInstructions": developer_instructions,
                "model": model,
                "ephemeral": ephemeral,
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "config": thread_config(),
            }
        )
        result = await self._rpc().request("thread/start", params, timeout=self._timeout)
        return self._register(result["thread"], request_handler)

    async def resume_thread(
        self,
        thread_id: str,
        *,
        cwd: str,
        developer_instructions: str | None,
        model: str | None,
        sandbox: str,
        approval_policy: str,
        request_handler: ServerRequestHandler,
    ) -> ThreadHandle | None:
        params = _drop_none(
            {
                "threadId": thread_id,
                "cwd": cwd,
                "developerInstructions": developer_instructions,
                "model": model,
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "config": thread_config(),
            }
        )
        try:
            result = await self._rpc().request("thread/resume", params, timeout=self._timeout)
        except RpcError as exc:
            logger.info("codex thread %s not resumable: %s", thread_id, exc)
            return None
        return self._register(result["thread"], request_handler)

    def drop_thread(self, handle: ThreadHandle) -> None:
        self._threads.pop(handle.thread_id, None)

    # --- turns --------------------------------------------------------------
    async def start_turn(
        self,
        handle: ThreadHandle,
        *,
        inputs: list[dict],
        model: str | None,
        effort: str | None,
        approval_policy: str,
        sandbox_policy: dict,
        output_schema: dict | None = None,
    ) -> str:
        params = _drop_none(
            {
                "threadId": handle.thread_id,
                "input": inputs,
                "model": model,
                "effort": effort,
                "approvalPolicy": approval_policy,
                "sandboxPolicy": sandbox_policy,
                "outputSchema": output_schema,
            }
        )
        result = await self._rpc().request("turn/start", params, timeout=self._timeout)
        turn_id = str(result["turn"]["id"])
        handle.current_turn_id = turn_id
        return turn_id

    async def interrupt(self, handle: ThreadHandle) -> None:
        turn_id = handle.current_turn_id
        if turn_id is None or not self.alive:
            return
        try:
            await self._rpc().request(
                "turn/interrupt", {"threadId": handle.thread_id, "turnId": turn_id}, timeout=10.0
            )
        except (RpcError, asyncio.TimeoutError, CodexUnavailable) as exc:
            logger.debug("codex interrupt of %s ignored: %s", turn_id, exc)

    async def steer(self, handle: ThreadHandle, text: str) -> bool:
        turn_id = handle.current_turn_id
        if turn_id is None or not self.alive:
            return False
        try:
            await self._rpc().request(
                "turn/steer",
                {"threadId": handle.thread_id, "expectedTurnId": turn_id, "input": [_text_input(text)]},
                timeout=10.0,
            )
        except (RpcError, asyncio.TimeoutError, CodexUnavailable) as exc:
            logger.info("codex steer rejected: %s", exc)
            return False
        return True

    async def compact(self, handle: ThreadHandle) -> None:
        if not self.alive:
            return
        try:
            await self._rpc().request(
                "thread/compact/start", {"threadId": handle.thread_id}, timeout=self._timeout
            )
        except (RpcError, asyncio.TimeoutError, CodexUnavailable) as exc:
            logger.info("codex compact skipped: %s", exc)

    async def list_models(self) -> list[dict]:
        result = await self._rpc().request("model/list", {}, timeout=30.0)
        data = result.get("data")
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


# --- shared instance --------------------------------------------------------
_shared: CodexServer | None = None


def shared_server() -> CodexServer:
    """The process-wide server. Created lazily so importing this module (or
    building a CodexCliModel) never spawns anything; ``start()`` does."""
    global _shared
    if _shared is None:
        _shared = CodexServer()
    return _shared


async def close_shared_server() -> None:
    global _shared
    if _shared is not None:
        await _shared.aclose()
        _shared = None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_server.py -v`
Expected: all PASS. If `test_crash_delivers_closed_with_stderr_and_respawns` races on the stderr tail (the pump can lag the EOF by a tick), make `_run_reader` `await` the stderr task with a 0.5s `wait_for` before building `tail` — that ordering is the fix, not a longer sleep in the test.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/codex/server.py tests/test_codex_server.py
git commit -m "feat(codex): app-server supervisor with per-thread routing and respawn

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6: (Only if Task 0 Q2 found no working config key) CODEX_HOME fallback.** Add to `server.py`:

```python
def isolated_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A marim-owned CODEX_HOME (``$XDG_DATA_HOME/marim-harness/codex-home``)
    that symlinks the user's ``auth.json`` but ships an empty ``config.toml``,
    so no global MCP servers/plugins load. Used only when ``thread_config()``
    cannot express the isolation."""
    from pathlib import Path

    from .env import codex_home

    env = dict(base or os.environ)
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "marim-harness/codex-home"
    home.mkdir(parents=True, exist_ok=True)
    auth = home / "auth.json"
    if not auth.exists() and (codex_home() / "auth.json").exists():
        auth.symlink_to(codex_home() / "auth.json")
    (home / "config.toml").touch()
    env["CODEX_HOME"] = str(home)
    return env
```

wire it as `CodexServer(env=isolated_env())` inside `shared_server()`, make `thread_config()` return `{}`, and add a test that `isolated_env(tmp env)["CODEX_HOME"]` points at a dir containing a symlinked `auth.json` and an empty `config.toml`. Commit as `feat(codex): isolate app-server via a marim-owned CODEX_HOME`.

---

### Task 5: `codex/translate.py` — notifications → neutral events

**Files:**
- Create: `src/marim_harness/codex/translate.py`
- Test: `tests/test_codex_translate.py`

**Interfaces:**
- Consumes: nothing from the package (pure).
- Produces frozen dataclasses (all in `translate.py`):
  - `TextDelta(item_id: str, delta: str)`
  - `ThinkingDelta(item_id: str, delta: str)`
  - `ActivityStart(item_id: str, tool_name: str, args: dict)`
  - `ActivityEnd(item_id: str, content: str, is_error: bool)`
  - `UsageUpdate(total: dict)` — the `tokenUsage.total` breakdown as sent.
  - `TurnDone(status: str, error: str | None)` — status ∈ `completed|interrupted|failed`.
  - `TurnFailure(message: str, will_retry: bool)`
  - `Notice(message: str)`
  - `class ItemTranslator` with `translate(method: str, params: dict) -> list[object]` (stateful: tracks which agentMessage/reasoning items already streamed, buffers `commandExecution/outputDelta` per item).
  - `tool_name_for(item: dict) -> str | None` and `args_for(item: dict) -> dict` helpers (used by the spawn transcript recorder in Task 11).
- Mapping (spec §Notification → event table):

| Notification | Event |
|---|---|
| `item/agentMessage/delta` | `TextDelta` |
| `item/reasoning/textDelta`, `item/reasoning/summaryTextDelta` | `ThinkingDelta` |
| `item/started` with item type `commandExecution` | `ActivityStart(tool_name="bash", args={"command", "cwd"})` |
| `item/started` `fileChange` | one `ActivityStart("apply_patch", {"path","kind","diff"})` per change, ids `f"{item_id}:{n}"` |
| `item/started` `mcpToolCall` | `ActivityStart(f"{server}.{tool}", arguments)` |
| `item/started` `webSearch` | `ActivityStart("web_search", {"query"})` |
| `item/started` `collabAgentToolCall` / `subAgentActivity` | `ActivityStart("codex_agent", {…prompt/model/agentPath})` |
| `item/started` `plan` | `ActivityStart("update_plan", {"text"})` + immediate `ActivityEnd` |
| `item/started` `contextCompaction` | `Notice("Codex compacted its context")` |
| `item/commandExecution/outputDelta` | buffered; flushed into `ActivityEnd.content` |
| `item/completed` `commandExecution` | `ActivityEnd(content=buffered output or aggregatedOutput + exit line, is_error=status!="completed")` |
| `item/completed` `fileChange` | one `ActivityEnd` per change (`content=diff`, `is_error=status!="completed"`) |
| `item/completed` `mcpToolCall` / `webSearch` / collab | `ActivityEnd(content=json result or error, is_error=bool(error))` |
| `item/completed` `agentMessage` / `reasoning` | `TextDelta`/`ThinkingDelta` with the **full** text only if no delta was seen for that item id (non-streaming servers); else nothing |
| `thread/tokenUsage/updated` | `UsageUpdate(tokenUsage["total"])` |
| `turn/completed` | `TurnDone(turn["status"], turn["error"]["message"] or None)` |
| `error` | `TurnFailure(error["message"], willRetry)` |
| `warning` | `Notice(message)` |
| anything else | `[]` |

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codex_translate.py
from __future__ import annotations

from marim_harness.codex.translate import (
    ActivityEnd,
    ActivityStart,
    ItemTranslator,
    Notice,
    TextDelta,
    ThinkingDelta,
    TurnDone,
    TurnFailure,
    UsageUpdate,
    args_for,
    tool_name_for,
)


def _t() -> ItemTranslator:
    return ItemTranslator()


def test_agent_message_deltas_then_completed_emits_nothing_twice():
    t = _t()
    assert t.translate("item/agentMessage/delta", {"itemId": "m1", "delta": "he"}) == [TextDelta("m1", "he")]
    assert t.translate("item/agentMessage/delta", {"itemId": "m1", "delta": "y"}) == [TextDelta("m1", "y")]
    done = {"item": {"type": "agentMessage", "id": "m1", "text": "hey"}}
    assert t.translate("item/completed", done) == []


def test_agent_message_completed_without_deltas_emits_full_text():
    t = _t()
    done = {"item": {"type": "agentMessage", "id": "m2", "text": "all at once"}}
    assert t.translate("item/completed", done) == [TextDelta("m2", "all at once")]


def test_reasoning_deltas_and_summary():
    t = _t()
    assert t.translate("item/reasoning/textDelta", {"itemId": "r1", "delta": "th"}) == [ThinkingDelta("r1", "th")]
    assert t.translate("item/reasoning/summaryTextDelta", {"itemId": "r1", "delta": "ink"}) == [
        ThinkingDelta("r1", "ink")
    ]
    done = {"item": {"type": "reasoning", "id": "r9", "content": ["a", "b"], "summary": ["s"]}}
    assert t.translate("item/completed", done) == [ThinkingDelta("r9", "s")]


def test_command_execution_lifecycle_buffers_output():
    t = _t()
    started = {"item": {"type": "commandExecution", "id": "c1", "command": "ls -la", "cwd": "/w",
                        "status": "inProgress"}}
    assert t.translate("item/started", started) == [
        ActivityStart("c1", "bash", {"command": "ls -la", "cwd": "/w"})
    ]
    assert t.translate("item/commandExecution/outputDelta", {"itemId": "c1", "delta": "a\n"}) == []
    assert t.translate("item/commandExecution/outputDelta", {"itemId": "c1", "delta": "b\n"}) == []
    done = {"item": {"type": "commandExecution", "id": "c1", "command": "ls -la", "status": "completed",
                     "exitCode": 0, "aggregatedOutput": "ignored"}}
    [end] = t.translate("item/completed", done)
    assert end == ActivityEnd("c1", "a\nb\n", False)


def test_command_failure_uses_aggregated_output_and_exit_code():
    t = _t()
    done = {"item": {"type": "commandExecution", "id": "c2", "command": ["git", "status"],
                     "status": "failed", "exitCode": 128, "aggregatedOutput": "fatal"}}
    [end] = t.translate("item/completed", done)
    assert end.is_error and "fatal" in end.content and "128" in end.content


def test_command_list_form_is_joined():
    item = {"type": "commandExecution", "command": ["git", "commit", "-m", "a b"], "cwd": "/w"}
    assert args_for(item) == {"command": "git commit -m 'a b'", "cwd": "/w"}
    assert tool_name_for(item) == "bash"


def test_file_change_fans_out_per_change():
    t = _t()
    item = {"type": "fileChange", "id": "f1", "status": "inProgress", "changes": [
        {"path": "/w/a.py", "kind": {"type": "update"}, "diff": "-x\n+y"},
        {"path": "/w/b.py", "kind": {"type": "add"}, "diff": "+new"},
    ]}
    starts = t.translate("item/started", {"item": item})
    assert starts == [
        ActivityStart("f1:0", "apply_patch", {"path": "/w/a.py", "kind": "update", "diff": "-x\n+y"}),
        ActivityStart("f1:1", "apply_patch", {"path": "/w/b.py", "kind": "add", "diff": "+new"}),
    ]
    ends = t.translate("item/completed", {"item": {**item, "status": "completed"}})
    assert ends == [ActivityEnd("f1:0", "-x\n+y", False), ActivityEnd("f1:1", "+new", False)]


def test_mcp_web_search_collab_plan_and_compaction():
    t = _t()
    mcp = {"type": "mcpToolCall", "id": "t1", "server": "gh", "tool": "issues", "arguments": {"q": 1}}
    assert t.translate("item/started", {"item": mcp}) == [ActivityStart("t1", "gh.issues", {"q": 1})]
    [end] = t.translate("item/completed", {"item": {**mcp, "result": {"ok": True}, "error": None,
                                                    "status": "completed"}})
    assert end == ActivityEnd("t1", '{"ok": true}', False)
    [end] = t.translate("item/completed", {"item": {**mcp, "id": "t2", "error": {"message": "nope"},
                                                    "status": "failed"}})
    assert end.is_error and "nope" in end.content
    ws = {"type": "webSearch", "id": "w1", "query": "python 3.14"}
    assert t.translate("item/started", {"item": ws}) == [ActivityStart("w1", "web_search", {"query": "python 3.14"})]
    collab = {"type": "collabAgentToolCall", "id": "k1", "prompt": "review", "model": "gpt-5.4-mini",
              "receiverThreadIds": ["x"], "status": "inProgress"}
    [start] = t.translate("item/started", {"item": collab})
    assert start.tool_name == "codex_agent" and start.args["prompt"] == "review"
    plan = {"type": "plan", "id": "p1", "text": "1. do\n2. done"}
    assert t.translate("item/started", {"item": plan}) == [
        ActivityStart("p1", "update_plan", {"text": "1. do\n2. done"}),
        ActivityEnd("p1", "1. do\n2. done", False),
    ]
    assert t.translate("item/started", {"item": {"type": "contextCompaction", "id": "z"}}) == [
        Notice("Codex compacted its context")
    ]
    assert t.translate("item/started", {"item": {"type": "userMessage", "id": "u"}}) == []


def test_turn_level_notifications():
    t = _t()
    usage = {"tokenUsage": {"total": {"inputTokens": 10, "outputTokens": 2}, "last": {}}}
    assert t.translate("thread/tokenUsage/updated", usage) == [UsageUpdate({"inputTokens": 10, "outputTokens": 2})]
    assert t.translate("turn/completed", {"turn": {"id": "t", "status": "completed", "error": None}}) == [
        TurnDone("completed", None)
    ]
    assert t.translate("turn/completed", {"turn": {"status": "failed", "error": {"message": "quota"}}}) == [
        TurnDone("failed", "quota")
    ]
    assert t.translate("error", {"error": {"message": "rate limited"}, "willRetry": True}) == [
        TurnFailure("rate limited", True)
    ]
    assert t.translate("warning", {"message": "slow"}) == [Notice("slow")]
    assert t.translate("thread/closed", {"threadId": "x"}) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_codex_translate.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# src/marim_harness/codex/translate.py
"""Pure translation of app-server notifications into neutral events.

The model layer (config/codex_cli_model.py) turns ``TextDelta``/``ThinkingDelta``
into pydantic-ai stream events and ``ActivityStart``/``ActivityEnd`` into
display-only tool cards. Tool names are harness names (``bash``,
``apply_patch``, ``web_search``) so the TUI renders them like native calls.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class TextDelta:
    item_id: str
    delta: str


@dataclass(frozen=True)
class ThinkingDelta:
    item_id: str
    delta: str


@dataclass(frozen=True)
class ActivityStart:
    item_id: str
    tool_name: str
    args: dict


@dataclass(frozen=True)
class ActivityEnd:
    item_id: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class UsageUpdate:
    total: dict


@dataclass(frozen=True)
class TurnDone:
    status: str
    error: str | None


@dataclass(frozen=True)
class TurnFailure:
    message: str
    will_retry: bool


@dataclass(frozen=True)
class Notice:
    message: str


_TOOL_NAMES = {
    "commandExecution": "bash",
    "fileChange": "apply_patch",
    "webSearch": "web_search",
    "collabAgentToolCall": "codex_agent",
    "subAgentActivity": "codex_agent",
    "plan": "update_plan",
}


def _command_text(command: object) -> str:
    if isinstance(command, list):
        return " ".join(shlex.quote(str(c)) for c in command)
    return str(command or "")


def tool_name_for(item: dict) -> str | None:
    kind = item.get("type")
    if kind == "mcpToolCall":
        return f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
    return _TOOL_NAMES.get(str(kind))


def args_for(item: dict) -> dict:
    kind = item.get("type")
    if kind == "commandExecution":
        return {"command": _command_text(item.get("command")), "cwd": item.get("cwd")}
    if kind == "mcpToolCall":
        args = item.get("arguments")
        return dict(args) if isinstance(args, dict) else {"arguments": args}
    if kind == "webSearch":
        return {"query": item.get("query", "")}
    if kind in ("collabAgentToolCall", "subAgentActivity"):
        return {
            k: item.get(k)
            for k in ("prompt", "model", "receiverThreadIds", "agentPath", "kind")
            if item.get(k) is not None
        }
    if kind == "plan":
        return {"text": item.get("text", "")}
    return {}


def _changes(item: dict) -> list[tuple[str, dict]]:
    out = []
    for n, change in enumerate(item.get("changes") or []):
        kind = change.get("kind") or {}
        args = {"path": change.get("path"), "kind": kind.get("type"), "diff": change.get("diff", "")}
        out.append((f"{item.get('id')}:{n}", args))
    return out


def _result_text(item: dict) -> tuple[str, bool]:
    error = item.get("error")
    if error:
        msg = error.get("message") if isinstance(error, dict) else str(error)
        return str(msg), True
    result = item.get("result")
    if result is None:
        return "", item.get("status") == "failed"
    text = result if isinstance(result, str) else json.dumps(result)
    return text, item.get("status") == "failed"


class ItemTranslator:
    def __init__(self) -> None:
        self._streamed: set[str] = set()
        self._output: dict[str, list[str]] = {}

    def translate(self, method: str, params: dict) -> list[object]:
        handler = _METHODS.get(method)
        return handler(self, params) if handler is not None else []

    # --- deltas -------------------------------------------------------------
    def _text_delta(self, params: dict) -> list[object]:
        item_id = str(params.get("itemId"))
        self._streamed.add(item_id)
        return [TextDelta(item_id, str(params.get("delta", "")))]

    def _thinking_delta(self, params: dict) -> list[object]:
        item_id = str(params.get("itemId"))
        self._streamed.add(item_id)
        return [ThinkingDelta(item_id, str(params.get("delta", "")))]

    def _output_delta(self, params: dict) -> list[object]:
        self._output.setdefault(str(params.get("itemId")), []).append(str(params.get("delta", "")))
        return []

    # --- items --------------------------------------------------------------
    def _started(self, params: dict) -> list[object]:
        item = params.get("item") or {}
        kind = item.get("type")
        item_id = str(item.get("id"))
        if kind == "fileChange":
            return [ActivityStart(cid, "apply_patch", args) for cid, args in _changes(item)]
        if kind == "plan":
            text = str(item.get("text", ""))
            return [ActivityStart(item_id, "update_plan", {"text": text}), ActivityEnd(item_id, text, False)]
        if kind == "contextCompaction":
            return [Notice("Codex compacted its context")]
        name = tool_name_for(item)
        if name is None:
            return []
        return [ActivityStart(item_id, name, args_for(item))]

    def _completed(self, params: dict) -> list[object]:
        item = params.get("item") or {}
        kind = item.get("type")
        item_id = str(item.get("id"))
        if kind == "agentMessage":
            return [] if item_id in self._streamed else [TextDelta(item_id, str(item.get("text", "")))]
        if kind == "reasoning":
            if item_id in self._streamed:
                return []
            text = "\n".join(item.get("summary") or item.get("content") or [])
            return [ThinkingDelta(item_id, text)] if text else []
        if kind == "commandExecution":
            return [self._command_end(item, item_id)]
        if kind == "fileChange":
            failed = item.get("status") != "completed"
            return [ActivityEnd(cid, str(args["diff"]), failed) for cid, args in _changes(item)]
        if kind in ("mcpToolCall", "webSearch", "collabAgentToolCall", "subAgentActivity"):
            text, is_error = _result_text(item)
            return [ActivityEnd(item_id, text, is_error)]
        return []

    def _command_end(self, item: dict, item_id: str) -> ActivityEnd:
        buffered = "".join(self._output.pop(item_id, []))
        content = buffered or str(item.get("aggregatedOutput") or "")
        status = item.get("status")
        exit_code = item.get("exitCode")
        if status != "completed" and exit_code not in (None, 0):
            content = f"{content}\n[exit {exit_code}]".strip()
        return ActivityEnd(item_id, content, status != "completed")

    # --- turn level ---------------------------------------------------------
    def _usage(self, params: dict) -> list[object]:
        total = (params.get("tokenUsage") or {}).get("total") or {}
        return [UsageUpdate(dict(total))]

    def _turn_completed(self, params: dict) -> list[object]:
        turn = params.get("turn") or {}
        error = turn.get("error") or {}
        return [TurnDone(str(turn.get("status", "completed")), error.get("message") if error else None)]

    def _error(self, params: dict) -> list[object]:
        error = params.get("error") or {}
        return [TurnFailure(str(error.get("message", "codex error")), bool(params.get("willRetry")))]

    def _warning(self, params: dict) -> list[object]:
        return [Notice(str(params.get("message", "")))]


_METHODS = {
    "item/agentMessage/delta": ItemTranslator._text_delta,
    "item/reasoning/textDelta": ItemTranslator._thinking_delta,
    "item/reasoning/summaryTextDelta": ItemTranslator._thinking_delta,
    "item/commandExecution/outputDelta": ItemTranslator._output_delta,
    "item/started": ItemTranslator._started,
    "item/completed": ItemTranslator._completed,
    "thread/tokenUsage/updated": ItemTranslator._usage,
    "turn/completed": ItemTranslator._turn_completed,
    "error": ItemTranslator._error,
    "warning": ItemTranslator._warning,
}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_translate.py -v`
Expected: all PASS. `uv run ruff check src/marim_harness/codex/translate.py` must show no C901 — `_completed` has 8 branches; if you add one, split the `kind` table into a dict first.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/codex/translate.py tests/test_codex_translate.py
git commit -m "feat(codex): translate app-server notifications into neutral events

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Approval broker (`codex/approvals.py`)

Maps marim's `Mode` onto Codex's approval policy + sandbox, and answers Codex's
server-side approval / user-input / elicitation requests by delegating to the
same `request_approval` / `ask_user` seams the native tools use (spec §Mode
mapping). Pure decision helpers (`policy_for`, `sandbox_for`, `decide`) are
side-effect-free and tested directly; `ApprovalBroker` is the thin async
wrapper that owns the UI callbacks.

**Files:**
- Create: `src/marim_harness/codex/approvals.py`
- Test: `tests/test_codex_approvals.py`

**Interfaces:**
- Consumes: `Mode` (`runtime/permissions.py`), `ToolCallPart` (pydantic-ai),
  `Question`/`Choice` (`marim_harness/ask_user.py`), `tool_name_for`/`args_for`
  (Task 5 `codex/translate.py`), `RpcError` (Task 2).
- Produces:
  - `Decision(accept: bool, reason: str = "", ask: bool = False)` frozen dataclass.
  - `policy_for(mode: Mode) -> str` — `"on-request"` / `"untrusted"` / `"never"`.
  - `sandbox_for(mode: Mode, root: str, *, read_only: bool = False) -> dict` —
    the `turn/start.sandboxPolicy` object.
  - `sandbox_mode_for(mode: Mode, *, read_only: bool = False) -> str` — the
    `thread/start.sandbox` string (`"read-only"` / `"workspace-write"`).
  - `decide(mode: Mode, method: str, params: dict, workspace_root: Path | None, scratchpad: Path | None) -> Decision`.
  - `ApprovalBroker(*, mode_getter, workspace_root, scratchpad_getter, request_approval, ask_user, label="")`
    with `async handle(method: str, params: dict) -> dict` (a `ServerRequestHandler`).

- [ ] **Step 1: Write the failing tests**

`tests/test_codex_approvals.py`:

```python
"""codex/approvals.py: Mode -> Codex policy/sandbox, and the server-request broker."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import ToolDenied

from marim_harness.codex.approvals import (
    ApprovalBroker,
    Decision,
    decide,
    policy_for,
    sandbox_for,
    sandbox_mode_for,
)
from marim_harness.codex.rpc import RpcError
from marim_harness.runtime.permissions import Mode

pytestmark = pytest.mark.anyio


def test_policy_for_maps_modes():
    assert policy_for(Mode.auto) == "on-request"
    assert policy_for(Mode.ask) == "untrusted"
    assert policy_for(Mode.plan) == "never"


def test_sandbox_for_plan_is_read_only_and_others_are_workspace_write():
    assert sandbox_for(Mode.plan, "/w") == {"type": "readOnly", "networkAccess": False}
    ws = sandbox_for(Mode.auto, "/w")
    assert ws["type"] == "workspaceWrite" and ws["writableRoots"] == ["/w"]
    assert ws["networkAccess"] is True
    # read_only forces the read-only sandbox regardless of mode (read-only spawns).
    assert sandbox_for(Mode.auto, "/w", read_only=True)["type"] == "readOnly"
    assert sandbox_mode_for(Mode.ask) == "workspace-write"
    assert sandbox_mode_for(Mode.plan) == "read-only"
    assert sandbox_mode_for(Mode.auto, read_only=True) == "read-only"


def _cmd(cmd: str = "ls") -> dict:
    return {"itemId": "c1", "command": cmd, "cwd": "/w", "reason": None}


def _change(path: str) -> dict:
    return {"itemId": "f1", "changes": [{"path": path, "kind": {"type": "update"}, "diff": "+x"}]}


def test_decide_plan_declines_everything():
    d = decide(Mode.plan, "item/commandExecution/requestApproval", _cmd(), Path("/w"), None)
    assert d == Decision(accept=False, reason="plan mode: read-only")


def test_decide_auto_accepts_in_root_and_asks_outside(tmp_path):
    inside = _change(str(tmp_path / "a.py"))
    outside = _change("/etc/passwd")
    assert decide(Mode.auto, "item/fileChange/requestApproval", inside, tmp_path, None).accept
    d = decide(Mode.auto, "item/fileChange/requestApproval", outside, tmp_path, None)
    assert d.ask is True and not d.accept
    assert decide(Mode.auto, "item/commandExecution/requestApproval", _cmd(), tmp_path, None).accept


def test_decide_ask_auto_accepts_scratchpad_writes_only(tmp_path):
    pad = tmp_path / "pad"
    pad.mkdir()
    in_pad = _change(str(pad / "notes.md"))
    in_root = _change(str(tmp_path / "a.py"))
    assert decide(Mode.ask, "item/fileChange/requestApproval", in_pad, tmp_path, pad).accept
    d = decide(Mode.ask, "item/fileChange/requestApproval", in_root, tmp_path, pad)
    assert d.ask and not d.accept
    assert decide(Mode.ask, "item/commandExecution/requestApproval", _cmd(), tmp_path, pad).ask


def _broker(mode: Mode, tmp_path: Path, *, request_approval=None, ask_user=None, label=""):
    return ApprovalBroker(
        mode_getter=lambda: mode,
        workspace_root=tmp_path,
        scratchpad_getter=lambda: None,
        request_approval=request_approval,
        ask_user=ask_user,
        label=label,
    )


async def test_handle_plan_declines_without_prompting(tmp_path):
    called = []

    async def approver(call):
        called.append(call)
        return True

    broker = _broker(Mode.plan, tmp_path, request_approval=approver)
    reply = await broker.handle("item/commandExecution/requestApproval", _cmd())
    assert reply == {"decision": "decline"} and called == []


async def test_handle_ask_routes_to_request_approval_as_tool_call_part(tmp_path):
    seen = []

    async def approver(call):
        seen.append(call)
        return True

    broker = _broker(Mode.ask, tmp_path, request_approval=approver, label="worker")
    reply = await broker.handle("item/commandExecution/requestApproval", _cmd("rm -rf build"))
    assert reply == {"decision": "accept"}
    call = seen[0]
    assert call.tool_name == "bash" and call.tool_call_id == "c1"
    assert call.args == {"command": "rm -rf build", "cwd": "/w", "label": "worker"}


async def test_handle_ask_denied_and_tool_denied_both_decline(tmp_path):
    async def deny_false(call):
        return False

    async def deny_obj(call):
        return ToolDenied("nope")

    assert (await _broker(Mode.ask, tmp_path, request_approval=deny_false).handle(
        "item/commandExecution/requestApproval", _cmd())) == {"decision": "decline"}
    assert (await _broker(Mode.ask, tmp_path, request_approval=deny_obj).handle(
        "item/commandExecution/requestApproval", _cmd())) == {"decision": "decline"}


async def test_handle_ask_without_approver_declines(tmp_path):
    broker = _broker(Mode.ask, tmp_path)  # headless: no approver bound
    reply = await broker.handle("item/fileChange/requestApproval", _change(str(tmp_path / "a")))
    assert reply == {"decision": "decline"}


async def test_handle_auto_accepts_without_prompting(tmp_path):
    broker = _broker(Mode.auto, tmp_path)
    assert (await broker.handle("item/commandExecution/requestApproval", _cmd())) == {
        "decision": "accept"
    }


async def test_handle_cancelled_prompt_answers_cancel(tmp_path):
    async def approver(call):
        raise asyncio.CancelledError

    broker = _broker(Mode.ask, tmp_path, request_approval=approver)
    with pytest.raises(asyncio.CancelledError):
        await broker.handle("item/commandExecution/requestApproval", _cmd())
    # The reply is still recorded so the caller (rpc dispatch) can answer Codex
    # before propagating the cancel.
    assert broker.last_reply == {"decision": "cancel"}


async def test_handle_permissions_request_echoes_on_accept(tmp_path):
    params = {"itemId": "p1", "permissions": {"network": True}, "reason": "curl"}
    ok = await _broker(Mode.auto, tmp_path).handle("item/permissions/requestApproval", params)
    assert ok == {"decision": "accept", "permissions": {"network": True}}
    no = await _broker(Mode.plan, tmp_path).handle("item/permissions/requestApproval", params)
    assert no == {"decision": "decline", "permissions": {}}


async def test_handle_user_input_uses_ask_user(tmp_path):
    async def ask(questions):
        assert [q.question for q in questions] == ["Which db?"]
        assert [c.label for c in questions[0].options] == ["sqlite", "postgres"]
        return {questions[0].header: "postgres"}

    params = {
        "itemId": "u1",
        "questions": [
            {"id": "db", "header": "Database", "question": "Which db?",
             "options": [{"label": "sqlite"}, {"label": "postgres", "description": "prod"}]},
        ],
    }
    reply = await _broker(Mode.auto, tmp_path, ask_user=ask).handle(
        "item/tool/requestUserInput", params
    )
    assert reply == {"answers": {"db": {"answers": ["postgres"]}}}


async def test_handle_user_input_headless_picks_first_option(tmp_path):
    params = {"itemId": "u1", "questions": [
        {"id": "q", "question": "Pick", "options": [{"label": "a"}, {"label": "b"}]},
        {"id": "free", "question": "Anything else?", "options": []},
    ]}
    reply = await _broker(Mode.auto, tmp_path).handle("item/tool/requestUserInput", params)
    assert reply == {"answers": {"q": {"answers": ["a"]}, "free": {"answers": [""]}}}


async def test_handle_elicitation_declines_and_unknown_raises(tmp_path):
    broker = _broker(Mode.auto, tmp_path)
    assert (await broker.handle("mcpServer/elicitation/request", {"itemId": "e"})) == {
        "action": "decline"
    }
    with pytest.raises(RpcError) as exc:
        await broker.handle("item/somethingNew/requestApproval", {})
    assert exc.value.code == -32601
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest --no-cov tests/test_codex_approvals.py -v`
Expected: FAIL — `ModuleNotFoundError: marim_harness.codex.approvals`.

- [ ] **Step 3: Implement**

`src/marim_harness/codex/approvals.py`:

```python
"""Mode -> Codex approval policy/sandbox, and the server-request broker.

Codex asks *marim* for approvals (``item/*/requestApproval``), for user input
(``item/tool/requestUserInput``) and for MCP elicitation answers over the
JSON-RPC back-channel. This module turns those into the same UI seams the
native tools use — ``Deps.ui.request_approval`` (the ApprovalPanel in the TUI,
None headless) and ``Deps.ui.ask_user`` — so a Codex turn in ask mode gates
exactly like a native turn, and plan mode never prompts.

The mapping (spec §Mode mapping):

    marim mode   approvalPolicy   sandbox (thread)   sandboxPolicy (turn)
    auto         on-request       workspace-write    workspaceWrite[root]
    ask          untrusted        workspace-write    workspaceWrite[root]
    plan         never            read-only          readOnly

``acceptForSession`` is never sent: marim keeps per-call gating so the user's
/mode switch mid-session takes effect on the very next request.

``decide`` is a pure function (mode + request -> Decision) so the policy is
unit-testable without an event loop; ``ApprovalBroker.handle`` is the thin
async wrapper that owns the callbacks and serializes prompts with a lock
(Codex can raise two approvals concurrently from parallel tool calls; the
ApprovalPanel shows one at a time).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ask_user import Choice, Question
from ..runtime.permissions import Mode
from .rpc import RpcError
from .translate import args_for, tool_name_for

logger = logging.getLogger(__name__)

METHOD_NOT_FOUND = -32601

_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }
)
USER_INPUT_METHOD = "item/tool/requestUserInput"
ELICITATION_METHOD = "mcpServer/elicitation/request"


@dataclass(frozen=True)
class Decision:
    accept: bool
    reason: str = ""
    ask: bool = False  # True: the caller must prompt (accept is the headless default)


def policy_for(mode: Mode) -> str:
    """``approvalPolicy`` for a marim mode."""
    if mode is Mode.auto:
        return "on-request"
    if mode is Mode.ask:
        return "untrusted"
    return "never"


def sandbox_mode_for(mode: Mode, *, read_only: bool = False) -> str:
    """``thread/start.sandbox`` for a marim mode (plan, or a read-only spawn, is
    read-only; everything else is workspace-write)."""
    return "read-only" if (read_only or mode is Mode.plan) else "workspace-write"


def sandbox_for(mode: Mode, root: str, *, read_only: bool = False) -> dict:
    """``turn/start.sandboxPolicy`` for a marim mode. Network stays on for
    workspace-write (marim's own net tools are ungated in auto/ask; plan mode
    is local-research only, so the read-only sandbox has it off)."""
    if read_only or mode is Mode.plan:
        return {"type": "readOnly", "networkAccess": False}
    return {
        "type": "workspaceWrite",
        "writableRoots": [root],
        "networkAccess": True,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def _change_paths(params: dict) -> list[Path]:
    return [Path(str(c.get("path", ""))) for c in params.get("changes") or [] if c.get("path")]


def _within(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def decide(
    mode: Mode,
    method: str,
    params: dict,
    workspace_root: Path | None,
    scratchpad: Path | None,
) -> Decision:
    """The policy answer for one approval request, before any prompting.

    plan  -> decline (never prompts; Codex should not even ask under ``never``,
             but a stale policy on a resumed thread could).
    auto  -> accept, except a fileChange touching a path outside the workspace
             root AND outside the scratchpad, which is escalated to a prompt
             (the sandbox already forbids it; the prompt is defense in depth).
    ask   -> a fileChange entirely inside the scratchpad is accepted (mirrors
             ``_scratchpad_approval`` for native tools); everything else prompts.
    """
    if mode is Mode.plan:
        return Decision(accept=False, reason="plan mode: read-only")
    paths = _change_paths(params) if method == "item/fileChange/requestApproval" else []
    if mode is Mode.auto:
        stray = [p for p in paths if not _within(p, workspace_root) and not _within(p, scratchpad)]
        if stray:
            return Decision(accept=False, reason=f"outside workspace: {stray[0]}", ask=True)
        return Decision(accept=True)
    # ask mode
    if paths and scratchpad is not None and all(_within(p, scratchpad) for p in paths):
        return Decision(accept=True, reason="scratchpad write")
    return Decision(accept=False, ask=True)


_METHOD_ITEM_TYPES = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
}


def _as_item(method: str, params: dict) -> dict:
    """Shape a request's params like a ThreadItem so ``tool_name_for``/
    ``args_for`` (Task 5) can name and describe it for the ApprovalPanel."""
    item = dict(params)
    item["type"] = _METHOD_ITEM_TYPES.get(method, "unknown")
    item["id"] = str(params.get("itemId", ""))
    return item


class ApprovalBroker:
    """Answers Codex server requests for one thread. ``label`` prefixes the
    ApprovalPanel entry for a spawn (``worker: bash``) so the user can tell a
    sub-agent's request from the main loop's."""

    def __init__(
        self,
        *,
        mode_getter: Callable[[], Mode],
        workspace_root: Path | None,
        scratchpad_getter: Callable[[], Path | None],
        request_approval: Callable[[Any], Awaitable[Any]] | None,
        ask_user: Callable[[list[Question]], Awaitable[dict | None]] | None,
        label: str = "",
    ) -> None:
        self._mode_getter = mode_getter
        self._root = workspace_root
        self._scratchpad_getter = scratchpad_getter
        self._request_approval = request_approval
        self._ask_user = ask_user
        self._label = label
        self._lock = asyncio.Lock()
        # The last reply built, including the "cancel" reply set on the way
        # out of a CancelledError (the rpc dispatcher reads it — see
        # ``handle`` — so Codex gets an answer before the cancel propagates).
        self.last_reply: dict | None = None

    async def handle(self, method: str, params: dict) -> dict:
        async with self._lock:
            try:
                reply = await self._handle(method, params)
            except asyncio.CancelledError:
                self.last_reply = {"decision": "cancel"}
                raise
            self.last_reply = reply
            return reply

    async def _handle(self, method: str, params: dict) -> dict:
        if method in _APPROVAL_METHODS:
            return await self._approval(method, params)
        if method == USER_INPUT_METHOD:
            return await self._user_input(params)
        if method == ELICITATION_METHOD:
            return {"action": "decline"}
        raise RpcError(METHOD_NOT_FOUND, f"unsupported server request {method}")

    async def _approval(self, method: str, params: dict) -> dict:
        mode = self._mode_getter()
        decision = decide(mode, method, params, self._root, self._scratchpad_getter())
        accept = decision.accept
        if decision.ask:
            accept = await self._prompt(method, params)
        if not accept and decision.reason:
            logger.info("codex %s declined (%s)", method, decision.reason)
        reply: dict = {"decision": "accept" if accept else "decline"}
        if method == "item/permissions/requestApproval":
            reply["permissions"] = params.get("permissions") or {} if accept else {}
        return reply

    async def _prompt(self, method: str, params: dict) -> bool:
        """Route to ``request_approval`` as a pydantic-ai ToolCallPart so the
        ApprovalPanel renders it like a native gated call. Headless (no
        approver) -> denied, exactly like ``resolve_approvals``."""
        if self._request_approval is None:
            return False
        from pydantic_ai.messages import ToolCallPart

        item = _as_item(method, params)
        args = args_for(item)
        if self._label:
            args["label"] = self._label
        call = ToolCallPart(
            tool_name=tool_name_for(item) or "codex",
            args=args,
            tool_call_id=str(params.get("itemId", "")),
        )
        result = await self._request_approval(call)
        return _is_approved(result)

    async def _user_input(self, params: dict) -> dict:
        questions = [_question(q) for q in params.get("questions") or []]
        ids = [str(q.get("id", "")) for q in params.get("questions") or []]
        answers: dict | None = None
        if self._ask_user is not None and questions:
            answers = await self._ask_user(questions)
        if not answers:
            # Headless / cancelled: the first option (or blank) per question,
            # logged so the choice is visible in the transcript.
            logger.info("codex requestUserInput answered with defaults (no UI)")
            answers = {q.header: (q.options[0].label if q.options else "") for q in questions}
        out = {}
        for qid, q in zip(ids, questions):
            value = answers.get(q.header, "")
            out[qid] = {"answers": list(value) if isinstance(value, list) else [str(value)]}
        return {"answers": out}


def _is_approved(result: object) -> bool:
    """Same acceptance rule as native gating: ``True`` or a ToolApproved is a
    yes; ``False``/``None``/ToolDenied is a no."""
    if result is True:
        return True
    if not result:
        return False
    return type(result).__name__ == "ToolApproved"


def _question(raw: dict) -> Question:
    header = str(raw.get("header") or raw.get("id") or "")
    options = [
        Choice(label=str(o.get("label", "")), description=o.get("description"))
        for o in raw.get("options") or []
        if o.get("label")
    ]
    return Question(question=str(raw.get("question", "")), header=header, options=options)
```

Note the `args_for` call for `"permissions"` items: Task 5's `args_for` returns
`{}` for unknown item types, so a permissions prompt renders as
`codex {label}` — acceptable; the reason text is in `params["reason"]`, add it
to `args` when present: after `args = args_for(item)` insert
`if params.get("reason"): args.setdefault("reason", str(params["reason"]))`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_approvals.py -v`
Expected: all PASS. If `test_handle_ask_routes_to_request_approval_as_tool_call_part` fails on `call.args`, check Task 5's `args_for` for a `commandExecution` item — it must return exactly `{"command": <str>, "cwd": <str>}` (no `reason` key when `reason` is None); adjust the test's expectation only if Task 5 documented a different shape.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/codex/approvals.py tests/test_codex_approvals.py
git commit -m "feat(codex): approval broker mapping marim modes onto Codex policies

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Shared external-CLI model base (`config/external_cli.py`)

Extract the seams `Harness.wire_cli_model` and `aux_model_for` bind on
`ClaudeCliModel` into an abstract base so `CodexCliModel` (Task 8) plugs into
the same three call sites (bootstrap, `bind_ui`, `set_model`) without adding a
second isinstance ladder (spec §Shared external-CLI base). Behavior of the
claude-cli provider is unchanged; this is a pure refactor plus two new seams
(`request_approval`, `ask_user`, `scratchpad_getter`, `thinking_getter`,
`session_ref_getter`/`on_session_ref`) that only Codex reads.

**Files:**
- Create: `src/marim_harness/config/external_cli.py`
- Modify: `src/marim_harness/config/claude_cli_model.py` (class header ~605-658, `_TextFolder` 766-823, `CliModelError` 536)
- Modify: `src/marim_harness/runtime/harness.py:1011-1025` (`wire_cli_model`)
- Modify: `src/marim_harness/session/ctrl.py:43-60` (`aux_model_for`)
- Test: `tests/test_external_cli_model.py`; keep `tests/test_aux_model_clone.py`, `tests/test_cli_backend.py`, `tests/test_claude_cli_model.py` (whichever exist under `tests/test_claude_cli*.py`) green.

**Interfaces:**
- Consumes: nothing new.
- Produces (`config/external_cli.py`):
  - `class CliModelError(Exception)` — moved here; `claude_cli_model.CliModelError` re-exports it (same object).
  - `class ExternalCliModel(Model)` with `provider_id: ClassVar[str]`, attributes
    `mode_getter: Callable[[], str] | None`, `cwd: str`, `on_activity`,
    `on_subagent`, `on_subagent_model`, `request_approval`, `ask_user`,
    `scratchpad_getter: Callable[[], Path | None] | None`,
    `thinking_getter: Callable[[], str | None] | None`,
    `session_ref_getter: Callable[[], str | None] | None`,
    `on_session_ref: Callable[[str], None] | None`, `ephemeral: bool`;
    abstract `ephemeral_clone(self, *, cwd: str) -> ExternalCliModel`;
    `steer(self, text: str) -> bool` (default `False`); `async compact_remote(self) -> None` (default no-op);
    `system` property returns `provider_id`.
  - `class TextFolder` — `_TextFolder` moved verbatim (alias `_TextFolder = TextFolder` kept in `claude_cli_model.py`).
  - `Harness.wire_cli_model(model)` binds the base seams for ANY `ExternalCliModel`.
  - `aux_model_for` clones ANY `ExternalCliModel`.

- [ ] **Step 1: Write the failing tests**

`tests/test_external_cli_model.py`:

```python
"""The ExternalCliModel base: one seam set shared by claude-cli and codex-cli."""

from __future__ import annotations

from marim_harness.config import claude_cli_model as ccm
from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.config.external_cli import CliModelError, ExternalCliModel, TextFolder
from marim_harness.session.ctrl import aux_model_for
from tests.conftest import _make_deps, _make_harness, _text_model


class _Fake(ExternalCliModel):
    provider_id = "fake-cli"

    def __init__(self) -> None:
        super().__init__()
        self.clones: list[str] = []

    def ephemeral_clone(self, *, cwd: str) -> _Fake:
        clone = _Fake()
        clone.cwd = cwd
        clone.ephemeral = True
        self.clones.append(cwd)
        return clone

    @property
    def model_name(self) -> str:
        return "fake"

    async def request(self, messages, model_settings, model_request_parameters):  # pragma: no cover
        raise NotImplementedError


def test_claude_cli_model_is_an_external_cli_model():
    assert issubclass(ClaudeCliModel, ExternalCliModel)
    assert ClaudeCliModel.provider_id == "claude-cli"
    assert ClaudeCliModel("x").system == "claude-cli"
    # Backwards-compatible names still resolve from the old module.
    assert ccm.CliModelError is CliModelError
    assert ccm._TextFolder is TextFolder


def test_base_defaults_are_inert():
    m = _Fake()
    assert m.system == "fake-cli"
    assert m.mode_getter is None and m.cwd == "."
    assert m.request_approval is None and m.ask_user is None
    assert m.scratchpad_getter is None and m.thinking_getter is None
    assert m.session_ref_getter is None and m.on_session_ref is None
    assert m.steer("x") is False
    assert m.ephemeral is False


def test_aux_model_for_clones_any_external_cli_model():
    raw = _Fake()
    aux = aux_model_for(raw, cwd="/ws")
    assert aux is not raw and isinstance(aux, _Fake) and aux.ephemeral and aux.cwd == "/ws"


def test_wire_cli_model_binds_all_seams(tmp_path):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    m = _Fake()
    harness.wire_cli_model(m)
    assert m.mode_getter is not None and m.mode_getter() == harness.mode.value
    assert m.cwd == str(harness.deps.workspace.root)
    assert m.on_activity is harness.deps.ui.on_cli_activity
    assert m.on_subagent is harness.deps.ui.on_subagent_event
    assert m.on_subagent_model is harness.deps.ui.on_subagent_model
    assert m.request_approval is harness.deps.ui.request_approval
    assert m.ask_user is harness.deps.ui.ask_user
    assert m.scratchpad_getter is not None
    assert m.thinking_getter is not None and m.thinking_getter() == harness.thinking_level_id
    assert m.session_ref_getter is not None and m.on_session_ref is not None


def test_wire_cli_model_ignores_other_models(tmp_path):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))

    class _Plain:
        pass

    plain = _Plain()
    harness.wire_cli_model(plain)  # no attribute errors, nothing set
    assert not hasattr(plain, "mode_getter")
```

`_make_harness(model, deps)`, `_make_deps(root)` and `_text_model()` are the
existing helpers in `tests/conftest.py` (a FunctionModel that answers "ok").

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest --no-cov tests/test_external_cli_model.py -v`
Expected: FAIL — `ModuleNotFoundError: marim_harness.config.external_cli`.

- [ ] **Step 3: Create the base module**

`src/marim_harness/config/external_cli.py`:

```python
"""The base for models that delegate a whole turn to an external coding CLI.

``ClaudeCliModel`` (claude-cli) and ``CodexCliModel`` (codex-cli) both make
marim a *launcher*: the external process runs its own tool loop and marim
receives a single text-only ``ModelResponse`` plus out-of-band activity. The
harness binds the same late seams on either at three call sites (bootstrap,
``Harness.bind_ui``, ``Harness.set_model``) through ``Harness.wire_cli_model``,
and ``session.ctrl.aux_model_for`` swaps either for an ``ephemeral_clone``
before building the aux (titler/summarizer) agents. Keeping those seams on one
base means a new external CLI never grows a second isinstance ladder.

Only the seams live here. Each subclass owns its transport, its parsing and
its ``ephemeral_clone``; ``TextFolder`` (the vendor-part-id bookkeeping the
streamed responses share) is here too because both providers interleave prose
with tool cards the same way.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic_ai.models import Model

if TYPE_CHECKING:
    from ..ask_user import Question


class CliModelError(Exception):
    """The external CLI was unavailable or produced no terminal result."""


class ExternalCliModel(Model):
    """Seams shared by claude-cli and codex-cli. All are late-bound (None
    until ``Harness.wire_cli_model`` runs) so a model can be built off-loop
    and without a UI, exactly like ``Deps.ui``."""

    provider_id: ClassVar[str] = "external-cli"

    def __init__(self) -> None:
        super().__init__()
        # Live marim approval mode ("auto"/"ask"/"plan"); read per turn.
        self.mode_getter: Callable[[], str] | None = None
        # Real workspace (or worktree) root. Running in the process cwd (".")
        # would make the CLI read/edit the WRONG directory — destructively so
        # under --worktree.
        self.cwd: str = "."
        # Ephemeral models serve one-shot aux agents (titler/summarizer): no
        # session resume/persist, always their own instructions, read-only.
        self.ephemeral: bool = False
        # TUI side-channels (None headless). on_activity renders the CLI's own
        # tool calls as native tool cards; on_subagent/on_subagent_model route
        # the CLI's sub-agents to the sub-agents screen.
        self.on_activity: Callable[[list], Awaitable[None]] | None = None
        self.on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
        self.on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None
        # Interactive gating (Deps.ui.request_approval / ask_user). claude-cli
        # cannot use them (headless claude can't prompt); codex-cli brokers its
        # server-side approval requests through them.
        self.request_approval: Callable[[object], Awaitable[object]] | None = None
        self.ask_user: Callable[[list[Question]], Awaitable[dict | None]] | None = None
        # The session scratchpad (auto-approved writes in ask mode).
        self.scratchpad_getter: Callable[[], Path | None] | None = None
        # The live thinking level id (Harness.thinking_level_id), read per turn.
        self.thinking_getter: Callable[[], str | None] | None = None
        # The provider-side conversation reference persisted with the marim
        # session (SessionStore.cli_thread_id, Task 10): read on the first turn
        # after a resume, written whenever the CLI reports a new one.
        self.session_ref_getter: Callable[[], str | None] | None = None
        self.on_session_ref: Callable[[str], None] | None = None

    @property
    def system(self) -> str:
        return self.provider_id

    def ephemeral_clone(self, *, cwd: str) -> ExternalCliModel:
        """A stateless, read-only copy for one-shot aux agents. Subclasses must
        override; the base raises so a forgotten override is loud."""
        raise NotImplementedError(f"{type(self).__name__} must implement ephemeral_clone")

    def steer(self, text: str) -> bool:
        """Inject ``text`` into the CLI's in-flight turn. Returns True when the
        CLI accepted it (so the harness must NOT also buffer it for the next
        turn), False when the provider cannot steer (default)."""
        return False

    async def compact_remote(self) -> None:
        """Ask the CLI to compact its own context (after marim compacts its
        copy). No-op by default."""
        return None


class TextFolder:
    """The vendor-part-id bookkeeping for a streamed response's two rendering
    modes.

    With a UI side-channel (cards mode, ``on_activity`` set), the CLI's
    tool calls/results become native tool cards pushed out-of-band, and each
    run of assistant prose gets its own text part (a fresh vendor_part_id after
    every tool) so the cards interleave between text blocks. Headless (fold
    mode, no side-channel) folds tool use into the text as ``▸`` lines in one
    growing part. ``part_n`` (cards mode) and ``folded_any`` (fold mode, for
    blank-line separation) both mutate across chunks AND across the text/tool
    arms, so they're threaded via this small stateful object rather than loose
    locals passed in/out of each arm.

    ``activity_events`` / ``fold_text`` are the provider's chunk -> events and
    chunk -> ``▸`` line translators (claude-cli passes ``cli_activity_events``
    and ``fold_chunk_text``; codex-cli passes its own in Task 8)."""

    def __init__(
        self,
        parts_manager,
        on_activity: Callable[[list], Awaitable[None]] | None,
        *,
        activity_events: Callable[[object], list],
        fold_text: Callable[[object, bool], str],
        is_call: Callable[[object], bool],
    ) -> None:
        self._parts_manager = parts_manager
        self._on_activity = on_activity
        self._cards = on_activity is not None
        self._activity_events = activity_events
        self._fold_text = fold_text
        self._is_call = is_call
        self.part_n = 0
        self.folded_any = False

    async def _emit(self, content: str, part_id: str):
        for event in self._parts_manager.handle_text_delta(vendor_part_id=part_id, content=content):
            yield event

    async def emit_text(self, delta: str):
        """Cards mode gives prose its own vendor part id (``text-{part_n}``);
        fold mode grows the single ``text-0`` part, blank-line-separated from
        anything already folded into it."""
        if self._cards:
            async for ev in self._emit(delta, f"text-{self.part_n}"):
                yield ev
            return
        seg = delta if not self.folded_any else f"\n\n{delta}"
        async for ev in self._emit(seg, "text-0"):
            yield ev
        self.folded_any = True

    async def emit_tool(self, chunk):
        """Cards mode pushes the chunk out-of-band via ``on_activity`` and, for
        a tool *call*, bumps ``part_n`` so following prose starts a fresh part
        below the card; fold mode folds it into the text as a ``▸`` line."""
        if self._on_activity is not None:
            events = self._activity_events(chunk)
            if events:
                await self._on_activity(events)
            if self._is_call(chunk):
                self.part_n += 1
            return
        seg = self._fold_text(chunk, not self.folded_any)
        if seg:
            async for ev in self._emit(seg, "text-0"):
                yield ev
            self.folded_any = True
```

- [ ] **Step 4: Rebase `ClaudeCliModel` onto the base**

In `src/marim_harness/config/claude_cli_model.py`:

1. Replace the `CliModelError` class (around line 536) with a re-export. Delete
   the three lines of the class and add to the imports block (after
   `from ..usage import COST_DETAIL_KEY`):
   ```python
   from .external_cli import CliModelError, ExternalCliModel, TextFolder
   ```
   `CliModelError` stays importable from this module (tests and
   `runtime/errors.py` may import it from here — grep `CliModelError` across
   `src/` and `tests/` to confirm nothing else needs touching).

2. Replace the class header and `__init__` (the block from `class ClaudeCliModel(Model):` through `self.cwd: str = "."`) with:
   ```python
   class ClaudeCliModel(ExternalCliModel):
       """A Pydantic AI model backed by the ``claude`` CLI (a Claude subscription).

       Each request spawns ``claude -p`` (resuming Claude's session when one is known)
       and returns a single text-only ``ModelResponse``; Claude runs its own tools
       internally. The late-bound seams (``mode_getter``, ``cwd``, ``on_activity``,
       ``on_subagent*``) live on ``ExternalCliModel`` and are bound by
       ``Harness.wire_cli_model``; ``session_id`` is held in-memory across turns of
       one process."""

       provider_id = "claude-cli"

       def __init__(self, model_id: str | None, *, ephemeral: bool = False) -> None:
           super().__init__()
           self._model_id = model_id
           self.session_id: str | None = None
           # See ExternalCliModel.ephemeral / ``ephemeral_clone``: aux agents never
           # resume or store a session, so they can't hijack the user's live one.
           self.ephemeral = ephemeral
           self.spawn = spawn_cli_objects  # I/O seam; tests monkeypatch this
   ```
   Keep `ephemeral_clone` and `model_name` as they are; DELETE the `system`
   property (the base returns `provider_id`). Keep the comment about
   `mode_getter`/`cwd` semantics by moving its substance into the base (done
   above) — do not duplicate it.

3. Replace the whole `_TextFolder` class (766-823) with a compatibility alias
   placed right where the class was:
   ```python
   # Moved to config/external_cli.py (shared with codex-cli); the old name stays
   # importable for tests that reach for it.
   _TextFolder = TextFolder
   ```
   and update the single constructor call in
   `ClaudeCliStreamedResponse._get_event_iterator`:
   ```python
   folder = TextFolder(
       self._parts_manager,
       self._on_activity,
       activity_events=cli_activity_events,
       fold_text=lambda chunk, leading: fold_chunk_text(chunk, leading=leading),
       is_call=lambda chunk: isinstance(chunk, ToolUseChunk),
   )
   ```
   Then remove the now-unused `Model` import from `pydantic_ai.models` if ruff
   flags it (`StreamedResponse` and `ModelRequestParameters` stay).

- [ ] **Step 5: Generalize the two binding sites**

`src/marim_harness/runtime/harness.py` — replace the body of `wire_cli_model`:

```python
    def wire_cli_model(self, model: Model) -> None:
        """Bind the late-bound seams an ``ExternalCliModel`` (claude-cli,
        codex-cli) needs — live approval mode, the real workspace (or worktree)
        cwd, the TUI tool-card and sub-agents side-channels, interactive gating
        (request_approval/ask_user — brokered by codex-cli, unused by
        claude-cli), the scratchpad, the live thinking level and the persisted
        provider-side conversation reference. A no-op for every other
        provider's model. Public because ``bootstrap`` (the CLI preset) binds it
        once after build, before any UI attaches — the internal set_model/bind_ui
        callers use it too, so a UI attached later re-binds the fresh callbacks."""
        from ..config.external_cli import ExternalCliModel

        if not isinstance(model, ExternalCliModel):
            return
        model.mode_getter = lambda: self.mode.value
        model.cwd = str(self.deps.workspace.root)
        model.on_activity = self.deps.ui.on_cli_activity
        model.on_subagent = self.deps.ui.on_subagent_event
        model.on_subagent_model = self.deps.ui.on_subagent_model
        model.request_approval = self.deps.ui.request_approval
        model.ask_user = self.deps.ui.ask_user
        services = self.deps.services
        model.scratchpad_getter = (
            services.get_scratchpad if services is not None and services.get_scratchpad else (lambda: None)
        )
        model.thinking_getter = lambda: self.thinking_level_id
        model.session_ref_getter = lambda: self.session.saved_cli_thread_id
        model.on_session_ref = self.session.set_cli_thread_id
```

`saved_cli_thread_id` / `set_cli_thread_id` are added to `SessionController`
in Task 10. Until Task 10 lands, keep this task green by binding them
defensively — use exactly this form now and simplify it in Task 10:

```python
        model.session_ref_getter = lambda: getattr(self.session, "saved_cli_thread_id", None)
        model.on_session_ref = getattr(self.session, "set_cli_thread_id", None)
```

`self.session` is the `SessionController` attribute on `Harness` (grep
`self.session = ` in `runtime/harness.py` to confirm the name; it is what
`set_model` calls `update_model` on).

`src/marim_harness/session/ctrl.py` — replace the `aux_model_for` body:

```python
def aux_model_for(model: Model, *, cwd: str) -> Model:
    """The model the aux agents (summarizer/titler) should run on.

    An ``ExternalCliModel`` (claude-cli, codex-cli) carries the live provider
    session/thread, so an aux agent sharing it would resume — and reply into —
    the user's real conversation (dropping its own instructions). Such a model
    is swapped for a stateless, read-only ``ephemeral_clone`` that never resumes
    or stores a session; every other provider reuses the one model unchanged.

    This is the SINGLE source of that decision: both bootstrap (initial build)
    and ``update_model`` (runtime ``/model`` switch) route the model through here,
    so the clone can never be dropped on one path but kept on the other."""
    from ..config.external_cli import ExternalCliModel

    if isinstance(model, ExternalCliModel):
        return model.ephemeral_clone(cwd=cwd)
    return model
```

- [ ] **Step 6: Run the affected suites**

Run: `uv run pytest --no-cov tests/test_external_cli_model.py tests/test_aux_model_clone.py tests/test_cli_backend.py $(ls tests/test_claude_cli*.py 2>/dev/null) -v`
Expected: all PASS. Then the full suite once: `uv run pytest -q` (the claude-cli
main-loop tests under `tests/` monkeypatch `ClaudeCliModel.spawn` and read
`model.system`; both still hold).

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/config/external_cli.py src/marim_harness/config/claude_cli_model.py \
        src/marim_harness/runtime/harness.py src/marim_harness/session/ctrl.py \
        tests/test_external_cli_model.py
git commit -m "refactor(config): ExternalCliModel base shared by claude-cli and codex-cli

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `CodexCliModel` — the main-loop provider (`config/codex_cli_model.py`)

The Pydantic AI `Model` for `MARIM_PROVIDER=codex-cli`. One marim turn = one
Codex `turn/start` on a per-session thread (spec §Data flow). Returns a
text-only `ModelResponse`; Codex's tool activity goes out-of-band through
`on_activity` (cards) or is folded into the text (headless), exactly like
claude-cli.

**Files:**
- Create: `src/marim_harness/codex/turn.py`
- Create: `src/marim_harness/config/codex_cli_model.py`
- Modify: `src/marim_harness/codex/server.py` (two small properties, see Step 3)
- Test: `tests/test_codex_cli_model.py`

**Interfaces:**
- Consumes: `ExternalCliModel`, `CliModelError`, `TextFolder` (Task 7);
  `CodexServer`, `ThreadHandle`, `CLOSED`, `shared_server` (Task 4);
  `ItemTranslator`, `TextDelta`, `ThinkingDelta`, `ActivityStart`,
  `ActivityEnd`, `UsageUpdate`, `TurnDone`, `TurnFailure`, `Notice` (Task 5);
  `ApprovalBroker`, `policy_for`, `sandbox_for`, `sandbox_mode_for` (Task 6);
  `Mode` (`runtime/permissions.py`); `extract_system`, `latest_user_text`,
  `flatten_history` (`config/claude_cli_model.py`); `CodexUnavailable`,
  `codex_available`, `INSTALL_HINT` (Task 1).
- Produces:
  - `codex/turn.py`: `TurnState` (dataclass: `translator`, `usage_total`, `done`, `failure`);
    `text_input(text: str) -> dict`; `usage_from_total(total: dict) -> RequestUsage`;
    `delta_since(total: dict, baseline: dict) -> dict`;
    `async turn_events(server: CodexServer, handle: ThreadHandle, state: TurnState) -> AsyncIterator[object]`;
    `finish_turn(handle: ThreadHandle, state: TurnState) -> RequestUsage`.
  - `effort_for(level: str | None, supported: list[str] | None) -> str | None`.
  - `class CodexCliModel(ExternalCliModel)`: `provider_id = "codex-cli"`,
    `__init__(model_id: str | None, *, ephemeral=False, server: CodexServer | None = None)`,
    `model_name`, `ephemeral_clone(*, cwd)`, `request(...)`, `request_stream(...)`,
    `steer(text) -> bool`, `compact_remote()`, attribute `thread: ThreadHandle | None`.
  - `activity_events(item: ActivityStart | ActivityEnd) -> list` (pydantic-ai
    `FunctionToolCallEvent`/`FunctionToolResultEvent`, reused by Task 11).
  - `fold_activity_text(item: ActivityStart | ActivityEnd, leading: bool) -> str`.
  - `class CodexStreamedResponse(StreamedResponse)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_codex_cli_model.py`:

```python
"""CodexCliModel against the scripted fake app-server (tests/fakes)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings

from marim_harness.codex.server import CodexServer
from marim_harness.config.codex_cli_model import CodexCliModel, CliModelError, effort_for
from marim_harness.runtime.permissions import Mode
from tests.fakes import fake_codex_bin, read_request_log

pytestmark = pytest.mark.anyio

PARAMS = ModelRequestParameters(function_tools=[], allow_text_output=True, output_tools=[])


def _msgs(text: str = "hello") -> list:
    return [ModelRequest(parts=[SystemPromptPart(content="SYS"), UserPromptPart(content=text)])]


def _hello_turn(text: str = "Hi there") -> list[dict]:
    return [
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": text}},
        {"notify": "thread/tokenUsage/updated",
         "params": {"tokenUsage": {"total": {"inputTokens": 12, "outputTokens": 5,
                                             "cachedInputTokens": 3}}}},
    ]


def _model(tmp_path, scenario: dict, *, model_id="gpt-5.6-sol", mode=Mode.auto) -> CodexCliModel:
    server = CodexServer(binary=fake_codex_bin(tmp_path, scenario))
    m = CodexCliModel(model_id, server=server)
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: mode.value
    return m


def test_effort_for_maps_levels():
    listed = ["low", "medium", "high", "xhigh"]
    assert effort_for(None, listed) is None
    assert effort_for("off", listed) is None
    assert effort_for("minimal", listed) == "low"
    assert effort_for("low", listed) == "low"
    assert effort_for("medium", listed) == "medium"
    assert effort_for("high", listed) == "high"
    assert effort_for("xhigh", listed) == "xhigh"
    assert effort_for("xhigh", ["low", "medium", "high"]) == "high"
    assert effort_for("xhigh", None) == "high"  # unknown catalog: conservative


async def test_request_returns_text_and_usage(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    try:
        resp = await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    assert resp.parts[0].content == "Hi there"
    assert resp.provider_name == "codex-cli" and resp.model_name == "gpt-5.6-sol"
    assert resp.usage.input_tokens == 12 and resp.usage.output_tokens == 5
    assert resp.usage.cache_read_tokens == 3
    log = read_request_log(tmp_path)
    start = next(r for r in log if r["method"] == "thread/start")
    assert start["params"]["developerInstructions"] == "SYS"
    assert start["params"]["approvalPolicy"] == "on-request"
    assert start["params"]["sandbox"] == "workspace-write"
    turn = next(r for r in log if r["method"] == "turn/start")
    assert turn["params"]["input"] == [{"type": "text", "text": "hello", "text_elements": []}]
    assert turn["params"]["sandboxPolicy"]["type"] == "workspaceWrite"
    assert "effort" not in turn["params"]


async def test_thread_is_reused_across_turns_and_reported(tmp_path):
    refs: list[str] = []
    m = _model(tmp_path, {"turns": [_hello_turn("a"), _hello_turn("b")]})
    m.on_session_ref = refs.append
    try:
        await m.request(_msgs("one"), None, PARAMS)
        await m.request(_msgs("two"), None, PARAMS)
    finally:
        await m.aclose()
    log = read_request_log(tmp_path)
    assert sum(r["method"] == "thread/start" for r in log) == 1
    assert sum(r["method"] == "turn/start" for r in log) == 2
    assert refs == ["codex-cli:thread-1"]


async def test_resumes_persisted_thread_or_falls_back(tmp_path):
    m = _model(tmp_path, {"resumable": ["thread-9"], "turns": [_hello_turn()]})
    m.session_ref_getter = lambda: "codex-cli:thread-9"
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    log = read_request_log(tmp_path)
    assert any(r["method"] == "thread/resume" for r in log)
    assert not any(r["method"] == "thread/start" for r in log)

    # Unknown id -> fresh thread, and the FULL history goes into the first input
    # (a cold start after a resume must not lose the conversation).
    m2 = _model(tmp_path, {"resumable": [], "turns": [_hello_turn()]})
    m2.session_ref_getter = lambda: "codex-cli:thread-gone"
    history = [
        ModelRequest(parts=[UserPromptPart(content="first question")]),
        ModelRequest(parts=[UserPromptPart(content="second question")]),
    ]
    try:
        await m2.request(history, None, PARAMS)
    finally:
        await m2.aclose()
    log = read_request_log(tmp_path)
    turn = [r for r in log if r["method"] == "turn/start"][-1]
    assert "first question" in turn["params"]["input"][0]["text"]
    assert "second question" in turn["params"]["input"][0]["text"]


async def test_foreign_session_ref_is_ignored(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    m.session_ref_getter = lambda: "claude-cli:abc"  # another provider's ref
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    assert not any(r["method"] == "thread/resume" for r in read_request_log(tmp_path))


async def test_effort_from_model_settings_thinking(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    try:
        await m.request(_msgs(), ModelSettings(thinking="xhigh"), PARAMS)  # type: ignore[typeddict-item]
    finally:
        await m.aclose()
    turn = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/start")
    assert turn["params"]["effort"] == "xhigh"  # gpt-5.6-sol lists xhigh in DEFAULT_MODELS


async def test_plan_mode_is_read_only_and_never(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]}, mode=Mode.plan)
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    log = read_request_log(tmp_path)
    start = next(r for r in log if r["method"] == "thread/start")
    assert start["params"]["approvalPolicy"] == "never" and start["params"]["sandbox"] == "read-only"
    turn = next(r for r in log if r["method"] == "turn/start")
    assert turn["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}


async def test_headless_folds_tool_activity_into_text(tmp_path):
    turn = [
        {"notify": "item/started", "params": {"item": {"id": "c1", "type": "commandExecution",
                                                        "command": ["ls", "-la"], "cwd": "/w"}}},
        {"notify": "item/completed", "params": {"item": {"id": "c1", "type": "commandExecution",
                                                          "command": ["ls", "-la"], "cwd": "/w",
                                                          "status": "completed", "exitCode": 0,
                                                          "aggregatedOutput": "a.py"}}},
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "Found a.py"}},
    ]
    m = _model(tmp_path, {"turns": [turn]})
    try:
        resp = await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    text = resp.parts[0].content
    assert "▸ bash" in text and "ls -la" in text and text.endswith("Found a.py")


async def test_stream_emits_cards_out_of_band(tmp_path):
    turn = [
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "Look: "}},
        {"notify": "item/started", "params": {"item": {"id": "c1", "type": "commandExecution",
                                                        "command": "pwd", "cwd": "/w"}}},
        {"notify": "item/completed", "params": {"item": {"id": "c1", "type": "commandExecution",
                                                          "command": "pwd", "cwd": "/w",
                                                          "status": "completed", "exitCode": 0,
                                                          "aggregatedOutput": "/w"}}},
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m2", "delta": "done"}},
    ]
    m = _model(tmp_path, {"turns": [turn]})
    seen: list = []

    async def on_activity(events):
        seen.extend(events)

    m.on_activity = on_activity
    texts: list[str] = []
    try:
        async with m.request_stream(_msgs(), None, PARAMS) as stream:
            async for ev in stream:
                delta = getattr(getattr(ev, "delta", None), "content_delta", None)
                if delta:
                    texts.append(delta)
            resp = stream.get()
    finally:
        await m.aclose()
    assert "".join(texts) == "Look: done"
    assert [type(e) for e in seen] == [FunctionToolCallEvent, FunctionToolResultEvent]
    assert seen[0].part.tool_name == "bash" and seen[0].part.args == {"command": "pwd", "cwd": "/w"}
    assert seen[1].part.content == "/w"
    # Two prose runs around the card -> two text parts (cards mode), no ▸ line.
    assert len(resp.parts) == 2 and "▸" not in resp.parts[0].content


async def test_failed_turn_raises_cli_model_error(tmp_path):
    m = _model(tmp_path, {"turns": [[{"fail": "quota exhausted"}]]})
    try:
        with pytest.raises(CliModelError, match="quota exhausted"):
            await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()


async def test_server_crash_raises_with_stderr_tail(tmp_path):
    m = _model(tmp_path, {"turns": [[{"exit": 3}]]})
    try:
        with pytest.raises(CliModelError, match="exited mid-turn"):
            await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()


async def test_cancel_interrupts_the_turn(tmp_path):
    m = _model(tmp_path, {"turns": [[{"hang": True}], _hello_turn()]})
    try:
        task = asyncio.create_task(m.request(_msgs(), None, PARAMS))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert any(r["method"] == "turn/interrupt" for r in read_request_log(tmp_path))
        # The thread survives the interrupt: the next turn reuses it.
        resp = await m.request(_msgs("again"), None, PARAMS)
        assert resp.parts[0].content == "Hi there"
    finally:
        await m.aclose()


async def test_steer_forwards_to_active_turn_only(tmp_path):
    m = _model(tmp_path, {"turns": [[{"hang": True}]]})
    try:
        assert m.steer("nothing running") is False
        task = asyncio.create_task(m.request(_msgs(), None, PARAMS))
        await asyncio.sleep(0.3)
        assert m.steer("also check tests") is True
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        steer = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/steer")
        assert steer["params"]["input"][0]["text"] == "also check tests"
    finally:
        await m.aclose()


async def test_ask_mode_brokers_approval_through_request_approval(tmp_path):
    turn = [
        {"request": "item/commandExecution/requestApproval",
         "params": {"itemId": "c1", "command": "rm -rf build", "cwd": "/w"},
         "record_as": "approval"},
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "ok"}},
    ]
    m = _model(tmp_path, {"turns": [turn]}, mode=Mode.ask)
    asked: list = []

    async def approver(call):
        asked.append(call.tool_name)
        return True

    m.request_approval = approver
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    assert asked == ["bash"]
    assert any(r.get("approval") == {"decision": "accept"} for r in read_request_log(tmp_path))
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["approvalPolicy"] == "untrusted"


async def test_ephemeral_clone_is_plan_mode_and_threadless(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    clone = m.ephemeral_clone(cwd="/elsewhere")
    assert isinstance(clone, CodexCliModel) and clone.ephemeral
    assert clone.cwd == "/elsewhere" and clone.mode_getter() == "plan"
    assert clone.thread is None and clone.session_ref_getter is None
    assert clone.model_name == m.model_name
    await m.aclose()


async def test_compact_remote_calls_thread_compact(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    try:
        await m.compact_remote()  # no thread yet: no-op, no error
        await m.request(_msgs(), None, PARAMS)
        await m.compact_remote()
    finally:
        await m.aclose()
    assert any(r["method"] == "thread/compact/start" for r in read_request_log(tmp_path))


async def test_missing_binary_is_a_cli_model_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", str(tmp_path / "no-such-codex"))
    m = CodexCliModel("gpt-5.6-sol")
    m.cwd = str(tmp_path)
    with pytest.raises(CliModelError, match="codex"):
        await m.request(_msgs(), None, PARAMS)
```

The `"thread/tokenUsage/updated"` method name and the `tokenUsage.total`
shape must match what Task 5's `ItemTranslator._usage` reads (see the
`_METHODS` table there) — if Task 0 renamed it, update both the fake step
here and the translator, not one of them.

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest --no-cov tests/test_codex_cli_model.py -v`
Expected: FAIL — `ModuleNotFoundError: marim_harness.config.codex_cli_model`.

- [ ] **Step 3: Implement**

`src/marim_harness/codex/turn.py`:

```python
"""One Codex turn, driven to completion.

The event loop shared by the main-loop model (``config/codex_cli_model.py``)
and the sub-agent backend (``subagents/codex_spawn.py``): pull notifications
off the thread's queue, translate them, hand text/activity items to the
caller, and fold usage + completion into a ``TurnState``. Cancellation
interrupts the turn but keeps the thread (the next turn reuses it); a dead
server surfaces as ``CliModelError`` carrying the stderr tail; idling past the
server's timeout interrupts the turn and raises.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from pydantic_ai.usage import RequestUsage

from ..config.external_cli import CliModelError
from .server import CLOSED, CodexServer, ThreadHandle
from .translate import ItemTranslator, TurnDone, TurnFailure, UsageUpdate

_USAGE_KEYS = ("inputTokens", "outputTokens", "cachedInputTokens", "reasoningOutputTokens")


@dataclass
class TurnState:
    """What one ``turn/start`` accumulates while its notifications stream."""

    translator: ItemTranslator = field(default_factory=ItemTranslator)
    usage_total: dict = field(default_factory=dict)
    done: TurnDone | None = None
    failure: str | None = None


def text_input(text: str) -> dict:
    """A ``turn/start``/``turn/steer`` text input item."""
    return {"type": "text", "text": text, "text_elements": []}


def usage_from_total(total: dict) -> RequestUsage:
    return RequestUsage(
        input_tokens=int(total.get("inputTokens") or 0),
        output_tokens=int(total.get("outputTokens") or 0),
        cache_read_tokens=int(total.get("cachedInputTokens") or 0),
    )


def delta_since(total: dict, baseline: dict) -> dict:
    """Codex reports cumulative thread totals; marim wants per-turn usage."""
    return {k: int(total.get(k) or 0) - int(baseline.get(k) or 0) for k in _USAGE_KEYS}


async def turn_events(
    server: CodexServer, handle: ThreadHandle, state: TurnState
) -> AsyncIterator[object]:
    """Yield translated items (TextDelta/ThinkingDelta/ActivityStart/ActivityEnd/
    Notice) for the current turn until it completes. Usage and completion are
    folded into ``state`` rather than yielded."""
    try:
        while state.done is None:
            method, params = await asyncio.wait_for(handle.events.get(), server.timeout)
            if method == CLOSED:
                tail = str(params.get("stderr") or "").strip()
                raise CliModelError(f"codex app-server exited mid-turn: {tail or 'no stderr'}")
            for item in state.translator.translate(method, params):
                if isinstance(item, UsageUpdate):
                    state.usage_total = item.total
                elif isinstance(item, TurnDone):
                    state.done = item
                elif isinstance(item, TurnFailure):
                    state.failure = item.message
                    if not item.will_retry:
                        state.done = TurnDone("failed", item.message)
                else:
                    yield item
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(Exception):
            await server.interrupt(handle)
        raise CliModelError(f"codex turn idle for {server.timeout:.0f}s; interrupted") from exc
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await server.interrupt(handle)
        raise
    finally:
        handle.current_turn_id = None


def finish_turn(handle: ThreadHandle, state: TurnState) -> RequestUsage:
    """Per-turn usage for a completed turn; raises ``CliModelError`` when the
    turn failed (or never reported completion). Advances the thread's usage
    baseline so the next turn's delta starts from here."""
    done = state.done
    if done is None or done.status == "failed":
        msg = (done.error if done else None) or state.failure or "codex turn failed"
        raise CliModelError(f"codex: {msg}")
    usage = usage_from_total(delta_since(state.usage_total, handle.usage_baseline))
    if state.usage_total:
        handle.usage_baseline = dict(state.usage_total)
    return usage
```

`src/marim_harness/config/codex_cli_model.py`:

```python
"""Run the OpenAI Codex CLI (``codex app-server``) as a main-loop model provider.

A ChatGPT/Codex subscription is reachable only through the ``codex`` CLI, which
runs its own agentic loop. Like claude-cli, this provider makes marim a
*launcher*: one marim turn becomes one ``turn/start`` on a Codex thread that
lives for the marim session, Codex runs its tools inside its own sandbox, and
marim receives a single **text-only** ``ModelResponse``. Emitting
``ToolCallPart``s here would make pydantic_ai's agent graph try to execute
Codex's tool calls a second time, so Codex's activity is either pushed
out-of-band as tool cards (``on_activity`` bound by the TUI) or folded into
the streamed text as ``▸`` lines (headless).

Unlike claude-cli, Codex *asks marim* before privileged actions: those server
requests are answered by ``codex.approvals.ApprovalBroker`` through the same
``request_approval``/``ask_user`` seams native tools use, so ask mode gates
per call and plan mode is read-only + never-prompt (spec §Mode mapping).

Threads: the thread id is persisted with the marim session (Task 10,
``SessionStore.cli_thread_id``) through ``on_session_ref``/``session_ref_getter``
as ``"codex-cli:<thread id>"``; a resumed marim session first tries
``thread/resume`` and, if Codex no longer has the thread, starts a fresh one
seeded with the flattened history (the same cold-start rule claude-cli uses).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.usage import RequestUsage

from ..codex.approvals import ApprovalBroker, policy_for, sandbox_for, sandbox_mode_for
from ..codex.env import INSTALL_HINT, CodexUnavailable, codex_available
from ..codex.server import CodexServer, ThreadHandle, shared_server
from ..codex.translate import ActivityEnd, ActivityStart, Notice, TextDelta, ThinkingDelta
from ..codex.turn import TurnState, finish_turn, text_input, turn_events
from ..runtime.permissions import Mode
from .claude_cli_model import extract_system, flatten_history, latest_user_text
from .external_cli import CliModelError, ExternalCliModel, TextFolder

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

__all__ = ["CliModelError", "CodexCliModel", "CodexStreamedResponse", "effort_for"]

SESSION_REF_PREFIX = "codex-cli:"

# marim thinking level -> Codex reasoning effort (spec §Thinking -> effort).
_EFFORT_FOR_LEVEL = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}


def effort_for(level: str | None, supported: list[str] | None) -> str | None:
    """``turn/start.effort`` for a marim thinking level. ``off``/None omit the
    key (Codex's own default). ``xhigh`` is sent only when the model lists it;
    otherwise (or when the catalog is unknown) it degrades to ``high``."""
    if not level or level == "off":
        return None
    if level == "xhigh":
        return "xhigh" if supported and "xhigh" in supported else "high"
    return _EFFORT_FOR_LEVEL.get(level, "medium")


# --- activity rendering (shared with the spawn path, Task 11) ----------------
def activity_events(item: ActivityStart | ActivityEnd) -> list:
    """pydantic-ai tool events for a Codex activity, for the TUI card sinks
    (``on_activity``) — the same shapes ``cli_activity_events`` builds for
    claude-cli. Never enters the ModelResponse."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    if isinstance(item, ActivityStart):
        part = ToolCallPart(tool_name=item.tool_name, args=item.args, tool_call_id=item.item_id)
        return [FunctionToolCallEvent(part=part)]
    part = ToolReturnPart(
        tool_name="tool",
        content=item.content,
        tool_call_id=item.item_id,
        timestamp=datetime.now(tz=timezone.utc),
        outcome="failed" if item.is_error else "success",
    )
    return [FunctionToolResultEvent(part=part)]


def fold_activity_text(item: ActivityStart | ActivityEnd, leading: bool) -> str:
    """Headless rendering: a tool call becomes a ``▸ name(args)`` line; results
    are folded only when they failed (so a headless transcript stays short but
    a failing command is visible)."""
    if isinstance(item, ActivityStart):
        arg = item.args.get("command") or item.args.get("path") or item.args.get("query") or ""
        line = f"▸ {item.tool_name} {arg}".rstrip()
    elif item.is_error:
        first = item.content.strip().splitlines()[:1]
        line = f"▸ failed: {first[0] if first else 'error'}"
    else:
        return ""
    return line + "\n" if leading else "\n" + line + "\n"


class CodexCliModel(ExternalCliModel):
    """A Pydantic AI model backed by ``codex app-server``. See the module
    docstring for the shape; the late-bound seams are on ``ExternalCliModel``."""

    provider_id = "codex-cli"

    def __init__(
        self,
        model_id: str | None,
        *,
        ephemeral: bool = False,
        server: CodexServer | None = None,
    ) -> None:
        super().__init__()
        self._model_id = model_id
        self.ephemeral = ephemeral
        # Injected in tests; production models share the process-wide server
        # (one `codex app-server` per marim process, spec §Supervisor).
        self._server = server
        self.thread: ThreadHandle | None = None
        # Per-model catalog of reasoning efforts (model id -> efforts), filled
        # lazily from model/list on the first turn; drives effort_for.
        self._efforts: dict[str, list[str]] | None = None
        self._broker: ApprovalBroker | None = None

    # --- identity -------------------------------------------------------------
    @property
    def model_name(self) -> str:
        return self._model_id or "default"

    def ephemeral_clone(self, *, cwd: str) -> CodexCliModel:
        """A stateless, read-only copy for one-shot aux agents (titler/
        summarizer): a fresh ephemeral Codex thread per request, plan mode so
        it can't edit, no session ref so it can never resume — or hijack — the
        user's live thread."""
        clone = CodexCliModel(self._model_id, ephemeral=True, server=self._server)
        clone.cwd = cwd
        clone.mode_getter = lambda: "plan"
        return clone

    @property
    def server(self) -> CodexServer:
        if self._server is None:
            self._server = shared_server()
        return self._server

    async def aclose(self) -> None:
        """Tests own their server; production closes the shared one via
        ``Harness.aclose`` -> ``close_shared_server`` (Task 10)."""
        if self._server is not None:
            await self._server.aclose()

    # --- mode / policy ----------------------------------------------------------
    def _mode(self) -> Mode:
        raw = self.mode_getter() if self.mode_getter is not None else "plan"
        try:
            return Mode(raw)
        except ValueError:
            return Mode.plan

    def _thinking(self, model_settings: ModelSettings | None) -> str | None:
        level = (model_settings or {}).get("thinking")
        if level is None and self.thinking_getter is not None:
            level = self.thinking_getter()
        return str(level) if level else None

    def _scratchpad(self) -> Path | None:
        return self.scratchpad_getter() if self.scratchpad_getter is not None else None

    def _make_broker(self) -> ApprovalBroker:
        return ApprovalBroker(
            mode_getter=self._mode,
            workspace_root=Path(self.cwd),
            scratchpad_getter=self._scratchpad,
            request_approval=self.request_approval,
            ask_user=self.ask_user,
        )

    # --- thread lifecycle -------------------------------------------------------
    async def _ensure_server(self) -> CodexServer:
        if not codex_available():
            raise CliModelError(f"codex CLI unavailable. {INSTALL_HINT}")
        server = self.server
        try:
            await server.start()
        except CodexUnavailable as exc:
            raise CliModelError(f"codex app-server failed to start: {exc}") from exc
        return server

    async def _load_efforts(self, server: CodexServer) -> None:
        if self._efforts is not None:
            return
        try:
            models = await server.list_models()
        except Exception as exc:  # best-effort catalog; effort degrades to `high`
            logger.debug("codex model/list failed: %s", exc)
            self._efforts = {}
            return
        self._efforts = {
            str(m.get("id") or m.get("model")): [
                str(e.get("reasoningEffort") or e) for e in m.get("supportedReasoningEfforts") or []
            ]
            for m in models
        }

    def _persisted_thread_id(self) -> str | None:
        if self.ephemeral or self.session_ref_getter is None:
            return None
        ref = self.session_ref_getter()
        if not ref or not ref.startswith(SESSION_REF_PREFIX):
            return None  # another provider's ref (e.g. claude-cli) — ignore
        return ref[len(SESSION_REF_PREFIX):] or None

    async def _thread_for(self, messages: list, server: CodexServer) -> tuple[ThreadHandle, bool]:
        """The thread to run this turn on, and whether it is FRESH (the first
        input must carry the flattened history). Order: the live handle; a
        ``thread/resume`` of the persisted id; a new thread."""
        if self.thread is not None and server.alive and self.thread.thread_id in server.thread_ids:
            return self.thread, False
        mode = self._mode()
        self._broker = self._make_broker()
        common: dict[str, Any] = {
            "cwd": self.cwd,
            "developer_instructions": extract_system(messages) or None,
            "model": self._model_id,
            "sandbox": sandbox_mode_for(mode),
            "approval_policy": policy_for(mode),
            "request_handler": self._broker.handle,
        }
        persisted = self._persisted_thread_id()
        if persisted is not None:
            handle = await server.resume_thread(persisted, **common)
            if handle is not None:
                self.thread = handle
                return handle, False
            logger.info("codex thread %s gone; starting fresh with flattened history", persisted)
        handle = await server.start_thread(ephemeral=self.ephemeral, **common)
        self.thread = handle
        if not self.ephemeral and self.on_session_ref is not None:
            self.on_session_ref(SESSION_REF_PREFIX + handle.thread_id)
        return handle, True

    async def _begin_turn(
        self, messages: list, model_settings: ModelSettings | None
    ) -> tuple[CodexServer, ThreadHandle]:
        server = await self._ensure_server()
        await self._load_efforts(server)
        handle, fresh = await self._thread_for(messages, server)
        text = flatten_history(messages) if fresh else latest_user_text(messages)
        mode = self._mode()
        supported = (self._efforts or {}).get(self._model_id or "", None)
        handle.usage_baseline = dict(handle.usage_baseline)
        await server.start_turn(
            handle,
            inputs=[text_input(text)],
            model=self._model_id,
            effort=effort_for(self._thinking(model_settings), supported),
            approval_policy=policy_for(mode),
            sandbox_policy=sandbox_for(mode, self.cwd),
        )
        return server, handle

    # --- Model API ----------------------------------------------------------------
    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        server, handle = await self._begin_turn(messages, model_settings)
        state = TurnState()
        parts: list[str] = []
        async for item in turn_events(server, handle, state):
            if isinstance(item, TextDelta):
                parts.append(item.delta)
            elif isinstance(item, (ActivityStart, ActivityEnd)):
                seg = fold_activity_text(item, leading=not parts)
                if seg:
                    parts.append(seg)
            elif isinstance(item, Notice):
                logger.info("codex: %s", item.message)
        usage = finish_turn(handle, state)
        return ModelResponse(
            parts=[TextPart(content="".join(parts))],
            model_name=self.model_name,
            timestamp=datetime.now(tz=timezone.utc),
            usage=usage,
            provider_name="codex-cli",
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncGenerator[StreamedResponse]:
        server, handle = await self._begin_turn(messages, model_settings)
        state = TurnState()
        stream = CodexStreamedResponse(
            model_request_parameters=model_request_parameters,
            _items=turn_events(server, handle, state),
            _finish=lambda: finish_turn(handle, state),
            _model_id=self.model_name,
            _ts=datetime.now(tz=timezone.utc),
            _on_activity=self.on_activity,
        )
        try:
            yield stream
        finally:
            if handle.current_turn_id is not None:  # abandoned mid-turn
                with contextlib.suppress(Exception):
                    await server.interrupt(handle)
                handle.current_turn_id = None

    # --- live controls ---------------------------------------------------------------
    def steer(self, text: str) -> bool:
        """Forward a mid-turn steer to ``turn/steer``. Fire-and-forget on the
        running loop: the harness calls this synchronously from the input
        path. Returns False (harness keeps buffering) when no turn is live."""
        handle = self.thread
        if handle is None or handle.current_turn_id is None or self._server is None:
            return False
        server = self._server
        loop = asyncio.get_running_loop()
        task = loop.create_task(server.steer(handle, text))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return True

    async def compact_remote(self) -> None:
        if self.thread is None or self._server is None:
            return
        await self._server.compact(self.thread)


@dataclass
class CodexStreamedResponse(StreamedResponse):
    """Streams the turn's translated items as text-delta events (prose) and
    out-of-band tool cards (``_on_activity``), folding ``▸`` lines instead
    when no UI is bound — the same two rendering modes as claude-cli."""

    _items: AsyncIterator[object] | None = None
    _finish: Callable[[], RequestUsage] | None = None
    _model_id: str = "default"
    _ts: datetime | None = None
    _on_activity: Callable[[list], Awaitable[None]] | None = None

    async def _get_event_iterator(self):
        if self._items is None:
            return
        folder = TextFolder(
            self._parts_manager,
            self._on_activity,
            activity_events=activity_events,
            fold_text=fold_activity_text,
            is_call=lambda item: isinstance(item, ActivityStart),
        )
        async for item in self._items:
            if isinstance(item, TextDelta):
                async for ev in folder.emit_text(item.delta):
                    yield ev
            elif isinstance(item, ThinkingDelta):
                for ev in self._parts_manager.handle_thinking_delta(
                    vendor_part_id=f"think-{item.item_id}", content=item.delta
                ):
                    yield ev
            elif isinstance(item, (ActivityStart, ActivityEnd)):
                async for ev in folder.emit_tool(item):
                    yield ev
        if self._finish is not None:
            self._usage = self._finish()
        self._finished = True

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def timestamp(self) -> datetime:
        return self._ts or datetime.now(tz=timezone.utc)

    @property
    def provider_name(self) -> str:
        return "codex-cli"

    @property
    def provider_url(self) -> str:
        return "https://openai.com/codex"
```

Two small additions this needs on Task 4's `CodexServer` (add them there now,
with one assertion each in `tests/test_codex_server.py`):

```python
    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def thread_ids(self) -> frozenset[str]:
        """Threads registered with the LIVE process (cleared on respawn), so a
        model can tell a stale handle from a usable one."""
        return frozenset(self._threads)
```

and make `start()` clear `self._threads` when it (re)spawns the process.
`handle_thinking_delta(vendor_part_id=..., content=...)` is the pydantic-ai
parts-manager method next to `handle_text_delta`; if the installed version
returns a single event instead of an iterable, wrap it: `for ev in [self._parts_manager.handle_thinking_delta(...)]`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_cli_model.py tests/test_codex_server.py -v`
Expected: all PASS. The cancel test needs the fake's `hang` step to answer
`turn/interrupt` (Task 3 implements `wait_for_interrupt`); if
`test_cancel_interrupts_the_turn` times out, the interrupt was sent before the
fake reached `hang` — raise the pre-cancel sleep to 0.5s, not the timeout.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/config/codex_cli_model.py src/marim_harness/codex/server.py \
        tests/test_codex_cli_model.py tests/test_codex_server.py
git commit -m "feat(config): CodexCliModel main-loop provider over codex app-server

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Provider registration, model catalog, TUI providers card

Make `codex-cli` a first-class provider: env config, credential detection
(binary + `codex login`), `build_model`, the model picker catalog (live
`model/list` with a static fallback), the qualified-id prefix set, thinking
support, and the Settings > Providers card (spec §Model catalog, §Supervisor
touchpoints).

**Files:**
- Create: `src/marim_harness/codex/catalog.py`
- Modify: `src/marim_harness/config/model.py:25,40,382-389,399-403,419,531-534,580-585`
- Modify: `src/marim_harness/config/context_limits.py:85`
- Modify: `src/marim_harness/interfaces/tui/providers.py:93,155-171,224-225,260-262,281-283`
- Modify: `src/marim_harness/interfaces/tui/settings.py:34,224`
- Modify: `src/marim_harness/runtime/bootstrap.py:102-105` (comment only)
- Modify: `tests/conftest.py:154-176` (isolation), `tests/test_providers_section.py:105-117`
- Test: `tests/test_codex_catalog.py`, `tests/test_config.py`, `tests/test_providers_section.py`

**Interfaces:**
- Consumes: `resolve_codex_binary`, `codex_available` (Task 1); `CodexServer`, `shared_server` (Task 4); `CodexCliModel` (Task 8); `ModelEntry` (`workspace/catalog.py`).
- Produces:
  - `codex/catalog.py`: `STATIC_MODELS: tuple[ModelEntry, ...]`,
    `entries_from(models: list[dict]) -> list[ModelEntry]`,
    `async list_codex_models(*, strict: bool = False, server: CodexServer | None = None) -> list[ModelEntry]`.
  - `config/model.py`: `"codex-cli"` in `KNOWN_PROVIDERS`; `_DEFAULT_CODEX_CLI_MODEL: str | None = None`; `_codex_cli_available() -> bool`.
  - `interfaces/tui/providers.py`: `_BINARY_PROVIDERS`, `ProvidersPane(..., cli_detected: bool, codex_detected: bool = False, ...)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_codex_catalog.py`:

```python
"""codex/catalog.py: model/list -> ModelEntry, with the static fallback."""

from __future__ import annotations

import pytest

from marim_harness.codex.catalog import STATIC_MODELS, entries_from, list_codex_models
from marim_harness.codex.server import CodexServer
from tests.fakes import fake_codex_bin

pytestmark = pytest.mark.anyio


def test_entries_from_marks_thinking_and_keeps_order():
    raw = [
        {"id": "gpt-5.6-sol", "displayName": "GPT-5.6 Sol", "isDefault": True,
         "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
        {"id": "gpt-5.4-mini", "displayName": "GPT-5.4 mini", "supportedReasoningEfforts": []},
    ]
    entries = entries_from(raw)
    assert [e.id for e in entries] == ["gpt-5.6-sol", "gpt-5.4-mini"]
    assert entries[0].name == "GPT-5.6 Sol" and entries[0].provider == "codex-cli"
    assert all(e.supports_thinking is True for e in entries)  # every Codex model takes effort
    assert entries[0].context_window is None


def test_static_fallback_has_six_models_with_thinking():
    assert len(STATIC_MODELS) == 6
    assert {e.provider for e in STATIC_MODELS} == {"codex-cli"}
    assert all(e.supports_thinking for e in STATIC_MODELS)


async def test_list_codex_models_live(tmp_path):
    server = CodexServer(binary=fake_codex_bin(tmp_path, {}))
    try:
        entries = await list_codex_models(server=server)
    finally:
        await server.aclose()
    assert [e.id for e in entries] == ["gpt-5.6-sol", "gpt-5.4-mini"]


async def test_list_codex_models_falls_back_when_server_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", str(tmp_path / "missing"))
    server = CodexServer()
    entries = await list_codex_models(server=server)
    assert entries == list(STATIC_MODELS)
    with pytest.raises(Exception):
        await list_codex_models(server=server, strict=True)
```

Append to `tests/test_config.py` (after the claude-cli block, ~line 920):

```python
# ---------------------------------------------------------------------------
# codex-cli provider
# ---------------------------------------------------------------------------


def test_codex_cli_is_a_known_provider():
    assert "codex-cli" in model_mod.KNOWN_PROVIDERS


def test_provider_config_codex_cli(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "codex-cli")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    cfg = model_mod.load_config()
    assert cfg.provider == "codex-cli"
    assert cfg.model is None and cfg.api_key is None and cfg.base_url is None


def test_provider_config_codex_cli_model_override(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "codex-cli")
    monkeypatch.setenv("MARIM_MODEL", "gpt-5.4-mini")
    assert model_mod.load_config().model == "gpt-5.4-mini"


def test_codex_has_creds_follows_binary_and_login(monkeypatch, tmp_path):
    monkeypatch.setattr(model_mod, "_codex_cli_available", lambda: True)
    assert model_mod._provider_has_creds("codex-cli") is True
    monkeypatch.setattr(model_mod, "_codex_cli_available", lambda: False)
    assert model_mod._provider_has_creds("codex-cli") is False
    # The real detector: binary AND auth.json under CODEX_HOME.
    monkeypatch.undo()
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", str(fake_bin))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    assert model_mod._codex_cli_available() is False
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "auth.json").write_text("{}")
    assert model_mod._codex_cli_available() is True


def test_build_model_codex_cli():
    from dataclasses import replace

    cfg = replace(model_mod.load_config(), provider="codex-cli", model="gpt-5.6-sol")
    m = model_mod.build_model(cfg)
    from marim_harness.config.codex_cli_model import CodexCliModel

    assert isinstance(m, CodexCliModel)
    assert m.model_name == "gpt-5.6-sol" and m.system == "codex-cli"


@pytest.mark.anyio
async def test_list_models_codex_cli_uses_catalog(monkeypatch):
    from dataclasses import replace

    from marim_harness.config import model as _m
    from marim_harness.workspace.catalog import ModelEntry

    async def fake_list(*, strict=False, server=None):
        return [ModelEntry(id="x", name="X", provider="codex-cli", supports_thinking=True)]

    monkeypatch.setattr("marim_harness.codex.catalog.list_codex_models", fake_list)
    cfg = replace(_m.load_config(), provider="codex-cli", model=None)
    entries = await _m.ModelSource(cfg).list_models()
    assert [e.id for e in entries] == ["x"]


def test_codex_cli_qualified_prefix_is_recognised():
    from marim_harness.config.context_limits import _bare_id

    assert _bare_id("codex-cli:gpt-5.6-sol") == "gpt-5.6-sol"
```

Append to `tests/test_providers_section.py` (the file already imports
`ProvidersPane`, `App`, and defines `_PaneHost`; extend `_PaneHost.__init__`
with `codex_detected=False` and pass it through to `ProvidersPane`):

```python
@pytest.mark.anyio
async def test_codex_card_reports_binary_detection():
    async with _PaneHost(codex_detected=True).run_test() as pilot:
        pane = pilot.app.query_one(ProvidersPane)
        status = pane.query_one("#prov-status-codex-cli", Static).renderable
        assert "detected" in str(status)
        assert "codex login" in str(pane.query_one("#prov-note-codex-cli", Static).renderable)
    async with _PaneHost(codex_detected=False).run_test() as pilot:
        pane = pilot.app.query_one(ProvidersPane)
        assert "not found" in str(pane.query_one("#prov-status-codex-cli", Static).renderable)
```

(`Static` is imported in that test module already for the claude-cli card
test; copy its import if not. The note gets an id so the test can find it —
see Step 3.)

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest --no-cov tests/test_codex_catalog.py tests/test_config.py -k codex tests/test_providers_section.py -k codex -v`
Expected: FAIL — catalog module missing; `"codex-cli" in KNOWN_PROVIDERS` false; `ProvidersPane` rejects `codex_detected`.

- [ ] **Step 3: Implement**

`src/marim_harness/codex/catalog.py`:

```python
"""The codex-cli model catalog for the picker: ``model/list`` from the shared
app-server, with a static fallback when the server is unavailable (not
installed, not logged in, or dead) so the picker never goes blank.

Every Codex model accepts a reasoning effort, so ``supports_thinking`` is
True for all entries (spec §Model catalog). Context windows are not reported
by ``model/list`` — ``context_limits`` falls back to its defaults.
"""

from __future__ import annotations

import logging

from ..workspace.catalog import ModelEntry
from .server import CodexServer, shared_server

logger = logging.getLogger(__name__)

PROVIDER = "codex-cli"


def _entry(model_id: str, name: str) -> ModelEntry:
    return ModelEntry(id=model_id, name=name, provider=PROVIDER, supports_thinking=True)


# The fallback list. Keep in sync with Task 0's `codex app-server` model/list
# output at the pinned MIN_CODEX_VERSION; order = the CLI's own (default first).
STATIC_MODELS: tuple[ModelEntry, ...] = (
    _entry("gpt-5.6-sol", "GPT-5.6 Sol"),
    _entry("gpt-5.6", "GPT-5.6"),
    _entry("gpt-5.6-codex", "GPT-5.6 Codex"),
    _entry("gpt-5.5", "GPT-5.5"),
    _entry("gpt-5.4-mini", "GPT-5.4 mini"),
    _entry("gpt-5.3-codex", "GPT-5.3 Codex"),
)


def entries_from(models: list[dict]) -> list[ModelEntry]:
    out: list[ModelEntry] = []
    for m in models:
        model_id = str(m.get("id") or m.get("model") or "")
        if not model_id:
            continue
        out.append(_entry(model_id, str(m.get("displayName") or model_id)))
    return out


async def list_codex_models(
    *, strict: bool = False, server: CodexServer | None = None
) -> list[ModelEntry]:
    """Live catalog, or ``STATIC_MODELS`` on any failure (``strict=True``
    re-raises instead — provider verification needs to tell "connected, 0
    models" from "failed to connect")."""
    srv = server if server is not None else shared_server()
    try:
        await srv.start()
        return entries_from(await srv.list_models()) or list(STATIC_MODELS)
    except Exception as exc:
        if strict:
            raise
        logger.info("codex model/list unavailable (%s); using the static catalog", exc)
        return list(STATIC_MODELS)
```

Fill `STATIC_MODELS` from Task 0's recorded `model/list` output — the six ids
above are the shape, not the truth; replace them with what the pinned CLI
actually lists (keep six or however many it lists).

`src/marim_harness/config/model.py` edits:

1. After line 25 (`_DEFAULT_CLAUDE_CLI_MODEL`):
   ```python
   # None ⇒ let the codex CLI use its own configured default model.
   _DEFAULT_CODEX_CLI_MODEL: str | None = None
   ```
2. Line 40: `KNOWN_PROVIDERS = frozenset({"openrouter", "local", "google", "claude-cli", "codex-cli", "zen", "zen-go"})`.
3. In `_provider_config`, after the claude-cli arm (line 389):
   ```python
       if provider == "codex-cli":
           return ModelConfig(
               provider="codex-cli",
               model=os.getenv("MARIM_MODEL", _DEFAULT_CODEX_CLI_MODEL),
               base_url=None,
               api_key=None,  # the CLI owns auth (`codex login`)
               **common,
           )
   ```
4. After `_claude_cli_available` (line 403):
   ```python
   def _codex_cli_available() -> bool:
       """True when a ``codex`` binary resolves AND ``codex login`` has run
       (auth.json under CODEX_HOME). Both, unlike claude-cli's binary-only
       check: an unauthenticated app-server fails only on the first turn."""
       from ..codex.env import codex_available

       return codex_available()
   ```
5. In `_provider_has_creds`, after the claude-cli line (419):
   ```python
       if provider == "codex-cli":
           return _codex_cli_available()
   ```
6. In `build_model`, after the claude-cli arm (534):
   ```python
       if cfg.provider == "codex-cli":
           from .codex_cli_model import CodexCliModel

           return CodexCliModel(cfg.model)
   ```
7. In `ModelSource.list_models`, after the claude-cli arm (585):
   ```python
           if self.cfg.provider == "codex-cli":
               from ..codex import catalog as codex_catalog

               return await codex_catalog.list_codex_models(strict=strict)
   ```
   (Module import + attribute access, so the test's `monkeypatch.setattr("marim_harness.codex.catalog.list_codex_models", ...)` takes effect.)

`src/marim_harness/config/context_limits.py:85`:
```python
_PROVIDER_PREFIXES = frozenset(
    {"openrouter", "local", "google", "claude-cli", "codex-cli", "zen", "zen-go"}
)
```

`src/marim_harness/interfaces/tui/providers.py`:

1. After the claude-cli `ProviderSpec` (line 93):
   ```python
       # codex-cli stores nothing either: `codex login` owns auth; status is
       # binary + login detection.
       ProviderSpec("codex-cli", write_key=None, key_fallbacks=(), read_keys=(), drop_keys=()),
   ```
   and after `_SPECS = {...}`:
   ```python
   # Providers whose "configured" state is the presence of an external binary
   # (plus its own login), not a stored key.
   _BINARY_PROVIDERS = frozenset({"claude-cli", "codex-cli"})
   ```
2. `__init__` (155): add `codex_detected: bool = False,` after `cli_detected: bool,` and store `self._codex_detected = codex_detected`.
3. Note (224-225): replace the `if name == "claude-cli":` branch with
   ```python
               if name == "claude-cli":
                   yield Static(
                       "(auth handled by the claude CLI itself)",
                       classes="prov-note",
                       id="prov-note-claude-cli",
                   )
               elif name == "codex-cli":
                   yield Static(
                       "(auth handled by `codex login`; needs codex ≥ 0.152)",
                       classes="prov-note",
                       id="prov-note-codex-cli",
                   )
   ```
4. `_configured` (260):
   ```python
       def _configured(self, spec: ProviderSpec) -> bool:
           if spec.name == "claude-cli":
               return self._cli_detected
           if spec.name == "codex-cli":
               return self._codex_detected
           return spec_configured(spec)
   ```
5. `_status_text` (281): `if spec.name in _BINARY_PROVIDERS:` with the same
   `"detected on PATH" if configured else "not found"` text — for codex-cli use
   `"detected + logged in" if configured else "not found or not logged in"`:
   ```python
           if spec.name == "claude-cli":
               base = "detected on PATH" if configured else "not found"
           elif spec.name == "codex-cli":
               base = "detected + logged in" if configured else "not found or not logged in"
   ```

`src/marim_harness/interfaces/tui/settings.py`: add
`from ...codex.env import codex_available` next to the `resolve_cli_binary`
import (line 34) and pass `codex_detected=codex_available(),` after
`cli_detected=...` (line 224).

`tests/conftest.py`: in `_isolated_provider_env`, after the `_PROVIDER_CRED_ENVS`
loop, add:
```python
    # codex-cli detection reads the developer's real ~/.codex/auth.json; point
    # CODEX_HOME at nothing so provider auto-detection is deterministic. Tests
    # that want a "logged in" codex set their own CODEX_HOME.
    monkeypatch.setenv("CODEX_HOME", "/nonexistent/marim-test-codex-home")
```

`src/marim_harness/runtime/bootstrap.py:102-105`: extend the comment —
"A None model (claude-cli's and codex-cli's "let the CLI choose" default) must
round-trip to a bare empty id ...".

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_codex_catalog.py tests/test_config.py tests/test_providers_section.py tests/test_context_limits.py -v`
Expected: all PASS (existing claude-cli assertions untouched).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/codex/catalog.py src/marim_harness/config/model.py \
        src/marim_harness/config/context_limits.py src/marim_harness/interfaces/tui/providers.py \
        src/marim_harness/interfaces/tui/settings.py src/marim_harness/runtime/bootstrap.py \
        tests/conftest.py tests/test_codex_catalog.py tests/test_config.py tests/test_providers_section.py
git commit -m "feat(config): register the codex-cli provider, catalog and providers card

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Session thread persistence + harness live controls

Persist the Codex thread id with the marim session so a resumed session
continues the same Codex thread (spec §Session persistence), and route the
harness's live controls to the external model: `/steer` → `turn/steer`,
`/compact` → `thread/compact/start`, shutdown → shared app-server close
(spec §Data flow, §Supervisor).

**Files:**
- Modify: `src/marim_harness/session/store.py:153-215,231-232,274-298,418-419,426-454`
- Modify: `src/marim_harness/session/ctrl.py:377-390,416-427`
- Modify: `src/marim_harness/runtime/harness.py:1011-1025,1208-1225,1246-1248,1277-1282`
- Test: `tests/test_session_cli_thread_id.py`
- Modify: `src/marim_harness/runtime/context.py` (`actionable_error_note`, ~line 154)
- Test: `tests/test_provider_errors.py` (two new tests, appended)

**Interfaces:**
- Consumes: `ExternalCliModel` (`steer`, `compact_remote`, `session_ref_getter`, `on_session_ref`) (Task 7); `close_shared_server` (Task 4).
- Produces:
  - `SessionInfo.cli_thread_id: str | None`; `SessionStore.__init__(..., cli_thread_id: str | None = None)` and attribute `cli_thread_id`, persisted by `save`/`save_meta`, read by `SessionManager.list`/`store`, **not** inherited by `create`.
  - `SessionController.saved_cli_thread_id -> str | None`; `SessionController.set_cli_thread_id(value: str | None) -> None`.
  - `Harness.steer` returns early when the current external model accepted the steer; `Harness.manual_compact` calls `compact_remote()` after a successful local compaction; `Harness.aclose` closes the shared Codex server.

- [ ] **Step 1: Write the failing tests**

`tests/test_session_cli_thread_id.py`:

```python
"""The external-CLI thread ref (`cli_thread_id`) rides on the session header."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage

from marim_harness.config.external_cli import ExternalCliModel
from marim_harness.session.ctrl import SessionController
from marim_harness.session.store import SessionManager
from tests.conftest import _make_deps, _make_harness, _text_model


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")


def _history() -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[TextPart(content="hello")]),
    ]


def test_cli_thread_id_roundtrips_through_save_and_save_meta(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Work")
    store.cli_thread_id = "codex-cli:thread-1"
    store.save(_history(), RunUsage(input_tokens=1, output_tokens=1))
    assert mgr.store(store.session_id).cli_thread_id == "codex-cli:thread-1"
    assert next(i for i in mgr.list() if i.id == store.session_id).cli_thread_id == (
        "codex-cli:thread-1"
    )

    store.cli_thread_id = "codex-cli:thread-2"
    store.save_meta()  # metadata-only patch, messages untouched
    again = mgr.store(store.session_id)
    assert again.cli_thread_id == "codex-cli:thread-2"
    messages, _, _, _, _ = again.load()
    assert len(messages) == 2


def test_new_session_does_not_inherit_thread_id(tmp_path: Path):
    mgr = _manager(tmp_path)
    first = mgr.create("A")
    first.cli_thread_id = "codex-cli:thread-1"
    first.save(_history(), RunUsage())
    second = mgr.create("B")  # inherits model/advisor/thinking — never the thread
    assert second.cli_thread_id is None


def test_controller_set_cli_thread_id_patches_meta(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Work")
    ctrl = SessionController(store, mgr, _make_deps(tmp_path), 100_000, 20)
    assert ctrl.saved_cli_thread_id is None
    ctrl.set_cli_thread_id("codex-cli:thread-1")  # no file yet -> forced clean persist
    assert store.path.exists()
    assert mgr.store(store.session_id).cli_thread_id == "codex-cli:thread-1"
    ctrl.set_cli_thread_id(None)
    assert mgr.store(store.session_id).cli_thread_id is None


def test_set_model_clears_thread_ref_when_provider_changes(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Work")
    ctrl = SessionController(store, mgr, _make_deps(tmp_path), 100_000, 20)
    ctrl.set_cli_thread_id("codex-cli:thread-1")
    ctrl.set_model("codex-cli:gpt-5.4-mini")  # same provider: the thread continues
    assert ctrl.saved_cli_thread_id == "codex-cli:thread-1"
    ctrl.set_model("openrouter:foo/bar")  # provider switch orphans the thread
    assert ctrl.saved_cli_thread_id is None
    assert mgr.store(store.session_id).cli_thread_id is None


class _Fake(ExternalCliModel):
    provider_id = "fake-cli"

    def __init__(self) -> None:
        super().__init__()
        self.steered: list[str] = []
        self.compacted = 0
        self.accept_steer = True

    @property
    def model_name(self) -> str:
        return "fake"

    def steer(self, text: str) -> bool:
        self.steered.append(text)
        return self.accept_steer

    async def compact_remote(self) -> None:
        self.compacted += 1

    async def request(self, messages, model_settings, model_request_parameters):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.anyio
async def test_harness_wires_thread_ref_seams_to_the_session(tmp_path: Path):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    fake = _Fake()
    harness.wire_cli_model(fake)
    assert fake.session_ref_getter is not None and fake.on_session_ref is not None
    assert fake.session_ref_getter() is None
    fake.on_session_ref("codex-cli:thread-1")
    assert harness.session.saved_cli_thread_id == "codex-cli:thread-1"
    assert fake.session_ref_getter() == "codex-cli:thread-1"


@pytest.mark.anyio
async def test_harness_steer_short_circuits_to_external_model(tmp_path: Path, monkeypatch):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    fake = _Fake()
    harness.wire_cli_model(fake)
    harness.current_model = fake
    buffered: list[str] = []
    monkeypatch.setattr(harness.turn_controller, "steer", lambda text, att=None: buffered.append(text))
    harness.steer("go left")
    assert fake.steered == ["go left"] and buffered == []
    fake.accept_steer = False  # no live turn: the harness keeps its own buffering
    harness.steer("go right")
    assert buffered == ["go right"]


@pytest.mark.anyio
async def test_manual_compact_also_compacts_remote_thread(tmp_path: Path, monkeypatch):
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    fake = _Fake()
    harness.wire_cli_model(fake)
    harness.current_model = fake

    async def ok(*, instructions=None):
        return True

    monkeypatch.setattr(harness.turn_controller, "manual_compact", ok)
    assert await harness.manual_compact() is True
    assert fake.compacted == 1

    async def blocked(*, instructions=None):
        return False

    monkeypatch.setattr(harness.turn_controller, "manual_compact", blocked)
    assert await harness.manual_compact() is False
    assert fake.compacted == 1  # a blocked local compaction never touches the thread


@pytest.mark.anyio
async def test_aclose_closes_the_shared_codex_server(tmp_path: Path, monkeypatch):
    closed: list[bool] = []

    async def fake_close():
        closed.append(True)

    monkeypatch.setattr("marim_harness.codex.server.close_shared_server", fake_close)
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    await harness.aclose()
    assert closed == [True]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest --no-cov tests/test_session_cli_thread_id.py -v`
Expected: FAIL — `SessionStore` has no attribute `cli_thread_id`; `SessionController` has no `saved_cli_thread_id`; steer goes to the controller; `compacted == 0`; `closed == []`.

- [ ] **Step 3: Implement**

`src/marim_harness/session/store.py`:

1. `SessionInfo` (line ~163, after `mode: str | None = None`):
   ```python
       # The external CLI's thread/session ref ("<provider>:<id>") when the
       # session ran on codex-cli (claude-cli keeps its own on the model today).
       cli_thread_id: str | None = None
   ```
2. `SessionStore.__init__` signature: add `cli_thread_id: str | None = None,` after `mode: str | None = None,`, and in the body after `self.mode = mode`:
   ```python
           # The external-CLI conversation this session continues (codex-cli
           # thread id, prefixed with the provider). Per-session, never
           # inherited by `create` — a new marim session is a new thread.
           self.cli_thread_id = cli_thread_id
   ```
3. `save` payload (after `"mode": self.mode,`): `"cli_thread_id": self.cli_thread_id,`.
4. `save_meta` (after `data["mode"] = self.mode`): `data["cli_thread_id"] = self.cli_thread_id`.
5. `SessionManager.list` (after `mode=data.get("mode"),`): `cli_thread_id=data.get("cli_thread_id"),`.
6. `SessionManager.store`: read `cli_thread_id = meta.get("cli_thread_id")` next to `mode = meta.get("mode")` and pass `cli_thread_id=cli_thread_id,` to the `SessionStore(...)` call.
7. `SessionManager.create`: no change — it passes model/advisor/thinking from `latest_*()` and leaves `cli_thread_id` at its `None` default. Add one comment line where those inherit: `# cli_thread_id is deliberately NOT inherited: a new session is a new thread.`

`src/marim_harness/session/ctrl.py`:

1. In `set_model`, right after `self.store.model = model_id`:
   ```python
               ref = self.store.cli_thread_id
               if ref and not ref.startswith(model_id.split(":", 1)[0] + ":"):
                   # A thread belongs to one external CLI. Switching provider
                   # orphans it — the new model would ignore the foreign
                   # prefix anyway, but a stale ref must not outlive the switch
                   # on disk (a later switch back would resume a thread whose
                   # history the session no longer matches).
                   self.store.cli_thread_id = None
   ```
2. After `set_thinking` (line ~427):
   ```python
       @property
       def saved_cli_thread_id(self) -> str | None:
           """The external-CLI thread ref persisted with this session
           ("<provider>:<id>"), or None if unset or no store."""
           return self.store.cli_thread_id if self.store is not None else None

       def set_cli_thread_id(self, value: str | None) -> None:
           """Persist the external-CLI thread ref. Same metadata-only patch
           rules as ``set_thinking``: the ref lands mid-turn (the thread is
           created on the first request), when in-memory history must never
           reach disk, so patch the header when a file exists, else force one
           clean persist."""
           if self.store is not None:
               self.store.cli_thread_id = value
               if self.store.path.exists():
                   self.store.save_meta()
               else:
                   self.persist(force=True)
   ```

`src/marim_harness/runtime/harness.py`:

1. `wire_cli_model` (Task 7 bound the session seams through `getattr`; now the
   controller has them for real):
   ```python
           session = self.session
           model.session_ref_getter = (
               (lambda: session.saved_cli_thread_id) if session is not None else None
           )
           model.on_session_ref = session.set_cli_thread_id if session is not None else None
   ```
   replacing the two `getattr(...)` lines.
2. `steer`:
   ```python
       def steer(self, text: str, attachments: list[tuple[bytes, str]] | None = None) -> None:
           """Delegate to ``turn_controller.steer`` — unless an external-CLI
           model owns the live turn and took the steer itself (codex-cli's
           ``turn/steer``): the harness's buffer would otherwise replay the text
           as a second user turn after Codex already acted on it."""
           model = self.current_model
           if not attachments and isinstance(model, ExternalCliModel) and model.steer(text):
               return
           self.turn_controller.steer(text, attachments)
   ```
   (`ExternalCliModel` is already imported in this module by Task 7.)
3. `manual_compact`:
   ```python
           ok = await self.turn_controller.manual_compact(instructions=instructions)
           if ok and isinstance(self.current_model, ExternalCliModel):
               # Best-effort: marim's history is compacted; ask the external CLI
               # to compact its own thread too so both sides shrink together.
               # A failure here must not undo the local compaction.
               try:
                   await self.current_model.compact_remote()
               except Exception as exc:  # noqa: BLE001 - remote compaction is advisory
                   logger.warning("remote compaction failed: %s", exc)
           return ok
   ```
4. `aclose`, inside the `try:` after the LSP close:
   ```python
               # The process-wide codex app-server (if any turn ever started
               # one). Idempotent; a no-op when codex-cli was never used.
               from ..codex import server as codex_server

               await codex_server.close_shared_server()
   ```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_session_cli_thread_id.py tests/test_session.py tests/test_session_persist_cache.py tests/test_session_v1_compat.py tests/test_external_cli_model.py -v`
Expected: all PASS. If `test_session_v1_compat` asserts an exact header key set, add `cli_thread_id` to its expected keys — the v1 reader path (`meta.get`) tolerates the missing key by construction.

- [ ] **Step 4b: Surface a failed Codex turn to the model (spec §Supervisor)**

`actionable_error_note` (`src/marim_harness/runtime/context.py`) is silent on
`CliModelError` today, so a Codex turn that ends `status: failed` (usage limit,
auth, "exited mid-turn") would leave the model with no idea its last turn was
cut off. Append two tests to `tests/test_provider_errors.py`:

```python
def test_actionable_note_for_cli_model_error():
    # A failed external-CLI turn (codex-cli/claude-cli) carries the CLI's own
    # reason; the model can act on usage-limit/auth/cut-off messages.
    from marim_harness.config.external_cli import CliModelError

    note = _actionable_error_note(CliModelError("codex: usage limit reached"))
    assert note is not None
    assert "usage limit reached" in note


def test_cli_model_error_note_is_capped():
    from marim_harness.config.external_cli import CliModelError

    note = _actionable_error_note(CliModelError("x" * 1000))
    assert note is not None and len(note) < 400
```

Run: `uv run pytest --no-cov tests/test_provider_errors.py -k cli_model_error -v`
Expected: FAIL (`note is None`).

Then add the arm at the end of `actionable_error_note`, just before the final
`return None` (import inside the function like the pydantic-ai names above it,
to keep `runtime/context.py` free of a `config` import at module load):

```python
    from ..config.external_cli import CliModelError

    if isinstance(exc, CliModelError):
        # An external CLI turn (claude-cli / codex-cli) failed or was cut off;
        # the CLI's message is the actionable part (usage limit, auth, crash).
        return f"{head} The external CLI reported: {_short(exc)}. Adjust and continue."
    return None
```

Run: `uv run pytest --no-cov tests/test_provider_errors.py -v` — Expected: PASS
(all, including the existing 4xx/5xx cases).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/session/store.py src/marim_harness/session/ctrl.py \
        src/marim_harness/runtime/harness.py src/marim_harness/runtime/context.py \
        tests/test_session_cli_thread_id.py tests/test_provider_errors.py
git commit -m "feat(session): persist the external-CLI thread ref; route steer/compact/close to the model

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: `backend: codex-cli` sub-agents

A spawn orchestrator for `backend: codex-cli` agents (spec §Sub-agent
backend): one Codex thread per spawn on the shared app-server, sandbox
read-only unless the agent has a mutating tool, approvals brokered to the
parent's panel with the agent's name as the label, native `outputSchema`,
resumable through the persisted thread id — all inside the same
`SpawnLifecycle` the native and claude-cli paths use.

**Files:**
- Create: `src/marim_harness/subagents/codex_spawn.py`
- Modify: `src/marim_harness/codex/env.py` (add `CODEX_MODEL_ENV`)
- Modify: `src/marim_harness/codex/turn.py` (no change; consumed)
- Modify: `src/marim_harness/subagents/runner.py:192-201,789-805,1384-1385`
- Modify: `src/marim_harness/subagents/cli_spawn.py:48-64` (accept and ignore two kwargs)
- Modify: `src/marim_harness/subagents/output_schema.py`
- Modify: `src/marim_harness/workspace/agents.py:74-81` (comment)
- Modify: `docs/reference/configuration.md` (one row: `MARIM_CODEX_CLI_MODEL`)
- Modify: `tests/conftest.py:_TRUST_PROJECT_SUITES`
- Test: `tests/test_subagent_codex_spawn.py`, `tests/test_subagent_output_schema.py`

**Interfaces:**
- Consumes: `CodexServer`, `ThreadHandle`, `shared_server` (Task 4); `TurnState`, `turn_events`, `finish_turn`, `text_input` (`codex/turn.py`, Task 8); `activity_events`, `effort_for` (Task 8); `ApprovalBroker`, `policy_for`, `sandbox_for`, `sandbox_mode_for` (Task 6); `codex_available`, `INSTALL_HINT`, `CodexUnavailable` (Task 1); `CliModelError` (Task 7); `SpawnLifecycle`, `SpawnRun`, `CONTINUATION_PROMPT` (`subagents/backend.py`); `SpawnTranscripts`, `count_tool_calls` (`subagents/persistence.py`); `SpawnWorktree` (`subagents/isolation.py`); `effective_tools` (`workspace/agents.py`); `GATED_TOOLS` (`tools/names.py`); `resolve_thinking` (`thinking.py`); `TurnHooks` (`hooks/dispatch.py`).
- Produces:
  - `codex/env.py`: `CODEX_MODEL_ENV = "MARIM_CODEX_CLI_MODEL"`.
  - `subagents/codex_spawn.py`: `CodexRun(output, transcript, usage, thread_id)`;
    `CodexSpawnOrchestrator(*, deps, hooks, transcripts, lifecycle, resolve_agent, thinking_default=None, server=None)` with
    `execute(defn, task, work_root, iso, mcp_names, max_output_chars, model, stream_id, *, background, resume_thread_id=None, original_task=None, depth=1, transcript_prefix=None, output_schema=None, thinking=None) -> str`,
    `run_codex(defn, task, work_root, model, stream_id, *, output_schema=None, thinking=None, resume_thread_id=None) -> CodexRun`,
    `resume(stream_id, meta) -> tuple[str | None, str]`.
  - `subagents/output_schema.py`: `resolve_output_schema(schema, "codex-cli")` returns `(schema, "")` for any schema root.

- [ ] **Step 1: Write the failing tests**

`tests/test_subagent_codex_spawn.py`:

```python
"""backend: codex-cli sub-agents against the scripted fake app-server."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.codex.server import close_shared_server
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps, _make_harness
from tests.fakes import fake_codex_bin, read_request_log

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _shared_server_cleanup():
    yield
    await close_shared_server()


def _login(monkeypatch, tmp_path: Path, scenario: dict) -> None:
    """Point codex-cli at the fake binary AND a logged-in CODEX_HOME (the
    conftest isolation points CODEX_HOME at nothing, so `codex_available()`
    would otherwise be False)."""
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", fake_codex_bin(tmp_path, scenario))
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))


def _report_turn(text: str = "Done: report body") -> list[dict]:
    return [
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": text}},
        {"notify": "thread/tokenUsage/updated",
         "params": {"tokenUsage": {"total": {"inputTokens": 9, "outputTokens": 5}}}},
    ]


def _write_codex_agent(tmp_path: Path, tools: str = "read_file", extra: str = "") -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex-worker.md").write_text(
        f"---\ndescription: Codex worker\nbackend: codex-cli\ntools: {tools}\n{extra}---\n"
        "You are a Codex worker.\n",
        encoding="utf-8",
    )


def _dummy_model() -> FunctionModel:
    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    return FunctionModel(fn)


async def test_codex_backend_spawn_returns_report(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "do the thing", stream_id="s1")
    assert "Done: report body" in out
    assert runner.session.usage.output_tokens == 5
    log = read_request_log(tmp_path)
    start = next(r for r in log if r["method"] == "thread/start")
    assert start["params"]["developerInstructions"].startswith("You are a Codex worker.")
    assert start["params"]["sandbox"] == "read-only"  # read_file only -> no writes
    turn = next(r for r in log if r["method"] == "turn/start")
    assert turn["params"]["input"][0]["text"] == "do the thing"
    # The sidecar records the backend and the thread for a later resume.
    meta = runner._transcripts.read_meta("s1")
    assert meta["backend"] == "codex-cli" and meta["codex_thread_id"] == "thread-1"
    assert meta["status"] == "finished"


async def test_mutating_tools_get_workspace_write_in_auto_mode(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path, tools="read_file, edit_file, bash")
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path, Mode.auto)).subagents
    await runner.run("codex-worker", "edit", stream_id="s1")
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["sandbox"] == "workspace-write"
    assert start["params"]["approvalPolicy"] == "on-request"


async def test_plan_mode_forces_read_only_even_with_mutating_tools(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path, tools="read_file, edit_file, bash")
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path, Mode.plan)).subagents
    await runner.run("codex-worker", "edit", stream_id="s1")
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["sandbox"] == "read-only"
    assert start["params"]["approvalPolicy"] == "never"


async def test_output_schema_and_effort_are_forwarded(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn('{"ok": true}')]})
    _write_codex_agent(tmp_path, extra="thinking: high\n")
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    schema = {"type": "array", "items": {"type": "string"}}  # non-object root: still native
    out = await runner.run("codex-worker", "go", stream_id="s1", output_schema=schema)
    assert out.strip().endswith('{"ok": true}')
    turn = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/start")
    assert turn["params"]["outputSchema"] == schema
    assert turn["params"]["effort"] == "high"


async def test_model_precedence_and_model_env(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    monkeypatch.setenv("MARIM_CODEX_CLI_MODEL", "gpt-5.4-mini")
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    seen: list[tuple[str, str]] = []

    async def on_model(stream_id, model):
        seen.append((stream_id, model))

    runner.deps.ui.on_subagent_model = on_model
    await runner.run("codex-worker", "go", stream_id="s1")
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["model"] == "gpt-5.4-mini"
    assert seen == [("s1", "codex-cli:gpt-5.4-mini")]


async def test_tool_activity_streams_as_subagent_events(tmp_path: Path, monkeypatch):
    turn = [
        {"notify": "item/started", "params": {"item": {"id": "c1", "type": "commandExecution",
                                                        "command": "ls", "cwd": "/w"}}},
        {"notify": "item/completed", "params": {"item": {"id": "c1", "type": "commandExecution",
                                                          "command": "ls", "cwd": "/w",
                                                          "status": "completed", "exitCode": 0,
                                                          "aggregatedOutput": "a.py"}}},
        *_report_turn("saw a.py"),
    ]
    _login(monkeypatch, tmp_path, {"turns": [turn]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    events: list = []

    async def on_event(stream_id, event, usage):
        events.append(type(event).__name__)

    runner.deps.ui.on_subagent_event = on_event
    await runner.run("codex-worker", "go", stream_id="s1")
    assert "FunctionToolCallEvent" in events and "FunctionToolResultEvent" in events
    assert "PartDeltaEvent" in events
    meta = runner._transcripts.read_meta("s1")
    assert meta["tool_count"] == 1


async def test_failed_turn_is_contained(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [[{"fail": "rate limited"}]]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "go", stream_id="s1")
    assert "failed" in out.lower() and "rate limited" in out


async def test_missing_binary_is_contained(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", "no-such-codex-binary")
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "go", stream_id="s1")
    assert "failed" in out.lower() and "codex" in out.lower()


async def test_mcp_grants_are_noted_not_forwarded(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "go", stream_id="s1", mcp_names=["gitea"])
    assert "not forwarded to codex-cli" in out


async def test_run_codex_resumes_a_persisted_thread(tmp_path: Path, monkeypatch):
    _login(monkeypatch, tmp_path, {"resumable": ["thread-7"], "turns": [_report_turn()]})
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    defn = runner._resolve_agent("codex-worker")
    assert defn is not None
    run = await runner._codex.run_codex(
        defn, "continue", None, None, "s1", resume_thread_id="thread-7"
    )
    assert run.thread_id == "thread-7" and "Done: report body" in run.output
    log = read_request_log(tmp_path)
    assert any(r["method"] == "thread/resume" for r in log)
    assert not any(r["method"] == "thread/start" for r in log)


async def test_resume_refuses_without_a_thread_id(tmp_path: Path, monkeypatch):
    _write_codex_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    job_id, msg = await runner._codex.resume(
        "s1", {"type": "codex-worker", "task": "t", "backend": "codex-cli", "status": "interrupted"}
    )
    assert job_id is None and "thread id was never recorded" in msg
```

Append to `tests/test_subagent_output_schema.py`:

```python
def test_resolve_passes_any_schema_to_codex_cli():
    # Codex enforces `outputSchema` natively for any root type, so the
    # codex-cli backend gets the schema itself and no prompt contract.
    array_schema = {"type": "array", "items": {"type": "string"}}
    assert resolve_output_schema(array_schema, "codex-cli") == (array_schema, "")
    assert resolve_output_schema(FINDINGS, "codex-cli") == (FINDINGS, "")
    assert resolve_output_schema(None, "codex-cli") == (None, "")
```

Add `"test_subagent_codex_spawn.py"` to `_TRUST_PROJECT_SUITES` in
`tests/conftest.py` (project-local `.marim/agents` load only for trusted
projects; the suite list is how the fixture trusts `tmp_path`).

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest --no-cov tests/test_subagent_codex_spawn.py tests/test_subagent_output_schema.py -v`
Expected: FAIL — the runner treats `backend: codex-cli` as native (no `read_file`-only sandbox, no `thread/start` in the log; `runner._codex` missing); the output-schema test fails on the contract string.

- [ ] **Step 3: Implement**

`src/marim_harness/codex/env.py` — add next to `CODEX_BINARY_ENV`:
```python
# Default model for `backend: codex-cli` sub-agents (the spec's `model:` wins;
# the spawn call's model= wins over both). None ⇒ the CLI's own default.
CODEX_MODEL_ENV = "MARIM_CODEX_CLI_MODEL"
```
and `docs/reference/configuration.md` — one row after `MARIM_CODEX_CLI_TIMEOUT`
(same table format as the `MARIM_CLAUDE_CLI_MODEL` row):
`| \`MARIM_CODEX_CLI_MODEL\` | Default model for \`backend: codex-cli\` sub-agents (a Codex model id such as \`gpt-5.4-mini\`). The spec's \`model:\` and a spawn's \`model=\` override it. |`
(`tests/test_docs_reference.py::test_every_env_var_is_documented` fails otherwise.)

`src/marim_harness/subagents/output_schema.py` — in `resolve_output_schema`,
before the `if backend == "claude-cli" or ...` line:
```python
    if backend == "codex-cli":
        # Codex enforces `outputSchema` natively (turn/start.outputSchema),
        # for any root type — no prompt contract needed.
        return schema, ""
```
and extend the docstring's backend list accordingly.

`src/marim_harness/subagents/cli_spawn.py` — `CliSpawnOrchestrator.execute`
gains two keyword-only parameters after `transcript_prefix`:
```python
        output_schema: dict | None = None,  # accepted for dispatch symmetry; the
        thinking: str | None = None,  # claude-cli backend applies neither (see docstring)
```
(no other change; the runner passes both to whichever backend it dispatches to.)

`src/marim_harness/workspace/agents.py:74-77` comment: "`"claude-cli"` spawns
the Claude Code CLI (subagents/cli_spawn.py); `"codex-cli"` runs a thread on
the shared `codex app-server` (subagents/codex_spawn.py)."

`src/marim_harness/subagents/codex_spawn.py`:

```python
"""``backend: codex-cli`` sub-agents: one Codex thread per spawn.

The spawn runs on the process-wide ``codex app-server`` (``codex.server``),
inside the same ``SpawnLifecycle`` the native and claude-cli paths use — hooks
bracketing, output cap/spill, worktree close, background persist are written
once, not per backend. What is codex-specific:

* **Reach is decided up front, never mid-run** (the native rule). The sandbox
  is ``read-only`` unless the agent's effective tools include a mutating tool
  (``GATED_TOOLS``: write_file/edit_file/bash), and plan mode is always
  read-only. Codex's ``approvalPolicy`` follows the parent's mode exactly like
  the main loop: auto → on-request, ask → untrusted (brokered to the parent's
  approval panel, labelled with the agent name), plan → never.
* **Structured output** rides Codex's native ``outputSchema`` (any root type),
  so ``resolve_output_schema`` hands the schema through untouched.
* **Resume** reopens the persisted thread (``codex_thread_id`` in the sidecar)
  with ``thread/resume`` and sends the continuation prompt as a new turn.

MCP grants are NOT forwarded (Codex has its own MCP config); a non-empty
``mcp_names`` is noted in the output, mirroring claude-cli.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from ..codex.approvals import ApprovalBroker, policy_for, sandbox_for, sandbox_mode_for
from ..codex.env import CODEX_MODEL_ENV, INSTALL_HINT, CodexUnavailable, codex_available
from ..codex.server import CodexServer, shared_server
from ..codex.translate import ActivityEnd, ActivityStart, Notice, TextDelta, ThinkingDelta
from ..codex.turn import TurnState, finish_turn, text_input, turn_events
from ..config.codex_cli_model import activity_events, effort_for
from ..config.external_cli import CliModelError
from ..runtime.permissions import Mode
from ..thinking import resolve_thinking
from ..tools.names import GATED_TOOLS
from ..workspace import effective_tools
from .backend import CONTINUATION_PROMPT, SpawnLifecycle, SpawnRun
from .isolation import SpawnWorktree
from .persistence import SpawnTranscripts, count_tool_calls

if TYPE_CHECKING:
    from ..hooks.dispatch import TurnHooks
    from ..runtime.deps import Deps
    from ..workspace.agents import AgentDef

logger = logging.getLogger(__name__)

BACKEND = "codex-cli"


@dataclass
class CodexRun:
    output: str
    transcript: list[Any]
    usage: RunUsage
    thread_id: str | None


@dataclass
class _Transcript:
    """Folds translated Codex items into (a) pydantic-ai stream events for the
    sub-agents screen and (b) a pydantic-ai message list for the sidecar —
    the codex analog of ``cli_backend.CliStreamTranslator``. The spawn's
    *output* is the text of the LAST agent message (with ``outputSchema``
    that is the JSON document)."""

    messages: list[Any] = field(default_factory=list)
    _index: int = 0
    _texts: dict[str, list[str]] = field(default_factory=dict)
    _open_text: str | None = None  # item id of the TextPart being streamed
    _call_names: dict[str, str] = field(default_factory=dict)

    def _response(self) -> ModelResponse:
        if self.messages and isinstance(self.messages[-1], ModelResponse):
            return self.messages[-1]
        resp = ModelResponse(parts=[])
        self.messages.append(resp)
        return resp

    def feed(self, item: object) -> list[Any]:
        if isinstance(item, TextDelta):
            return self._text(item)
        if isinstance(item, ThinkingDelta):
            return self._thinking(item)
        if isinstance(item, ActivityStart):
            self._open_text = None
            self._call_names[item.item_id] = item.tool_name
            self._response().parts.append(
                ToolCallPart(tool_name=item.tool_name, args=item.args, tool_call_id=item.item_id)
            )
            return activity_events(item)
        if isinstance(item, ActivityEnd):
            self._open_text = None
            self.messages.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name=self._call_names.get(item.item_id, "tool"),
                            content=item.content,
                            tool_call_id=item.item_id,
                            timestamp=datetime.now(tz=timezone.utc),
                        )
                    ]
                )
            )
            return activity_events(item)
        if isinstance(item, Notice):
            logger.info("codex spawn: %s", item.message)
        return []

    def _text(self, item: TextDelta) -> list[Any]:
        events: list[Any] = []
        resp = self._response()
        if self._open_text != item.item_id:
            self._open_text = item.item_id
            self._index += 1
            resp.parts.append(TextPart(content=""))
            events.append(PartStartEvent(index=self._index, part=TextPart(content="")))
        part = resp.parts[-1]
        assert isinstance(part, TextPart)
        part.content += item.delta
        self._texts.setdefault(item.item_id, []).append(item.delta)
        events.append(
            PartDeltaEvent(index=self._index, delta=TextPartDelta(content_delta=item.delta))
        )
        return events

    def _thinking(self, item: ThinkingDelta) -> list[Any]:
        resp = self._response()
        self._open_text = None
        last = resp.parts[-1] if resp.parts else None
        if isinstance(last, ThinkingPart):
            last.content += item.delta
            return [PartDeltaEvent(index=self._index, delta=ThinkingPartDelta(content_delta=item.delta))]
        self._index += 1
        resp.parts.append(ThinkingPart(content=item.delta))
        return [PartStartEvent(index=self._index, part=ThinkingPart(content=item.delta))]

    def output(self) -> str:
        if not self._texts:
            return ""
        last_id = next(reversed(self._texts))
        return "".join(self._texts[last_id])


def _efforts_for(models: list[dict], model_id: str | None) -> list[str] | None:
    for m in models:
        if str(m.get("id") or m.get("model")) == model_id:
            return [str(e.get("reasoningEffort") or e) for e in m.get("supportedReasoningEfforts") or []]
    return None


class CodexSpawnOrchestrator:
    """Execute/resume ``backend: codex-cli`` spawns. Same constructor shape as
    ``CliSpawnOrchestrator`` plus the inherited-thinking reader the native
    path has (``thinking_default``) and an injectable server for tests."""

    def __init__(
        self,
        *,
        deps: Deps,
        hooks: TurnHooks,
        transcripts: SpawnTranscripts,
        lifecycle: SpawnLifecycle,
        resolve_agent: Callable[[str], AgentDef | None],
        thinking_default: Callable[[], str | None] | None = None,
        server: CodexServer | None = None,
    ) -> None:
        self.deps = deps
        self.hooks = hooks
        self._transcripts = transcripts
        self._lifecycle = lifecycle
        self._resolve_agent = resolve_agent
        self._thinking_default = thinking_default
        self._server = server

    # --- lifecycle wrapper (mirrors CliSpawnOrchestrator.execute) ---------------
    async def execute(
        self,
        defn: AgentDef,
        task: str,
        work_root: Path | None,
        iso: SpawnWorktree | None,
        mcp_names: list[str] | None,
        max_output_chars: int | None,
        model: str | None,
        stream_id: str,
        *,
        background: bool,
        resume_thread_id: str | None = None,
        original_task: str | None = None,
        depth: int = 1,
        transcript_prefix: list[Any] | None = None,
        output_schema: dict | None = None,
        thinking: str | None = None,
    ) -> str:
        hook_task = original_task or task
        t0 = time.perf_counter()
        meta: dict[str, Any] = {}
        if stream_id:
            meta = {
                "stream_id": stream_id,
                "type": defn.name,
                "task": hook_task,
                "model": model,
                "mcp": None,
                "depth": depth,
                "max_output_chars": max_output_chars,
                "isolation": iso.branch if iso else None,
                "status": "running",
                "backend": BACKEND,
                "codex_thread_id": resume_thread_id,
            }
            self._transcripts.save(
                stream_id, list(transcript_prefix or []), meta=meta, cap_reasoning=True
            )
        await self.hooks.subagent_start(defn.name, hook_task)
        resumed = resume_thread_id is not None

        async def _run() -> SpawnRun:
            result = await self.run_codex(
                defn,
                task,
                work_root,
                model,
                stream_id,
                output_schema=output_schema,
                thinking=thinking,
                resume_thread_id=resume_thread_id,
            )
            full_transcript = list(transcript_prefix or []) + result.transcript
            final_meta = {
                **meta,
                "status": "finished",
                "codex_thread_id": result.thread_id,
                "usage": {
                    "input": result.usage.input_tokens,
                    "output": result.usage.output_tokens,
                },
                "tool_count": count_tool_calls(full_transcript),
                "duration": time.perf_counter() - t0,
            }
            return SpawnRun(
                output=result.output,
                transcript=full_transcript,
                usage=result.usage,
                final_meta=final_meta,
                child_transcripts={},
            )

        return await self._lifecycle(
            _run,
            iso=iso,
            resumed=resumed,
            background=background,
            name=defn.name,
            stop_task=hook_task,
            note=self._mcp_note(mcp_names),
            max_output_chars=max_output_chars,
            stream_id=stream_id,
            timing=None,
        )

    @staticmethod
    def _mcp_note(mcp_names: list[str] | None) -> str:
        if not mcp_names:
            return ""
        names = ", ".join(mcp_names)
        return (
            f"[note: MCP servers ({names}) are not forwarded to codex-cli sub-agents; "
            "configure them in Codex's own config]\n\n"
        )

    # --- the codex run ---------------------------------------------------------------
    def _policy(self, defn: AgentDef) -> tuple[Mode, bool]:
        mode = self.deps.workspace.mode
        tools = effective_tools(defn, allow_gated=mode is Mode.auto, allow_net=mode is not Mode.plan)
        read_only = mode is Mode.plan or not (tools & GATED_TOOLS)
        return mode, read_only

    def _broker(self, mode: Mode, cwd: str, label: str) -> ApprovalBroker:
        services = getattr(self.deps, "services", None)
        get_scratchpad = getattr(services, "get_scratchpad", None)
        cbs = self.deps.ui
        return ApprovalBroker(
            mode_getter=lambda: mode,
            workspace_root=Path(cwd),
            scratchpad_getter=get_scratchpad or (lambda: None),
            request_approval=cbs.request_approval,
            ask_user=cbs.ask_user,
            label=label,
        )

    async def _server_or_raise(self) -> CodexServer:
        if not codex_available():
            raise CliModelError(f"codex CLI unavailable. {INSTALL_HINT}")
        server = self._server if self._server is not None else shared_server()
        try:
            await server.start()
        except CodexUnavailable as exc:
            raise CliModelError(f"codex app-server failed to start: {exc}") from exc
        return server

    async def run_codex(
        self,
        defn: AgentDef,
        task: str,
        work_root: Path | None,
        model: str | None,
        stream_id: str,
        *,
        output_schema: dict | None = None,
        thinking: str | None = None,
        resume_thread_id: str | None = None,
    ) -> CodexRun:
        server = await self._server_or_raise()
        mode, read_only = self._policy(defn)
        cwd = str(work_root or self.deps.workspace.root)
        model_name = model or defn.model or os.environ.get(CODEX_MODEL_ENV) or None
        inherited = self._thinking_default() if self._thinking_default is not None else None
        level = resolve_thinking(thinking, defn.thinking, inherited)
        broker = self._broker(mode, cwd, defn.name)
        common: dict[str, Any] = {
            "cwd": cwd,
            "developer_instructions": defn.prompt,
            "model": model_name,
            "sandbox": sandbox_mode_for(mode, read_only=read_only),
            "approval_policy": policy_for(mode),
            "request_handler": broker.handle,
        }
        if resume_thread_id is not None:
            handle = await server.resume_thread(resume_thread_id, **common)
            if handle is None:
                raise CliModelError(f"codex thread {resume_thread_id} is gone; cannot resume")
        else:
            handle = await server.start_thread(ephemeral=False, **common)
        cbs = self.deps.ui
        if stream_id and cbs.on_subagent_model is not None:
            await cbs.on_subagent_model(stream_id, f"{BACKEND}:{model_name or 'default'}")
        try:
            models = await server.list_models()
        except Exception:  # best-effort: effort degrades to `high` for xhigh
            models = []
        state = TurnState()
        tx = _Transcript()
        try:
            await server.start_turn(
                handle,
                inputs=[text_input(task)],
                model=model_name,
                effort=effort_for(level, _efforts_for(models, model_name)),
                approval_policy=policy_for(mode),
                sandbox_policy=sandbox_for(mode, cwd, read_only=read_only),
                output_schema=output_schema,
            )
            async for item in turn_events(server, handle, state):
                for event in tx.feed(item):
                    if stream_id and cbs.on_subagent_event is not None:
                        await cbs.on_subagent_event(stream_id, event, None)
            req_usage = finish_turn(handle, state)
        finally:
            server.drop_thread(handle)
        usage = RunUsage(
            requests=1,
            input_tokens=req_usage.input_tokens,
            output_tokens=req_usage.output_tokens,
        )
        if stream_id and cbs.on_subagent_usage is not None:
            await cbs.on_subagent_usage(stream_id, usage)
        return CodexRun(
            output=tx.output(), transcript=tx.messages, usage=usage, thread_id=handle.thread_id
        )

    # --- resume -----------------------------------------------------------------------
    async def resume(self, stream_id: str, meta: dict) -> tuple[str | None, str]:
        """Re-open the persisted thread and send the continuation prompt as a
        new turn, as a background job — the codex analog of
        ``CliSpawnOrchestrator.resume``."""
        thread_id = meta.get("codex_thread_id")
        if not thread_id:
            return None, (
                "The Codex thread id was never recorded for this spawn (it was "
                "interrupted before the thread started) — can't resume."
            )
        type_ = str(meta.get("type") or "")
        task = str(meta.get("task") or "")
        defn = self._resolve_agent(type_)
        if defn is None:
            return None, f"Unknown agent type {type_!r} — can't resume."
        if defn.backend != BACKEND:
            return None, f"Agent {type_!r} is no longer a codex-cli agent — can't resume."
        iso = None
        branch = meta.get("isolation")
        if branch:
            iso, err = SpawnWorktree.reopen(self.deps.workspace.root, str(branch))
            if err is not None:
                return None, err
        prior = self._transcripts.read(stream_id) or []
        label = f"{type_}: resumed — {task}"
        job_id = self.deps.jobs.register(
            "agent",
            label,
            self.execute(
                defn,
                CONTINUATION_PROMPT,
                iso.path if iso else None,
                iso,
                None,
                meta.get("max_output_chars"),
                meta.get("model"),
                stream_id,
                background=True,
                resume_thread_id=str(thread_id),
                original_task=task,
                depth=int(meta.get("depth") or 1),
                transcript_prefix=prior,
            ),
            stream_id=stream_id,
            prompt=task,
        )
        return job_id, f"Resumed as {job_id}."
```

`src/marim_harness/subagents/runner.py`:

1. Imports: `from .codex_spawn import CodexSpawnOrchestrator` next to the
   `CliSpawnOrchestrator` import.
2. Constructor (after `self._cli = CliSpawnOrchestrator(...)`, ~line 201; note
   `thinking_default` is the ctor parameter stored at line ~221 — build the
   orchestrator after that assignment or pass the parameter directly):
   ```python
           self._codex = CodexSpawnOrchestrator(
               deps=deps,
               hooks=hooks,
               transcripts=self._transcripts,
               lifecycle=self._run_spawn_lifecycle,
               resolve_agent=self._resolve_agent,
               thinking_default=thinking_default,
           )
   ```
3. Dispatch (after the `claude-cli` early return, ~line 805):
   ```python
           if defn is not None and defn.backend == "codex-cli":
               return await self._codex.execute(
                   defn,
                   task,
                   work_root,
                   iso,
                   mcp_names,
                   max_output_chars,
                   model,
                   stream_id,
                   background=background,
                   depth=depth,
                   output_schema=output_schema,
                   thinking=thinking,
               )
   ```
   and extend the comment above the claude-cli branch: "CLI-backed agents
   (claude-cli, codex-cli) run an external process …".
4. Resume arm (after the `claude-cli` one, ~line 1385):
   ```python
               if meta.get("backend") == "codex-cli":
                   return await self._codex.resume(stream_id, meta)
   ```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_subagent_codex_spawn.py tests/test_subagent_output_schema.py tests/test_subagent_cli_spawn.py tests/test_subagent_resume.py tests/test_docs_reference.py -v`
Expected: all PASS. If `test_codex_backend_spawn_returns_report` sees
`usage.output_tokens == 0`, the lifecycle reads `SpawnRun.usage` — check that
`finish_turn` got a `UsageUpdate` (the fake must emit `thread/tokenUsage/updated`
*before* `turn/completed`, which `_report_turn` does).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests && uv run pyright
git add src/marim_harness/subagents/codex_spawn.py src/marim_harness/subagents/runner.py \
        src/marim_harness/subagents/cli_spawn.py src/marim_harness/subagents/output_schema.py \
        src/marim_harness/codex/env.py src/marim_harness/workspace/agents.py \
        docs/reference/configuration.md tests/conftest.py \
        tests/test_subagent_codex_spawn.py tests/test_subagent_output_schema.py
git commit -m "feat(subagents): backend: codex-cli spawns on the shared app-server

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Documentation, example agent, changelog

Every user-facing surface that names `claude-cli` gets its `codex-cli`
counterpart (spec §Model catalog, §Sub-agent backend, §Isolation), plus an
example agent and the changelog entry. `tests/test_docs_reference.py` keeps
the env-var table and relative links honest.

**Files:**
- Modify: `.env.example:30-38,190-191`
- Modify: `README.md:121-123,201`
- Modify: `CLAUDE.md` (the `MARIM_PROVIDER` sentence and the `subagents/` bullet)
- Modify: `docs/reference/configuration.md:64,72-74,82-84,107-115`
- Modify: `docs/guides/subagents.md:130-131,213-236`
- Modify: `docs/guides/tui.md:124-125`
- Modify: `docs/guides/sessions.md:60-62`
- Modify: `docs/guides/headless.md:333-345`
- Modify: `CHANGELOG.md:9`
- Create: `docs/examples/agents/codex-worker.md`
- Test: `tests/test_agent_backend_field.py`, `tests/test_docs_reference.py`

**Interfaces:**
- Consumes: the env names from Task 1/11 (`MARIM_CODEX_CLI_BIN`, `MARIM_CODEX_CLI_TIMEOUT`, `MARIM_CODEX_CLI_MODEL`), the provider id `codex-cli`, the `backend: codex-cli` frontmatter value.
- Produces: `docs/examples/agents/codex-worker.md` (parsed by the new test).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_backend_field.py`:

```python
def test_example_codex_agent_parses_as_codex_cli(tmp_path: Path):
    import shutil

    src = Path("docs/examples/agents/codex-worker.md")
    dst = tmp_path / ".marim" / "agents" / "codex-worker.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    defn = find_agent(tmp_path, "codex-worker")
    assert defn is not None
    assert defn.backend == "codex-cli"
    assert defn.model is None  # let the CLI pick; MARIM_CODEX_CLI_MODEL overrides
    assert defn.thinking == "medium"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest --no-cov tests/test_agent_backend_field.py -v`
Expected: FAIL — `FileNotFoundError: docs/examples/agents/codex-worker.md`.

- [ ] **Step 3: Write the example agent and the docs**

`docs/examples/agents/codex-worker.md`:

```markdown
---
description: Autonomous worker backed by the OpenAI Codex CLI (codex app-server).
backend: codex-cli
thinking: medium
tools: read_file, glob, grep, edit_file, write_file, bash
---
You are an autonomous worker. Carry out the task you are given end-to-end
using your own tools, keep changes minimal and focused, then report what you
did and any results as your final message.
```

`.env.example` — after the claude-cli block (line 38, before
`# --- Context window & budget ---`):

```
# --- OpenAI Codex CLI (ChatGPT / Codex subscription) ---
# MARIM_PROVIDER=codex-cli delegates each turn to `codex app-server` (one
# long-lived process per marim session; needs `codex login` and codex >= 0.152).
# Approvals flow through marim's own panel; plan mode is read-only.
# MARIM_PROVIDER=codex-cli
# MARIM_MODEL=gpt-5.6-sol            # unset = the CLI's configured default
# MARIM_CODEX_CLI_BIN=codex          # override the binary if not on PATH
# MARIM_CODEX_CLI_TIMEOUT=600        # idle seconds before a turn is interrupted
```

and after line 191 (`# MARIM_CLAUDE_CLI_MODEL=sonnet`):

```
# Codex model for `backend: codex-cli` sub-agents (a Codex model id).
# MARIM_CODEX_CLI_MODEL=gpt-5.4-mini
```

`README.md`:
- Line 121-123: after "…a Claude Pro/Max subscription works via
  `MARIM_PROVIDER=claude-cli` (delegates turns to the `claude` CLI)." add
  ", and a ChatGPT/Codex subscription via `MARIM_PROVIDER=codex-cli`
  (delegates turns to `codex app-server`, with approvals brokered through
  marim's own panel)."
- Line 201 table row: `| \`MARIM_PROVIDER\` | \`openrouter\` (default), \`google\`, \`local\`, \`claude-cli\`, \`codex-cli\`, \`zen\`, or \`zen-go\` |`.

`CLAUDE.md`:
- In the "Set `MARIM_DEBUG=1`…" paragraph, the provider list becomes
  ``(`openrouter`|`local`|`google`|`claude-cli`|`codex-cli`|`zen`|`zen-go`)``, and
  after the claude-cli sentences add: "`codex-cli` delegates each turn to
  `codex app-server` (JSON-RPC over stdio, one process per marim session,
  `codex/` package): Codex runs its own tools in its own sandbox, but its
  approval and user-input requests are brokered back through marim's
  approval panel / ask-user flow (`codex/approvals.py`), so `auto`/`ask`/`plan`
  keep their meaning. The thread id persists on the session
  (`SessionStore.cli_thread_id`) and resumes via `thread/resume`."
- In the `subagents/` bullet, after `cli_spawn.py`: "`codex_spawn.py`
  (`backend: codex-cli` spawns: one Codex thread per spawn on the shared
  app-server, read-only sandbox unless the agent has a mutating tool, native
  `outputSchema`)".

`docs/reference/configuration.md`:
- Row 64 (`MARIM_PROVIDER`): add `codex-cli` to the value list.
- Rows 72-74: Task 1 and Task 11 already added the three `MARIM_CODEX_CLI_*`
  rows; verify they sit together after the claude-cli rows.
- Lines 82-84 defaults paragraph: extend "…and *unset* for `claude-cli` (the
  CLI uses its own configured default)" with "and for `codex-cli` (the Codex
  CLI's configured default model)".
- After the claude-cli paragraph (107-115) add:

  ```markdown
  Under the `codex-cli` provider marim delegates each turn to `codex app-server`
  (the Codex CLI's JSON-RPC front end), one long-lived process per marim
  session. Codex runs its own tools inside its own sandbox, so marim's tools,
  LSP and MCP servers do not apply — but unlike `claude-cli`, Codex *asks*
  before privileged actions and marim answers: approvals go through the same
  approval panel native tools use. The modes map as follows.

  | marim mode | Codex `approvalPolicy` | Codex sandbox |
  |---|---|---|
  | `auto` | `on-request` | workspace-write (the workspace root; the scratchpad is always writable) |
  | `ask` | `untrusted` (every command/edit is brokered to the approval panel; scratchpad-only edits auto-accept) | workspace-write |
  | `plan` | `never` | read-only |

  Requires `codex login` (marim never handles the OpenAI credentials) and
  `codex >= 0.152`. `/think` levels map to Codex reasoning effort
  (`minimal`/`low` → low, `medium`, `high`, `xhigh` when the model lists it);
  `/steer` forwards to the running turn; `/compact` also compacts the Codex
  thread. The thread id is saved with the session, so `--resume` continues the
  same Codex thread; if Codex no longer has it, a fresh thread starts from the
  saved history.
  ```

`docs/guides/subagents.md`:
- Row 130 (`backend`): "`native` (default, in-process Pydantic AI loop),
  `claude-cli`, or `codex-cli`."
- Row 131 (`model`): add "For `codex-cli`, a Codex model id (falls back to
  `MARIM_CODEX_CLI_MODEL`, then the CLI's default)."
- After the "## The claude-cli backend" section (before "## Limits and
  operations") add:

  ```markdown
  ## The codex-cli backend

  `backend: codex-cli` runs the agent as one thread on the shared
  `codex app-server` process (the same one the `codex-cli` main-loop provider
  uses; it is started on first use and closed with the session). The agent's
  prompt becomes the thread's developer instructions; the task is the first
  turn.

  - **Reach is fixed up front.** The Codex sandbox is `read-only` unless the
    agent's tools include `write_file`, `edit_file` or `bash` (and never in
    plan mode). The approval policy follows the parent's mode: `auto` →
    `on-request`, `ask` → `untrusted` (prompts land in *your* approval panel,
    labelled with the agent name), `plan` → `never`.
  - **Structured output** (`output_schema=` on `spawn_agent`) is enforced by
    Codex natively, for any schema root — no prompt contract is appended.
  - **Thinking** follows the usual precedence (spawn `thinking=` → spec
    `thinking:` → the session level) and maps to Codex reasoning effort.
  - **Resume** reopens the persisted Codex thread (`codex_thread_id` in the
    spawn's sidecar) and sends the continuation prompt as a new turn.
  - MCP grants are not forwarded (configure servers in Codex's own config); a
    non-empty grant list is noted in the output, as with `claude-cli`.

  See [`docs/examples/agents/codex-worker.md`](../examples/agents/codex-worker.md).
  ```

`docs/guides/tui.md:124-125`: after the claude-cli sentence add "Under
`codex-cli`, Codex runs its own tools but its approval requests are brokered
into this same panel (with the Codex command or file diff), so `ask` mode
still gates every privileged action and `plan` mode is read-only."

`docs/guides/sessions.md:60-62`: extend the titler sentence: "…or, under the
`claude-cli`/`codex-cli` providers, on an ephemeral read-only clone…", and add
one sentence to the resume paragraph: "A `codex-cli` session also stores its
Codex thread id and resumes that thread; if Codex has forgotten it, the turn
starts a fresh thread seeded with the saved history."

`docs/guides/headless.md`: after "## The claude-cli provider" add a sibling
section:

```markdown
## The codex-cli provider

`MARIM_PROVIDER=codex-cli` works headless the same way: each turn is one Codex
turn on a per-session thread, and the answer is the agent's final message.
Codex's tool activity is folded into the output as `▸ tool …` lines (there is
no card UI to send it to). Approvals: in `auto` mode Codex acts within its
workspace-write sandbox without prompting; in `ask` mode there is nobody to
ask, so every brokered request is **declined** (use `auto` or `plan` for
unattended runs); `plan` mode is read-only. Structured output (`--output-schema`)
is enforced by Codex natively.
```

`CHANGELOG.md` under `## [Unreleased]`:

```markdown
### Added
- `codex-cli` provider: `MARIM_PROVIDER=codex-cli` delegates each turn to
  `codex app-server` (JSON-RPC over stdio) on a ChatGPT/Codex subscription,
  with approvals brokered through marim's own panel, thinking levels mapped
  to reasoning effort, `/steer` and `/compact` forwarded to the thread, and
  the thread id persisted with the session for resume.
- `backend: codex-cli` sub-agents: one Codex thread per spawn on the shared
  app-server, read-only sandbox unless the agent has a mutating tool, native
  `outputSchema` enforcement, resumable via the persisted thread id.
- Settings > Providers shows a `codex-cli` card (binary + `codex login`
  detection) and the model picker lists Codex models from `model/list`.
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest --no-cov tests/test_agent_backend_field.py tests/test_docs_reference.py -v`
Expected: all PASS — every `MARIM_CODEX_*` name in `src/` appears in
`configuration.md`, the new relative link resolves, and the example parses.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src tests && uv run ruff format src tests
git add .env.example README.md CLAUDE.md CHANGELOG.md docs/reference/configuration.md \
        docs/guides/subagents.md docs/guides/tui.md docs/guides/sessions.md docs/guides/headless.md \
        docs/examples/agents/codex-worker.md tests/test_agent_backend_field.py
git commit -m "docs: codex-cli provider and sub-agent backend

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Env-guarded live smoke + full gate

A real-`codex` smoke that never runs in CI or by default (spec §Testing:
"one live smoke, gated"). It needs the user's Codex subscription, so **ask
before running it** (it costs their quota) — the file itself is committed
either way, skipped unless `MARIM_LIVE_CODEX=1`.

**Files:**
- Create: `tests/test_codex_live.py`

**Interfaces:**
- Consumes: `CodexCliModel` (Task 8); `SubagentRunner` via `_make_harness` (Task 11); `Mode`.
- Produces: nothing new.

- [ ] **Step 1: Write the live smoke**

`tests/test_codex_live.py`:

```python
"""Live smoke against a real `codex app-server` — OFF unless MARIM_LIVE_CODEX=1.

Uses the developer's own Codex login (CODEX_HOME defaults back to ~/.codex
here; the conftest isolation points it at nothing for every other test).
Four probes, each one short turn: a main-loop reply, effort + thread reuse,
an ask-mode file change that must be declined, and a codex-cli sub-agent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.settings import ModelSettings

from marim_harness.codex.server import close_shared_server
from marim_harness.config.codex_cli_model import CodexCliModel
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps, _make_harness

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("MARIM_LIVE_CODEX") != "1", reason="set MARIM_LIVE_CODEX=1"),
]

PARAMS = ModelRequestParameters(function_tools=[], allow_text_output=True, output_tools=[])


@pytest.fixture(autouse=True)
async def _real_codex_home(monkeypatch):
    home = os.environ.get("MARIM_LIVE_CODEX_HOME") or str(Path.home() / ".codex")
    monkeypatch.setenv("CODEX_HOME", home)
    monkeypatch.delenv("MARIM_CODEX_CLI_BIN", raising=False)
    yield
    await close_shared_server()


def _msgs(text: str) -> list:
    return [
        ModelRequest(
            parts=[
                SystemPromptPart(content="Answer in one short line. No tools unless asked."),
                UserPromptPart(content=text),
            ]
        )
    ]


async def test_main_loop_turn_and_thread_reuse(tmp_path: Path):
    m = CodexCliModel(None)  # the CLI's default model
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: Mode.plan.value
    resp = await m.request(_msgs("Reply with exactly the word: pong"), None, PARAMS)
    assert "pong" in resp.parts[0].content.lower()
    assert resp.usage.output_tokens > 0
    first_thread = m.thread
    resp2 = await m.request(
        _msgs("What word did you just reply with? One word."),
        ModelSettings(thinking="low"),  # type: ignore[typeddict-item]
        PARAMS,
    )
    assert "pong" in resp2.parts[0].content.lower()  # same thread: it remembers
    assert m.thread is first_thread


async def test_ask_mode_file_change_is_brokered_and_declined(tmp_path: Path):
    m = CodexCliModel(None)
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: Mode.ask.value
    asked: list[str] = []

    async def decline(call):
        asked.append(call.tool_name)
        return False

    m.request_approval = decline
    await m.request(
        _msgs("Create a file named probe.txt in the current directory containing 'hi'. "
              "If you are not allowed, say so."),
        None,
        PARAMS,
    )
    assert asked, "Codex never asked before writing — check approvalPolicy=untrusted"
    assert not (tmp_path / "probe.txt").exists()


async def test_codex_cli_subagent_reports(tmp_path: Path):
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True)
    (d / "codex-worker.md").write_text(
        "---\ndescription: w\nbackend: codex-cli\ntools: read_file\n---\n"
        "Answer in one short line.\n"
    )

    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    runner = _make_harness(FunctionModel(fn), _make_deps(tmp_path)).subagents
    out = await runner.run("codex-worker", "Reply with exactly the word: pong", stream_id="live1")
    assert "pong" in out.lower()
    assert runner._transcripts.read_meta("live1")["codex_thread_id"]
```

Note: `tests/test_codex_live.py` needs `tmp_path` to be trusted for the
project-local agent — add `"test_codex_live.py"` to `_TRUST_PROJECT_SUITES`
in `tests/conftest.py`.

- [ ] **Step 2: Verify it is skipped by default**

Run: `uv run pytest --no-cov tests/test_codex_live.py -v`
Expected: 3 skipped, 0 failed.

- [ ] **Step 3: Ask the user, then run live (only with their OK)**

One message: "Task 13's live smoke runs 4 short turns on your Codex
subscription (`MARIM_LIVE_CODEX=1`). OK to run now, or skip?" If approved:

```bash
MARIM_LIVE_CODEX=1 uv run pytest --no-cov -n 0 tests/test_codex_live.py -v
```

Expected: 3 passed. If `test_ask_mode_file_change_is_brokered_and_declined`
fails on `asked` being empty, Codex answered without attempting the write
(models sometimes refuse up front) — rerun once; if it still fails, the
`untrusted` policy isn't reaching `thread/start` — compare against Task 0's
probe log. If skipped by the user, say so in the final report.

- [ ] **Step 4: Full CI order locally**

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright && uv run pytest
```

Expected: all green (the live file skips). Then the quality gate
(`docs/quality-gate.md`): coverage must not drop below the baseline — the new
modules ship with their own suites, so it should rise.

- [ ] **Step 5: Commit**

```bash
git add tests/test_codex_live.py tests/conftest.py
git commit -m "test(codex): env-guarded live smoke (MARIM_LIVE_CODEX=1)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Follow-ups (not in this plan)

- Gitea #109: give claude-cli the same session-persisted thread ref and
  `steer`/`compact_remote` seams now that `ExternalCliModel` exists.
- `account/rateLimits/read` polling for a status-bar quota hint (spec
  §Supervisor lists it as best-effort; nothing in the TUI consumes it yet).
- Live catalog refresh in the Settings > Providers card (today the picker
  calls `model/list` on open; the card only detects the binary).
