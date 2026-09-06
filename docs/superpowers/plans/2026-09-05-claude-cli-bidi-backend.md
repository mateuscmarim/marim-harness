# claude-cli Bidirectional Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-shot `claude -p --permission-mode …` launch with one long-lived bidirectional `claude` process per conversation over `stream-json` stdin/stdout, so marim's approval `Mode`, approval panel, `ask_user`, steer and interrupt all apply to the claude-cli main-loop provider and the `backend: claude-cli` sub-agent backend (closes Gitea #109).

**Architecture:** A new `src/marim_harness/claude/` package mirrors `codex/`: `protocol.py` (newline-JSON framing + `control_request`/`control_response` correlation), `process.py` (`ClaudeProcess`: spawn/resume, `TurnHandle` queues, stderr tail, reaper, idle timer), `approvals.py` (`classify` + `ClaudeApprovalBroker` answering `can_use_tool`), `env.py` (binary/timeouts). The approval policy table codex already encodes moves into `runtime/permissions.py` as `decide_external`, shared by both brokers. `config/claude_cli_model.py` keeps its chunk pipeline but reads a turn queue instead of a process's whole stdout; `subagents/cli_spawn.py` runs a spawn as one turn on its own `ClaudeProcess` with a labelled broker.

**Tech Stack:** Python ≥3.10, asyncio subprocess, Pydantic AI `Model`/`StreamedResponse`, Claude Code ≥2.1 stream-json control protocol (`--input-format stream-json --output-format stream-json --permission-prompt-tool stdio`), pytest + anyio, a scripted fake `claude` under `tests/fakes/`.

**Spec:** `docs/superpowers/specs/2026-09-05-claude-cli-bidi-backend-design.md` — read it first; every wire shape below was verified by probe against Claude Code 2.1.261 and is recorded in the spec's *Background* section.

## Global Constraints

- Run everything with `uv run …`; never bare `python`/`pytest`/`pip`. CI order is `uv run ruff check src tests` → `uv run ruff format --check src tests` → `uv run pyright` → `uv run pytest`, on Python 3.10, 3.12, 3.14 (`uv run --python 3.12 pytest …` for other legs).
- `requires-python >= 3.10`: no `match`, no `except*`, no `Self`, no `X | Y` in runtime `isinstance`; `from __future__ import annotations` in every new module.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity ≤ 10 per function — extract helpers, **never** add `# noqa: C901`.
- The ratchet quality gate: coverage must not drop and src complexity violations must stay ≤ baseline. **Never edit `quality-baseline.json`, `quality-gate.toml`, or `.gitleaks.toml`.**
- Every `MARIM_[A-Z_]+` string in `src/` must be documented in `docs/reference/configuration.md` (`tests/test_docs_reference.py::test_every_env_var_is_documented`). The one new variable is `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` (default `600`, `0` = never).
- Import ndjson from `marim_harness.ndjson` — never via `subagents.cli_backend` (cycles).
- Key every turn on the `TurnHandle` the send call returned, never on mutable process state — the loaded 3.10 CI leg orders a turn's completion before the send call returns.
- The live smoke (`tests/test_claude_cli_live.py`, gated on `MARIM_LIVE_CLAUDE=1`) spends subscription quota: write it, **never run it** without the user's explicit OK.
- `stream-json` output requires `--verbose`. `--bare` is never used (breaks subscription auth). `--permission-mode` is never passed (the CLI stays in `default` mode; marim's `Mode` is the only policy source).
- Commits end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Preserve the long "why" comments around resumability and the deps/services cycle when editing nearby code.

---

## File structure

**Create**

| Path | Responsibility |
|---|---|
| `src/marim_harness/claude/__init__.py` | Package marker (docstring only; re-exports nothing, like `codex/__init__.py`). |
| `src/marim_harness/claude/env.py` | Env constants, `resolve_cli_binary`, `cli_timeout`, `cli_idle_timeout`, `CliUnavailable`, `MIN_CLAUDE_VERSION`, `INSTALL_HINT`. Pure lookups. |
| `src/marim_harness/claude/protocol.py` | `StreamJsonClient`: framing + control correlation. Knows only the envelope. |
| `src/marim_harness/claude/process.py` | `ProcessOptions`, `build_process_argv`, `child_env`, `TurnHandle`, `ClaudeProcess`, `turn_objects`. Process lifecycle + turn routing. |
| `src/marim_harness/claude/approvals.py` | `ToolRequest`, `classify`, `ClaudeApprovalBroker`, deny/allow reply builders. |
| `tests/fakes/fake_claude.py` | Scripted stand-in for `claude` speaking the verified stream-json vocabulary. |
| `tests/test_claude_env.py`, `tests/test_claude_protocol.py`, `tests/test_claude_fake.py`, `tests/test_claude_process.py`, `tests/test_claude_approvals.py`, `tests/test_claude_cli_live.py` | New unit/integration/live tests. |

**Modify**

| Path | Change |
|---|---|
| `src/marim_harness/runtime/permissions.py` | Add `Decision`, `UiSeams`, `ExternalRequest`, `decide_external`, `_within`. |
| `src/marim_harness/codex/approvals.py` | `decide` becomes an adapter over `decide_external`; `Decision`/`UiSeams` re-exported from permissions. |
| `src/marim_harness/config/external_cli.py` | `aclose()` on the base (no-op); seam comment update. |
| `src/marim_harness/config/claude_cli_model.py` | Bidi `ClaudeCliModel`; `ThinkingChunk`/`InitChunk`; `consume_cli_stream` over a turn queue; delete `_MODE_MAP`, `permission_mode_for`, `note_ask_limitation_once`, `spawn_cli_objects`. |
| `src/marim_harness/subagents/cli_backend.py` | Delete `build_cli_argv`, `cli_permission_mode`, `_RunState`, `_read_next_line`, `_POST_LOOP_GRACE`, `_cli_timeout`, `_kill_process_group`; `ClaudeCliRunner.run` becomes one bidi turn; env names re-exported from `claude.env`. |
| `src/marim_harness/subagents/cli_spawn.py` | `run_cli` builds a labelled `ClaudeApprovalBroker`; `resume` unchanged in shape. |
| `src/marim_harness/runtime/harness.py` | `aclose` + `set_model` close any `ExternalCliModel`; `wire_cli_model` docstring. |
| `src/marim_harness/config/model.py`, `src/marim_harness/interfaces/tui/settings.py` | Import `resolve_cli_binary` from `claude.env`. |
| `src/marim_harness/config/env.py` | Blocklist `MARIM_CLAUDE_CLI_IDLE_TIMEOUT`. |
| `tests/test_claude_cli_model.py`, `tests/test_cli_backend.py`, `tests/test_subagents_cli.py`, `tests/test_subagent_cli_spawn.py`, `tests/test_subagent_plan_egress.py`, `tests/test_bootstrap.py`, `tests/test_env_blocklist.py`, `tests/fakes/__init__.py` | Migrate to the new seams / fake. |
| `docs/reference/configuration.md`, `docs/guides/headless.md`, `docs/guides/subagents.md`, `docs/guides/tui.md`, `docs/guides/sessions.md`, `docs/sdk/subagents.md`, `docs/README.md`, `docs/examples/agents/cli-worker.md`, `README.md`, `.env.example`, `CHANGELOG.md`, `CLAUDE.md` | Documentation. |

---

### Task 1: `claude/env.py` — env constants, timeouts, blocklist, docs row

**Files:**
- Create: `src/marim_harness/claude/__init__.py`, `src/marim_harness/claude/env.py`
- Modify: `src/marim_harness/subagents/cli_backend.py` (top: constants, `_cli_timeout`, `resolve_cli_binary`, `CliUnavailable`), `src/marim_harness/config/env.py:76-99` (blocklist), `src/marim_harness/config/model.py:447-449`, `src/marim_harness/interfaces/tui/settings.py:35,226`, `docs/reference/configuration.md` (blocklist line 32 and the env table rows near 72-74)
- Test: `tests/test_claude_env.py`, `tests/test_env_blocklist.py`

**Interfaces:**
- Produces (in `marim_harness.claude.env`):
  ```python
  CLI_BINARY_ENV = "MARIM_CLAUDE_CLI_BIN"
  CLI_MODEL_ENV = "MARIM_CLAUDE_CLI_MODEL"
  CLI_TIMEOUT_ENV = "MARIM_CLAUDE_CLI_TIMEOUT"
  CLI_IDLE_TIMEOUT_ENV = "MARIM_CLAUDE_CLI_IDLE_TIMEOUT"
  DEFAULT_CLI_TIMEOUT = 600.0
  DEFAULT_IDLE_TIMEOUT = 600.0
  MIN_CLAUDE_VERSION = "2.1"
  INSTALL_HINT: str
  class CliUnavailable(Exception)
  def resolve_cli_binary() -> str | None
  def cli_timeout() -> float          # silence bound; garbage/<=0 -> 600
  def cli_idle_timeout() -> float     # 0 -> 0.0 (never); garbage/<0 -> 600
  ```
- `subagents.cli_backend` keeps re-exporting `CLI_BINARY_ENV`, `CLI_MODEL_ENV`, `CLI_TIMEOUT_ENV`, `CliUnavailable`, `resolve_cli_binary`, plus the legacy names `_DEFAULT_CLI_TIMEOUT` and `_cli_timeout` (tests import them).

- [ ] **Step 1: Write the failing tests**

`tests/test_claude_env.py`:

```python
"""claude/env.py: pure lookups for the Claude Code binary and timeouts."""

from __future__ import annotations

import sys

from marim_harness.claude import env


def test_resolve_binary_prefers_env_override(monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", sys.executable)
    assert env.resolve_cli_binary() == sys.executable


def test_resolve_binary_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", str(tmp_path / "nope"))
    assert env.resolve_cli_binary() is None


def test_cli_timeout_defaults_and_rejects_garbage(monkeypatch):
    monkeypatch.delenv("MARIM_CLAUDE_CLI_TIMEOUT", raising=False)
    assert env.cli_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "abc")
    assert env.cli_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0")
    assert env.cli_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "12.5")
    assert env.cli_timeout() == 12.5


def test_idle_timeout_zero_means_never(monkeypatch):
    monkeypatch.delenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", raising=False)
    assert env.cli_idle_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "0")
    assert env.cli_idle_timeout() == 0.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "-3")
    assert env.cli_idle_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "garbage")
    assert env.cli_idle_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "30")
    assert env.cli_idle_timeout() == 30.0


def test_legacy_names_still_importable_from_cli_backend():
    from marim_harness.subagents import cli_backend

    assert cli_backend.CLI_BINARY_ENV == "MARIM_CLAUDE_CLI_BIN"
    assert cli_backend.resolve_cli_binary is env.resolve_cli_binary
    assert cli_backend._cli_timeout is env.cli_timeout
    assert cli_backend._DEFAULT_CLI_TIMEOUT == 600.0
```

Append to `tests/test_env_blocklist.py`, right after `test_project_env_cannot_weaken_cli_timeout` (it uses the same `isolated_env` fixture, `_setup` helper and `load_environment` import that test already uses):

```python
def test_project_env_cannot_change_cli_idle_timeout(isolated_env, monkeypatch, tmp_path):
    # The idle reaper is a machine-level knob: a project .env must not be able
    # to pin an idle `claude` open forever (0) or reap it under every turn.
    _setup(tmp_path, monkeypatch, "MARIM_CLAUDE_CLI_IDLE_TIMEOUT=0\n")
    monkeypatch.delenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", raising=False)

    load_environment()

    assert "MARIM_CLAUDE_CLI_IDLE_TIMEOUT" not in os.environ
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_claude_env.py tests/test_env_blocklist.py -q`
Expected: `ModuleNotFoundError: No module named 'marim_harness.claude'` and the blocklist test failing because the variable leaks in.

- [ ] **Step 3: Create the package and `env.py`**

`src/marim_harness/claude/__init__.py`:

```python
"""Claude Code (``claude``) as a bidirectional stream-json backend.

Submodules only — import ``marim_harness.claude.process`` etc. directly; the
package root deliberately re-exports nothing (same rule as ``codex/``).
"""
```

`src/marim_harness/claude/env.py`:

```python
"""Environment probes for the Claude Code CLI: binary and timeouts.

Pure lookups (``shutil.which`` and env reads) so the settings screen and
provider detection can call these freely; nothing here spawns a process.
"""

from __future__ import annotations

import os
import shutil

CLI_BINARY_ENV = "MARIM_CLAUDE_CLI_BIN"
# Default model for `backend: claude-cli` sub-agents (the spec's `model:` wins;
# the spawn call's model= wins over both). Unset ⇒ the CLI's own default.
CLI_MODEL_ENV = "MARIM_CLAUDE_CLI_MODEL"
# Silence bound for one open turn (seconds between stream objects, excluding
# time an approval prompt is open in the panel).
CLI_TIMEOUT_ENV = "MARIM_CLAUDE_CLI_TIMEOUT"
# How long a main-loop `claude` may sit with no open turn before marim closes
# it (the next turn resumes the session by id). `0` = never reap.
CLI_IDLE_TIMEOUT_ENV = "MARIM_CLAUDE_CLI_IDLE_TIMEOUT"
DEFAULT_CLI_TIMEOUT = 600.0
DEFAULT_IDLE_TIMEOUT = 600.0
# `--permission-prompt-tool stdio` + `--input-format stream-json` landed in the
# 2.1 line; older binaries reject the argv and exit before `system/init`.
MIN_CLAUDE_VERSION = "2.1"
INSTALL_HINT = (
    "Install Claude Code (npm i -g @anthropic-ai/claude-code) and sign in; "
    f"marim needs claude >= {MIN_CLAUDE_VERSION}. Set MARIM_CLAUDE_CLI_BIN to "
    "point at a specific binary."
)


class CliUnavailable(Exception):
    """The Claude Code CLI is missing (or too old to speak stream-json control)."""


def resolve_cli_binary() -> str | None:
    """The Claude Code executable to spawn: ``$MARIM_CLAUDE_CLI_BIN`` if set,
    else ``claude`` on PATH. Absolute path, or None when nothing is found so
    the caller reports a clean error instead of crashing."""
    return shutil.which(os.environ.get(CLI_BINARY_ENV) or "claude")


def _positive_float(raw: str, default: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def cli_timeout() -> float:
    """Silence timeout (seconds) for one open turn; ``MARIM_CLAUDE_CLI_TIMEOUT``
    or 600. Garbage and non-positive values fall back to the default."""
    return _positive_float(os.environ.get(CLI_TIMEOUT_ENV, ""), DEFAULT_CLI_TIMEOUT)


def cli_idle_timeout() -> float:
    """Idle-reaper delay (seconds); ``MARIM_CLAUDE_CLI_IDLE_TIMEOUT`` or 600.
    ``0`` disables reaping (returns 0.0); negatives and garbage fall back."""
    raw = os.environ.get(CLI_IDLE_TIMEOUT_ENV, "")
    if raw.strip() == "0":
        return 0.0
    return _positive_float(raw, DEFAULT_IDLE_TIMEOUT)
```

- [ ] **Step 4: Re-point `cli_backend.py`, `config/model.py`, `settings.py`, and the blocklist**

In `src/marim_harness/subagents/cli_backend.py` delete the module-level `CLI_BINARY_ENV`, `CLI_MODEL_ENV`, `CLI_TIMEOUT_ENV`, `_DEFAULT_CLI_TIMEOUT`, `_cli_timeout`, `resolve_cli_binary`, and `class CliUnavailable` definitions and replace them with one import block right after the existing imports:

```python
from ..claude.env import (  # re-exported: spawn code and tests import these here
    CLI_BINARY_ENV,
    CLI_MODEL_ENV,
    CLI_TIMEOUT_ENV,
    DEFAULT_CLI_TIMEOUT as _DEFAULT_CLI_TIMEOUT,
    CliUnavailable,
    cli_timeout as _cli_timeout,
    resolve_cli_binary,
)
```

Keep `CliUnavailable` out of the deleted set if it has a docstring you'd lose — the new one in `env.py` carries it. Remove the now-unused `shutil` import (ruff F401 will tell you).

In `src/marim_harness/config/model.py` `_claude_cli_available` (line ~447), change the lazy import to `from ..claude.env import resolve_cli_binary`. In `src/marim_harness/interfaces/tui/settings.py` (lines 35 and 226) change the import to `from ...claude.env import resolve_cli_binary`.

In `src/marim_harness/config/env.py`, in the blocklist right after the `"MARIM_CLAUDE_CLI_TIMEOUT"` entry, add:

```python
    # The idle reaper for a main-loop `claude` process: a project must not be
    # able to pin a ~200 MB node process open forever (0) or reap it under
    # every turn (tiny value) — machine-level, like the silence timeout.
    "MARIM_CLAUDE_CLI_IDLE_TIMEOUT",
```

- [ ] **Step 5: Document the variable**

In `docs/reference/configuration.md`: add `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` to the blocklist sentence on line ~32 (next to `MARIM_CLAUDE_CLI_TIMEOUT`), and add a row to the env table next to the `MARIM_CLAUDE_CLI_TIMEOUT` row:

```markdown
| `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` | `600` | Seconds a main-loop `claude` process may sit with no turn open before marim closes it; the next turn resumes the same Claude session by id. `0` disables reaping. Spawns and aux clones never idle (their process closes at run end). |
```

Also reword the `MARIM_CLAUDE_CLI_TIMEOUT` row's description to: "Seconds of silence (no stream object) allowed inside one open claude-cli turn, excluding time an approval prompt is waiting in the panel; on expiry marim interrupts, waits 2 s, then kills the process."

- [ ] **Step 6: Run the tests, lint, docs test**

Run: `uv run pytest --no-cov tests/test_claude_env.py tests/test_env_blocklist.py tests/test_docs_reference.py tests/test_cli_backend.py tests/test_subagents_cli.py -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`
Expected: all pass (the existing claude-cli tests still run against the old runner; only names moved).

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/claude src/marim_harness/subagents/cli_backend.py src/marim_harness/config/env.py src/marim_harness/config/model.py src/marim_harness/interfaces/tui/settings.py docs/reference/configuration.md tests/test_claude_env.py tests/test_env_blocklist.py
git commit -m "feat(claude): env module with idle-timeout knob; re-export legacy names from cli_backend

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Shared policy core in `runtime/permissions.py`

**Files:**
- Modify: `src/marim_harness/runtime/permissions.py` (add after the `Mode` enum), `src/marim_harness/codex/approvals.py` (`Decision`, `UiSeams`, `_within`, `decide`)
- Test: `tests/test_permissions_external.py`; `tests/test_codex_approvals.py` must stay green **unchanged**

**Interfaces:**
- Produces (in `marim_harness.runtime.permissions`):
  ```python
  @dataclass(frozen=True)
  class Decision:
      accept: bool
      reason: str = ""
      ask: bool = False   # True: caller must prompt; accept is the headless default

  @dataclass(frozen=True)
  class UiSeams:
      request_approval: Callable[[Any], Awaitable[Any]] | None
      ask_user: Callable[[list[Question]], Awaitable[dict | None]] | None

  @dataclass(frozen=True)
  class ExternalRequest:
      mutating: bool
      paths: tuple[Path, ...] = ()

  PLAN_READ_ONLY = "plan mode: read-only"
  def within_root(path: Path, root: Path | None) -> bool
  def decide_external(mode: Mode, req: ExternalRequest, workspace_root: Path | None, scratchpad: Path | None) -> Decision
  ```
- `marim_harness.codex.approvals` re-exports `Decision` and `UiSeams` (its tests import them from there) and its `decide(mode, method, params, workspace_root, scratchpad)` keeps its exact signature and results.

- [ ] **Step 1: Write the failing tests**

`tests/test_permissions_external.py`:

```python
"""decide_external: the transport-neutral approval table shared by the
codex-cli and claude-cli brokers (spec §Shared policy core)."""

from __future__ import annotations

from pathlib import Path

from marim_harness.runtime.permissions import (
    PLAN_READ_ONLY,
    Decision,
    ExternalRequest,
    Mode,
    decide_external,
    within_root,
)


def _req(mutating: bool, *paths: Path) -> ExternalRequest:
    return ExternalRequest(mutating=mutating, paths=tuple(paths))


def test_plan_accepts_reads_and_denies_every_mutation(tmp_path: Path):
    inside = tmp_path / "a.txt"
    assert decide_external(Mode.plan, _req(False), tmp_path, None) == Decision(accept=True)
    for req in (_req(True, inside), _req(True, tmp_path.parent / "x"), _req(True)):
        d = decide_external(Mode.plan, req, tmp_path, None)
        assert d == Decision(accept=False, reason=PLAN_READ_ONLY)
        assert not d.ask


def test_auto_accepts_inside_and_unknown_paths_but_asks_outside(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    pad = tmp_path / "pad"
    pad.mkdir()
    assert decide_external(Mode.auto, _req(True, root / "f"), root, pad) == Decision(accept=True)
    assert decide_external(Mode.auto, _req(True, pad / "f"), root, pad) == Decision(accept=True)
    assert decide_external(Mode.auto, _req(True), root, pad) == Decision(accept=True)
    stray = tmp_path / "elsewhere" / "f"
    d = decide_external(Mode.auto, _req(True, root / "ok", stray), root, pad)
    assert d.ask and not d.accept
    assert d.reason == f"outside workspace: {stray}"


def test_ask_accepts_reads_and_scratchpad_only_writes_else_asks(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    pad = tmp_path / "pad"
    pad.mkdir()
    assert decide_external(Mode.ask, _req(False), root, pad) == Decision(accept=True)
    d = decide_external(Mode.ask, _req(True, pad / "notes.md"), root, pad)
    assert d == Decision(accept=True, reason="scratchpad write")
    assert decide_external(Mode.ask, _req(True, root / "f"), root, pad).ask
    assert decide_external(Mode.ask, _req(True, pad / "f", root / "g"), root, pad).ask
    assert decide_external(Mode.ask, _req(True), root, pad).ask
    # No scratchpad bound: a mutating request always prompts.
    assert decide_external(Mode.ask, _req(True, pad / "f"), root, None).ask


def test_within_root_rejects_dotdot_and_symlink_escape(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    assert within_root(root / "sub" / "f.txt", root)
    assert not within_root(root / ".." / "outside" / "f", root)
    assert not within_root(root / "link" / "f", root)
    assert not within_root(root / "f", None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_permissions_external.py -q`
Expected: `ImportError: cannot import name 'ExternalRequest'`.

- [ ] **Step 3: Add the core to `runtime/permissions.py`**

Add `from dataclasses import dataclass`, `from pathlib import Path`, `from collections.abc import Awaitable, Callable` and `from typing import TYPE_CHECKING, Any` to the imports (keep any lazy-import note intact — nothing added here imports pydantic_ai). Under `if TYPE_CHECKING:` add `from ..ask_user import Question`. Then, directly after the `Mode` enum:

```python
@dataclass(frozen=True)
class Decision:
    """The policy answer for one external-CLI request, before any prompting."""

    accept: bool
    reason: str = ""
    ask: bool = False  # True: the caller must prompt (accept is the headless default)


@dataclass(frozen=True)
class UiSeams:
    """The two UI callbacks an external-CLI broker needs to reach the human —
    grouped so a broker's constructor reads as mode/paths, then UI."""

    request_approval: Callable[[Any], Awaitable[Any]] | None
    ask_user: Callable[[list[Question]], Awaitable[dict | None]] | None


@dataclass(frozen=True)
class ExternalRequest:
    """A transport-neutral view of one thing an external CLI (codex-cli,
    claude-cli) wants to do: would it change files / run commands / reach the
    network, and which file paths does it name (when known)."""

    mutating: bool
    paths: tuple[Path, ...] = ()


PLAN_READ_ONLY = "plan mode: read-only"


def within_root(path: Path, root: Path | None) -> bool:
    """True when ``path`` resolves to somewhere under ``root`` (symlinks and
    ``..`` resolved first, so an escape through either is caught)."""
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def decide_external(
    mode: Mode,
    req: ExternalRequest,
    workspace_root: Path | None,
    scratchpad: Path | None,
) -> Decision:
    """The one approval table both external-CLI brokers apply (spec §Shared
    policy core). ``ask`` means "prompt if a request_approval seam is bound";
    with no seam the caller accepts (the headless default codex spawns rely on).

    plan  -> non-mutating accepted; every mutation denied, never prompts.
    auto  -> accepted, except a mutation naming a path outside the workspace
             root AND outside the scratchpad, which is escalated to a prompt.
    ask   -> non-mutating accepted; a mutation whose paths all sit inside the
             scratchpad is accepted (mirrors ``_scratchpad_approval`` for
             native tools); everything else prompts.
    """
    if not req.mutating:
        return Decision(accept=True)
    if mode is Mode.plan:
        return Decision(accept=False, reason=PLAN_READ_ONLY)
    if mode is Mode.auto:
        stray = [
            p
            for p in req.paths
            if not within_root(p, workspace_root) and not within_root(p, scratchpad)
        ]
        if stray:
            return Decision(accept=False, reason=f"outside workspace: {stray[0]}", ask=True)
        return Decision(accept=True)
    if req.paths and scratchpad is not None and all(within_root(p, scratchpad) for p in req.paths):
        return Decision(accept=True, reason="scratchpad write")
    return Decision(accept=False, ask=True)
```

- [ ] **Step 4: Make `codex/approvals.decide` an adapter**

In `src/marim_harness/codex/approvals.py`: delete the local `Decision` and `UiSeams` dataclasses and `_within`; change the permissions import to `from ..runtime.permissions import Decision, ExternalRequest, Mode, UiSeams, decide_external`. Ruff (F401) would flag `Decision`/`UiSeams` as unused, so add near the top:

```python
__all__ = [
    "ApprovalBroker",
    "Decision",
    "UiSeams",
    "decide",
    "policy_for",
    "sandbox_for",
    "sandbox_mode_for",
]
```

Replace the body of `decide` with:

```python
def decide(
    mode: Mode,
    method: str,
    params: dict,
    workspace_root: Path | None,
    scratchpad: Path | None,
) -> Decision:
    """Codex's approval requests, shaped for the shared table: every
    ``requestApproval`` is a mutation (Codex never asks for reads), and a
    fileChange names its paths so auto/ask can apply the workspace and
    scratchpad rules. See ``runtime.permissions.decide_external`` for the
    per-mode table."""
    paths = _change_paths(params) if method == "item/fileChange/requestApproval" else []
    req = ExternalRequest(mutating=True, paths=tuple(paths))
    return decide_external(mode, req, workspace_root, scratchpad)
```

Keep `_change_paths`. The plan-mode behaviour is unchanged for codex: `decide` was only ever called for approval methods, all of which are mutations, so `Decision(accept=False, reason="plan mode: read-only")` still comes back and `test_decide_plan_declines_everything` passes as written.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest --no-cov tests/test_permissions_external.py tests/test_codex_approvals.py tests/test_codex_cli_model.py -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`
Expected: all pass, no lint findings.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/permissions.py src/marim_harness/codex/approvals.py tests/test_permissions_external.py
git commit -m "refactor(permissions): decide_external shared policy core; codex decide becomes an adapter

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `claude/protocol.py` — `StreamJsonClient`

**Files:**
- Create: `src/marim_harness/claude/protocol.py`
- Test: `tests/test_claude_protocol.py`

**Interfaces:**
- Consumes: `marim_harness.ndjson.iter_ndjson_lines(stream)` (async generator of decoded `str` lines, no 64 KiB cap).
- Produces:
  ```python
  CLOSED = "__closed__"                      # pseudo-event type published on EOF
  class ControlError(Exception)              # the CLI answered a control_request with subtype "error"
  class ProcessClosed(Exception)             # stdout hit EOF / stdin broke while talking to the CLI

  RequestHandler = Callable[[str, dict], Awaitable[dict]]   # (request_id, request) -> response body

  class StreamJsonClient:
      def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                   on_event: Callable[[dict], None],
                   on_request: RequestHandler | None = None,
                   on_cancel: Callable[[str], None] | None = None) -> None
      closed: asyncio.Event
      prompts_open: int                     # can_use_tool requests currently being answered
      async def run(self) -> None           # reader loop; returns on EOF
      async def write(self, obj: dict) -> None
      async def user(self, text: str) -> None
      async def control(self, subtype: str, *, timeout: float | None = None, **fields) -> dict
      async def respond(self, request_id: str, response: dict) -> None
      async def respond_error(self, request_id: str, message: str) -> None
      async def aclose(self) -> None        # cancel handler tasks, fail pending futures
  ```
- Wire shapes (spec §Background, verified by the probes): host→CLI `{"type":"control_request","request_id":R,"request":{"subtype":S,…}}`; CLI→host `{"type":"control_response","response":{"subtype":"success"|"error","request_id":R,"response":{…}|"error":"…"}}`; CLI→host request `{"type":"control_request","request_id":R,"request":{"subtype":"can_use_tool",…}}` answered with `{"type":"control_response","response":{"subtype":"success","request_id":R,"response":BODY}}`; cancel `{"type":"control_cancel_request","request_id":R}`; user input `{"type":"user","message":{"role":"user","content":[{"type":"text","text":T}]}}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_claude_protocol.py`:

```python
"""StreamJsonClient: newline-JSON framing and control_request correlation
over an in-memory pipe (no subprocess)."""

from __future__ import annotations

import asyncio
import json

import pytest

from marim_harness.claude.protocol import (
    CLOSED,
    ControlError,
    ProcessClosed,
    StreamJsonClient,
)

pytestmark = pytest.mark.anyio


class _Writer:
    """Stands in for the subprocess stdin StreamWriter; records parsed objects."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.sent.append(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


class _Pipe:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = _Writer()

    @property
    def sent(self) -> list[dict]:
        return self.writer.sent

    def feed(self, obj: dict) -> None:
        self.reader.feed_data((json.dumps(obj) + "\n").encode())

    def eof(self) -> None:
        self.reader.feed_eof()


def _client(pipe: _Pipe, **kw) -> tuple[StreamJsonClient, list[dict]]:
    events: list[dict] = []
    client = StreamJsonClient(
        pipe.reader,
        pipe.writer,  # type: ignore[arg-type]
        on_event=events.append,
        **kw,
    )
    return client, events


def _ok(rid: str, body: dict) -> dict:
    return {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": rid, "response": body},
    }


async def test_control_correlates_by_request_id_and_returns_body():
    pipe = _Pipe()
    client, _ = _client(pipe)
    reader = asyncio.create_task(client.run())
    task = asyncio.create_task(client.control("initialize", hooks={}))
    await asyncio.sleep(0)
    sent = pipe.sent[-1]
    assert sent["type"] == "control_request"
    assert sent["request"] == {"subtype": "initialize", "hooks": {}}
    rid = sent["request_id"]
    # An unrelated response id first: must be ignored, not mis-correlated.
    pipe.feed(_ok("other", {}))
    pipe.feed(_ok(rid, {"pid": 7}))
    assert await asyncio.wait_for(task, 1) == {"pid": 7}
    pipe.eof()
    await reader


async def test_control_error_subtype_raises_control_error():
    pipe = _Pipe()
    client, _ = _client(pipe)
    reader = asyncio.create_task(client.run())
    task = asyncio.create_task(client.control("set_model", model="x"))
    await asyncio.sleep(0)
    rid = pipe.sent[-1]["request_id"]
    pipe.feed(
        {
            "type": "control_response",
            "response": {"subtype": "error", "request_id": rid, "error": "nope"},
        }
    )
    with pytest.raises(ControlError, match="nope"):
        await asyncio.wait_for(task, 1)
    pipe.eof()
    await reader


async def test_eof_fails_pending_control_and_publishes_closed():
    pipe = _Pipe()
    client, events = _client(pipe)
    reader = asyncio.create_task(client.run())
    task = asyncio.create_task(client.control("interrupt"))
    await asyncio.sleep(0)
    pipe.eof()
    with pytest.raises(ProcessClosed):
        await asyncio.wait_for(task, 1)
    await reader
    assert client.closed.is_set()
    assert events[-1]["type"] == CLOSED


async def test_control_after_close_raises_immediately():
    pipe = _Pipe()
    client, _ = _client(pipe)
    pipe.eof()
    await client.run()
    with pytest.raises(ProcessClosed):
        await client.control("interrupt")


async def test_events_are_routed_to_on_event_in_order():
    pipe = _Pipe()
    client, events = _client(pipe)
    reader = asyncio.create_task(client.run())
    pipe.feed({"type": "system", "subtype": "init", "session_id": "s1"})
    pipe.reader.feed_data(b"not json at all\n")  # noise on stdout is skipped
    pipe.feed({"type": "result", "subtype": "success"})
    pipe.eof()
    await reader
    assert [e["type"] for e in events] == ["system", "result", CLOSED]


async def test_request_handler_runs_concurrently_and_answer_is_written():
    pipe = _Pipe()
    gate = asyncio.Event()

    async def handler(rid: str, request: dict) -> dict:
        assert request["subtype"] == "can_use_tool"
        await gate.wait()
        return {"behavior": "allow", "updatedInput": request["input"]}

    client, events = _client(pipe, on_request=handler)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {
            "type": "control_request",
            "request_id": "r1",
            "request": {"subtype": "can_use_tool", "tool_name": "Write", "input": {"a": 1}},
        }
    )
    await asyncio.sleep(0.01)
    assert client.prompts_open == 1
    # The reader is NOT blocked behind the handler: a plain event still lands.
    pipe.feed({"type": "assistant", "message": {"content": []}})
    await asyncio.sleep(0.01)
    assert events[-1]["type"] == "assistant"
    gate.set()
    await asyncio.sleep(0.01)
    assert client.prompts_open == 0
    assert pipe.sent[-1] == _ok("r1", {"behavior": "allow", "updatedInput": {"a": 1}})
    pipe.eof()
    await reader


async def test_cancel_request_cancels_handler_and_calls_on_cancel():
    pipe = _Pipe()
    cancelled: list[str] = []
    started = asyncio.Event()

    async def handler(rid: str, request: dict) -> dict:
        started.set()
        await asyncio.sleep(10)
        return {"behavior": "allow", "updatedInput": {}}

    client, _ = _client(pipe, on_request=handler, on_cancel=cancelled.append)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {
            "type": "control_request",
            "request_id": "r9",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
        }
    )
    await asyncio.wait_for(started.wait(), 1)
    before = len(pipe.sent)
    pipe.feed({"type": "control_cancel_request", "request_id": "r9"})
    await asyncio.sleep(0.01)
    assert cancelled == ["r9"]
    assert client.prompts_open == 0
    assert len(pipe.sent) == before  # a cancelled prompt writes no answer
    pipe.eof()
    await reader


async def test_handler_exception_answers_with_error_response():
    pipe = _Pipe()

    async def handler(rid: str, request: dict) -> dict:
        raise RuntimeError("boom")

    client, _ = _client(pipe, on_request=handler)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {"type": "control_request", "request_id": "r2", "request": {"subtype": "can_use_tool"}}
    )
    await asyncio.sleep(0.01)
    assert pipe.sent[-1]["response"] == {"subtype": "error", "request_id": "r2", "error": "boom"}
    pipe.eof()
    await reader


async def test_no_handler_bound_answers_with_error_response():
    pipe = _Pipe()
    client, _ = _client(pipe)
    reader = asyncio.create_task(client.run())
    pipe.feed(
        {"type": "control_request", "request_id": "r3", "request": {"subtype": "can_use_tool"}}
    )
    await asyncio.sleep(0.01)
    assert pipe.sent[-1]["response"]["subtype"] == "error"
    assert pipe.sent[-1]["response"]["request_id"] == "r3"
    pipe.eof()
    await reader


async def test_user_writes_text_block_message():
    pipe = _Pipe()
    client, _ = _client(pipe)
    await client.user("hi there")
    assert pipe.sent[-1] == {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi there"}]},
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_claude_protocol.py -q`
Expected: `ModuleNotFoundError: No module named 'marim_harness.claude.protocol'`.

- [ ] **Step 3: Implement `protocol.py`**

```python
"""Claude Code's stream-json wire, host side: framing and control correlation.

One JSON object per line in each direction. Outbound ``control_request``s are
keyed by a host-minted ``request_id`` and answered by a ``control_response``
carrying the same id; inbound ``control_request``s (``can_use_tool``) are
answered the same way in reverse. Everything else on stdout (``system``,
``stream_event``, ``assistant``, ``user``, ``result``) is an *event* and goes
to ``on_event`` in arrival order. This module knows only the envelope — what a
``can_use_tool`` means is ``approvals.py``'s business, what a ``result`` means
is ``process.py``'s.

Request handlers run as their own tasks so the reader loop never blocks
behind an approval prompt: the ``control_cancel_request`` for that prompt
(the CLI sends one ahead of an interrupt's aborted ``result``) must still be
read while the prompt is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from ..ndjson import iter_ndjson_lines

logger = logging.getLogger(__name__)

CLOSED = "__closed__"
_CONTROL_TIMEOUT = 30.0

RequestHandler = Callable[[str, dict], Awaitable[dict]]


class ControlError(Exception):
    """The CLI answered a control_request with ``subtype: "error"``."""


class ProcessClosed(Exception):
    """stdout reached EOF (or stdin broke) while talking to the CLI."""


class StreamJsonClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_event: Callable[[dict], None],
        on_request: RequestHandler | None = None,
        on_cancel: Callable[[str], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_event = on_event
        self._on_request = on_request
        self._on_cancel = on_cancel
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._handlers: dict[str, asyncio.Task[None]] = {}
        self._write_lock = asyncio.Lock()
        self.closed = asyncio.Event()
        # can_use_tool requests currently awaiting an answer. The turn's
        # silence clock (process.turn_objects) pauses while this is > 0: a
        # human deciding in the panel is not the CLI being silent.
        self.prompts_open = 0

    # --- outbound -------------------------------------------------------------
    async def write(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        async with self._write_lock:
            if self._writer.is_closing():
                raise ProcessClosed("claude stdin is closed")
            self._writer.write(data)
            try:
                await self._writer.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ProcessClosed(str(exc)) from exc

    async def user(self, text: str) -> None:
        await self.write(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )

    async def control(self, subtype: str, *, timeout: float | None = None, **fields) -> dict:
        """Send one control_request and await its body. Raises ControlError
        on an ``error`` reply, ProcessClosed on EOF, ``asyncio.TimeoutError``
        after ``timeout`` (default 30 s)."""
        if self.closed.is_set():
            raise ProcessClosed("claude process is closed")
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            await self.write(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": subtype, **fields},
                }
            )
            return await asyncio.wait_for(fut, timeout or _CONTROL_TIMEOUT)
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, request_id: str, response: dict) -> None:
        await self.write(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": response},
            }
        )

    async def respond_error(self, request_id: str, message: str) -> None:
        await self.write(
            {
                "type": "control_response",
                "response": {"subtype": "error", "request_id": request_id, "error": message},
            }
        )

    # --- inbound --------------------------------------------------------------
    async def run(self) -> None:
        """Read stdout until EOF, routing each object. Always ends by failing
        pending control futures and publishing the CLOSED pseudo-event."""
        try:
            async for raw in iter_ndjson_lines(self._reader):
                obj = _parse(raw)
                if obj is not None:
                    self._route(obj)
        finally:
            self._fail_pending("claude exited")
            self._on_event({"type": CLOSED})

    def _fail_pending(self, why: str) -> None:
        self.closed.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ProcessClosed(why))
        self._pending.clear()

    def _route(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "control_response":
            self._on_control_response(obj.get("response") or {})
        elif kind == "control_request":
            self._on_control_request(str(obj.get("request_id", "")), obj.get("request") or {})
        elif kind == "control_cancel_request":
            self._on_cancel_request(str(obj.get("request_id", "")))
        else:
            self._on_event(obj)

    def _on_control_response(self, response: dict) -> None:
        fut = self._pending.get(str(response.get("request_id", "")))
        if fut is None or fut.done():
            return
        if response.get("subtype") == "error":
            fut.set_exception(ControlError(str(response.get("error") or "control request failed")))
        else:
            fut.set_result(response.get("response") or {})

    def _on_control_request(self, request_id: str, request: dict) -> None:
        task = asyncio.create_task(self._serve(request_id, request))
        self._handlers[request_id] = task
        task.add_done_callback(lambda _t: self._handlers.pop(request_id, None))

    def _on_cancel_request(self, request_id: str) -> None:
        task = self._handlers.get(request_id)
        if task is not None and not task.done():
            task.cancel()
        if self._on_cancel is not None:
            self._on_cancel(request_id)

    async def _serve(self, request_id: str, request: dict) -> None:
        """One inbound control_request as its own task. A cancelled handler
        (control_cancel_request, or aclose) writes nothing — the CLI has
        already moved on."""
        self.prompts_open += 1
        try:
            with contextlib.suppress(ProcessClosed):
                await self._answer(request_id, request)
        finally:
            self.prompts_open -= 1

    async def _answer(self, request_id: str, request: dict) -> None:
        if self._on_request is None:
            await self.respond_error(request_id, "no request handler bound")
            return
        try:
            body = await self._on_request(request_id, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the CLI must always get an answer
            logger.warning("claude control_request %s handler failed: %s", request_id, exc)
            await self.respond_error(request_id, str(exc))
            return
        await self.respond(request_id, body)

    async def aclose(self) -> None:
        for task in list(self._handlers.values()):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers.values(), return_exceptions=True)
        self._handlers.clear()
        self._fail_pending("client closed")


def _parse(raw: str) -> dict | None:
    line = raw.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        logger.debug("claude stdout noise: %.200s", line)
        return None
    return obj if isinstance(obj, dict) else None
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_claude_protocol.py -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright`
Expected: 10 passed, no lint/type findings. (`BLE001` is not in the enabled lint set; if ruff reports an unused `noqa`, drop the comment.)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/claude/protocol.py tests/test_claude_protocol.py
git commit -m "feat(claude): StreamJsonClient — stream-json framing and control correlation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

---

### Task 4: Scenario-driven fake `claude` (`tests/fakes/fake_claude.py`)

**Files:**
- Create: `tests/fakes/fake_claude.py`
- Modify: `tests/fakes/__init__.py` (append three helpers)
- Test: `tests/test_claude_fake.py`

**Interfaces:**
- Consumes: nothing from `src/` — the fake is a standalone script that speaks
  the stream-json control protocol the probes verified (spec §Protocol).
- Produces: `tests.fakes.fake_claude_bin(tmp_path: Path, scenario: dict) -> str`
  (path of an executable wrapper; set it as `MARIM_CLAUDE_CLI_BIN` or pass it as
  `binary=`), `tests.fakes.read_claude_argvs(tmp_path) -> list[list[str]]` (one
  argv per launch, in order), `tests.fakes.read_claude_argv(tmp_path) -> list[str]`
  (the last launch), `tests.fakes.read_claude_log(tmp_path) -> list[dict]` (every
  stdin line the fake read, all launches). Later tasks (5, 7, 9) drive every
  process-level test through this fake — no more ad-hoc `#!python` scripts that
  never read stdin (they would stall `initialize`).

Scenario schema (JSON, written by `fake_claude_bin`):

```
{"session_id": "S1", "model": "claude-sonnet-4-6", "version": "2.1.261",
 "known_sessions": ["S1"],          # --resume ids that exist; others exit 1
 "turns": [[step, ...], ...]}       # turn i runs turns[min(i, len(turns)-1)]
```

Steps (exactly one key each):

| step | behavior |
|---|---|
| `{"text": "Hello"}` | `stream_event` text deltas in 3-char chunks, then an `assistant` message with the text block |
| `{"thinking": "hmm"}` | one `thinking_delta` stream_event, then an `assistant` message with a thinking block |
| `{"tool_use": {"name": "Read", "input": {...}, "id": "tu1"}}` | an `assistant` message with a tool_use block (no prompt) |
| `{"tool_result": {"id": "tu1", "content": "...", "is_error": false}}` | a `user` message with a tool_result block |
| `{"can_use_tool": {"tool_name": "Write", "input": {...}, "tool_use_id": "tu1", "requires_user_interaction": false}}` | emits the tool_use, then a `control_request` `can_use_tool`; blocks until the host answers. allow → tool_result `ok` and text `Write done` (for `updatedInput.answers` the text is `answers=<sorted json>`); deny → `is_error` tool_result carrying the message, text `denied: <message>`, and the denial recorded in `result.permission_denials` |
| `{"await_user": true}` | blocks until a non-replay `user` message arrives, echoes it with `isReplay`, emits text `heard: <its text>` |
| `{"await_interrupt": true}` | blocks until an `interrupt` control_request; answers it, emits an aborted `result` (`is_error`, `terminal_reason: aborted_streaming`), ends the turn |
| `{"sleep": 30}` | blocks without servicing stdin (the hung-CLI scenario) |
| `{"exit": {"code": 3, "stderr": "boom"}}` | writes stderr and exits — no `result` |
| `{"raw": {...}}` | emits the object verbatim |
| `{"result": {...}}` | merged over the auto `result` (e.g. `{"result": {"usage": {...}}}`) |

Every turn ends with a `success` result unless `exit`/`await_interrupt` ended it.
An `interrupt` that arrives while a `can_use_tool` prompt is open produces a
`control_cancel_request` for that request first (as the real CLI does), then the
aborted result. `system/init` is emitted once, right after the first user message
(the real CLI's order). `--resume <id>` with an unknown id writes
`No conversation found with session ID: <id>` to stderr and exits 1 before
reading stdin — the exact failure the model's one-shot retry (Task 7) detects.

- [ ] **Step 1: Write the fake**

`tests/fakes/fake_claude.py`:

```python
"""Scenario-driven fake ``claude`` for tests (bidirectional stream-json).

Speaks the subset of Claude Code's stream-json control protocol that
``marim_harness.claude.process.ClaudeProcess`` uses: answers ``initialize`` /
``interrupt`` / any other control_request with an empty success, echoes user
messages with ``isReplay``, emits ``system/init`` after the first user message,
and runs one scripted turn per user message. Nothing here spawns anything —
every emitted object is scripted by the scenario JSON (see the table in the
plan / ``tests.fakes.fake_claude_bin``).

Env: ``MARIM_CLAUDE_FAKE_SCENARIO`` (scenario JSON path, required) and
``MARIM_CLAUDE_FAKE_LOG`` (every stdin line is appended here; argv is appended
as one JSON line to ``<log>.argv`` per launch).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _user_text(msg: dict) -> str:
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = [c.get("text", "") for c in content or [] if isinstance(c, dict)]
    return "".join(parts)


class Fake:
    def __init__(self, scenario: dict, log_path: str | None) -> None:
        self.scenario = scenario
        self.log_path = log_path
        self.session_id = scenario.get("session_id", "S1")
        self.model = scenario.get("model", "claude-sonnet-4-6")
        self.version = scenario.get("version", "2.1.261")
        self.turn_n = 0
        self.init_sent = False
        self.ended = False  # the current turn was ended by a step (exit/abort)
        self.denials: list[dict] = []
        self._request_n = 0

    # --- wire ----------------------------------------------------------------
    def send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def record(self, line: str) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line if line.endswith("\n") else line + "\n")

    def read(self) -> dict | None:
        line = sys.stdin.readline()
        if not line:
            return None
        self.record(line)
        try:
            obj = json.loads(line)
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def respond(self, request_id: str, body: dict) -> None:
        self.send(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": body},
            }
        )

    def _assistant(self, content: list[dict]) -> dict:
        return {
            "type": "assistant",
            "message": {"role": "assistant", "model": self.model, "content": content},
            "session_id": self.session_id,
        }

    def _tool_result(self, tool_use_id: str, content: str, is_error: bool) -> dict:
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error:
            block["is_error"] = True
        return {
            "type": "user",
            "message": {"role": "user", "content": [block]},
            "session_id": self.session_id,
        }

    def _delta(self, delta: dict) -> dict:
        return {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0, "delta": delta},
            "session_id": self.session_id,
        }

    def _result(self, text: str, overrides: dict) -> dict:
        base = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": self.session_id,
            "num_turns": 1,
            "duration_ms": 5,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": 7,
                "output_tokens": 4,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "permission_denials": list(self.denials),
        }
        base.update(overrides)
        return base

    def _aborted_result(self) -> dict:
        return {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "terminal_reason": "aborted_streaming",
            "session_id": self.session_id,
            "num_turns": 1,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "permission_denials": [],
        }

    # --- main loop -----------------------------------------------------------
    def serve(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                return
            kind = msg.get("type")
            if kind == "control_request":
                # Between turns every control request (initialize, set_model,
                # a late interrupt) gets an empty success — nothing to abort.
                self.respond(msg["request_id"], {})
            elif kind == "user" and not msg.get("isReplay"):
                self.run_turn(msg)

    def run_turn(self, msg: dict) -> None:
        self.send({**msg, "isReplay": True})
        if not self.init_sent:
            self.init_sent = True
            self.send(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": self.session_id,
                    "model": self.model,
                    "claude_code_version": self.version,
                    "tools": ["Read", "Write", "Edit", "Bash"],
                    "cwd": os.getcwd(),
                }
            )
        turns = self.scenario.get("turns") or [[{"text": "ok"}]]
        script = turns[min(self.turn_n, len(turns) - 1)]
        self.turn_n += 1
        self.ended = False
        self.denials = []
        overrides: dict = {}
        text_parts: list[str] = []
        for step in script:
            if self.ended:
                break
            self.step(step, text_parts, overrides)
        if not self.ended:
            self.send(self._result("".join(text_parts), overrides))

    # --- steps ---------------------------------------------------------------
    def step(self, step: dict, text_parts: list[str], overrides: dict) -> None:
        if "text" in step:
            self.emit_text(step["text"])
            text_parts.append(step["text"])
        elif "thinking" in step:
            self.send(self._delta({"type": "thinking_delta", "thinking": step["thinking"]}))
            self.send(self._assistant([{"type": "thinking", "thinking": step["thinking"]}]))
        elif "tool_use" in step:
            t = step["tool_use"]
            self.send(
                self._assistant(
                    [{"type": "tool_use", "id": t["id"], "name": t["name"], "input": t.get("input", {})}]
                )
            )
        elif "tool_result" in step:
            r = step["tool_result"]
            self.send(self._tool_result(r["id"], r.get("content", ""), bool(r.get("is_error"))))
        elif "can_use_tool" in step:
            self.prompt(step["can_use_tool"], text_parts)
        elif "await_user" in step:
            self.await_user(text_parts)
        elif "await_interrupt" in step:
            self.await_interrupt()
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
        elif "exit" in step:
            e = step["exit"]
            sys.stdout.flush()
            sys.stderr.write(str(e.get("stderr", "")) + "\n")
            sys.stderr.flush()
            sys.exit(int(e.get("code", 1)))
        elif "raw" in step:
            self.send(step["raw"])
        elif "result" in step:
            overrides.update(step["result"])
        else:
            raise SystemExit(f"fake_claude: unknown step {step!r}")

    def emit_text(self, text: str) -> None:
        for i in range(0, len(text), 3):
            self.send(self._delta({"type": "text_delta", "text": text[i : i + 3]}))
        self.send(self._assistant([{"type": "text", "text": text}]))

    def prompt(self, spec: dict, text_parts: list[str]) -> None:
        self._request_n += 1
        rid = f"req-{self._request_n}"
        tool_name = spec["tool_name"]
        tool_input = spec.get("input", {})
        tid = spec.get("tool_use_id", "tu1")
        request = {
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "input": tool_input,
            "tool_use_id": tid,
            "permission_suggestions": [],
        }
        if spec.get("requires_user_interaction"):
            request["requires_user_interaction"] = True
        self.send(self._assistant([{"type": "tool_use", "id": tid, "name": tool_name, "input": tool_input}]))
        self.send({"type": "control_request", "request_id": rid, "request": request})
        reply = self.await_response(rid)
        if reply is None:
            return  # interrupted (aborted result already sent) or EOF
        if reply.get("behavior") == "allow":
            answers = (reply.get("updatedInput") or {}).get("answers")
            if answers is None:
                self.send(self._tool_result(tid, "ok", False))
                text = f"{tool_name} done"
            else:
                text = "answers=" + json.dumps(answers, sort_keys=True)
                self.send(self._tool_result(tid, text, False))
        else:
            message = str(reply.get("message") or "denied")
            self.send(self._tool_result(tid, message, True))
            self.denials.append({"tool_name": tool_name, "tool_use_id": tid, "tool_input": tool_input})
            text = f"denied: {message}"
        self.emit_text(text)
        text_parts.append(text)

    def await_response(self, rid: str) -> dict | None:
        while True:
            msg = self.read()
            if msg is None:
                self.ended = True
                return None
            if msg.get("type") == "control_response":
                resp = msg.get("response") or {}
                if resp.get("request_id") != rid:
                    continue
                if resp.get("subtype") == "error":
                    return {"behavior": "deny", "message": resp.get("error") or "error"}
                return resp.get("response") or {}
            if msg.get("type") == "control_request":
                self.handle_control(msg, pending=rid)
                if self.ended:
                    return None

    def await_user(self, text_parts: list[str]) -> None:
        while True:
            msg = self.read()
            if msg is None:
                self.ended = True
                return
            if msg.get("type") == "user" and not msg.get("isReplay"):
                self.send({**msg, "isReplay": True})
                text = "heard: " + _user_text(msg)
                self.emit_text(text)
                text_parts.append(text)
                return
            if msg.get("type") == "control_request":
                self.handle_control(msg, pending=None)
                if self.ended:
                    return

    def await_interrupt(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                self.ended = True
                return
            if msg.get("type") == "control_request":
                self.handle_control(msg, pending=None)
                if self.ended:
                    return

    def handle_control(self, msg: dict, pending: str | None) -> None:
        subtype = (msg.get("request") or {}).get("subtype")
        if subtype != "interrupt":
            self.respond(msg["request_id"], {})
            return
        if pending is not None:
            self.send({"type": "control_cancel_request", "request_id": pending})
        self.respond(msg["request_id"], {})
        self.send(self._aborted_result())
        self.ended = True


def main(argv: list[str]) -> int:
    scenario = json.loads(Path(os.environ["MARIM_CLAUDE_FAKE_SCENARIO"]).read_text(encoding="utf-8"))
    log = os.environ.get("MARIM_CLAUDE_FAKE_LOG")
    if log:
        with open(log + ".argv", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    if "--resume" in argv:
        rid = argv[argv.index("--resume") + 1]
        if rid not in (scenario.get("known_sessions") or []):
            sys.stderr.write(f"No conversation found with session ID: {rid}\n")
            sys.stderr.flush()
            return 1
        scenario["session_id"] = rid
    Fake(scenario, log).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 2: Append the helpers to `tests/fakes/__init__.py`**

```python
_FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


def fake_claude_bin(tmp_path: Path, scenario: dict) -> str:
    """Write ``scenario`` and an executable ``claude`` wrapper into ``tmp_path``;
    return the wrapper path (set it as ``MARIM_CLAUDE_CLI_BIN`` or pass it as
    ``binary=``). One scenario file serves every launch from that ``tmp_path``
    (a resume-retry launches twice)."""
    scenario_path = tmp_path / "claude-scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    log_path = tmp_path / "claude-requests.jsonl"
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'MARIM_CLAUDE_FAKE_SCENARIO="{scenario_path}" '
        f'MARIM_CLAUDE_FAKE_LOG="{log_path}" '
        f'exec "{sys.executable}" "{_FAKE_CLAUDE}" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def read_claude_argvs(tmp_path: Path) -> list[list[str]]:
    """Every argv the fake ``claude`` was launched with, oldest first."""
    path = tmp_path / "claude-requests.jsonl.argv"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_claude_argv(tmp_path: Path) -> list[str]:
    """The most recent launch's argv (``[]`` when it never launched)."""
    argvs = read_claude_argvs(tmp_path)
    return argvs[-1] if argvs else []


def read_claude_log(tmp_path: Path) -> list[dict]:
    """Every stdin line the fake read, across launches."""
    path = tmp_path / "claude-requests.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
```

- [ ] **Step 3: Write the smoke test**

`tests/test_claude_fake.py`:

```python
"""The fake ``claude`` speaks the stream-json protocol the real one does —
driven raw here (no marim code) so a broken fake fails this file, not the
process tests that depend on it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.fakes import fake_claude_bin, read_claude_argvs, read_claude_log


async def _drive(binary: str, cwd: Path, *lines: dict, timeout: float = 5.0) -> tuple[list[dict], int, str]:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--output-format",
        "stream-json",
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    for line in lines:
        proc.stdin.write((json.dumps(line) + "\n").encode())
    await proc.stdin.drain()
    proc.stdin.close()
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    objs = [json.loads(raw) for raw in out.decode().splitlines() if raw.strip()]
    return objs, proc.returncode or 0, err.decode()


def _init() -> dict:
    return {"type": "control_request", "request_id": "r1", "request": {"subtype": "initialize", "hooks": {}}}


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


@pytest.mark.anyio
async def test_text_turn_emits_init_deltas_assistant_and_result(tmp_path: Path):
    binary = fake_claude_bin(tmp_path, {"session_id": "S7", "turns": [[{"text": "Hello"}]]})
    objs, code, _ = await _drive(binary, tmp_path, _init(), _user("hi"))
    assert code == 0
    kinds = [o["type"] for o in objs]
    assert kinds[0] == "control_response"
    assert objs[0]["response"] == {"subtype": "success", "request_id": "r1", "response": {}}
    assert objs[1]["type"] == "user" and objs[1]["isReplay"] is True
    init = objs[2]
    assert init["type"] == "system" and init["subtype"] == "init" and init["session_id"] == "S7"
    deltas = "".join(
        o["event"]["delta"]["text"] for o in objs if o["type"] == "stream_event"
    )
    assert deltas == "Hello"
    assert objs[-2]["type"] == "assistant"
    assert objs[-1]["type"] == "result" and objs[-1]["result"] == "Hello"
    assert read_claude_argvs(tmp_path) == [["--output-format", "stream-json"]]
    assert [m["type"] for m in read_claude_log(tmp_path)] == ["control_request", "user"]


@pytest.mark.anyio
async def test_can_use_tool_blocks_until_answered_and_records_denial(tmp_path: Path):
    step = {"can_use_tool": {"tool_name": "Write", "input": {"file_path": "x"}, "tool_use_id": "tu9"}}
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    deny = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "req-1",
            "response": {"behavior": "deny", "message": "plan mode: read-only"},
        },
    }
    objs, _, _ = await _drive(binary, tmp_path, _init(), _user("write"), deny)
    req = next(o for o in objs if o["type"] == "control_request")
    assert req["request_id"] == "req-1"
    assert req["request"]["tool_name"] == "Write" and req["request"]["tool_use_id"] == "tu9"
    tool_result = next(o for o in objs if o["type"] == "user" and not o.get("isReplay"))
    block = tool_result["message"]["content"][0]
    assert block["is_error"] is True and block["content"] == "plan mode: read-only"
    result = objs[-1]
    assert result["type"] == "result" and result["result"] == "denied: plan mode: read-only"
    assert result["permission_denials"][0]["tool_name"] == "Write"


@pytest.mark.anyio
async def test_exit_step_writes_stderr_and_no_result(tmp_path: Path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "a"}, {"exit": {"code": 3, "stderr": "boom"}}]]})
    objs, code, err = await _drive(binary, tmp_path, _init(), _user("go"))
    assert code == 3 and "boom" in err
    assert all(o["type"] != "result" for o in objs)


@pytest.mark.anyio
async def test_unknown_resume_id_fails_before_reading_stdin(tmp_path: Path):
    binary = fake_claude_bin(tmp_path, {"known_sessions": ["S1"], "turns": [[{"text": "a"}]]})
    proc = await asyncio.create_subprocess_exec(
        binary, "--resume", "NOPE", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await asyncio.wait_for(proc.communicate(), 5.0)
    assert proc.returncode == 1
    assert "No conversation found with session ID: NOPE" in err.decode()
    assert read_claude_log(tmp_path) == []
```

- [ ] **Step 4: Run the smoke test**

Run: `uv run pytest --no-cov tests/test_claude_fake.py -v`
Expected: 4 passed.

- [ ] **Step 5: Lint/format and commit**

```bash
uv run ruff check --fix tests/fakes tests/test_claude_fake.py && uv run ruff format tests/fakes tests/test_claude_fake.py
git add tests/fakes/fake_claude.py tests/fakes/__init__.py tests/test_claude_fake.py
git commit -m "test(claude): scenario-driven fake claude speaking the stream-json control protocol

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `claude/process.py` — one long-lived `claude` per conversation

**Files:**
- Create: `src/marim_harness/claude/process.py`
- Test: `tests/test_claude_process.py`

**Interfaces:**
- Consumes: `StreamJsonClient`, `CLOSED`, `ControlError`, `ProcessClosed`,
  `RequestHandler` from Task 3 (`claude/protocol.py`); `INSTALL_HINT` from Task 1;
  `CliModelError` from `config/external_cli.py`; `kill_process_tree` from
  `tools/impl/process.py`; `tests.fakes.fake_claude_bin` from Task 4.
- Produces (used by Tasks 7 and 9):
  - `ProcessOptions(binary: str, cwd: str, model: str | None = None, resume_id: str | None = None, tools: tuple[str, ...] = (), disallowed_tools: tuple[str, ...] = (), append_system: str | None = None, persist: bool = True, env: dict[str, str] | None = None)` — frozen dataclass.
  - `build_process_argv(options: ProcessOptions) -> list[str]` — pure.
  - `child_env(base: dict[str, str] | None = None) -> dict[str, str]` — pure.
  - `TurnHandle` — `events: asyncio.Queue[dict]`, `open: bool`, `closed: asyncio.Event`, `finish() -> None`.
  - `ClaudeProcess(options, *, on_request: RequestHandler | None = None, silence_timeout: float = 0.0, idle_timeout: float = 0.0)` with `async start()`, `async send_turn(text) -> TurnHandle`, `async send_user(text)`, `async interrupt(handle, grace=2.0)` (kills the process when the CLI ignores the interrupt), `async aclose()`, properties `alive: bool`, `turn_open: bool`, `pid: int | None`, `session_id: str | None`, `init_info: dict`, `last_stderr: str`, `prompts_open: int`, `closed: asyncio.Event`.
  - `next_turn_object(process, handle) -> dict` (coroutine; raises `CliModelError` on silence timeout after interrupting the turn) and `turn_objects(process, handle, first=None)` (async generator yielding objects until the terminal `result`/`CLOSED`; interrupts the turn when the consumer is cancelled).
  - A closed process delivers one synthetic terminal object
    `{"type": CLOSED, "stderr": <tail>, "returncode": <int|None>}` to the open turn.

Design notes the implementer must keep (spec §Process supervisor):
- `start()` sends `initialize` and waits for its answer OR the process closing,
  bounded by `_INIT_TIMEOUT` (30 s). It never fails on a missing answer — a fake
  or a CLI that answers late still gets its user message; the CLI serializes
  stdin, so the user message is ordered after `initialize` regardless.
- `system/init` is captured on every turn (session id + `init_info`) *before*
  it is queued to the turn, so a consumer that only wants the id can read
  `process.session_id` as soon as the object arrives.
- A `result` closes the current turn. Objects arriving with no open turn (a
  late async sub-agent notification, a stray replay) are logged at DEBUG and
  dropped — the CLI stays alive, so nothing is lost for the next turn.
- The silence timeout lives in `next_turn_object`, not in the queue: it is
  re-armed on every object and *suspended while a control_request handler is
  open* (`client.prompts_open > 0`), so a user staring at the approval panel
  for ten minutes never trips it.
- Idle reaper: after each `result` (only when `idle_timeout > 0`) a timer
  closes the process; the next turn respawns with `--resume <session_id>`
  (Task 7). `aclose()` must not cancel the idle task when it *is* the idle
  task (`asyncio.current_task()` check) or it cancels itself mid-close.
- `interrupt()` sends the control request and waits for the turn's aborted
  `result` under one 2 s grace (not sequentially — a silent CLI would cost
  two graces); a turn still open afterwards means the CLI is wedged, so the
  process is closed and the next turn resumes by id.
- Shutdown order: cancel handlers via `client.aclose()`, close stdin, SIGTERM
  the process group, wait `_TERM_GRACE` (2 s), then `kill_process_tree` — the
  CLI launches MCP servers in their own groups, so `killpg` alone can leak them.

- [ ] **Step 1: Write the failing tests**

`tests/test_claude_process.py`:

```python
"""``ClaudeProcess`` against the scenario fake: argv shape, turn routing,
silence/idle timeouts, interrupt, mid-turn user messages, death handling."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from marim_harness.claude.process import (
    CLOSED,
    ClaudeProcess,
    ProcessOptions,
    build_process_argv,
    child_env,
    next_turn_object,
    turn_objects,
)
from marim_harness.config.external_cli import CliModelError
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_log

pytestmark = pytest.mark.anyio


# --- pure helpers -----------------------------------------------------------


def test_argv_carries_isolation_and_stream_json_flags():
    argv = build_process_argv(ProcessOptions(binary="/bin/claude", cwd="/ws"))
    assert argv[0] == "/bin/claude"
    for flag in (
        "--strict-mcp-config",
        "--safe-mode",
        "--permission-prompt-tool",
        "--include-partial-messages",
        "--replay-user-messages",
        "--verbose",
    ):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "host"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # Never: the old one-shot launch flags, and --bare (breaks subscription auth).
    assert "-p" not in argv and "--permission-mode" not in argv and "--bare" not in argv
    assert "--model" not in argv and "--resume" not in argv
    assert "--append-system-prompt" not in argv and "--no-session-persistence" not in argv


def test_argv_optional_flags():
    argv = build_process_argv(
        ProcessOptions(
            binary="claude",
            cwd="/ws",
            model="opus",
            resume_id="S1",
            tools=("Read", "Write"),
            disallowed_tools=("Task", "Agent"),
            append_system="SYS",
            persist=False,
        )
    )
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--resume") + 1] == "S1"
    assert argv[argv.index("--tools") + 1] == "Read,Write"
    assert argv[argv.index("--disallowedTools") + 1] == "Task,Agent"
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert "--no-session-persistence" in argv


def test_child_env_strips_nested_claude_markers():
    base = {
        "PATH": "/bin",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SSE_PORT": "1234",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "HOME": "/h",
    }
    env = child_env(base)
    assert env == {"PATH": "/bin", "HOME": "/h"}
    assert child_env({"X": "1"}) == {"X": "1"}


# --- process ----------------------------------------------------------------


def _process(tmp_path: Path, scenario: dict, **kwargs) -> ClaudeProcess:
    binary = fake_claude_bin(tmp_path, scenario)
    return ClaudeProcess(ProcessOptions(binary=binary, cwd=str(tmp_path)), **kwargs)


async def _collect(process: ClaudeProcess, text: str) -> list[dict]:
    handle = await process.send_turn(text)
    return [obj async for obj in turn_objects(process, handle)]


async def test_turn_streams_until_result_and_captures_init(tmp_path: Path):
    process = _process(tmp_path, {"session_id": "S7", "version": "2.1.261", "turns": [[{"text": "Hello"}]]})
    await process.start()
    try:
        objs = await _collect(process, "hi")
    finally:
        await process.aclose()
    kinds = [o["type"] for o in objs]
    assert kinds[0] == "user" and objs[0]["isReplay"] is True
    assert kinds[1] == "system" and kinds[-1] == "result"
    assert "stream_event" in kinds and "assistant" in kinds
    assert objs[-1]["result"] == "Hello"
    assert process.session_id == "S7"
    assert process.init_info["claude_code_version"] == "2.1.261"
    argv = read_claude_argv(tmp_path)
    assert "--input-format" in argv and "--resume" not in argv
    sent = read_claude_log(tmp_path)
    assert sent[0]["request"]["subtype"] == "initialize"
    assert sent[1]["type"] == "user"
    assert sent[1]["message"]["content"] == [{"type": "text", "text": "hi"}]


async def test_second_turn_reuses_the_same_process(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "one"}], [{"text": "two"}]]})
    await process.start()
    try:
        pid = process.pid
        first = await _collect(process, "a")
        second = await _collect(process, "b")
    finally:
        await process.aclose()
    assert first[-1]["result"] == "one" and second[-1]["result"] == "two"
    assert process.pid is None and pid is not None  # closed now; was one pid throughout
    assert [o["type"] for o in second].count("system") == 0  # init only once per process
    assert len(read_claude_log(tmp_path)) == 3  # initialize + two user messages


async def test_death_mid_turn_delivers_closed_with_stderr(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"exit": {"code": 3, "stderr": "boom"}}]]})
    await process.start()
    try:
        objs = await _collect(process, "go")
    finally:
        await process.aclose()
    assert objs[-1]["type"] == CLOSED
    assert "boom" in objs[-1]["stderr"] and objs[-1]["returncode"] == 3
    assert process.alive is False
    assert "boom" in process.last_stderr


async def test_send_turn_on_dead_process_yields_closed(tmp_path: Path):
    binary = fake_claude_bin(tmp_path, {"known_sessions": ["S1"], "turns": [[{"text": "a"}]]})
    process = ClaudeProcess(ProcessOptions(binary=binary, cwd=str(tmp_path), resume_id="NOPE"))
    await process.start()  # the fake exits 1 before reading stdin
    try:
        handle = await process.send_turn("hi")
        first = await next_turn_object(process, handle)
    finally:
        await process.aclose()
    assert first["type"] == CLOSED
    assert "No conversation found with session ID: NOPE" in first["stderr"]
    assert handle.open is False


async def test_silence_timeout_interrupts_and_raises(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]}, silence_timeout=0.3)
    await process.start()
    start = time.monotonic()
    try:
        with pytest.raises(CliModelError) as exc:
            await _collect(process, "go")
    finally:
        await process.aclose()
    assert "timed out" in str(exc.value)
    assert time.monotonic() - start < 5


async def test_silence_clock_pauses_while_a_prompt_is_open(tmp_path: Path):
    step = {"can_use_tool": {"tool_name": "Write", "input": {"file_path": "x"}, "tool_use_id": "tu1"}}

    async def slow_allow(request_id: str, request: dict) -> dict:
        await asyncio.sleep(0.6)  # longer than the silence timeout below
        return {"behavior": "allow", "updatedInput": request["input"]}

    process = _process(tmp_path, {"turns": [[step]]}, on_request=slow_allow, silence_timeout=0.3)
    await process.start()
    try:
        objs = await _collect(process, "write it")
    finally:
        await process.aclose()
    assert objs[-1]["type"] == "result" and objs[-1]["result"] == "Write done"
    assert objs[-1]["permission_denials"] == []


async def test_request_handler_errors_become_error_responses(tmp_path: Path):
    step = {"can_use_tool": {"tool_name": "Write", "input": {}, "tool_use_id": "tu1"}}

    async def broken(request_id: str, request: dict) -> dict:
        raise RuntimeError("panel exploded")

    process = _process(tmp_path, {"turns": [[step]]}, on_request=broken)
    await process.start()
    try:
        objs = await _collect(process, "write it")
    finally:
        await process.aclose()
    # The fake treats an error response as a deny carrying the error text.
    assert objs[-1]["result"] == "denied: panel exploded"


async def test_interrupt_ends_turn_with_aborted_result(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"await_interrupt": True}]]})
    await process.start()
    try:
        handle = await process.send_turn("go")
        seen = []
        while True:
            obj = await next_turn_object(process, handle)
            seen.append(obj)
            if obj["type"] == "assistant":
                break
        await process.interrupt(handle)
        async for obj in turn_objects(process, handle):
            seen.append(obj)
    finally:
        await process.aclose()
    result = seen[-1]
    assert result["type"] == "result" and result["is_error"] is True
    assert result["terminal_reason"] == "aborted_streaming"
    assert handle.open is False
    assert any(m.get("request", {}).get("subtype") == "interrupt" for m in read_claude_log(tmp_path))


async def test_cancelled_consumer_interrupts_the_turn(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"await_interrupt": True}]]})
    await process.start()
    try:
        handle = await process.send_turn("go")

        async def consume():
            async for _obj in turn_objects(process, handle):
                pass

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(handle.closed.wait(), 3.0)
    finally:
        await process.aclose()
    assert any(m.get("request", {}).get("subtype") == "interrupt" for m in read_claude_log(tmp_path))


async def test_send_user_mid_turn_folds_into_the_live_turn(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"await_user": True}]]})
    await process.start()
    try:
        handle = await process.send_turn("go")
        while (await next_turn_object(process, handle))["type"] != "assistant":
            pass
        assert process.turn_open is True
        await process.send_user("go left")
        rest = [obj async for obj in turn_objects(process, handle)]
    finally:
        await process.aclose()
    assert rest[-1]["result"] == "aheard: go left"
    assert process.turn_open is False


async def test_idle_timeout_closes_process_between_turns(tmp_path: Path):
    process = _process(tmp_path, {"session_id": "S3", "turns": [[{"text": "a"}]]}, idle_timeout=0.2)
    await process.start()
    try:
        await _collect(process, "go")
        assert process.alive is True
        await asyncio.wait_for(process.closed.wait(), 3.0)
    finally:
        await process.aclose()
    assert process.alive is False
    assert process.session_id == "S3"  # kept for the next turn's --resume


async def test_idle_timer_is_disarmed_by_the_next_turn(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}]]}, idle_timeout=0.4)
    await process.start()
    try:
        await _collect(process, "one")
        await asyncio.sleep(0.25)
        await _collect(process, "two")  # re-arms: the old timer must not fire
        await asyncio.sleep(0.25)
        assert process.alive is True
    finally:
        await process.aclose()


async def test_aclose_kills_a_hung_child_promptly(tmp_path: Path):
    process = _process(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]})
    await process.start()
    handle = await process.send_turn("go")
    await next_turn_object(process, handle)
    pid = process.pid
    start = time.monotonic()
    await process.aclose()
    assert time.monotonic() - start < 4
    assert process.alive is False and process.closed.is_set()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # type: ignore[arg-type]
    assert handle.open is False and handle.events.get_nowait()["type"] == CLOSED


async def test_missing_binary_raises_cli_model_error(tmp_path: Path):
    process = ClaudeProcess(ProcessOptions(binary=str(tmp_path / "nope"), cwd=str(tmp_path)))
    with pytest.raises(CliModelError) as exc:
        await process.start()
    assert "claude" in str(exc.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_claude_process.py -v`
Expected: ImportError — `marim_harness.claude.process` does not exist.

- [ ] **Step 3: Write `src/marim_harness/claude/process.py`**

```python
"""One long-lived bidirectional ``claude`` process (spec §Process supervisor).

``ClaudeProcess`` spawns the CLI with the isolation argv, runs the
``StreamJsonClient`` reader as a task, routes every stdout object to the
*current* turn's queue, and owns the three clocks the spec names: the
silence timeout (per turn, paused while an approval prompt is open), the
idle reaper (between turns; the next turn resumes by session id), and the
shutdown grace (SIGTERM the group, then ``kill_process_tree``).

The model layer (``config/claude_cli_model.py``) and the spawn runner
(``subagents/cli_backend.py``) both consume it through ``send_turn`` +
``turn_objects``; neither touches the pipes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..config.external_cli import CliModelError
from ..tools.impl.process import kill_process_tree
from .env import INSTALL_HINT
from .protocol import CLOSED, ControlError, ProcessClosed, RequestHandler, StreamJsonClient

logger = logging.getLogger(__name__)

_INIT_TIMEOUT = 30.0
_INTERRUPT_GRACE = 2.0
_TERM_GRACE = 2.0
_STDERR_LINES = 40
_STDERR_TAIL_CHARS = 2000

# Isolation + framing flags every launch carries (spec §Isolation). Never
# ``--bare`` (breaks subscription auth) and never ``--permission-mode`` —
# every permission decision is brokered over stdio instead.
ISOLATION_ARGV: tuple[str, ...] = (
    "--strict-mcp-config",
    "--setting-sources",
    "",
    "--safe-mode",
    "--permission-prompts",
    "host",
    "--permission-prompt-tool",
    "stdio",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--replay-user-messages",
)

# A marim launched from inside Claude Code inherits these; a child claude that
# sees them thinks it is nested and refuses or misroutes.
_STRIPPED_ENV = ("CLAUDE_CODE_SSE_PORT", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


@dataclass(frozen=True)
class ProcessOptions:
    binary: str
    cwd: str
    model: str | None = None
    resume_id: str | None = None
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    append_system: str | None = None
    persist: bool = True
    env: dict[str, str] | None = None


def build_process_argv(options: ProcessOptions) -> list[str]:
    argv = [options.binary, *ISOLATION_ARGV]
    if options.model:
        argv += ["--model", options.model]
    if options.resume_id:
        argv += ["--resume", options.resume_id]
    if options.tools:
        argv += ["--tools", ",".join(options.tools)]
    if options.disallowed_tools:
        argv += ["--disallowedTools", ",".join(options.disallowed_tools)]
    if options.append_system:
        argv += ["--append-system-prompt", options.append_system]
    if not options.persist:
        argv.append("--no-session-persistence")
    return argv


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if k not in _STRIPPED_ENV}


@dataclass
class TurnHandle:
    """The receiving side of one turn. ``events`` carries every stdout object
    routed to the turn, ending with a ``result`` or a synthetic ``CLOSED``."""

    events: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    open: bool = True
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def finish(self) -> None:
        self.open = False
        self.closed.set()


def _is_terminal(obj: dict) -> bool:
    return obj.get("type") in ("result", CLOSED)


def _swallow(task: asyncio.Future) -> None:
    """Retrieve a fire-and-forget task's outcome so a failed interrupt/steer
    logs instead of surfacing as "exception was never retrieved"."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("claude control write failed: %s", exc)


class ClaudeProcess:
    def __init__(
        self,
        options: ProcessOptions,
        *,
        on_request: RequestHandler | None = None,
        silence_timeout: float = 0.0,
        idle_timeout: float = 0.0,
    ) -> None:
        self._opts = options
        self._on_request = on_request
        self.silence_timeout = silence_timeout
        self._idle_timeout = idle_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._client: StreamJsonClient | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_LINES)
        self._turn: TurnHandle | None = None
        self.session_id: str | None = options.resume_id
        self.init_info: dict = {}
        self.closed = asyncio.Event()

    # --- state ---------------------------------------------------------------
    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.returncode is None and not self.closed.is_set()

    @property
    def turn_open(self) -> bool:
        return self._turn is not None and self._turn.open

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def last_stderr(self) -> str:
        return "".join(self._stderr_tail)[-_STDERR_TAIL_CHARS:]

    @property
    def prompts_open(self) -> int:
        return self._client.prompts_open if self._client is not None else 0

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        argv = build_process_argv(self._opts)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._opts.cwd,
                env=child_env(self._opts.env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise CliModelError(f"could not launch claude ({exc}). {INSTALL_HINT}") from exc
        proc = self._proc
        assert proc.stdout is not None and proc.stdin is not None and proc.stderr is not None
        self._client = StreamJsonClient(
            proc.stdout, proc.stdin, on_event=self._on_event, on_request=self._on_request
        )
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._run_reader(self._client))
        self._stderr_task = loop.create_task(self._pump_stderr(proc.stderr))
        await self._initialize(self._client)

    async def _initialize(self, client: StreamJsonClient) -> None:
        # Best-effort handshake: the CLI serializes stdin, so the first user
        # message is ordered after `initialize` whether or not the answer has
        # arrived. Waiting (bounded) just keeps the logs honest and lets a
        # launch that dies immediately surface on the first `send_turn`.
        init = asyncio.ensure_future(client.control("initialize", timeout=_INIT_TIMEOUT, hooks={}))
        closing = asyncio.ensure_future(self.closed.wait())
        try:
            await asyncio.wait({init, closing}, timeout=_INIT_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (init, closing):
                if not fut.done():
                    fut.cancel()
                    with contextlib.suppress(BaseException):
                        await fut
        if init.done() and not init.cancelled() and init.exception() is not None:
            logger.debug("claude initialize did not complete: %s", init.exception())

    async def _run_reader(self, client: StreamJsonClient) -> None:
        try:
            await client.run()
        finally:
            # stdout EOF means the process is gone (or going): give stderr a
            # moment to land so the CLOSED object carries the real reason.
            if self._stderr_task is not None:
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._stderr_task), 0.5)
            self._deliver_closed()
            self.closed.set()

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", "replace"))

    def _closed_object(self) -> dict:
        code = self._proc.returncode if self._proc is not None else None
        return {"type": CLOSED, "stderr": self.last_stderr, "returncode": code}

    def _deliver_closed(self) -> None:
        turn = self._turn
        if turn is not None and turn.open:
            turn.events.put_nowait(self._closed_object())
            turn.finish()
        self._turn = None

    async def aclose(self) -> None:
        self._cancel_idle()
        proc = self._proc
        if proc is None:
            return
        if self._client is not None:
            await self._client.aclose()
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                assert proc.stdin is not None
                proc.stdin.close()
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), _TERM_GRACE)
            except (asyncio.TimeoutError, TimeoutError):
                kill_process_tree(proc.pid)
                with contextlib.suppress(Exception):
                    await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._deliver_closed()
        self.closed.set()
        self._proc = None

    # --- turns ---------------------------------------------------------------
    def _on_event(self, obj: dict) -> None:
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            self.session_id = str(obj.get("session_id") or self.session_id or "") or None
            self.init_info = obj
        turn = self._turn
        if turn is None or not turn.open:
            logger.debug("claude object with no open turn dropped: %s", obj.get("type"))
            return
        turn.events.put_nowait(obj)
        if obj.get("type") == "result":
            turn.finish()
            self._turn = None
            self._arm_idle()

    async def send_turn(self, text: str) -> TurnHandle:
        """Open a turn and send its user message. A dead process still returns
        a handle — one whose queue already holds the ``CLOSED`` object — so the
        consumer has a single code path."""
        self._cancel_idle()
        handle = TurnHandle()
        self._turn = handle
        if self._client is None or self.closed.is_set():
            self._deliver_closed()
            return handle
        try:
            await self._client.user(text)
        except ProcessClosed:
            self._deliver_closed()
        return handle

    async def send_user(self, text: str) -> None:
        """A user message while a turn is open folds into that turn (steer)."""
        if self._client is None or self.closed.is_set():
            raise ProcessClosed("claude is not running")
        await self._client.user(text)

    async def interrupt(self, handle: TurnHandle, grace: float = _INTERRUPT_GRACE) -> None:
        """Send ``interrupt`` and wait up to ``grace`` for the turn's aborted
        ``result``. A CLI that does not answer in time is killed — the next
        turn resumes by session id (spec §Interrupt)."""
        if not handle.open or self._client is None or self.closed.is_set():
            return
        request = asyncio.ensure_future(self._client.control("interrupt", timeout=grace))
        request.add_done_callback(_swallow)
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(handle.closed.wait(), grace)
        if not request.done():
            request.cancel()
        if handle.open:
            logger.warning("claude ignored interrupt for %.0fs; killing it", grace)
            await self.aclose()

    # --- idle reaper ---------------------------------------------------------
    def _arm_idle(self) -> None:
        if self._idle_timeout <= 0:
            return
        self._cancel_idle()
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_close())

    def _cancel_idle(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _idle_close(self) -> None:
        await asyncio.sleep(self._idle_timeout)
        logger.info("claude idle for %.0fs; closing (the next turn resumes by id)", self._idle_timeout)
        await self.aclose()


# --- consuming a turn -------------------------------------------------------


async def next_turn_object(process: ClaudeProcess, handle: TurnHandle) -> dict:
    """The next object of the turn, honoring the process's silence timeout.
    The clock counts only while no control_request handler is open, so an
    approval panel waiting on the user never trips it. On timeout the turn is
    interrupted and ``CliModelError`` raised."""
    timeout = process.silence_timeout
    while True:
        try:
            return await asyncio.wait_for(handle.events.get(), timeout if timeout > 0 else None)
        except (asyncio.TimeoutError, TimeoutError):
            if process.prompts_open > 0:
                continue
            await process.interrupt(handle)
            raise CliModelError(f"claude timed out after {timeout:.0f}s of silence (interrupted)") from None


async def turn_objects(
    process: ClaudeProcess, handle: TurnHandle, first: dict | None = None
) -> AsyncIterator[dict]:
    """Every object of the turn through its terminal ``result``/``CLOSED``.
    ``first`` re-injects an object a caller already pulled (the resume probe in
    the model layer). A cancelled consumer interrupts the turn so the CLI stops
    working on an answer nobody will read."""
    if first is not None:
        yield first
        if _is_terminal(first):
            return
    while True:
        try:
            obj = await next_turn_object(process, handle)
        except asyncio.CancelledError:
            await process.interrupt(handle)
            raise
        yield obj
        if _is_terminal(obj):
            return
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_claude_process.py -v`
Expected: 16 passed. If `test_aclose_kills_a_hung_child_promptly` reports the
pid still alive, the fake's `sleep` step is being killed only in the shell
wrapper — confirm the wrapper uses `exec` (Task 4) so the python fake *is* the
group leader.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check --fix src/marim_harness/claude tests/test_claude_process.py && uv run ruff format src/marim_harness/claude tests/test_claude_process.py && uv run pyright
git add src/marim_harness/claude/process.py tests/test_claude_process.py
git commit -m "feat(claude): ClaudeProcess — long-lived bidirectional claude with silence/idle clocks

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `claude/approvals.py` — `can_use_tool` → Mode / panel / ask_user

**Files:**
- Create: `src/marim_harness/claude/approvals.py`
- Test: `tests/test_claude_approvals.py`

**Interfaces:**
- Consumes: `Decision`, `ExternalRequest`, `Mode`, `PLAN_READ_ONLY`, `UiSeams`,
  `decide_external` from `runtime/permissions.py` (Task 2); `Choice`, `Question`
  from `marim_harness.ask_user`; `normalize_cc_tool` from
  `subagents/cli_backend.py` (lazy import inside `_prompt` — `cli_backend`
  will import `claude.process` in Task 9, so a module-level import here would
  be a cycle); `RequestHandler` shape from Task 3 (`(request_id, request) -> dict`).
- Produces (used by Tasks 7 and 9):
  - `ToolRequest(mutating: bool, paths: tuple[Path, ...] = (), question: bool = False)`
  - `classify(tool_name: str, tool_input: dict, annotations: dict | None = None) -> ToolRequest` — pure, table-driven (spec table).
  - `allow_reply(tool_input: dict) -> dict`, `deny_reply(message: str) -> dict`
  - message constants `PLAN_DENY_MESSAGE`, `CANCELLED_MESSAGE`, `NO_USER_MESSAGE`, `HEADLESS_DENY_MESSAGE`, `USER_DENIED_MESSAGE`
  - `ClaudeApprovalBroker(*, mode_getter: Callable[[], Mode], workspace_root: Path | None, scratchpad_getter: Callable[[], Path | None], ui: UiSeams, label: str = "")` with `async handle(request_id: str, request: dict) -> dict` (the `RequestHandler` a `ClaudeProcess` takes as `on_request`) and `last_reply: dict | None`.

Behavior (spec §Mode → approval mapping):
- Non-`can_use_tool` subtypes raise `ValueError` → the client answers with an
  error response (Task 3 `_answer` does that), the CLI treats it as a deny.
- `AskUserQuestion` (or any request flagged `requires_user_interaction`) never
  hits the policy table: `ask_user` answers it (`updatedInput.answers =
  {question text: label}`; multi-select joins labels with `", "`); unbound or
  cancelled → deny with `NO_USER_MESSAGE`.
- Relative `paths` from `classify` are anchored at `workspace_root` (the CLI
  runs with `cwd` = that root) before the policy check.
- A prompt renders as a `ToolCallPart` named by `normalize_cc_tool` (`Write` →
  `write_file`, args `{"path": …}`), `tool_call_id` = the CLI `tool_use_id`,
  args carrying `reason` (the table's reason, when any) and `label` (spawn name).
  Approve → `allow` with the *original* input; deny → `deny` with the panel's
  `ToolDenied.message` when it has one, else `USER_DENIED_MESSAGE`.
- Prompts are serialized with a lock (arrival order, one panel at a time); a
  cancelled handler (the client's `control_cancel_request` path, or `aclose`)
  records `deny_reply(CANCELLED_MESSAGE)` as `last_reply` and re-raises so the
  client writes nothing.

- [ ] **Step 1: Write the failing tests**

`tests/test_claude_approvals.py`:

```python
"""ClaudeApprovalBroker: the can_use_tool table, prompting through the
approval panel seam, AskUserQuestion through ask_user, cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import ToolDenied

from marim_harness.claude.approvals import (
    CANCELLED_MESSAGE,
    HEADLESS_DENY_MESSAGE,
    NO_USER_MESSAGE,
    PLAN_DENY_MESSAGE,
    USER_DENIED_MESSAGE,
    ClaudeApprovalBroker,
    ToolRequest,
    allow_reply,
    classify,
    deny_reply,
)
from marim_harness.runtime.permissions import Mode, UiSeams

pytestmark = pytest.mark.anyio


# --- classify ---------------------------------------------------------------


def test_classify_read_only_tools():
    for name in ("Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoRead", "ToolSearch"):
        assert classify(name, {"file_path": "/x"}) == ToolRequest(mutating=False)


def test_classify_file_mutators_carry_their_path():
    assert classify("Write", {"file_path": "/ws/a.py"}) == ToolRequest(mutating=True, paths=(Path("/ws/a.py"),))
    assert classify("Edit", {"file_path": "rel.py"}) == ToolRequest(mutating=True, paths=(Path("rel.py"),))
    assert classify("MultiEdit", {"file_path": "/m"}).paths == (Path("/m"),)
    assert classify("NotebookEdit", {"notebook_path": "/n.ipynb"}).paths == (Path("/n.ipynb"),)
    assert classify("Write", {}) == ToolRequest(mutating=True)  # no path → mutating, unanchored


def test_classify_commands_agents_mcp_and_unknown_are_mutating():
    for name in ("Bash", "Task", "Agent", "Skill", "CronCreate", "SendMessage", "EnterWorktree", "Frobnicate"):
        assert classify(name, {}) == ToolRequest(mutating=True)
    assert classify("mcp__srv__tool", {}) == ToolRequest(mutating=True)
    assert classify("mcp__srv__tool", {}, {"readOnlyHint": True}) == ToolRequest(mutating=False)


def test_classify_ask_user_question_is_a_question():
    assert classify("AskUserQuestion", {"questions": []}) == ToolRequest(mutating=False, question=True)


def test_reply_shapes():
    assert allow_reply({"a": 1}) == {"behavior": "allow", "updatedInput": {"a": 1}}
    assert deny_reply("no") == {"behavior": "deny", "message": "no"}


# --- broker -----------------------------------------------------------------


class _Panel:
    """Records approval prompts; answers with the queued replies in order."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls: list = []
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, call):
        self.calls.append(call)
        await self.release.wait()
        return self.replies.pop(0) if self.replies else False


def _broker(mode: Mode, root: Path, *, panel=None, ask_user=None, pad: Path | None = None, label=""):
    return ClaudeApprovalBroker(
        mode_getter=lambda: mode,
        workspace_root=root,
        scratchpad_getter=lambda: pad,
        ui=UiSeams(request_approval=panel, ask_user=ask_user),
        label=label,
    )


def _write(path: str, tid: str = "tu1") -> dict:
    return {"subtype": "can_use_tool", "tool_name": "Write", "input": {"file_path": path, "content": "x"}, "tool_use_id": tid}


async def test_plan_denies_mutation_without_prompting(tmp_path: Path):
    panel = _Panel(True)
    broker = _broker(Mode.plan, tmp_path, panel=panel)
    reply = await broker.handle("r1", _write(str(tmp_path / "a.py")))
    assert reply == deny_reply(PLAN_DENY_MESSAGE)
    assert panel.calls == []
    assert broker.last_reply == reply


async def test_plan_allows_reads(tmp_path: Path):
    broker = _broker(Mode.plan, tmp_path, panel=_Panel())
    req = {"subtype": "can_use_tool", "tool_name": "Read", "input": {"file_path": "/etc/hosts"}, "tool_use_id": "t"}
    assert await broker.handle("r1", req) == allow_reply({"file_path": "/etc/hosts"})


async def test_auto_allows_inside_workspace_and_prompts_outside(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    panel = _Panel(True)
    broker = _broker(Mode.auto, root, panel=panel)
    assert (await broker.handle("r1", _write(str(root / "a.py"))))["behavior"] == "allow"
    assert panel.calls == []
    stray = tmp_path / "elsewhere.py"
    reply = await broker.handle("r2", _write(str(stray), tid="tu2"))
    assert reply["behavior"] == "allow"
    call = panel.calls[0]
    assert call.tool_name == "write_file"
    assert call.args["path"] == str(stray)
    assert call.args["reason"] == f"outside workspace: {stray}"
    assert call.tool_call_id == "tu2"


async def test_ask_prompts_for_workspace_writes_and_allows_scratchpad(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    pad = tmp_path / "pad"
    pad.mkdir()
    panel = _Panel(False)
    broker = _broker(Mode.ask, root, panel=panel, pad=pad, label="worker")
    assert (await broker.handle("r1", _write(str(pad / "notes.md"))))["behavior"] == "allow"
    assert panel.calls == []
    reply = await broker.handle("r2", _write(str(root / "a.py")))
    assert reply == deny_reply(USER_DENIED_MESSAGE)
    assert panel.calls[0].args["label"] == "worker"
    assert "reason" not in panel.calls[0].args


async def test_relative_paths_anchor_at_workspace_root(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    panel = _Panel(True)
    broker = _broker(Mode.auto, root, panel=panel)
    assert (await broker.handle("r1", _write("src/a.py")))["behavior"] == "allow"
    assert panel.calls == []
    await broker.handle("r2", _write("../escape.py"))
    assert len(panel.calls) == 1  # outside the root once resolved → prompted


async def test_panel_denial_message_reaches_claude(tmp_path: Path):
    panel = _Panel(ToolDenied(message="use the scratchpad instead"))
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    reply = await broker.handle("r1", _write(str(tmp_path / "a.py")))
    assert reply == deny_reply("use the scratchpad instead")


async def test_headless_ask_denies_with_explanation(tmp_path: Path):
    broker = _broker(Mode.ask, tmp_path, panel=None)
    reply = await broker.handle("r1", _write(str(tmp_path / "a.py")))
    assert reply == deny_reply(HEADLESS_DENY_MESSAGE)


async def test_bash_prompts_in_ask_mode_as_marim_bash(tmp_path: Path):
    panel = _Panel(True)
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    req = {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}, "tool_use_id": "b1"}
    assert (await broker.handle("r1", req))["updatedInput"] == {"command": "ls"}
    assert panel.calls[0].tool_name == "bash" and panel.calls[0].args["command"] == "ls"


async def test_ask_user_question_routes_to_ask_user(tmp_path: Path):
    seen: list = []

    async def ask(questions):
        seen.extend(questions)
        return {"Color": "Blue", "Extras": ["Bold", "Wide"]}

    broker = _broker(Mode.plan, tmp_path, ask_user=ask)  # plan mode does not block questions
    req = {
        "subtype": "can_use_tool",
        "tool_name": "AskUserQuestion",
        "requires_user_interaction": True,
        "input": {
            "questions": [
                {"question": "Which color?", "header": "Color", "options": [{"label": "Red"}, {"label": "Blue", "description": "cool"}], "multiSelect": False},
                {"question": "Any extras?", "header": "Extras", "options": [{"label": "Bold"}, {"label": "Wide"}], "multiSelect": True},
            ]
        },
        "tool_use_id": "q1",
    }
    reply = await broker.handle("r1", req)
    assert reply["behavior"] == "allow"
    assert reply["updatedInput"]["answers"] == {"Which color?": "Blue", "Any extras?": "Bold, Wide"}
    assert reply["updatedInput"]["questions"] == req["input"]["questions"]  # original input kept
    assert [q.header for q in seen] == ["Color", "Extras"]
    assert seen[0].options[1].description == "cool" and seen[1].multi is True


async def test_ask_user_unbound_or_cancelled_denies_with_guidance(tmp_path: Path):
    req = {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "input": {"questions": [{"question": "q", "header": "H", "options": [{"label": "a"}]}]}, "tool_use_id": "q1"}
    assert await _broker(Mode.auto, tmp_path).handle("r1", req) == deny_reply(NO_USER_MESSAGE)

    async def cancelled(questions):
        return None

    assert await _broker(Mode.auto, tmp_path, ask_user=cancelled).handle("r2", req) == deny_reply(NO_USER_MESSAGE)


async def test_unsupported_subtype_raises(tmp_path: Path):
    broker = _broker(Mode.auto, tmp_path)
    with pytest.raises(ValueError):
        await broker.handle("r1", {"subtype": "hook_callback"})


async def test_cancelled_prompt_records_cancel_and_reraises(tmp_path: Path):
    panel = _Panel(True)
    panel.release.clear()  # the user never answers
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    task = asyncio.ensure_future(broker.handle("r1", _write(str(tmp_path / "a.py"))))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.last_reply == deny_reply(CANCELLED_MESSAGE)


async def test_prompts_are_serialized_in_arrival_order(tmp_path: Path):
    panel = _Panel(True, True)
    panel.release.clear()
    broker = _broker(Mode.ask, tmp_path, panel=panel)
    first = asyncio.ensure_future(broker.handle("r1", _write(str(tmp_path / "a.py"), "tu1")))
    second = asyncio.ensure_future(broker.handle("r2", _write(str(tmp_path / "b.py"), "tu2")))
    await asyncio.sleep(0.01)
    assert [c.tool_call_id for c in panel.calls] == ["tu1"]  # the second waits for the lock
    panel.release.set()
    await asyncio.gather(first, second)
    assert [c.tool_call_id for c in panel.calls] == ["tu1", "tu2"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_claude_approvals.py -q`
Expected: ImportError — `marim_harness.claude.approvals` does not exist.

- [ ] **Step 3: Write `src/marim_harness/claude/approvals.py`**

```python
"""``can_use_tool`` → marim's Mode, approval panel, or ask_user
(spec §Mode → approval mapping).

The CLI runs in its ``default`` permission mode and asks marim, over stdio,
before every tool it would not auto-allow. ``classify`` turns the request
into the transport-neutral ``ExternalRequest`` the shared policy core
(``runtime/permissions.decide_external``) decides on; the broker then
answers directly, prompts through ``UiSeams.request_approval`` (the
ApprovalPanel in the TUI, None headless), or routes ``AskUserQuestion``
through ``UiSeams.ask_user``. Deny messages are written for Claude — they
land verbatim in its tool_result — so each says what was refused and what
to do instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..ask_user import Choice, Question
from ..runtime.permissions import (
    PLAN_READ_ONLY,
    Decision,
    ExternalRequest,
    Mode,
    UiSeams,
    decide_external,
)

logger = logging.getLogger(__name__)

QUESTION_TOOL = "AskUserQuestion"
READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "WebFetch",
        "WebSearch",
        "TodoRead",
        "TaskGet",
        "TaskList",
        "ListAgents",
        "ToolSearch",
    }
)
_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

PLAN_DENY_MESSAGE = "plan mode: read-only — describe the change instead of making it"
CANCELLED_MESSAGE = "cancelled by user"
USER_DENIED_MESSAGE = "denied by the user; do not retry this action, ask what they want instead"
HEADLESS_DENY_MESSAGE = (
    "no approver is attached (headless run); this action is not permitted here — "
    "explain what you would have done instead"
)
NO_USER_MESSAGE = (
    "the user is not available to answer; proceed on your best judgement and say what you assumed"
)


@dataclass(frozen=True)
class ToolRequest:
    mutating: bool
    paths: tuple[Path, ...] = ()
    question: bool = False


def classify(tool_name: str, tool_input: dict, annotations: dict | None = None) -> ToolRequest:
    """The spec's tool table. Unknown tools are mutating (the safe default);
    an MCP tool is read-only only when the CLI's annotations say so."""
    if tool_name == QUESTION_TOOL:
        return ToolRequest(mutating=False, question=True)
    if tool_name in READ_ONLY_TOOLS:
        return ToolRequest(mutating=False)
    if tool_name.startswith("mcp__"):
        return ToolRequest(mutating=not bool((annotations or {}).get("readOnlyHint")))
    key = _PATH_KEYS.get(tool_name)
    if key is not None:
        raw = tool_input.get(key)
        return ToolRequest(mutating=True, paths=(Path(str(raw)),) if raw else ())
    return ToolRequest(mutating=True)


def allow_reply(tool_input: dict) -> dict:
    return {"behavior": "allow", "updatedInput": dict(tool_input)}


def deny_reply(message: str) -> dict:
    return {"behavior": "deny", "message": message}


def wire_deny_message(decision: Decision) -> str:
    """The shared core's reason is a log label; on the wire plan mode gets
    the fuller hint Claude can act on."""
    if decision.reason == PLAN_READ_ONLY:
        return PLAN_DENY_MESSAGE
    return decision.reason or "not permitted"


def _is_approved(result: object) -> bool:
    """Same acceptance rule as native gating: ``True`` or a ToolApproved is a
    yes; ``False``/``None``/ToolDenied is a no."""
    if result is True:
        return True
    if not result:
        return False
    from pydantic_ai import ToolApproved  # lazy — see module note in runtime/permissions.py.

    return isinstance(result, ToolApproved)


def _denial_message(result: object) -> str:
    message = getattr(result, "message", None)
    return str(message) if message else USER_DENIED_MESSAGE


def _question(raw: dict) -> Question:
    text = str(raw.get("question", ""))
    return Question(
        question=text,
        header=str(raw.get("header") or text),
        options=[
            Choice(label=str(o.get("label", "")), description=o.get("description"))
            for o in raw.get("options") or []
            if o.get("label")
        ],
        multi=bool(raw.get("multiSelect")),
    )


def _answer_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value or "")


class ClaudeApprovalBroker:
    def __init__(
        self,
        *,
        mode_getter: Callable[[], Mode],
        workspace_root: Path | None,
        scratchpad_getter: Callable[[], Path | None],
        ui: UiSeams,
        label: str = "",
    ) -> None:
        self._mode_getter = mode_getter
        self._root = workspace_root
        self._scratchpad_getter = scratchpad_getter
        self._ui = ui
        self._label = label
        self._lock = asyncio.Lock()
        self.last_reply: dict | None = None

    async def handle(self, request_id: str, request: dict) -> dict:
        """Answer one ``control_request``. Serialized: the CLI can raise two
        ``can_use_tool`` requests from parallel tool calls and the panel shows
        one at a time. A cancelled handler (the client's
        ``control_cancel_request`` path, or ``aclose``) records the cancel and
        re-raises so nothing is written for a request the CLI abandoned."""
        async with self._lock:
            try:
                reply = await self._handle(request)
            except asyncio.CancelledError:
                self.last_reply = deny_reply(CANCELLED_MESSAGE)
                raise
            self.last_reply = reply
            return reply

    async def _handle(self, request: dict) -> dict:
        subtype = request.get("subtype")
        if subtype != "can_use_tool":
            raise ValueError(f"unsupported control_request {subtype!r}")
        tool_name = str(request.get("tool_name") or "")
        tool_input = dict(request.get("input") or {})
        req = classify(tool_name, tool_input, request.get("annotations"))
        if req.question or request.get("requires_user_interaction"):
            return await self._ask(tool_input)
        decision = decide_external(
            self._mode_getter(), self._anchored(req), self._root, self._scratchpad_getter()
        )
        if decision.ask:
            return await self._prompt(tool_name, tool_input, request, decision)
        if decision.accept:
            return allow_reply(tool_input)
        logger.info("claude %s denied (%s)", tool_name, decision.reason)
        return deny_reply(wire_deny_message(decision))

    def _anchored(self, req: ToolRequest) -> ExternalRequest:
        """The CLI runs with cwd = the workspace root, so a relative
        ``file_path`` means "under the root" — resolve it there before the
        policy check rather than against marim's own cwd."""
        root = self._root or Path(".")
        paths = tuple(p if p.is_absolute() else root / p for p in req.paths)
        return ExternalRequest(mutating=req.mutating, paths=paths)

    async def _prompt(self, tool_name: str, tool_input: dict, request: dict, decision: Decision) -> dict:
        """Route to ``request_approval`` as a pydantic-ai ToolCallPart so the
        ApprovalPanel renders it like a native gated call. Headless (no
        approver) → denied, exactly like ``resolve_approvals``."""
        if self._ui.request_approval is None:
            return deny_reply(HEADLESS_DENY_MESSAGE)
        from pydantic_ai.messages import ToolCallPart

        from ..subagents.cli_backend import normalize_cc_tool  # lazy: cli_backend imports claude.process

        name, args = normalize_cc_tool(tool_name, tool_input)
        args = dict(args)
        if decision.reason:
            args["reason"] = decision.reason
        if self._label:
            args["label"] = self._label
        call = ToolCallPart(tool_name=name, args=args, tool_call_id=str(request.get("tool_use_id") or ""))
        result = await self._ui.request_approval(call)
        if _is_approved(result):
            return allow_reply(tool_input)
        logger.info("claude %s denied by the user", tool_name)
        return deny_reply(_denial_message(result))

    async def _ask(self, tool_input: dict) -> dict:
        questions = [_question(q) for q in tool_input.get("questions") or []]
        answers: dict | None = None
        if self._ui.ask_user is not None and questions:
            answers = await self._ui.ask_user(questions)
        if not answers:
            return deny_reply(NO_USER_MESSAGE)
        updated = dict(tool_input)
        updated["answers"] = {q.question: _answer_text(answers.get(q.header)) for q in questions}
        return allow_reply(updated)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_claude_approvals.py -v`
Expected: 18 passed. `test_relative_paths_anchor_at_workspace_root` depends on
Task 2's `within_root` resolving `..` — if it fails, check that the root
directory exists (`resolve()` on a missing parent still works, but the test
creates it anyway).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check --fix src/marim_harness/claude tests/test_claude_approvals.py && uv run ruff format src/marim_harness/claude tests/test_claude_approvals.py && uv run pyright
git add src/marim_harness/claude/approvals.py tests/test_claude_approvals.py
git commit -m "feat(claude): ClaudeApprovalBroker — can_use_tool through Mode, the approval panel and ask_user

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Port `ClaudeCliModel` to the long-lived process

**Files:**
- Modify: `src/marim_harness/config/claude_cli_model.py` (rewrite the module docstring, the chunk vocabulary, `consume_cli_stream`, `ClaudeCliModel`, `ClaudeCliStreamedResponse`; delete `_MODE_MAP`, `permission_mode_for`, `note_ask_limitation_once`, `_ask_noticed`, `spawn_cli_objects`, `_STDERR_TAIL_CHARS`, `_merge_session_id`, `_result_done_chunk`, `_assistant_chunks`, `_stream_chunks`, `ClaudeCliModel._argv`, `ClaudeCliModel.spawn`)
- Test: `tests/test_claude_cli_model.py` (rewrite the process-driving tests on `fake_claude_bin`; keep the pure-helper tests)

**Interfaces:**
- Consumes: `ClaudeProcess`, `ProcessOptions`, `TurnHandle`, `next_turn_object`, `turn_objects` (Task 5); `CLOSED` from `marim_harness.claude.protocol` (Task 3); `ClaudeApprovalBroker` (Task 6); `INSTALL_HINT`, `MIN_CLAUDE_VERSION`, `cli_timeout`, `cli_idle_timeout`, `resolve_cli_binary` from `marim_harness.claude.env` (Task 1); `Mode`, `UiSeams` from `runtime.permissions` (Task 2); `fake_claude_bin`, `read_claude_argv`, `read_claude_argvs`, `read_claude_log` (Task 4).
- Produces (used by Task 8's harness tests and Task 10's docs):
  - `SESSION_REF_PREFIX = "claude-cli:"`
  - chunks `TextChunk(delta)`, `ThinkingChunk(delta)`, `ToolUseChunk(name, tool_input, call_id)`, `ToolResultChunk(call_id, content, is_error)`, `InitChunk(session_id, version, model)`, `DoneChunk(session_id, usage, complete, error_detail="", aborted=False)`
  - `consume_cli_stream(objs: AsyncIterator[dict]) -> AsyncGenerator` — one turn's objects → chunks, ending with exactly one `DoneChunk`
  - `note_old_version_once(version: str) -> None`
  - `ClaudeCliModel(model_id, *, ephemeral=False)` with `session_id` (property), `ephemeral_clone(cwd=)`, `request`, `request_stream`, `steer(text) -> bool`, `async aclose()`
  - `ClaudeCliStreamedResponse` (dataclass) with `_on_init: Callable[[InitChunk], None] | None`

What stays **unchanged** (leave these definitions exactly as they are in the file): `_part_text`, `latest_user_text`, `extract_system`, `_render_tool_args`, `_request_lines`, `_response_lines`, `flatten_history`, `request_usage_from_cli`, `_ACTIVITY_ARG`, `_ACTIVITY_MARKER`, `format_activity_line`, `ToolUseChunk`, `ToolResultChunk`, `_flatten_result_content`, `_is_subagent_noise`, `_user_chunks`, `_result_error_subtype`, `fold_chunk_text`, `cli_activity_events`, `_no_result_message`, `_TextFolder = TextFolder`. Their tests in `tests/test_claude_cli_model.py` (`test_latest_user_text_*`, `test_extract_system_*`, `test_flatten_history_*`, `test_fold_chunk_text_*`, `test_request_usage_*`, `test_format_activity_line_*`, `test_stream_renderer_on_cli_activity_dispatches_each_event`, `test_cli_activity_events_builds_native_tool_events`, `test_activity_line_names_agent_spawns`) stay verbatim.

Behavior changes (spec §Model layer, §Chunk table):
- One `ClaudeProcess` per model instance, spawned on the first turn and reused while alive. A dead process (idle-closed, crashed, killed after an ignored interrupt) is respawned with `--resume <its session id>` on the next turn; a brand-new model with a persisted `claude-cli:<id>` session ref resumes that; otherwise a cold start sends the flattened history plus `--append-system-prompt`.
- Resumed turns send only the newest user text. A resume whose first object is `CLOSED` carrying `No conversation found` respawns fresh (flattened history) and logs a warning.
- Prose/thinking come from `stream_event` deltas; `assistant` objects contribute only `tool_use` blocks; `user` objects contribute `tool_result` blocks (`isReplay` echoes dropped); `system/init` becomes `InitChunk` (session ref + version check); `result` ends the turn (`is_error` + `terminal_reason` in `aborted_tools`/`aborted_streaming` = an interrupted-but-complete turn); `CLOSED` = incomplete with `claude exited (code N): <stderr>` as the detail. An errored result WITH streamed text keeps the partial output (the pre-existing policy — see `_result_chunk`), one without raises.
- Ephemeral clones (titler/summarizer) never resume or persist: no `session_ref`, `--no-session-persistence`, idle timeout 0, and the process is closed after every turn.
- `steer` writes a user message into the open turn; `aclose` closes the process (the model-swap and harness-teardown hooks of Task 8 call it).

- [ ] **Step 1: Rewrite the process-driving tests**

Replace everything in `tests/test_claude_cli_model.py` from `@pytest.fixture(autouse=True)` through `test_permission_mode_mapping`, plus the tests named `test_ephemeral_model_never_resumes_and_always_sends_system`, `test_ephemeral_model_does_not_store_session_id`, `test_response_timestamp_is_per_request_not_construction`, `test_stream_abandon_closes_child_deterministically`, `test_request_closes_child_on_consumer_cancel`, `test_consume_streams_text_activity_and_done`, `test_consume_surfaces_tool_results`, `test_request_stream_pushes_tool_cards_and_keeps_response_text_only`, `test_consume_marks_incomplete_when_no_result`, `test_consume_errored_result_without_text_is_incomplete`, `test_consume_errored_result_with_text_keeps_partial`, `test_consume_success_result_has_no_error_detail`, `test_consume_skips_subagent_child_traffic`, `test_consume_survives_multiple_results_and_folds_usage`, `_fake_objs`, `_INIT`, `_result`, `test_request_stream_routes_claude_subagents_to_side_channels`, `test_request_returns_text_only_response_and_captures_session`, `test_argv_runs_claude_in_safe_mode`, `test_second_turn_uses_resume`, `test_request_raises_on_incomplete_stream`, `test_argv_omits_model_flag_when_id_blank`, `test_request_uses_configured_cwd`, `test_request_stream_raises_on_incomplete_stream`, `test_request_stream_yields_text_events`, `test_request_error_includes_stderr_from_sentinel`, `test_request_stream_error_includes_stderr_from_sentinel`, `test_request_surfaces_real_cli_stderr_on_nonzero_exit` with the following. Update the module's import block to:

```python
import asyncio
import os
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    TextPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from marim_harness.claude.env import CLI_BINARY_ENV
from marim_harness.claude.protocol import CLOSED
from marim_harness.config.claude_cli_model import (
    SESSION_REF_PREFIX,
    ClaudeCliModel,
    DoneChunk,
    InitChunk,
    TextChunk,
    ThinkingChunk,
    ToolResultChunk,
    ToolUseChunk,
    cli_activity_events,
    consume_cli_stream,
    extract_system,
    flatten_history,
    fold_chunk_text,
    format_activity_line,
    latest_user_text,
    request_usage_from_cli,
)
from marim_harness.config.external_cli import CliModelError
from marim_harness.usage import COST_DETAIL_KEY
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_argvs, read_claude_log
```

(Drop any import the retained tests no longer use — `ruff check --fix` removes them.) Then add these tests:

```python
# --- consume_cli_stream (pure) ------------------------------------------------


def _delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    }


def _think(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


_INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "S1",
    "model": "claude-x",
    "claude_code_version": "2.1.261",
}


def _result(text: str, sid: str = "S1", **extra) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "result": text,
        "session_id": sid,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "total_cost_usd": 0.0,
        **extra,
    }


async def _collect(objs):
    async def gen():
        for o in objs:
            yield o

    out = []
    async for chunk in consume_cli_stream(gen()):
        out.append(chunk)
    return out


@pytest.mark.anyio
async def test_consume_streams_deltas_tools_and_done():
    chunks = await _collect(
        [
            _INIT,
            _think("hmm"),
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
            _delta("hel"),
            _delta("lo"),
            # The assistant object repeats the text; only its tool_use block counts.
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a"}},
                    ]
                },
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            },
            {"type": "user", "isReplay": True, "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "system", "subtype": "status", "status": "compacting"},
            _result("hello"),
        ]
    )
    assert chunks[0] == InitChunk(session_id="S1", version="2.1.261", model="claude-x")
    assert chunks[1] == ThinkingChunk("hmm")
    assert chunks[2:4] == [TextChunk("hel"), TextChunk("lo")]
    assert chunks[4] == ToolUseChunk(name="Read", tool_input={"file_path": "/a"}, call_id="t1")
    assert chunks[5] == ToolResultChunk(call_id="t1", content="ok", is_error=False)
    done = chunks[-1]
    assert isinstance(done, DoneChunk) and done.complete and done.session_id == "S1"
    assert done.usage.input_tokens == 1 and done.aborted is False
    assert len(chunks) == 7  # the replay, the status and the assistant text block added nothing


@pytest.mark.anyio
async def test_consume_skips_subagent_child_traffic():
    chunks = await _collect(
        [
            {**_delta("child"), "parent_tool_use_id": "tsub"},
            {
                "type": "assistant",
                "parent_tool_use_id": "tsub",
                "message": {"content": [{"type": "tool_use", "id": "c1", "name": "Read", "input": {}}]},
            },
            {"type": "system", "subtype": "task_started", "tool_use_id": "tsub"},
            _delta("main"),
            _result("main"),
        ]
    )
    assert [type(c) for c in chunks] == [TextChunk, DoneChunk]
    assert chunks[0].delta == "main"


@pytest.mark.anyio
async def test_consume_aborted_result_is_complete_and_flagged():
    chunks = await _collect(
        [
            _delta("partial"),
            _result(
                "",
                subtype="error_during_execution",
                is_error=True,
                terminal_reason="aborted_streaming",
            ),
        ]
    )
    done = chunks[-1]
    assert done.complete is True and done.aborted is True and done.error_detail == ""


@pytest.mark.anyio
async def test_consume_errored_result_without_text_is_incomplete(caplog):
    with caplog.at_level("WARNING"):
        chunks = await _collect([_result("", subtype="error_max_turns", is_error=True)])
    done = chunks[-1]
    assert done.complete is False and "error_max_turns" in done.error_detail
    assert "error_max_turns" in caplog.text


@pytest.mark.anyio
async def test_consume_errored_result_with_text_keeps_partial(caplog):
    with caplog.at_level("WARNING"):
        chunks = await _collect([_delta("half"), _result("", subtype="error_max_turns", is_error=True)])
    done = chunks[-1]
    assert done.complete is True and "error_max_turns" in done.error_detail


@pytest.mark.anyio
async def test_consume_closed_is_incomplete_with_exit_detail():
    chunks = await _collect([_delta("a"), {"type": CLOSED, "stderr": "boom", "returncode": 3}])
    done = chunks[-1]
    assert done.complete is False and done.error_detail == "claude exited (code 3): boom"


@pytest.mark.anyio
async def test_consume_without_terminal_object_is_incomplete():
    (done,) = await _collect([])
    assert isinstance(done, DoneChunk) and done.complete is False


# --- the model against the fake claude ------------------------------------------


def _user(text: str, system: str = "SYS") -> list:
    return [ModelRequest(parts=[UserPromptPart(content=text)], instructions=system)]


def _model(tmp_path: Path, monkeypatch, scenario: dict, model_id: str | None = "sonnet") -> ClaudeCliModel:
    monkeypatch.setenv(CLI_BINARY_ENV, fake_claude_bin(tmp_path, scenario))
    model = ClaudeCliModel(model_id)
    model.cwd = str(tmp_path)
    model.mode_getter = lambda: "auto"
    return model


async def _stream_text(model: ClaudeCliModel, messages: list | None = None) -> str:
    async with model.request_stream(messages or _user("hi"), None, ModelRequestParameters()) as stream:
        async for _ in stream:
            pass
        final = stream.get()
    return "".join(getattr(p, "content", "") for p in final.parts)


def _user_texts(tmp_path: Path) -> list[str]:
    """Every user message the fake received, in order (steers included)."""
    out = []
    for msg in read_claude_log(tmp_path):
        if msg.get("type") == "user":
            out.append("".join(b.get("text", "") for b in msg["message"]["content"]))
    return out


@pytest.mark.anyio
async def test_request_returns_text_only_response_and_captures_session(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"session_id": "S9", "turns": [[{"text": "hello"}]]})
    refs: list[str] = []
    model.on_session_ref = refs.append
    try:
        resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert [type(p) for p in resp.parts] == [TextPart]
    assert resp.parts[0].content == "hello"
    assert not any(isinstance(p, ToolCallPart) for p in resp.parts)
    assert model.session_id == "S9"
    assert refs == [SESSION_REF_PREFIX + "S9"]
    assert resp.usage.input_tokens == 7 and resp.usage.details[COST_DETAIL_KEY] == 1000
    assert resp.provider_name == "claude-cli"


@pytest.mark.anyio
async def test_first_turn_is_cold_with_system_and_isolation_flags(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    history = [
        ModelRequest(parts=[UserPromptPart(content="first")], instructions="SYS"),
        ModelResponse(parts=[TextPart(content="reply")]),
        ModelRequest(parts=[UserPromptPart(content="second")], instructions="SYS"),
    ]
    try:
        await model.request(history, None, ModelRequestParameters())
    finally:
        await model.aclose()
    argv = read_claude_argv(tmp_path)
    for flag in ("--safe-mode", "--strict-mcp-config", "--permission-prompt-tool", "--input-format"):
        assert flag in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--resume" not in argv and "--no-session-persistence" not in argv
    assert _user_texts(tmp_path) == [flatten_history(history)]


@pytest.mark.parametrize("model_id", [None, ""])
@pytest.mark.anyio
async def test_blank_model_id_omits_model_flag(tmp_path, monkeypatch, model_id):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]}, model_id=model_id)
    try:
        await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert "--model" not in read_claude_argv(tmp_path)
    assert model.model_name == "default"


@pytest.mark.anyio
async def test_second_turn_reuses_the_process_and_sends_only_new_text(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "one"}], [{"text": "two"}]]})
    history = _user("first")
    try:
        r1 = await model.request(history, None, ModelRequestParameters())
        history = history + [r1, ModelRequest(parts=[UserPromptPart(content="second")])]
        r2 = await model.request(history, None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert r2.parts[0].content == "two"
    assert len(read_claude_argvs(tmp_path)) == 1  # one launch
    assert _user_texts(tmp_path) == ["User: first", "second"]


@pytest.mark.anyio
async def test_dead_process_is_respawned_with_resume(tmp_path, monkeypatch):
    model = _model(
        tmp_path,
        monkeypatch,
        {"session_id": "S4", "known_sessions": ["S4"], "turns": [[{"text": "one"}], [{"text": "two"}]]},
    )
    try:
        await model.request(_user("a"), None, ModelRequestParameters())
        await model._process.aclose()  # idle reaper / crash stand-in
        r2 = await model.request(_user("a") + [ModelRequest(parts=[UserPromptPart(content="b")])], None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert r2.parts[0].content == "two"
    argvs = read_claude_argvs(tmp_path)
    assert len(argvs) == 2
    assert argvs[1][argvs[1].index("--resume") + 1] == "S4"
    assert "--append-system-prompt" not in argvs[1]


@pytest.mark.anyio
async def test_persisted_session_ref_is_resumed_on_first_turn(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"known_sessions": ["OLD"], "turns": [[{"text": "back"}]]})
    model.session_ref_getter = lambda: SESSION_REF_PREFIX + "OLD"
    try:
        resp = await model.request(_user("again"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "back"
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--resume") + 1] == "OLD"
    assert _user_texts(tmp_path) == ["again"]  # newest text only, no flattening


@pytest.mark.anyio
async def test_foreign_session_ref_is_ignored(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    model.session_ref_getter = lambda: "codex-cli:THREAD"
    try:
        await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert "--resume" not in read_claude_argv(tmp_path)


@pytest.mark.anyio
async def test_missing_session_falls_back_to_a_fresh_start(tmp_path, monkeypatch, caplog):
    model = _model(tmp_path, monkeypatch, {"known_sessions": [], "turns": [[{"text": "fresh"}]]})
    model.session_ref_getter = lambda: SESSION_REF_PREFIX + "GONE"
    refs: list[str] = []
    model.on_session_ref = refs.append
    try:
        with caplog.at_level("WARNING"):
            resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "fresh"
    argvs = read_claude_argvs(tmp_path)
    assert "--resume" in argvs[0] and "--resume" not in argvs[1]
    assert argvs[1][argvs[1].index("--append-system-prompt") + 1] == "SYS"
    assert refs == [SESSION_REF_PREFIX + "S1"]  # the new session replaces the stale ref
    assert "GONE" in caplog.text


@pytest.mark.anyio
async def test_ephemeral_model_never_resumes_never_persists_and_closes(tmp_path, monkeypatch):
    base = _model(tmp_path, monkeypatch, {"session_id": "LIVE", "turns": [[{"text": "t"}]]})
    base.session_ref_getter = lambda: SESSION_REF_PREFIX + "LIVE"
    refs: list[str] = []
    base.on_session_ref = refs.append
    clone = base.ephemeral_clone(cwd=str(tmp_path))
    assert clone.ephemeral and clone.mode_getter() == "plan" and clone.cwd == str(tmp_path)
    clone.session_ref_getter = base.session_ref_getter
    clone.on_session_ref = base.on_session_ref
    await clone.request(_user("title this"), None, ModelRequestParameters())
    await clone.request(_user("title this"), None, ModelRequestParameters())
    argvs = read_claude_argvs(tmp_path)
    assert len(argvs) == 2  # a fresh process per call, closed after each
    for argv in argvs:
        assert "--resume" not in argv and "--no-session-persistence" in argv
    assert refs == [] and clone._process is None


@pytest.mark.anyio
async def test_request_runs_claude_in_the_configured_cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    model.cwd = str(work)
    try:
        await model.request(_user("hi"), None, ModelRequestParameters())
        assert model._process.init_info["cwd"] == str(work.resolve())
    finally:
        await model.aclose()


@pytest.mark.anyio
async def test_response_timestamp_is_per_request(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}]]})
    try:
        r1 = await model.request(_user("hi"), None, ModelRequestParameters())
        r2 = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert r2.timestamp > r1.timestamp


@pytest.mark.anyio
async def test_request_raises_with_exit_detail_when_claude_dies(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}, {"exit": {"code": 3, "stderr": "not logged in"}}]]})
    try:
        with pytest.raises(CliModelError) as exc:
            await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert "no result" in str(exc.value)
    assert "claude exited (code 3): not logged in" in str(exc.value)


@pytest.mark.anyio
async def test_request_stream_raises_with_exit_detail_when_claude_dies(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"exit": {"code": 2, "stderr": "bad flag"}}]]})
    try:
        with pytest.raises(CliModelError) as exc:
            await _stream_text(model)
    finally:
        await model.aclose()
    assert "claude exited (code 2): bad flag" in str(exc.value)


@pytest.mark.anyio
async def test_missing_binary_raises_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv(CLI_BINARY_ENV, "definitely-not-a-claude-xyz")
    model = ClaudeCliModel("sonnet")
    with pytest.raises(CliModelError) as exc:
        await model.request(_user("hi"), None, ModelRequestParameters())
    assert "claude CLI not found" in str(exc.value)


@pytest.mark.anyio
async def test_request_stream_yields_text_and_thinking_events(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"thinking": "let me see"}, {"text": "Hello!"}]]})
    events = []
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for ev in stream:
                events.append(ev)
            final = stream.get()
    finally:
        await model.aclose()
    thinking = "".join(
        ev.delta.content_delta for ev in events if isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, ThinkingPartDelta)
    )
    assert thinking == "let me see"
    assert "".join(getattr(p, "content", "") for p in final.parts if isinstance(p, TextPart)) == "Hello!"
    assert final.usage.input_tokens == 7


@pytest.mark.anyio
async def test_request_stream_pushes_tool_cards_and_keeps_response_text_only(tmp_path, monkeypatch):
    scenario = {
        "turns": [
            [
                {"text": "Looking."},
                {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a.py"}}},
                {"tool_result": {"id": "t1", "content": "print(1)"}},
                {"text": "Done."},
            ]
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)
    activity: list = []

    async def on_activity(events):
        activity.extend(events)

    model.on_activity = on_activity
    try:
        text = await _stream_text(model)
    finally:
        await model.aclose()
    assert text == "Looking.Done."
    calls = [e for e in activity if isinstance(e, FunctionToolCallEvent)]
    results = [e for e in activity if isinstance(e, FunctionToolResultEvent)]
    assert calls[0].part.tool_name == "read_file" and calls[0].part.args == {"path": "/a.py"}
    assert isinstance(results[0].part, ToolReturnPart) and results[0].part.content == "print(1)"


@pytest.mark.anyio
async def test_headless_stream_folds_tool_activity_into_text(tmp_path, monkeypatch):
    scenario = {"turns": [[{"tool_use": {"id": "t1", "name": "Bash", "input": {"command": "ls"}}}, {"text": "ok"}]]}
    model = _model(tmp_path, monkeypatch, scenario)
    try:
        text = await _stream_text(model)
    finally:
        await model.aclose()
    assert text == "▸ Bash ls\n\nok"


@pytest.mark.anyio
async def test_request_stream_routes_claude_subagents_to_side_channels(tmp_path, monkeypatch):
    scenario = {
        "turns": [
            [
                {
                    "raw": {
                        "type": "assistant",
                        "message": {
                            "id": "m1",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tsub",
                                    "name": "Agent",
                                    "input": {"description": "d", "subagent_type": "Explore", "prompt": "p"},
                                }
                            ],
                        },
                    }
                },
                {"raw": {"type": "system", "subtype": "task_started", "tool_use_id": "tsub"}},
                {
                    "raw": {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": "tsub", "content": "Async agent launched..."}
                            ]
                        },
                    }
                },
                {
                    "raw": {
                        "type": "stream_event",
                        "parent_tool_use_id": "tsub",
                        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "4"}},
                    }
                },
                {
                    "raw": {
                        "type": "assistant",
                        "parent_tool_use_id": "tsub",
                        "message": {
                            "id": "m2",
                            "model": "claude-haiku-4-5",
                            "usage": {"input_tokens": 3, "output_tokens": 2},
                            "content": [{"type": "text", "text": "4"}],
                        },
                    }
                },
                {
                    "raw": {
                        "type": "system",
                        "subtype": "task_notification",
                        "tool_use_id": "tsub",
                        "status": "completed",
                        "summary": "4",
                    }
                },
                {"text": "Four."},
            ]
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)
    activity: list = []
    sub_events: list = []
    sub_models: list = []

    async def on_activity(events):
        activity.extend(events)

    async def on_subagent(sid, event, usage):
        sub_events.append((sid, event, usage))

    async def on_subagent_model(sid, m):
        sub_models.append((sid, m))

    model.on_activity = on_activity
    model.on_subagent = on_subagent
    model.on_subagent_model = on_subagent_model
    try:
        text = await _stream_text(model)
    finally:
        await model.aclose()
    spawn_names = [e.part.tool_name for e in activity if hasattr(e, "part")]
    assert spawn_names.count("spawn_agent") == 2
    assert sub_events and all(sid == "tsub" for sid, _, _ in sub_events)
    assert any(u is not None and u.output_tokens == 2 for _, _, u in sub_events)
    assert ("tsub", "claude-haiku-4-5") in sub_models
    assert text == "Four."  # the child's "4" (delta AND block) never entered the main text


@pytest.mark.anyio
async def test_stream_abandon_interrupts_the_turn_and_keeps_the_process(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}, {"await_interrupt": True}], [{"text": "next"}]]})
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for _ in stream:
                break  # abandon mid-turn
        assert model._process.alive and not model._process.turn_open
        # the process is still usable: the next turn runs on it
        resp = await model.request(_user("hi") + [ModelRequest(parts=[UserPromptPart(content="more")])], None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "next"
    assert len(read_claude_argvs(tmp_path)) == 1
    assert any(m.get("type") == "control_request" and m["request"]["subtype"] == "interrupt" for m in read_claude_log(tmp_path))


@pytest.mark.anyio
async def test_request_cancel_interrupts_the_turn(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}, {"await_interrupt": True}]]})
    task = asyncio.ensure_future(model.request(_user("hi"), None, ModelRequestParameters()))
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert not model._process.turn_open
    finally:
        await model.aclose()


@pytest.mark.anyio
async def test_steer_folds_into_the_open_turn(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}, {"await_user": True}]]})
    assert model.steer("early") is False  # no turn open yet
    texts: list[str] = []
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for ev in stream:
                if isinstance(ev, PartDeltaEvent) and not isinstance(ev.delta, ThinkingPartDelta):
                    texts.append(ev.delta.content_delta)
                if "".join(texts) == "a" and len(texts) == 1:
                    assert model.steer("go left") is True
    finally:
        await model.aclose()
    assert "".join(texts) == "aheard: go left"


@pytest.mark.anyio
async def test_approval_prompts_go_through_the_broker(tmp_path, monkeypatch):
    outside = tmp_path.parent / "elsewhere.txt"
    step = {"can_use_tool": {"tool_name": "Write", "input": {"file_path": str(outside), "content": "x"}}}
    model = _model(tmp_path, monkeypatch, {"turns": [[step]]})
    seen: list = []

    async def approve(call):
        seen.append(call)
        return True

    model.request_approval = approve
    try:
        resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "Write done"
    assert seen[0].tool_name == "write_file" and seen[0].args["reason"].startswith("outside workspace")


@pytest.mark.anyio
async def test_plan_mode_denies_writes_with_the_wire_message(tmp_path, monkeypatch):
    step = {"can_use_tool": {"tool_name": "Write", "input": {"file_path": str(tmp_path / "a"), "content": "x"}}}
    model = _model(tmp_path, monkeypatch, {"turns": [[step]]})
    model.mode_getter = lambda: "plan"
    try:
        resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "denied: plan mode: read-only — describe the change instead of making it"
    log = read_claude_log(tmp_path)
    denials = [m for m in log if m.get("type") == "control_response"]
    assert denials and denials[0]["response"]["response"]["behavior"] == "deny"


@pytest.mark.anyio
async def test_old_claude_version_warns_once(tmp_path, monkeypatch, caplog):
    import marim_harness.config.claude_cli_model as mod

    monkeypatch.setattr(mod, "_version_warned", False)
    model = _model(tmp_path, monkeypatch, {"version": "1.0.99", "turns": [[{"text": "a"}]]})
    try:
        with caplog.at_level("WARNING"):
            await model.request(_user("hi"), None, ModelRequestParameters())
            await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert caplog.text.count("1.0.99") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_claude_cli_model.py -q 2>&1 | tail -5`
Expected: ImportError on `SESSION_REF_PREFIX`/`InitChunk`/`ThinkingChunk` (the module still has the old API).

- [ ] **Step 3: Rewrite the module**

Replace the module docstring and everything from the `_MODE_MAP` comment through `flatten_history`'s preceding helpers **only where named below**; the retained helpers listed above stay. The new module, top to bottom (retained definitions marked `# (unchanged)` — keep the existing bodies):

```python
"""Run Claude Code as a main-loop model provider over one long-lived,
bidirectional ``claude`` process.

A Claude subscription is reachable only through the ``claude`` CLI, which runs
its own agentic loop — there is no raw per-step model endpoint behind it. So
this provider makes marim a *launcher*: ``ClaudeCliModel`` keeps one ``claude``
process per conversation (``claude/process.py``), sends each user turn down its
stdin as ``stream-json``, and returns a single **text-only** ``ModelResponse``.
Emitting ``ToolCallPart``s here would make pydantic_ai's agent graph try to
execute Claude's tool calls a second time, so Claude's tool activity is folded
into the streamed text (headless) or pushed out-of-band as native tool cards
(``on_activity``). Claude's own Agent/Task sub-agents are split off via
``cli_demux.CliSubagentDemux`` onto ``on_subagent``.

What the bidirectional transport buys over the old one-shot ``claude -p``:
Claude asks marim before every tool through ``can_use_tool`` control requests,
so marim's ``auto``/``ask``/``plan`` modes, the approval panel and ``ask_user``
apply (``claude/approvals.py``); a steer folds into the live turn; an
interrupt is a control request rather than a kill; and the conversation
resumes by session id after an idle close, a crash, or a marim restart.

Prose and thinking arrive as ``stream_event`` deltas; ``assistant`` objects
contribute only their ``tool_use`` blocks (their text repeats the deltas);
``user`` objects contribute ``tool_result`` blocks. See ``consume_cli_stream``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.usage import RequestUsage

from ..claude.approvals import ClaudeApprovalBroker
from ..claude.env import (
    INSTALL_HINT,
    MIN_CLAUDE_VERSION,
    cli_idle_timeout,
    cli_timeout,
    resolve_cli_binary,
)
from ..claude.process import (
    ClaudeProcess,
    ProcessOptions,
    TurnHandle,
    next_turn_object,
    turn_objects,
)
from ..claude.protocol import CLOSED
from ..runtime.permissions import Mode, UiSeams
from ..usage import COST_DETAIL_KEY
from .external_cli import CliModelError, ExternalCliModel, TextFolder

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from pydantic_ai.messages import ModelMessage, ModelRequest
    from pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

# The provider-side conversation reference persisted on the marim session
# (SessionStore.cli_thread_id) is namespaced so a switch to another external
# CLI never resumes a foreign id.
SESSION_REF_PREFIX = "claude-cli:"


def _part_text(content) -> str:  # (unchanged)
    ...


def latest_user_text(messages: list[ModelMessage]) -> str:  # (unchanged)
    ...


def extract_system(messages: list[ModelMessage]) -> str:  # (unchanged)
    ...


def _render_tool_args(args) -> str:  # (unchanged)
    ...


def _request_lines(msg: ModelRequest) -> list[str]:  # (unchanged)
    ...


def _response_lines(msg: ModelResponse) -> list[str]:  # (unchanged)
    ...


def flatten_history(messages: list[ModelMessage]) -> str:  # (unchanged)
    ...


def request_usage_from_cli(cli_usage: dict | None, total_cost_usd: float | None) -> RequestUsage:  # (unchanged)
    ...


_ACTIVITY_ARG = {...}  # (unchanged)
_ACTIVITY_MARKER = "▸"  # (unchanged)


def format_activity_line(name: str, tool_input: dict) -> str:  # (unchanged)
    ...


@dataclass
class TextChunk:
    """A run of assistant prose (one ``text_delta``)."""

    delta: str


@dataclass
class ThinkingChunk:
    """A run of Claude's thinking (one ``thinking_delta``); rendered as a
    thinking part so the TUI can fold it like a native model's."""

    delta: str


@dataclass
class ToolUseChunk:  # (unchanged)
    name: str
    tool_input: dict
    call_id: str


@dataclass
class ToolResultChunk:  # (unchanged)
    call_id: str
    content: str
    is_error: bool


@dataclass
class InitChunk:
    """The turn's ``system/init``: the session id (the resume key, persisted
    as the session ref) and the CLI version (checked against
    ``MIN_CLAUDE_VERSION`` once)."""

    session_id: str | None
    version: str
    model: str


@dataclass
class DoneChunk:
    """Terminal chunk: Claude's session id, usage, and whether a proper
    ``result`` was seen. ``complete=False`` ⇒ the turn failed (``error_detail``
    says why: the CLI's exit code and stderr tail, or the result's error
    subtype). ``aborted`` marks an interrupted turn that still ended cleanly
    with an aborted result (steer/Ctrl-C) — complete, just cut short."""

    session_id: str | None
    usage: RequestUsage
    complete: bool
    error_detail: str = ""
    aborted: bool = False


def _flatten_result_content(content) -> str:  # (unchanged)
    ...


def _is_subagent_noise(obj: dict) -> bool:  # (unchanged)
    ...


def _delta_chunk(obj: dict) -> TextChunk | ThinkingChunk | None:
    """The chunk for one ``stream_event``, or None for the ones that carry no
    text (block starts/stops, signature deltas, message deltas)."""
    event = obj.get("event") or {}
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    kind = delta.get("type")
    if kind == "text_delta":
        text = delta.get("text") or ""
        return TextChunk(text) if text else None
    if kind == "thinking_delta":
        thinking = delta.get("thinking") or ""
        return ThinkingChunk(thinking) if thinking else None
    return None


def _tool_use_chunks(obj: dict) -> Iterator[ToolUseChunk]:
    """The ``tool_use`` blocks of one ``assistant`` object. Its text/thinking
    blocks are skipped: the deltas already carried them."""
    for block in (obj.get("message") or {}).get("content") or []:
        if block.get("type") == "tool_use":
            yield ToolUseChunk(
                name=block.get("name", "tool"),
                tool_input=block.get("input") or {},
                call_id=block.get("id", ""),
            )


def _user_chunks(obj: dict) -> Iterator:  # (unchanged)
    ...


def _result_error_subtype(obj: dict) -> str | None:  # (unchanged)
    ...


# terminal_reason values of the aborted result an ``interrupt`` control request
# produces (probe s7): a normal, if truncated, end of turn — not a failure.
_ABORTED_REASONS = frozenset({"aborted_tools", "aborted_streaming"})


def _result_chunk(obj: dict, *, produced_text: bool) -> DoneChunk:
    """The ``DoneChunk`` for a turn's ``result``.

    An *errored* result must not masquerade as a clean turn, but an interrupted
    one is not an error: ``is_error`` with an aborted ``terminal_reason`` is how
    the CLI ends a turn marim itself cut short. Otherwise the pre-existing
    policy holds — log the failure subtype; KEEP any prose already streamed
    (partial output beats none) by leaving ``complete=True`` with the error in
    ``error_detail``; with NO usable text the turn is a bare failure and
    ``complete=False`` makes the model raise ``CliModelError``."""
    usage = request_usage_from_cli(obj.get("usage"), obj.get("total_cost_usd"))
    session_id = obj.get("session_id")
    error = _result_error_subtype(obj)
    if error is None:
        return DoneChunk(session_id=session_id, usage=usage, complete=True)
    if obj.get("terminal_reason") in _ABORTED_REASONS:
        return DoneChunk(session_id=session_id, usage=usage, complete=True, aborted=True)
    logger.warning("claude CLI result reported an error: %s", error)
    return DoneChunk(
        session_id=session_id,
        usage=usage,
        complete=produced_text,
        error_detail=f"CLI result error: {error}",
    )


def _closed_detail(obj: dict) -> str:
    """The failure text for a turn that ended with the process closing: the
    exit code plus the stderr tail the process captured (a stack tail, "Invalid
    API key", "No conversation found with session ID …")."""
    stderr = str(obj.get("stderr") or "").strip()
    return f"claude exited (code {obj.get('returncode')}): {stderr}"


async def consume_cli_stream(objs: AsyncIterator[dict]) -> AsyncGenerator:
    """One turn's stream-json objects → structured chunks, ending with exactly
    one ``DoneChunk``.

    Prose/thinking come from ``stream_event`` deltas (``TextChunk`` /
    ``ThinkingChunk``); ``assistant`` objects add ``ToolUseChunk``s; ``user``
    objects add ``ToolResultChunk``s (``isReplay`` echoes of our own messages
    are dropped by ``_user_chunks``' caller); ``system/init`` becomes an
    ``InitChunk``; every other ``system`` subtype (status, thinking_tokens, the
    sub-agent lifecycle noise) is skipped. Objects tagged ``parent_tool_use_id``
    belong to a Claude-side sub-agent: with a UI the demux tee consumed them
    before we see them; headless they are dropped here so a child's prose never
    leaks into the main text.

    The turn ends at its ``result`` (one per turn on the bidirectional
    transport — the process layer closes the turn there) or at the synthetic
    ``CLOSED`` object the process publishes when the CLI exits mid-turn."""
    produced_text = False
    async for obj in objs:
        if _is_subagent_noise(obj):
            continue
        kind = obj.get("type")
        if kind == CLOSED:
            yield DoneChunk(
                session_id=None, usage=RequestUsage(), complete=False, error_detail=_closed_detail(obj)
            )
            return
        if kind == "system":
            if obj.get("subtype") == "init":
                yield InitChunk(
                    session_id=obj.get("session_id") or None,
                    version=str(obj.get("claude_code_version") or ""),
                    model=str(obj.get("model") or ""),
                )
        elif kind == "stream_event":
            chunk = _delta_chunk(obj)
            if chunk is not None:
                produced_text = produced_text or isinstance(chunk, TextChunk)
                yield chunk
        elif kind == "assistant":
            for tool_chunk in _tool_use_chunks(obj):
                yield tool_chunk
        elif kind == "user" and not obj.get("isReplay"):
            for result_chunk in _user_chunks(obj):
                yield result_chunk
        elif kind == "result":
            yield _result_chunk(obj, produced_text=produced_text)
            return
    yield DoneChunk(session_id=None, usage=RequestUsage(), complete=False)


def fold_chunk_text(chunk, *, leading: bool) -> str:  # (unchanged)
    ...


def cli_activity_events(chunk) -> list:  # (unchanged)
    ...


def _no_result_message(done: DoneChunk | None) -> str:  # (unchanged)
    ...


def _version_tuple(version: str) -> tuple[int, ...]:
    """``"2.1.261"`` → ``(2, 1, 261)``; non-numeric segments end the tuple so a
    ``2.1.0-beta`` compares as ``(2, 1, 0)``."""
    out: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


_version_warned = False


def note_old_version_once(version: str) -> None:
    """Warn once per process when the CLI predates the protocol this provider
    was built against (``MIN_CLAUDE_VERSION``). The turn still runs — the check
    is a hint for the "why does approval never prompt?" support question, not
    a gate (spec §Version check)."""
    global _version_warned
    if _version_warned or not version:
        return
    if _version_tuple(version) < _version_tuple(MIN_CLAUDE_VERSION):
        _version_warned = True
        logger.warning(
            "claude %s is older than %s; marim's approval/steer integration may not work. "
            "Update Claude Code (`claude update`).",
            version,
            MIN_CLAUDE_VERSION,
        )


def _is_missing_session(obj: dict) -> bool:
    """True when a ``--resume`` start died because the CLI no longer has the
    session (probe s8: stderr ``No conversation found with session ID: …``,
    exit 1) — the one CLOSED the model recovers from by starting fresh."""
    return obj.get("type") == CLOSED and "No conversation found" in str(obj.get("stderr") or "")


class ClaudeCliModel(ExternalCliModel):
    """A Pydantic AI model backed by one long-lived ``claude`` process.

    The late-bound seams (``mode_getter``, ``cwd``, ``request_approval``,
    ``ask_user``, ``on_activity``, ``on_subagent*``, ``scratchpad_getter``,
    ``session_ref_getter``/``on_session_ref``) live on ``ExternalCliModel`` and
    are bound by ``Harness.wire_cli_model``. The approval broker snapshots the
    UI seams when the process starts; ``bind_ui`` after the first turn is
    picked up by the next process (a documented residual)."""

    provider_id = "claude-cli"

    def __init__(self, model_id: str | None, *, ephemeral: bool = False) -> None:
        super().__init__()
        self._model_id = model_id
        # See ExternalCliModel.ephemeral / ``ephemeral_clone``: aux agents never
        # resume or store a session, so they can't hijack the user's live one.
        self.ephemeral = ephemeral
        self._process: ClaudeProcess | None = None
        self._broker: ClaudeApprovalBroker | None = None

    def ephemeral_clone(self, *, cwd: str) -> ClaudeCliModel:
        """A stateless, read-only copy for one-shot aux agents (titler/summarizer).

        It never resumes or stores a Claude session — so titling/summarizing can't
        continue or hijack the user's live conversation — always sends its own
        instructions, runs in plan (read-only) mode so it can't edit files, and
        closes its process after every call."""
        clone = ClaudeCliModel(self._model_id, ephemeral=True)
        clone.cwd = cwd
        clone.mode_getter = lambda: "plan"
        return clone

    @property
    def model_name(self) -> str:
        return self._model_id or "default"

    @property
    def session_id(self) -> str | None:
        """The live process's session id, else the persisted one (the key the
        next process resumes with)."""
        if self._process is not None and self._process.session_id:
            return self._process.session_id
        return self._persisted_session_id()

    # --- collaborators ------------------------------------------------------------
    def _mode(self) -> Mode:
        raw = self.mode_getter() if self.mode_getter is not None else "plan"
        try:
            return Mode(raw)
        except ValueError:
            return Mode.plan

    def _scratchpad(self) -> Path | None:
        return self.scratchpad_getter() if self.scratchpad_getter is not None else None

    def _make_broker(self) -> ClaudeApprovalBroker:
        return ClaudeApprovalBroker(
            mode_getter=self._mode,
            workspace_root=Path(self.cwd),
            scratchpad_getter=self._scratchpad,
            ui=UiSeams(request_approval=self.request_approval, ask_user=self.ask_user),
        )

    def _persisted_session_id(self) -> str | None:
        if self.ephemeral or self.session_ref_getter is None:
            return None
        ref = self.session_ref_getter()
        if not ref or not ref.startswith(SESSION_REF_PREFIX):
            return None  # another provider's ref (e.g. codex-cli) — ignore
        return ref[len(SESSION_REF_PREFIX) :] or None

    def _options(self, *, resume_id: str | None, system: str | None) -> ProcessOptions:
        binary = resolve_cli_binary()
        if binary is None:
            raise CliModelError(f"claude CLI not found. {INSTALL_HINT}")
        return ProcessOptions(
            binary=binary,
            cwd=self.cwd,
            model=self._model_id or None,
            resume_id=resume_id,
            append_system=system,
            persist=not self.ephemeral,
        )

    # --- process lifecycle --------------------------------------------------------
    async def _spawn(self, *, resume_id: str | None, system: str | None) -> ClaudeProcess:
        self._broker = self._make_broker()
        process = ClaudeProcess(
            self._options(resume_id=resume_id, system=system),
            on_request=self._broker.handle,
            silence_timeout=cli_timeout(),
            # Aux clones close after every call, so they never idle.
            idle_timeout=0.0 if self.ephemeral else cli_idle_timeout(),
        )
        await process.start()
        self._process = process
        return process

    async def _ensure_process(self, messages: list) -> tuple[ClaudeProcess, bool]:
        """The process to run this turn on and whether it RESUMES a Claude
        session (so the turn sends only the newest user text). Order: the live
        process; a respawn on a dead process's session id (idle close, crash,
        an ignored interrupt); the persisted session ref; a cold start carrying
        the flattened history and the system prompt."""
        process = self._process
        if process is not None and process.alive:
            return process, True
        resume_id = process.session_id if process is not None else self._persisted_session_id()
        if resume_id:
            return await self._spawn(resume_id=resume_id, system=None), True
        return await self._spawn(resume_id=None, system=extract_system(messages) or None), False

    async def _start_turn(self, messages: list) -> tuple[ClaudeProcess, TurnHandle, dict]:
        """Send the turn and pull its first object, so a resume of a session the
        CLI no longer has (probe s8) is caught here and retried as a cold start
        — the caller then streams the rest uniformly."""
        process, resumed = await self._ensure_process(messages)
        text = latest_user_text(messages) if resumed else flatten_history(messages)
        handle = await process.send_turn(text)
        first = await next_turn_object(process, handle)
        if resumed and _is_missing_session(first):
            logger.warning(
                "claude session %s is gone; starting a fresh one from the flattened history",
                process.session_id,
            )
            await process.aclose()
            process = await self._spawn(resume_id=None, system=extract_system(messages) or None)
            handle = await process.send_turn(flatten_history(messages))
            first = await next_turn_object(process, handle)
        return process, handle, first

    async def _after_turn(self, process: ClaudeProcess, handle: TurnHandle) -> None:
        """Every exit path of a turn: a turn still open (the consumer abandoned
        or cancelled the stream) is interrupted so Claude stops working on an
        answer nobody reads; an ephemeral clone's process is closed outright."""
        if handle.open and process.alive:
            await process.interrupt(handle)
        if self.ephemeral:
            await process.aclose()
            self._process = None

    def _note_init(self, chunk: InitChunk) -> None:
        note_old_version_once(chunk.version)
        if chunk.session_id and not self.ephemeral and self.on_session_ref is not None:
            self.on_session_ref(SESSION_REF_PREFIX + chunk.session_id)

    # --- pydantic-ai entry points --------------------------------------------------
    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        process, handle, first = await self._start_turn(messages)
        done: DoneChunk | None = None
        parts: list[str] = []  # assistant prose + folded ▸ tool lines (no UI here)
        objs = turn_objects(process, handle, first)
        try:
            async with aclosing(consume_cli_stream(objs)) as stream:
                async for chunk in stream:
                    if isinstance(chunk, InitChunk):
                        self._note_init(chunk)
                    elif isinstance(chunk, DoneChunk):
                        done = chunk
                    else:
                        segment = fold_chunk_text(chunk, leading=not parts)
                        if segment:
                            parts.append(segment)
        finally:
            await objs.aclose()
            await self._after_turn(process, handle)
        if done is None or not done.complete:
            raise CliModelError(_no_result_message(done))
        return ModelResponse(
            parts=[TextPart(content="".join(parts))],
            model_name=self.model_name,
            # Stamp each response with the current time — not a shared
            # construction-time value — so a multi-turn history doesn't carry
            # identical, stale timestamps across every ModelResponse.
            timestamp=datetime.now(tz=timezone.utc),
            usage=done.usage,
            provider_name="claude-cli",
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncGenerator[StreamedResponse]:
        process, handle, first = await self._start_turn(messages)
        objs = turn_objects(process, handle, first)
        stream = ClaudeCliStreamedResponse(
            model_request_parameters=model_request_parameters,
            _objs=objs,
            _model_id=self.model_name,
            # Per-response timestamp (see request()): stamped when the stream is
            # opened, not once at model construction.
            _ts=datetime.now(tz=timezone.utc),
            _on_init=self._note_init,
            _on_activity=self.on_activity,
            _on_subagent=self.on_subagent,
            _on_subagent_model=self.on_subagent_model,
        )
        try:
            yield stream
        finally:
            # Deterministic on every exit path — normal completion, error, or
            # Ctrl-C mid-turn: finish the turn iterator, then interrupt the turn
            # if it is still open (the process itself stays for the next turn).
            await objs.aclose()
            await self._after_turn(process, handle)

    # --- live controls -------------------------------------------------------------
    def steer(self, text: str) -> bool:
        """Fold ``text`` into the open turn (a mid-turn user message — probe s6).
        Fire-and-forget on the running loop: the harness calls this
        synchronously from the input path. False (harness keeps buffering) when
        no turn is open."""
        process = self._process
        if process is None or not process.turn_open:
            return False
        task = asyncio.get_running_loop().create_task(process.send_user(text))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return True

    async def aclose(self) -> None:
        """Close the process. The session id survives on it, so a later turn on
        this model resumes; ``Harness.set_model`` calls this on the outgoing
        model and ``Harness.aclose`` on teardown."""
        if self._process is not None:
            await self._process.aclose()


# Moved to config/external_cli.py (shared with codex-cli); the old name stays
# importable for tests that reach for it.
_TextFolder = TextFolder


class _ThinkingParts:
    """Vendor-part-id bookkeeping for thinking deltas: one thinking part per
    contiguous run, a fresh id once prose or a tool card intervened — the same
    interleaving rule ``TextFolder`` applies to text."""

    def __init__(self, parts_manager) -> None:
        self._parts_manager = parts_manager
        self._n = 0
        self._open = False

    def close(self) -> None:
        self._open = False

    def emit(self, delta: str):
        if not self._open:
            self._n += 1
            self._open = True
        return self._parts_manager.handle_thinking_delta(
            vendor_part_id=f"think-{self._n}", content=delta
        )


@dataclass
class ClaudeCliStreamedResponse(StreamedResponse):
    """Streams ``consume_cli_stream`` output as text/thinking-delta events plus
    out-of-band tool cards (``_on_activity``), folding ``▸`` lines instead when
    no UI is bound."""

    _objs: AsyncIterator[dict] | None = None
    _model_id: str = "default"
    _ts: datetime | None = None
    _on_init: Callable[[InitChunk], None] | None = None
    _on_activity: Callable[[list], Awaitable[None]] | None = None
    _on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
    _on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None

    async def _demuxed_objs(self) -> AsyncIterator[dict]:
        """Tee the raw stream through a CliSubagentDemux: Claude-side sub-agent
        traffic is delivered out-of-band (the synthesized spawn_agent call/
        return via _on_activity — the top-level sink claims those and builds
        the live card — and child events via _on_subagent, keyed by the spawn's
        tool_use id); everything else flows on to the chunk pipeline.

        ``stream_event`` objects bypass the demux: it only knows whole
        assistant/user messages. The main turn's deltas pass straight through;
        a child's (tagged ``parent_tool_use_id``) are dropped — the child's
        whole assistant message reaches its card via the demux anyway."""
        from ..subagents.cli_demux import CliSubagentDemux

        demux = CliSubagentDemux()
        assert self._objs is not None
        async for obj in self._objs:
            if obj.get("type") == "stream_event":
                if not obj.get("parent_tool_use_id"):
                    yield obj
                continue
            routed, remainder = demux.route(obj)
            for r in routed:
                if r.stream_id is None:
                    if self._on_activity is not None:
                        await self._on_activity([r.event])
                elif self._on_subagent is not None:
                    if r.model and self._on_subagent_model is not None:
                        await self._on_subagent_model(r.stream_id, r.model)
                    await self._on_subagent(r.stream_id, r.event, r.usage)
            if remainder is not None:
                yield remainder

    def _finalize_done(self, done: DoneChunk | None) -> None:
        """Mirror ``request()``: a stream that ends without a proper ``result``
        (Claude died / produced no result) is a FAILED turn — raise so the
        harness flushes its resumable baseline (clean failure)."""
        if done is None or not done.complete:
            raise CliModelError(_no_result_message(done))
        self._usage = done.usage
        self._finished = True

    async def _events_for(self, chunk, folder: TextFolder, thinking: _ThinkingParts):
        """The pydantic-ai events for one non-terminal chunk."""
        if isinstance(chunk, TextChunk):
            thinking.close()
            async for ev in folder.emit_text(chunk.delta):
                yield ev
        elif isinstance(chunk, ThinkingChunk):
            for ev in thinking.emit(chunk.delta):
                yield ev
        elif isinstance(chunk, (ToolUseChunk, ToolResultChunk)):
            thinking.close()
            async for ev in folder.emit_tool(chunk):
                yield ev
        elif isinstance(chunk, InitChunk) and self._on_init is not None:
            self._on_init(chunk)

    async def _get_event_iterator(self):
        if self._objs is None:
            return
        # The demux tee is active only when the sub-agent side-channel is wired
        # (a UI is bound); headless keeps the cheap filter-only path in
        # consume_cli_stream (Claude-side child traffic is simply dropped there).
        objs = self._demuxed_objs() if self._on_subagent is not None else self._objs
        folder = TextFolder(
            self._parts_manager,
            self._on_activity,
            activity_events=cli_activity_events,
            fold_text=lambda chunk, leading: fold_chunk_text(chunk, leading=leading),
            is_call=lambda chunk: isinstance(chunk, ToolUseChunk),
        )
        thinking = _ThinkingParts(self._parts_manager)
        done: DoneChunk | None = None
        # aclosing() so an abandoned/cancelled consumer finalizes the chunk
        # pipeline (and, transitively, the demux wrapper) rather than leaking it
        # to GC. The turn itself is interrupted by request_stream's finally.
        async with aclosing(consume_cli_stream(objs)) as stream:
            async for chunk in stream:
                if isinstance(chunk, DoneChunk):
                    done = chunk
                    continue
                async for ev in self._events_for(chunk, folder, thinking):
                    yield ev
        self._finalize_done(done)

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def timestamp(self) -> datetime:
        return self._ts or datetime.now(tz=timezone.utc)

    @property
    def provider_name(self) -> str:
        return "claude-cli"

    @property
    def provider_url(self) -> str:
        return "https://claude.com/claude-code"
```

`json` stays imported only if `_render_tool_args` (unchanged) still uses it — it does. Remove `contextlib` (no longer used).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_claude_cli_model.py -v 2>&1 | tail -60`
Expected: all pass. Likely first failures and their fixes:
- `test_steer_folds_into_the_open_turn` hangs → the fake's `await_user` step needs the steer to arrive while the turn is open; the assertion `model.steer(...) is True` runs inside the stream loop, so it is. If `heard:` never arrives, check that `ClaudeProcess.send_user` writes the same `{"type":"user",...}` envelope as `send_turn` (Task 5 `_client.user`).
- `test_request_cancel_interrupts_the_turn`: `turn_objects` interrupts on `CancelledError` and `_after_turn` sees the handle closed; if `turn_open` is still True after the sleep, the interrupt's aborted result was not delivered — check `TurnHandle.finish()` is called from `_on_event` on `result`.
- Thinking events: `ThinkingPartDelta.content_delta` is the field name in pydantic-ai ≥ 1.0; if the import fails, check `uv run python -c "from pydantic_ai.messages import ThinkingPartDelta; print(ThinkingPartDelta.__dataclass_fields__.keys())"`.

- [ ] **Step 5: Run the wider suite for regressions in the model's other consumers**

Run: `uv run pytest --no-cov -q tests/test_claude_cli_model.py tests/test_external_cli.py tests/test_session_ctrl.py tests/test_bootstrap.py tests/test_model_config.py 2>&1 | tail -15`
Expected: green except `tests/test_bootstrap.py::test_build_harness_wires_claude_cli_mode_getter` / `..._default_model_is_blank_not_none` if they still monkeypatch `marim_harness.subagents.cli_backend.resolve_cli_binary` — those construct the model without running a turn, so the patch is harmless either way; leave them. (File names that do not exist are simply omitted from the command.)

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check --fix src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py && uv run ruff format src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py && uv run pyright
git add src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py
git commit -m "feat(claude-cli): drive one long-lived bidirectional claude per conversation

Turns go down stdin as stream-json; approval, ask_user, steer and interrupt
ride the control protocol; the session resumes by id after idle/crash/restart.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Harness lifecycle — close the outgoing/discarded `ExternalCliModel`

**Files:**
- Modify: `src/marim_harness/config/external_cli.py:58-62` (comment) and after `compact_remote` (new `aclose`)
- Modify: `src/marim_harness/runtime/harness.py:984-1021` (`set_model`, `wire_cli_model` docstring) and `:1222-1240` (`aclose`)
- Test: `tests/test_harness_model_switch.py` (new)

**Interfaces:**
- Consumes: `ClaudeCliModel` / `ExternalCliModel` (Task 7), `Harness.set_model`, `Harness.aclose`, `Harness.wire_cli_model`.
- Produces: `ExternalCliModel.aclose()` (async, no-op base, overridden by `ClaudeCliModel` and already by `CodexCliModel`); `Harness._close_model_later(old: Model) -> None`.

Why: `ClaudeCliModel` now holds a live subprocess. Before this task a `/model` switch away from claude-cli dropped the model object and leaked the process until the idle reaper (up to 10 min); harness teardown closed only codex. The spec (§Model change) says the harness schedules `aclose()` on the outgoing model.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_harness_model_switch.py`:

```python
"""Harness lifecycle for external-CLI models: a model switch closes the
outgoing model, teardown closes the current one."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.function import FunctionModel

from marim_harness.config.external_cli import ExternalCliModel
from tests.conftest import _make_deps, _make_harness


class _Closable(ExternalCliModel):
    provider_id = "fake-cli"

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    @property
    def model_name(self) -> str:
        return "fake"

    async def request(self, *a, **k):  # pragma: no cover - never driven here
        raise NotImplementedError

    async def aclose(self) -> None:
        self.closed += 1


class _Source:
    """A ModelSource stand-in: `build` hands out prebuilt models by id."""

    def __init__(self, models: dict) -> None:
        self._models = models

    def build(self, model_id: str):
        return self._models[model_id]

    def label(self, model_id: str) -> str:
        return model_id


def _dummy() -> FunctionModel:
    async def fn(messages, info):  # pragma: no cover
        raise NotImplementedError

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_set_model_closes_the_outgoing_external_model(tmp_path):
    old = _Closable()
    new = _dummy()
    h = _make_harness(old, _make_deps(tmp_path))
    h.model_source = _Source({"old": old, "new": new})
    h.set_model("new", persist=False)
    await asyncio.sleep(0)  # the close is scheduled, not awaited inline
    assert old.closed == 1
    assert h.current_model is new


@pytest.mark.anyio
async def test_set_model_to_the_same_object_does_not_close_it(tmp_path):
    same = _Closable()
    h = _make_harness(same, _make_deps(tmp_path))
    h.model_source = _Source({"same": same})
    h.set_model("same", persist=False)
    await asyncio.sleep(0)
    assert same.closed == 0


@pytest.mark.anyio
async def test_harness_aclose_closes_any_external_model(tmp_path):
    model = _Closable()
    h = _make_harness(model, _make_deps(tmp_path))
    await h.aclose()
    assert model.closed == 1


def test_base_aclose_is_a_no_op():
    class _Plain(ExternalCliModel):
        provider_id = "plain"

        @property
        def model_name(self) -> str:
            return "p"

        async def request(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

    asyncio.run(_Plain().aclose())
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --no-cov tests/test_harness_model_switch.py -q 2>&1 | tail -8`
Expected: `test_set_model_closes_the_outgoing_external_model` and `test_harness_aclose_closes_any_external_model` fail (`closed == 0`); `test_base_aclose_is_a_no_op` fails with `AttributeError: aclose` (or passes if `ExternalCliModel` already has one — then leave it).

- [ ] **Step 3: `ExternalCliModel`: fix the comment, add the base `aclose`**

In `src/marim_harness/config/external_cli.py` replace the three comment lines above `self.request_approval`:

```python
        # Interactive gating (Deps.ui.request_approval / ask_user). Both
        # external CLIs broker their tool-permission requests through them:
        # codex-cli its server-side approval requests, claude-cli the
        # `can_use_tool` control requests of its long-lived process.
```

After `compact_remote` add:

```python
    async def aclose(self) -> None:
        """Release whatever the provider holds open (a subprocess, a server
        thread). Called by the harness when the model is switched away from
        and at teardown. The base does nothing."""
        return None
```

- [ ] **Step 4: Harness: schedule the close on switch, close any external model at teardown**

In `src/marim_harness/runtime/harness.py`, `set_model`: change the first lines after the `model_source` guard to keep the outgoing model, and add the scheduling at the end:

```python
        model = self.model_source.build(model_id)
        old = self.current_model
        self.current_model = model
```
…(existing body unchanged)…
```python
        # Re-wire the late-bound hooks if the new model is an ExternalCliModel,
        # so switching TO such a provider at runtime honors live /mode, the
        # workspace cwd, and the TUI side-channels.
        self.wire_cli_model(model)
        # The outgoing model may hold a live `claude` process / codex thread:
        # release it now rather than when its idle reaper fires. Scheduled, not
        # awaited — set_model is sync (called from the TUI's command path).
        if old is not model:
            self._close_model_later(old)

    def _close_model_later(self, old: Model) -> None:
        if not isinstance(old, ExternalCliModel):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a sync caller before the app starts): nothing to close yet
        task = loop.create_task(old.aclose())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
```

`harness.py` does not import `asyncio` yet: add `import asyncio` to its stdlib import block (ruff's isort will place it).

In `wire_cli_model`'s docstring replace `(request_approval/ask_user — brokered by codex-cli, unused by claude-cli)` with `(request_approval/ask_user — both CLIs broker their tool-permission prompts through them)`.

In `aclose`, replace the codex-specific block (from `# The process-wide codex app-server …` through `await self.current_model.aclose()`) with:

```python
            # An external-CLI model holds provider-side state — claude-cli a
            # long-lived `claude` subprocess, codex-cli this harness's thread on
            # the process-wide app-server (`marim serve` holds many SessionHosts
            # over one app-server, so codex drops only ITS thread and lets the
            # server close itself once nothing else is registered — see
            # `CodexCliModel.aclose`/`close_shared_server_if_idle`). A no-op for
            # every other provider.
            if isinstance(self.current_model, ExternalCliModel):
                await self.current_model.aclose()
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest --no-cov tests/test_harness_model_switch.py tests/test_codex_cli_model.py tests/test_harness.py -q 2>&1 | tail -8`
Expected: green. (`tests/test_codex_cli_model.py`'s harness-aclose test, if any, still passes: `CodexCliModel` is an `ExternalCliModel`.)

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check --fix src/marim_harness/config/external_cli.py src/marim_harness/runtime/harness.py tests/test_harness_model_switch.py && uv run ruff format src/marim_harness/config/external_cli.py src/marim_harness/runtime/harness.py tests/test_harness_model_switch.py && uv run pyright
git add src/marim_harness/config/external_cli.py src/marim_harness/runtime/harness.py tests/test_harness_model_switch.py
git commit -m "feat(harness): close the outgoing external-CLI model on switch and at teardown

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: `backend: claude-cli` spawns on `ClaudeProcess`

**Files:**
- Modify: `src/marim_harness/subagents/cli_backend.py` (header docstring; delete `cli_permission_mode`, `build_cli_argv`, `_POST_LOOP_GRACE`, `_iter_ndjson_lines`, `_read_next_line`, `_kill_process_group`, `ClaudeCliRunner._process_line`, `_drain_stderr`, `_reap`; rewrite `ClaudeCliRunner.run`, `_finalize`; add `_consume`; `_RunState` gains `closed_detail`)
- Modify: `src/marim_harness/subagents/cli_spawn.py:202-270` (`run_cli`)
- Modify tests: `tests/test_cli_backend.py`, `tests/test_subagents_cli.py`, `tests/test_subagent_cli_spawn.py`, `tests/test_subagent_plan_egress.py`

**Interfaces:**
- Consumes: `ClaudeProcess`, `ProcessOptions`, `turn_objects` (Task 5); `ClaudeApprovalBroker` (Task 6); `UiSeams` (Task 2); `fake_claude_bin`, `read_claude_argv`, `read_claude_log` (Task 4); `cli_timeout` re-exported as `_cli_timeout` (Task 1).
- Produces: `ClaudeCliRunner.run(*, binary, prompt, system_prompt, cwd, allowed_tools, model, stream_id, disallowed_tools=None, checkpoint=None, resume_session_id=None, broker=None) -> CliResult` — `allow_gated` is GONE (the broker decides per tool); `CliRunError` messages: `"claude produced no result (<detail>)"` and `"claude timed out after Ns of silence (interrupted)"` (wrapped from `CliModelError`).

Behavior (spec §Sub-agent backend): one `ClaudeProcess` per spawn, closed when the spawn ends. `--append-system-prompt` on a fresh spawn, omitted on `resume_session_id`. Tools: `--tools <mapped allowlist>`; `--disallowedTools` carries the caller's hard-deny list (plan-mode web tools). Every `can_use_tool` goes to the spawn's `ClaudeApprovalBroker` (mode/panel/ask_user with the spawn's name as the panel label); without a UI the broker denies mutating tools with `HEADLESS_DENY_MESSAGE`. Transcripts: whole `assistant`/`user` objects through `CliStreamTranslator` as before; `stream_event` objects are dropped (the spawn card shows whole messages, no token streaming). The silence timeout comes from `MARIM_CLAUDE_CLI_TIMEOUT` as before; a hung spawn is interrupted, then killed, and fails with "timed out".

- [ ] **Step 1: Rewrite the runner tests in `tests/test_cli_backend.py`**

Delete `test_build_cli_argv_resume_and_no_system`, `test_build_cli_argv_defaults_unchanged`, `test_build_cli_argv_disallowed_tools`, `test_build_cli_argv_no_disallowed_by_default`, `test_build_cli_argv_safe_mode`, `test_run_captures_session_id_and_checkpoints`, `test_resume_session_id_threads_into_argv`, `test_run_skips_non_json_lines`, `test_run_raises_when_stream_ends_without_result`, `test_run_returns_when_stderr_never_eofs`, `test_run_parses_ndjson_line_over_64kib` and the `_FAKE_*` script strings / `_fake_cli` helpers they used; keep `test_synth_usage_rounds_micro_usd`. Set the import block to:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from marim_harness.claude.approvals import ClaudeApprovalBroker, HEADLESS_DENY_MESSAGE
from marim_harness.runtime.permissions import Mode, UiSeams
from marim_harness.subagents.cli_backend import ClaudeCliRunner, CliRunError, synth_usage
from marim_harness.usage import COST_DETAIL_KEY
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_log
```

and add:

```python
def _run_kwargs(binary: str, cwd: Path, **overrides) -> dict:
    kwargs = dict(
        binary=binary,
        prompt="do the task",
        system_prompt="role",
        cwd=str(cwd),
        allowed_tools=["read_file", "write_file"],
        model="opus",
        stream_id="s1",
    )
    kwargs.update(overrides)
    return kwargs


def _broker(mode: Mode, root: Path, *, panel=None) -> ClaudeApprovalBroker:
    return ClaudeApprovalBroker(
        mode_getter=lambda: mode,
        workspace_root=root,
        scratchpad_getter=lambda: None,
        ui=UiSeams(request_approval=panel, ask_user=None),
        label="worker",
    )


@pytest.mark.anyio
async def test_run_streams_events_captures_session_and_checkpoints(tmp_path):
    scenario = {
        "session_id": "SID-1",
        "turns": [[{"text": "hi"}, {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a"}}}, {"tool_result": {"id": "t1", "content": "x"}}, {"text": "Done: report"}]],
    }
    binary = fake_claude_bin(tmp_path, scenario)
    events: list = []
    ckpts: list = []

    async def on_event(sid, ev, usage):
        events.append((sid, ev))

    def checkpoint(transcript, session_id):
        ckpts.append((len(transcript), session_id))

    runner = ClaudeCliRunner(on_event, None)
    result = await runner.run(**_run_kwargs(binary, tmp_path, checkpoint=checkpoint))
    assert result.output == "Done: report" and result.session_id == "SID-1"
    assert result.usage.input_tokens == 7 and result.usage.details[COST_DETAIL_KEY] == 1000
    assert events and all(sid == "s1" for sid, _ in events)
    assert ckpts and ckpts[-1][1] == "SID-1"
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--append-system-prompt") + 1] == "role"
    assert argv[argv.index("--tools") + 1] == "Read,Write"
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--resume" not in argv and "--disallowedTools" not in argv
    assert "--permission-mode" not in argv and "--safe-mode" in argv


@pytest.mark.anyio
async def test_resume_omits_system_prompt_and_threads_the_id(tmp_path):
    binary = fake_claude_bin(tmp_path, {"known_sessions": ["OLD"], "turns": [[{"text": "back"}]]})
    result = await ClaudeCliRunner(None, None).run(
        **_run_kwargs(binary, tmp_path, resume_session_id="OLD", disallowed_tools=["WebFetch", "WebSearch"])
    )
    assert result.output == "back"
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--resume") + 1] == "OLD"
    assert "--append-system-prompt" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "WebFetch,WebSearch"


@pytest.mark.anyio
async def test_run_raises_when_claude_exits_without_result(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "a"}, {"exit": {"code": 2, "stderr": "bad"}}]]})
    with pytest.raises(CliRunError) as exc:
        await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    assert "no result" in str(exc.value) and "claude exited (code 2): bad" in str(exc.value)


@pytest.mark.anyio
async def test_run_times_out_on_silence(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0.3")
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]})
    with pytest.raises(CliRunError) as exc:
        await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    assert "timed out" in str(exc.value)


@pytest.mark.anyio
async def test_prompts_go_through_the_broker(tmp_path):
    step = {"can_use_tool": {"tool_name": "Write", "input": {"file_path": str(tmp_path / "out.txt"), "content": "x"}}}
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    seen: list = []

    async def panel(call):
        seen.append(call)
        return True

    result = await ClaudeCliRunner(None, None).run(
        **_run_kwargs(binary, tmp_path, broker=_broker(Mode.ask, tmp_path, panel=panel))
    )
    assert result.output == "Write done"
    assert seen[0].tool_name == "write_file" and seen[0].args["label"] == "worker"


@pytest.mark.anyio
async def test_headless_broker_denies_mutations(tmp_path):
    step = {"can_use_tool": {"tool_name": "Write", "input": {"file_path": str(tmp_path / "out.txt"), "content": "x"}}}
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    result = await ClaudeCliRunner(None, None).run(
        **_run_kwargs(binary, tmp_path, broker=_broker(Mode.auto, tmp_path))
    )
    assert result.output == "denied: " + HEADLESS_DENY_MESSAGE


@pytest.mark.anyio
async def test_run_without_broker_answers_prompts_with_an_error(tmp_path):
    step = {"can_use_tool": {"tool_name": "Read", "input": {"file_path": "/a"}}}
    binary = fake_claude_bin(tmp_path, {"turns": [[step]]})
    await ClaudeCliRunner(None, None).run(**_run_kwargs(binary, tmp_path))
    replies = [m for m in read_claude_log(tmp_path) if m.get("type") == "control_response"]
    assert replies and replies[0]["response"]["subtype"] == "error"


@pytest.mark.anyio
async def test_stream_events_do_not_reach_the_transcript(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "abcdef"}]]})
    events: list = []

    async def on_event(sid, ev, usage):
        events.append(ev)

    result = await ClaudeCliRunner(on_event, None).run(**_run_kwargs(binary, tmp_path))
    # The fake emits 3-char deltas then one assistant block: the transcript
    # holds the single whole message, not the deltas.
    assert result.output == "abcdef"
    texts = [getattr(p, "content", None) for m in result.transcript for p in m.parts]
    assert texts.count("abcdef") == 1
```

- [ ] **Step 2: Migrate `tests/test_subagents_cli.py`**

Delete `test_permission_mode_maps_to_auto_and_plan`, `test_build_argv_includes_required_flags`, `test_build_argv_omits_model_and_tools_when_absent`, `test_runner_drains_stderr_concurrently_no_deadlock`, `test_runner_handles_line_larger_than_64kib`, and every `_FAKE_CLI*` script string with its `_fake_cli*` helper. Drop `build_cli_argv`, `cli_permission_mode` from the import at line 22 and add `from tests.fakes import fake_claude_bin`. Rewrite the six remaining runner tests on scenarios:

```python
def _run(binary: str, tmp_path, runner: ClaudeCliRunner, **overrides):
    kwargs = dict(
        binary=binary, prompt="p", system_prompt="s", cwd=str(tmp_path),
        allowed_tools=["read_file"], model=None, stream_id="s1",
    )
    kwargs.update(overrides)
    return runner.run(**kwargs)


@pytest.mark.anyio
async def test_runner_streams_events_and_returns_result(tmp_path):
    binary = fake_claude_bin(
        tmp_path,
        {"turns": [[{"text": "hi"}, {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a"}}}, {"tool_result": {"id": "t1", "content": "x"}}, {"text": "Done"}]]},
    )
    seen: list = []

    async def on_event(sid, ev, usage):
        seen.append((sid, type(ev).__name__))

    result = await _run(binary, tmp_path, ClaudeCliRunner(on_event, None))
    assert result.output == "Done" and result.usage.input_tokens == 7
    assert ("s1", "PartStartEvent") in seen and ("s1", "FunctionToolCallEvent") in seen


@pytest.mark.anyio
async def test_runner_surfaces_real_model_from_init_event(tmp_path):
    binary = fake_claude_bin(tmp_path, {"model": "claude-opus-4-1", "turns": [[{"text": "ok"}]]})
    models: list = []

    async def on_model(sid, m):
        models.append((sid, m))

    await _run(binary, tmp_path, ClaudeCliRunner(None, None, on_model))
    assert models[0] == ("s1", "claude-opus-4-1")


@pytest.mark.anyio
async def test_runner_skips_model_callback_without_stream_id(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "ok"}]]})
    models: list = []

    async def on_model(sid, m):
        models.append(m)

    await _run(binary, tmp_path, ClaudeCliRunner(None, None, on_model), stream_id=None)
    assert models == []


@pytest.mark.anyio
async def test_runner_raises_when_no_result(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "a"}, {"exit": {"code": 0, "stderr": ""}}]]})
    with pytest.raises(CliRunError, match="no result"):
        await _run(binary, tmp_path, ClaudeCliRunner(None, None))


@pytest.mark.anyio
async def test_runner_kills_subprocess_when_event_callback_raises(tmp_path):
    binary = fake_claude_bin(tmp_path, {"turns": [[{"text": "a"}, {"sleep": 30}]]})

    async def boom(sid, ev, usage):
        raise RuntimeError("sink failed")

    runner = ClaudeCliRunner(boom, None)
    with pytest.raises(RuntimeError, match="sink failed"):
        await _run(binary, tmp_path, runner)
    # `run`'s finally closed the process (SIGTERM to its group): the pid the
    # fake recorded is gone from the process table.
    pid = int((tmp_path / "claude.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.anyio
async def test_runner_returns_transcript(tmp_path):
    binary = fake_claude_bin(
        tmp_path,
        {"turns": [[{"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a"}}}, {"tool_result": {"id": "t1", "content": "x"}}, {"text": "Done"}]]},
    )
    result = await _run(binary, tmp_path, ClaudeCliRunner(None, None))
    kinds = [type(p).__name__ for m in result.transcript for p in m.parts]
    assert kinds == ["ToolCallPart", "ToolReturnPart", "TextPart"]


```

`test_runner_demuxes_claude_side_subagents` keeps its body and every assertion verbatim (`stream_id="parent"`, output `"Four."`, usage `1083 + 48`, cost `50_000`, child stream `"tsub"`, models `claude-haiku-4-5`/`claude-opus-4-8`, the transcript pairing) — only drop `allow_gated=False` from the `run(...)` call and replace `_make_fake_cli_agent(tmp_path)` with a scenario built from the objects the old `_FAKE_CLI_AGENT` script printed: every JSON line the script emits becomes a `{"raw": <that object>}` step, in order, EXCEPT the final `result` line, which becomes `{"result": {<its fields minus "type">}}` so the fake's auto result carries the same `result`, `usage` and `total_cost_usd` (the earlier, intermediate `result` line stays a `raw` step: the runner must see two results and fold their usage). The `system/init` line of the old script is dropped (the fake emits its own init; set `"model": "claude-opus-4-8"` at the scenario top level so the run's own model still surfaces). Write it as:

```python
def _agent_scenario() -> dict:
    lines = [json.loads(line) for line in _AGENT_STREAM.strip().splitlines()]
    init = [o for o in lines if o.get("type") == "system" and o.get("subtype") == "init"][0]
    body = [o for o in lines if o is not init]
    final = body.pop()  # the last result
    assert final["type"] == "result"
    steps = [{"raw": o} for o in body]
    steps.append({"result": {k: v for k, v in final.items() if k != "type"}})
    return {"model": init["model"], "turns": [steps]}
```

where `_AGENT_STREAM` is the old script's NDJSON payload extracted into a plain multi-line string (copy the JSON lines out of `_FAKE_CLI_AGENT`, dropping the Python wrapper), and the test does `binary = fake_claude_bin(tmp_path, _agent_scenario())`.

The fake's `claude.pid` file does not exist yet: in `tests/fakes/fake_claude.py`, at the top of `main()`, add `Path(os.environ["MARIM_CLAUDE_FAKE_LOG"]).with_name("claude.pid").write_text(str(os.getpid()))` (the log path sits in `tmp_path`, so the pid file lands at `<tmp_path>/claude.pid`; `sys.argv[0]` is the fake module itself, not the wrapper, so do not derive it from argv). The test file needs `import json` and `import os` at the top for `_agent_scenario` and the pid probe.

- [ ] **Step 3: Migrate `tests/test_subagent_cli_spawn.py` and `tests/test_subagent_plan_egress.py`**

`tests/test_subagent_cli_spawn.py`: replace `_FAKE_CLI`/`_fake_cli` and `_FAKE_CLI_CHILD`/`_fake_cli_child` with

```python
from tests.fakes import fake_claude_bin


def _fake_cli(tmp_path: Path) -> str:
    return fake_claude_bin(tmp_path, {"turns": [[{"text": "hi"}, {"text": "Done: report body"}]]})


def _fake_cli_child(tmp_path: Path) -> str:
    return fake_claude_bin(
        tmp_path,
        {
            "turns": [
                [
                    {"raw": {"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "id": "tsub", "name": "Agent", "input": {"description": "d", "subagent_type": "Explore", "prompt": "p"}}]}}},
                    {"raw": {"type": "system", "subtype": "task_started", "tool_use_id": "tsub"}},
                    {"raw": {"type": "assistant", "parent_tool_use_id": "tsub", "message": {"id": "m2", "content": [{"type": "text", "text": "4"}]}}},
                    {"raw": {"type": "system", "subtype": "task_notification", "tool_use_id": "tsub", "status": "completed", "summary": "4"}},
                    {"text": "Done"},
                ]
            ]
        },
    )
```

(drop the now-unused `stat`/`sys` imports). The fake's result usage is `input_tokens 7 / output_tokens 4` (Task 4), so `test_cli_backend_fires_usage_callback`'s existing `usage.input_tokens == 7 and usage.output_tokens == 4` assertion stays as is.

`test_cli_runner_times_out_on_hung_cli`: replace the `hang.py` script with `fake_claude_bin(tmp_path, {"turns": [[{"sleep": 30}]]})`, drop `allow_gated=False` from the `run(...)` call, keep the `"timed out"` and `elapsed < 5` assertions. `test_cli_timeout_env_falls_back_on_garbage` stays (the re-export from Task 1 keeps `_DEFAULT_CLI_TIMEOUT`/`_cli_timeout` importable).

`tests/test_subagent_plan_egress.py`: delete `test_cli_argv_carries_no_mcp_flags` (its argv builder is gone; `test_cli_spawn_never_receives_marim_mcp_config` still pins the assumption at the kwargs layer). In `_captured_cli_run_kwargs`, the stubbed `fake_run` now also receives `broker=`; add `assert isinstance(kwargs["broker"], ClaudeApprovalBroker)` to `test_cli_spawn_in_plan_mode_strips_net_tools` (import `ClaudeApprovalBroker` from `marim_harness.claude.approvals`). The MCP sweep in `test_cli_spawn_never_receives_marim_mcp_config` stringifies every structured kwarg — the broker's `repr` must not contain "mcp"; it doesn't (a dataclass-free class with the default repr).

- [ ] **Step 4: Run the four files to verify they fail**

Run: `uv run pytest --no-cov tests/test_cli_backend.py tests/test_subagents_cli.py tests/test_subagent_cli_spawn.py tests/test_subagent_plan_egress.py -q 2>&1 | tail -5`
Expected: TypeError `run() got an unexpected keyword argument 'broker'` / import errors for the deleted names.

- [ ] **Step 5: Rewrite `cli_backend.py`**

Replace the module docstring (lines 1-13) with:

```python
"""Optional Claude Code backend for sub-agents: one long-lived, bidirectional
``claude`` per spawn.

A ``backend: claude-cli`` sub-agent runs on the user's Claude subscription: the
spawn's task goes down a ``ClaudeProcess`` (``claude/process.py``) as one
stream-json turn, Claude's tool calls are approved per tool through the spawn's
``ClaudeApprovalBroker`` (marim's ``auto``/``ask``/``plan`` mode, the approval
panel, ``ask_user``), and the assistant/user objects it emits are translated
into pydantic-ai streaming events for the sub-agents screen. The process is
closed when the spawn ends; an interrupted spawn resumes by session id.
"""
```

Delete: the `_POST_LOOP_GRACE` constant and its comment, `_iter_ndjson_lines`, `_read_next_line`, `_kill_process_group` (and its `kill_process_tree` import), `cli_permission_mode`, `build_cli_argv`, `ClaudeCliRunner._process_line`, `_drain_stderr`, `_reap`; the imports `contextlib`, `json`, `shutil`, `signal`, `iter_ndjson_lines` if `ruff` reports them unused. Keep `_CC_TOOL_MAP`, `normalize_cc_tool`, `CliRunError`, `CliResult`, `map_tools_to_cc`, `synth_usage`, `sum_result_usages`, `CliStreamTranslator`, `_RunState`, `_dispatch_remainder`, `_deliver`, and the Task 1 re-exports.

Add to the imports:

```python
from ..claude.process import ClaudeProcess, ProcessOptions, turn_objects
from ..claude.protocol import CLOSED
from ..config.external_cli import CliModelError

if TYPE_CHECKING:
    from ..claude.approvals import ClaudeApprovalBroker
```

`_RunState` gains one field:

```python
    closed_detail: str = ""  # "claude exited (code N): <stderr>" when the process died mid-turn
```

Replace `ClaudeCliRunner.run` through `_finalize` with:

```python
    async def run(
        self,
        *,
        binary: str,
        prompt: str,
        system_prompt: str,
        cwd: str,
        allowed_tools: list[str],
        model: str | None,
        stream_id: str | None,
        disallowed_tools: list[str] | None = None,
        checkpoint: Callable[[list, str | None], None] | None = None,
        resume_session_id: str | None = None,
        broker: ClaudeApprovalBroker | None = None,
    ) -> CliResult:
        """Run one spawn to completion on its own ``claude`` process.

        ``allowed_tools`` are marim tool names (mapped to Claude Code's via
        ``map_tools_to_cc``); ``disallowed_tools`` are Claude Code names the
        caller hard-denies (plan mode's web tools). ``broker`` answers the
        process's ``can_use_tool`` requests; without one the process replies
        with an error response (Claude treats it as a denial) — tests only,
        ``cli_spawn.run_cli`` always builds one. On ``resume_session_id`` the
        system prompt is omitted (the session already has it) and ``prompt``
        is the resume text.

        A silence timeout (``MARIM_CLAUDE_CLI_TIMEOUT``, paused while a prompt
        waits in the panel) interrupts and then kills a hung spawn: it must not
        pin its concurrency slot forever."""
        options = ProcessOptions(
            binary=binary,
            cwd=cwd,
            model=model,
            resume_id=resume_session_id,
            tools=tuple(map_tools_to_cc(allowed_tools)),
            disallowed_tools=tuple(disallowed_tools or ()),
            append_system=None if resume_session_id else system_prompt,
        )
        process = ClaudeProcess(
            options,
            on_request=broker.handle if broker is not None else None,
            silence_timeout=_cli_timeout(),
        )
        from .cli_demux import CliSubagentDemux

        translator = CliStreamTranslator()
        demux = CliSubagentDemux()
        state = _RunState(session_id=resume_session_id)
        await process.start()
        try:
            handle = await process.send_turn(prompt)
            async for obj in turn_objects(process, handle):
                await self._consume(obj, state, translator, demux, stream_id, checkpoint)
        except CliModelError as exc:
            # next_turn_object's silence timeout (already interrupted the turn).
            raise CliRunError(str(exc)) from exc
        finally:
            # One process per spawn: whatever happened (a sink raised, the
            # spawn was cancelled, the CLI died), nothing outlives the spawn.
            await process.aclose()
        return self._finalize(state, translator, demux)

    async def _consume(
        self,
        obj: dict,
        state: _RunState,
        translator: CliStreamTranslator,
        demux: CliSubagentDemux,
        stream_id: str | None,
        checkpoint: Callable[[list, str | None], None] | None,
    ) -> None:
        """Route one turn object: checkpoint the transcript growth seen so far
        (at the top, so the message that fails to deliver is not lost), capture
        the session id, note a mid-turn death, and split Claude-side sub-agent
        traffic off through the demux. ``stream_event`` deltas are dropped —
        the spawn card renders whole messages."""
        if checkpoint is not None:
            snapshot = translator.transcript()
            if len(snapshot) != state.last_ckpt_len:
                state.last_ckpt_len = len(snapshot)
                checkpoint(snapshot, state.session_id)
        if obj.get("type") == CLOSED:
            state.closed_detail = f"claude exited (code {obj.get('returncode')}): {str(obj.get('stderr') or '').strip()}"
            return
        if obj.get("type") == "stream_event":
            return
        if state.session_id is None:
            sid = obj.get("session_id")
            if isinstance(sid, str) and sid:
                state.session_id = sid
        routed, remainder = demux.route(obj)
        for r in routed:
            await self._deliver(r, translator, stream_id)
        if remainder is not None:
            await self._dispatch_remainder(remainder, state, translator, stream_id)

    def _finalize(
        self, state: _RunState, translator: CliStreamTranslator, demux: CliSubagentDemux
    ) -> CliResult:
        """Raise CliRunError when the turn never produced a result object (the
        process died — `closed_detail` carries its exit code and stderr — or
        the stream simply ended), else build the finished CliResult."""
        if not state.results:
            detail = state.closed_detail or "the turn ended without a result object"
            raise CliRunError(f"claude produced no result ({detail})")
        return CliResult(
            output=state.output,
            usage=synth_usage(*sum_result_usages(state.results)),
            transcript=translator.transcript(),
            child_transcripts=demux.child_transcripts(),
            session_id=state.session_id,
        )
```

`_dispatch_remainder` and `_deliver` are unchanged. `CliSubagentDemux` is imported lazily inside `run` today (`from .cli_demux import CliSubagentDemux`) — keep that import where it is and add it under `TYPE_CHECKING` for the annotations. The `if state.session_id is None` guard and the checkpoint block are the current `_process_line`'s, moved verbatim.

The checkpoint's `cli_session_id` on the first checkpoint may be `None` (before `system/init` arrived) — same as before.

- [ ] **Step 6: `cli_spawn.run_cli` builds the broker**

In `src/marim_harness/subagents/cli_spawn.py`, `run_cli` (lines 202-270): add `from ..claude.approvals import ClaudeApprovalBroker` and `from ..runtime.permissions import UiSeams` to the imports (top of the file; both are import-cycle-free — `claude.approvals` imports `runtime.permissions` and lazily `subagents.cli_backend`). Replace the body from `mode = self.deps.workspace.mode` to the `runner.run(...)` call with:

```python
        mode = self.deps.workspace.mode
        # Plan mode strips marim's net tools from the allowlist AND hard-denies
        # Claude Code's web tools: absence from `--tools` alone is not a denial.
        deny_net = mode is Mode.plan
        tools = effective_tools(defn, allow_gated=True, allow_net=not deny_net)
        cwd = str(work_root or self.deps.workspace.root)
        model_name = model or defn.model or os.environ.get(CLI_MODEL_ENV)
        cbs = self.deps.ui
        # Every tool Claude wants to run comes back as a can_use_tool request;
        # the broker applies the live mode (auto/ask/plan) per call, so the
        # allowlist no longer has to pre-decide gated tools (allow_gated=True):
        # a mutating tool is granted to the process and gated per use.
        broker = ClaudeApprovalBroker(
            mode_getter=lambda: self.deps.workspace.mode,
            workspace_root=Path(cwd),
            scratchpad_getter=self.deps.get_scratchpad or (lambda: None),
            ui=UiSeams(request_approval=cbs.request_approval, ask_user=cbs.ask_user),
            label=defn.name,
        )
        runner = ClaudeCliRunner(cbs.on_subagent_event, cbs.on_subagent_notice, cbs.on_subagent_model)
        result = await runner.run(
            binary=binary,
            prompt=task,
            system_prompt=defn.prompt,
            cwd=cwd,
            allowed_tools=tools,
            model=model_name,
            disallowed_tools=map_tools_to_cc(NET_TOOLS) if deny_net else None,
            stream_id=stream_id,
            checkpoint=checkpoint,
            resume_session_id=resume_session_id,
            broker=broker,
        )
```

`Deps.get_scratchpad` is the `Callable[[], Path | None] | None` field at `runtime/deps.py:178`; `request_approval`/`ask_user` are the `UiCallbacks` fields at `runtime/deps.py:246-247`. Trim the long `--disallowedTools` comment above to the four lines shown.

- [ ] **Step 7: Run the four files**

Run: `uv run pytest --no-cov tests/test_cli_backend.py tests/test_subagents_cli.py tests/test_subagent_cli_spawn.py tests/test_subagent_plan_egress.py -q 2>&1 | tail -15`
Expected: green.

- [ ] **Step 8: Full suite, lint, types, commit**

```bash
uv run ruff check --fix src tests && uv run ruff format src tests && uv run pyright && uv run pytest -q 2>&1 | tail -5
git add src/marim_harness/subagents/cli_backend.py src/marim_harness/subagents/cli_spawn.py tests/test_cli_backend.py tests/test_subagents_cli.py tests/test_subagent_cli_spawn.py tests/test_subagent_plan_egress.py tests/fakes/fake_claude.py
git commit -m "feat(subagents): claude-cli spawns run on a bidirectional ClaudeProcess with per-tool approval

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Documentation

**Files:**
- Modify: `docs/reference/configuration.md` (claude-cli rows), `CHANGELOG.md`, `.env.example`, `docs/guides/headless.md`, `docs/guides/subagents.md`, `docs/guides/tui.md`, `docs/guides/sessions.md:61`, `docs/sdk/subagents.md:34`, `docs/README.md:12`, `docs/examples/agents/cli-worker.md:3`, `README.md:53,123,203`, `CLAUDE.md` (the `claude-cli` paragraph under *Commands* and the `subagents/` bullet)
- Test: `tests/test_docs_reference.py::test_every_env_var_is_documented` (existing; must stay green), `uv run python scripts/doc_lint.py` if present (`ls scripts/`)

**Interfaces:** none produced; consumes the env names from Task 1 (`MARIM_CLAUDE_CLI_BIN`, `MARIM_CLAUDE_CLI_MODEL`, `MARIM_CLAUDE_CLI_TIMEOUT`, `MARIM_CLAUDE_CLI_IDLE_TIMEOUT`).

- [ ] **Step 1: Find every stale "claude -p" / "headless claude" statement**

Run: `grep -rn "claude -p\|permission-mode\|headless claude\|can't prompt\|cannot prompt\|one-shot" docs README.md CLAUDE.md CHANGELOG.md .env.example src/marim_harness --include=*.md --include=*.py --include=.env.example | grep -iv "codex" | grep -v "docs/superpowers/"`
Expected: the list below (plus any this grep finds that the list misses — fix those too).

- [ ] **Step 2: `docs/reference/configuration.md`**

Task 1 already added the `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` row and reworded `MARIM_CLAUDE_CLI_TIMEOUT`. Now rewrite the provider paragraph for `claude-cli` (find it with `grep -n "claude-cli" docs/reference/configuration.md`) to:

```markdown
`claude-cli` runs your Claude subscription through the `claude` CLI. marim keeps
one long-lived `claude` process per conversation and talks to it over its
stream-json control protocol: every tool Claude wants to run comes back to marim
as a permission request, so `auto`/`ask`/`plan`, the approval panel and
`ask_user` apply exactly as with a native model; a steer folds into the running
turn; an interrupt is a control request (the process is killed only if it
ignores the interrupt for 2 s). The conversation resumes by session id after an
idle close (`MARIM_CLAUDE_CLI_IDLE_TIMEOUT`), a crash, a model switch, or a
marim restart. Claude runs its own tools, LSP and MCP servers — marim's tools,
LSP and MCP do not apply. The process is launched with `--safe-mode
--strict-mcp-config --setting-sources ""`, so Claude's own hooks, MCP servers,
plugins and settings do NOT load; its skills and `CLAUDE.md` files are plain
files under the workspace and remain readable (the CLI's `--bare` flag would
close that gap but breaks subscription auth, so it is not used). Requires Claude
Code 2.1 or newer (older versions log a warning). The thinking level (`/think`)
is a no-op under this provider.
```

- [ ] **Step 3: `CHANGELOG.md`**

Under `## [Unreleased]` → `### Changed` (create the heading if missing) add:

```markdown
- **claude-cli is bidirectional.** The `claude-cli` provider and `backend: claude-cli`
  sub-agents now keep one long-lived `claude` process per conversation over its
  stream-json control protocol instead of launching `claude -p` per turn. marim's
  `auto`/`ask`/`plan` modes, the approval panel and `ask_user` now apply to Claude's
  tool calls (`--permission-mode` is no longer passed); steer folds into the live turn;
  Ctrl-C sends an interrupt (kill after a 2 s grace); the session resumes by id after
  an idle close, crash, model switch or restart. New knob
  `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` (default 600 s) closes an idle process;
  `MARIM_CLAUDE_CLI_TIMEOUT` is now a per-turn *silence* bound that pauses while an
  approval prompt waits. Claude Code ≥ 2.1 required (older versions warn). Closes #109.
```

- [ ] **Step 4: `.env.example`**

Next to the existing `MARIM_CLAUDE_CLI_TIMEOUT` line add:

```bash
# Seconds a claude-cli process may sit idle between turns before marim closes it
# (the conversation resumes by session id on the next turn). Default 600.
# MARIM_CLAUDE_CLI_IDLE_TIMEOUT=600
```

and reword the `MARIM_CLAUDE_CLI_TIMEOUT` comment to `# Seconds of silence allowed inside one claude-cli turn (paused while an approval prompt is open). Default 600.`

- [ ] **Step 5: Guides and README**

Apply these edits (each is a find → replace on the sentence the grep in Step 1 surfaces):
- `docs/guides/headless.md`: where it says claude-cli runs headless Claude with `--permission-mode` (or that approval does not apply), replace with: "Under `claude-cli`, headless runs have no approver: `auto` mode lets Claude edit inside the workspace, and any tool that would need the approval panel (a write outside the workspace, anything in `ask` mode, `AskUserQuestion`) is denied with a message telling Claude to explain what it would have done instead."
- `docs/guides/subagents.md` and `docs/sdk/subagents.md:34`: replace the `claude -p` description with "`backend: claude-cli` runs the spawn on its own bidirectional `claude` process; each tool Claude runs is approved through marim's mode and panel (the panel names the sub-agent), and an interrupted spawn resumes by session id."
- `docs/guides/tui.md`: in the steer/interrupt section note "Under `claude-cli` a steer is delivered into Claude's running turn and Ctrl-C sends Claude an interrupt (the process is killed only if it does not stop within 2 s)."
- `docs/guides/sessions.md:61`: replace the claude-cli resume sentence with "`claude-cli` sessions persist the Claude session id as `claude-cli:<id>` and resume it on the next turn, even after a restart; if Claude no longer has the session, marim starts a fresh one from the flattened history."
- `docs/README.md:12`, `docs/examples/agents/cli-worker.md:3`, `README.md:53,123,203`: replace "`claude -p`" / "one-shot" wording with "a long-lived `claude` process (bidirectional stream-json)".

- [ ] **Step 6: `CLAUDE.md`**

Replace the `claude-cli` sentences in the *Commands* paragraph (from "`claude-cli` delegates each turn" through "spawn's sidecar meta).") with:

```markdown
`claude-cli` runs the conversation on one long-lived bidirectional `claude`
process (`claude/` package: `protocol.py` stream-json client, `process.py`
turn/idle/interrupt lifecycle, `approvals.py` `can_use_tool` → Mode/panel/
ask_user, `env.py` knobs) on a Claude subscription — marim acts as a launcher
(Claude runs its own tools/LSP/MCP), but marim's `auto`/`ask`/`plan`,
approval panel, `ask_user`, steer and interrupt DO apply through the control
protocol. Claude's own Agent/Task sub-agents are demuxed out of the stream
(`subagents/cli_demux.py`) and rendered as first-class cards in the sub-agents
screen, for both the main-loop provider and `backend: claude-cli` spawns. The
session id persists as `claude-cli:<id>` on `SessionStore.cli_thread_id` and
resumes after idle close/crash/restart; interrupted spawns resume the same way
(the id is checkpointed in the spawn's sidecar meta).
```

In the `subagents/` bullet replace "`cli_spawn.py` (`claude -p` execute/resume orchestration)" with "`cli_spawn.py` (`backend: claude-cli` execute/resume orchestration: builds the spawn's approval broker)" and "`cli_backend.py` (the optional `claude -p` CLI backend it delegates to)" with "`cli_backend.py` (the `ClaudeCliRunner` that drives one `ClaudeProcess` per spawn)".

- [ ] **Step 7: Verify**

Run: `uv run pytest --no-cov tests/test_docs_reference.py -q && grep -rn "claude -p" docs README.md CLAUDE.md --include=*.md | grep -v "docs/superpowers/" ; ls scripts/ 2>/dev/null | grep -i lint && uv run python scripts/doc_lint.py`
Expected: tests green; the grep prints nothing (or only historical CHANGELOG entries, which stay).

- [ ] **Step 8: Commit**

```bash
git add docs README.md CLAUDE.md CHANGELOG.md .env.example
git commit -m "docs(claude-cli): describe the bidirectional backend, approval brokering and the idle timeout

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Live smoke (env-gated), full gate, follow-ups

**Files:**
- Create: `tests/test_claude_cli_live.py`
- Verify: the whole CI order on 3.10/3.12/3.14 + the ratchet gate

**Interfaces:** consumes `ClaudeCliModel` (Task 7), `ClaudeCliRunner` (Task 9).

The live file talks to the real `claude` binary on the user's subscription. It is skipped unless `MARIM_LIVE_CLAUDE=1`, and **the executor must not set that variable** — the user runs it after giving an explicit OK (it spends quota). The file exists so the smoke is one command away and its expectations are pinned in code.

- [ ] **Step 1: Write the live smoke**

```python
"""Live smoke against the real `claude` CLI (subscription quota!).

Runs only with MARIM_LIVE_CLAUDE=1 — never set it in CI or by default:

    MARIM_LIVE_CLAUDE=1 uv run pytest --no-cov -n 0 tests/test_claude_cli_live.py -v
"""

from __future__ import annotations

import os
import shutil

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from marim_harness.config.claude_cli_model import ClaudeCliModel

pytestmark = pytest.mark.skipif(
    os.environ.get("MARIM_LIVE_CLAUDE") != "1" or shutil.which("claude") is None,
    reason="live claude smoke: set MARIM_LIVE_CLAUDE=1 with a logged-in `claude` on PATH",
)


def _user(text: str) -> list:
    return [ModelRequest(parts=[UserPromptPart(content=text)], instructions="Answer tersely.")]


@pytest.fixture
def model(tmp_path):
    # The tests close the model themselves (in `finally`); the fixture only builds it.
    m = ClaudeCliModel(os.environ.get("MARIM_CLAUDE_CLI_MODEL") or "haiku")
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: "auto"
    return m


@pytest.mark.anyio
async def test_two_turns_on_one_process_and_resume_after_close(model, tmp_path):
    try:
        r1 = await model.request(_user("Reply with exactly: PING"), None, ModelRequestParameters())
        assert "PING" in r1.parts[0].content
        sid = model.session_id
        assert sid
        history = _user("Reply with exactly: PING") + [r1, ModelRequest(parts=[UserPromptPart(content="Now reply with exactly: PONG")])]
        r2 = await model.request(history, None, ModelRequestParameters())
        assert "PONG" in r2.parts[0].content
        await model.aclose()
        history = history + [r2, ModelRequest(parts=[UserPromptPart(content="What two words did you reply with, in order?")])]
        r3 = await model.request(history, None, ModelRequestParameters())
        assert "PING" in r3.parts[0].content and "PONG" in r3.parts[0].content
        assert model.session_id == sid
    finally:
        await model.aclose()


@pytest.mark.anyio
async def test_plan_mode_denies_a_write(model, tmp_path):
    model.mode_getter = lambda: "plan"
    try:
        resp = await model.request(
            _user(f"Create the file {tmp_path}/x.txt containing 'hi' using the Write tool, then say whether it worked."),
            None,
            ModelRequestParameters(),
        )
    finally:
        await model.aclose()
    assert not (tmp_path / "x.txt").exists()
    assert resp.parts[0].content


@pytest.mark.anyio
async def test_auto_mode_writes_inside_the_workspace(model, tmp_path):
    try:
        await model.request(
            _user(f"Create the file {tmp_path}/y.txt containing exactly 'hi' using the Write tool."),
            None,
            ModelRequestParameters(),
        )
    finally:
        await model.aclose()
    assert (tmp_path / "y.txt").read_text().strip() == "hi"
```

- [ ] **Step 2: Confirm it is skipped by default**

Run: `uv run pytest --no-cov tests/test_claude_cli_live.py -q 2>&1 | tail -3`
Expected: `3 skipped`.

- [ ] **Step 3: Full CI order on all three legs**

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright && uv run pytest -q 2>&1 | tail -3
uv run --python 3.10 pytest -q 2>&1 | tail -3
uv run --python 3.14 pytest -q 2>&1 | tail -3
```
Expected: all green. On 3.10 watch for the completion-before-start ordering (the fake replies fast): every turn is keyed on the `TurnHandle` the start call returned, never on mutable process state, so it should hold — if a 3.10-only failure appears in `tests/test_claude_process.py`, the fix goes there, not in the test.

- [ ] **Step 4: Ratchet gate**

Follow `docs/quality-gate.md` to run the gate locally (its README section names the command; typically `uv run python scripts/quality_gate.py --baseline quality-baseline.json`). Expected: coverage ≥ baseline, src complexity violations ≤ 60, bandit/secrets unchanged. **Do not edit `quality-baseline.json`, `quality-gate.toml`, or `.gitleaks.toml`** — if a number regressed, fix the code (add the missing test, split the function).

- [ ] **Step 5: Commit**

```bash
git add tests/test_claude_cli_live.py
git commit -m "test(claude-cli): env-gated live smoke (MARIM_LIVE_CLAUDE=1)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 6: Report** what the user still owes: the live smoke run (`MARIM_LIVE_CLAUDE=1 …`, spends quota — needs their explicit OK), and the residuals the spec lists (skills/CLAUDE.md remain readable under `--safe-mode`; `bind_ui` after the first turn reaches the broker only on the next process).

---

## Self-review notes

- Spec coverage: env/knobs (T1), policy core (T2), transport (T3), fake (T4), process lifecycle incl. silence/idle/interrupt/kill (T5), approvals incl. ask_user + headless deny (T6), model port incl. resume/missing-session/ephemeral/steer/version check/thinking (T7), model switch + teardown (T8), spawns incl. plan-mode web hard-deny + broker (T9), docs incl. every env var (T10), live smoke + gate (T11).
- Type consistency: `ProcessOptions(binary, cwd, model, resume_id, tools, disallowed_tools, append_system, persist, env)`; `ClaudeProcess(options, *, on_request, silence_timeout, idle_timeout)`; `TurnHandle.open/closed`; `turn_objects(process, handle, first=None)`; `ClaudeApprovalBroker(*, mode_getter, workspace_root, scratchpad_getter, ui, label)` + `.handle`; `UiSeams(request_approval, ask_user)`; `CLOSED` objects carry `returncode` and `stderr`; `fake_claude_bin(tmp_path, scenario)`, `read_claude_argv(s)`, `read_claude_log`.
