# Interactive Project Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the env-var-only project trust gate with an informed, persistent, per-project trust decision surfaced in the TUI (first-open panel + `/trust`), the CLI (`marim trust`), and the serve API — hot-applied on grant.

**Architecture:** One store (`$XDG_STATE_HOME/marim-harness/trusted-projects.json`), one live `TrustState` on `Deps`, one reload seam (`Harness.apply_project_trust()`), three front-ends. Resolution order: explicit config → env var → store (fingerprint-fresh) → untrusted. The spec is `docs/superpowers/specs/2026-07-26-project-trust-prompt-design.md` — read it before starting any task.

**Tech Stack:** Python 3.10+, dataclasses, Textual (TUI), Starlette (serve), pytest.

## Global Constraints

- `requires-python >=3.10` — no 3.11+-only syntax (no `datetime.UTC`; use `timezone.utc`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity cap 10 — extract helpers, never `# noqa: C901`.
- Local gate order before claiming done: `uv run ruff check src tests && uv run pyright && uv run pytest`. Use `uv` for everything.
- The suite runs parallel by default (pytest-xdist); tests must not share mutable global state. Use `tmp_path` and `monkeypatch` for env/XDG isolation — **every test touching the trust store must `monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))`** and every test touching trust resolution must `monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)`.
- Preserve existing "why" comments when editing nearby code.
- Fail closed everywhere: unreadable store ⇒ empty; unreadable config file ⇒ empty surface section; no decision ⇒ untrusted.
- The env var gains force-untrusted semantics: an explicit falsy `MARIM_TRUST_PROJECT_HOOKS` overrides a trusting store; *unset* falls through to the store.
- Tool docstrings and user-facing copy are product — match the tone of existing panels/commands.
- Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_0193XpgcgACByjJYegtLEKnw`

---

### Task 1: Trust store + resolution (`trust.py`)

**Files:**
- Modify: `src/marim_harness/trust.py` (extend; keep `project_trusted` byte-identical in behavior)
- Test: `tests/test_trust_store.py` (create)

**Interfaces:**
- Consumes: `marim_harness.atomic_io.atomic_write_text`, `file_lock` (existing).
- Produces (later tasks rely on these exact names):
  - `StoredDecision(trusted: bool, fingerprint: str, decided_at: str)` frozen dataclass
  - `trusted_projects_path() -> Path`
  - `stored_decision(workspace_root) -> StoredDecision | None`
  - `record_decision(workspace_root, *, trusted: bool, fingerprint: str, now: str) -> None`
  - `trust_env() -> bool | None` (tri-state read of `MARIM_TRUST_PROJECT_HOOKS`)
  - `TrustResolution(trusted: bool, source: str, prompt_needed: bool)` frozen dataclass; `source` ∈ `{"config","env","store","default"}`
  - `resolve_project_trust(workspace_root, *, explicit: bool | None, fingerprint: str, surface_empty: bool) -> TrustResolution`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trust_store.py`:

```python
"""Trust store + resolution: fail-closed persistence keyed by resolved path."""

import json

import pytest

from marim_harness.trust import (
    StoredDecision,
    TrustResolution,
    record_decision,
    resolve_project_trust,
    stored_decision,
    trust_env,
    trusted_projects_path,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)


def test_store_round_trip(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    record_decision(ws, trusted=True, fingerprint="fp1", now="2026-07-26T00:00:00+00:00")
    got = stored_decision(ws)
    assert got == StoredDecision(trusted=True, fingerprint="fp1",
                                 decided_at="2026-07-26T00:00:00+00:00")


def test_decline_is_remembered(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    record_decision(ws, trusted=False, fingerprint="fp1", now="t")
    got = stored_decision(ws)
    assert got is not None and got.trusted is False


def test_missing_store_is_none(tmp_path):
    assert stored_decision(tmp_path) is None


def test_corrupt_store_is_empty(tmp_path):
    path = trusted_projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert stored_decision(tmp_path) is None
    # And recording over a corrupt store recovers rather than raising.
    record_decision(tmp_path, trusted=True, fingerprint="f", now="t")
    assert stored_decision(tmp_path) is not None


def test_keyed_by_resolved_path(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(), b.mkdir()
    record_decision(a, trusted=True, fingerprint="f", now="t")
    assert stored_decision(b) is None  # entry for A never trusts B


def test_record_overwrites_previous(tmp_path):
    record_decision(tmp_path, trusted=True, fingerprint="f1", now="t1")
    record_decision(tmp_path, trusted=False, fingerprint="f2", now="t2")
    got = stored_decision(tmp_path)
    assert got == StoredDecision(trusted=False, fingerprint="f2", decided_at="t2")


def test_store_file_shape(tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    record_decision(ws, trusted=True, fingerprint="fp", now="t")
    data = json.loads(trusted_projects_path().read_text(encoding="utf-8"))
    key = str(ws.resolve())
    assert data[key] == {"trusted": True, "fingerprint": "fp", "decided_at": "t"}


def test_trust_env_tristate(monkeypatch):
    assert trust_env() is None
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "")
    assert trust_env() is None
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    assert trust_env() is True
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "yes")
    assert trust_env() is True
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "0")
    assert trust_env() is False
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "junk")
    assert trust_env() is False


def test_resolution_explicit_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    record_decision(tmp_path, trusted=True, fingerprint="fp", now="t")
    r = resolve_project_trust(tmp_path, explicit=False, fingerprint="fp", surface_empty=False)
    assert r == TrustResolution(trusted=False, source="config", prompt_needed=False)


def test_resolution_env_beats_store(monkeypatch, tmp_path):
    record_decision(tmp_path, trusted=True, fingerprint="fp", now="t")
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "0")
    r = resolve_project_trust(tmp_path, explicit=None, fingerprint="fp", surface_empty=False)
    assert r == TrustResolution(trusted=False, source="env", prompt_needed=False)


def test_resolution_store_fresh_fingerprint(tmp_path):
    record_decision(tmp_path, trusted=True, fingerprint="fp", now="t")
    r = resolve_project_trust(tmp_path, explicit=None, fingerprint="fp", surface_empty=False)
    assert r == TrustResolution(trusted=True, source="store", prompt_needed=False)


def test_resolution_stale_fingerprint_reprompts(tmp_path):
    record_decision(tmp_path, trusted=True, fingerprint="old", now="t")
    r = resolve_project_trust(tmp_path, explicit=None, fingerprint="new", surface_empty=False)
    assert r == TrustResolution(trusted=False, source="default", prompt_needed=True)


def test_resolution_default_untrusted_prompts_only_with_surface(tmp_path):
    r = resolve_project_trust(tmp_path, explicit=None, fingerprint="fp", surface_empty=False)
    assert r == TrustResolution(trusted=False, source="default", prompt_needed=True)
    r = resolve_project_trust(tmp_path, explicit=None, fingerprint="fp", surface_empty=True)
    assert r == TrustResolution(trusted=False, source="default", prompt_needed=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'StoredDecision'`.

- [ ] **Step 3: Implement in `trust.py`**

Append to `src/marim_harness/trust.py` (keep `project_trusted` and `_TRUTHY` as-is; extend the module docstring's "imports only the stdlib" sentence to say "stdlib plus the package's own `atomic_io`"; add the new imports at the top):

```python
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_write_text, file_lock

logger = logging.getLogger(__name__)


def trust_env() -> bool | None:
    """Tri-state read of ``MARIM_TRUST_PROJECT_HOOKS``: None when unset or
    blank (no decision — fall through to the store), True for a truthy
    spelling, False for anything else (an explicit falsy value force-untrusts,
    overriding even a trusting store entry)."""
    raw = os.getenv("MARIM_TRUST_PROJECT_HOOKS")
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUTHY


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "marim-harness"


def trusted_projects_path() -> Path:
    """The per-machine trust store. State, not data: operator decisions about
    local checkouts — never inside the repo (a repo must not self-trust) and
    not synced content."""
    return _state_dir() / "trusted-projects.json"


@dataclass(frozen=True)
class StoredDecision:
    trusted: bool
    fingerprint: str
    decided_at: str


def _load_store() -> dict:
    """The whole store mapping, or {} on any read problem — a broken store
    fails CLOSED (everything untrusted), never fatal."""
    path = trusted_projects_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        logger.warning("trust store unreadable, treating as empty: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def stored_decision(workspace_root) -> StoredDecision | None:
    """The remembered decision for ``workspace_root`` (resolved), or None.
    Malformed entries read as absent — fail closed."""
    entry = _load_store().get(str(Path(workspace_root).resolve()))
    if not isinstance(entry, dict) or not isinstance(entry.get("trusted"), bool):
        return None
    return StoredDecision(
        trusted=entry["trusted"],
        fingerprint=str(entry.get("fingerprint", "")),
        decided_at=str(entry.get("decided_at", "")),
    )


def record_decision(workspace_root, *, trusted: bool, fingerprint: str, now: str) -> None:
    """Persist a decision for ``workspace_root``. Read-modify-write under the
    same advisory lock discipline as the plugin registry so two concurrent
    sessions can't clobber each other's entries."""
    path = trusted_projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        store = _load_store()
        store[str(Path(workspace_root).resolve())] = {
            "trusted": trusted, "fingerprint": fingerprint, "decided_at": now,
        }
        atomic_write_text(path, json.dumps(store, indent=2, sort_keys=True))


@dataclass(frozen=True)
class TrustResolution:
    """The outcome of full store-aware trust resolution. ``source`` names the
    layer that decided (config/env/store/default) — surfaced by /trust, the
    settings row, `marim trust`, and GET /v1/.../trust. ``prompt_needed`` is
    True only in the one state where an interactive front-end should ask:
    no decision anywhere and a non-empty gated surface."""

    trusted: bool
    source: str
    prompt_needed: bool


def resolve_project_trust(
    workspace_root, *, explicit: bool | None, fingerprint: str, surface_empty: bool
) -> TrustResolution:
    """Store-aware trust resolution: explicit caller decision → env var →
    stored decision (honored only while its fingerprint matches the current
    executable surface) → untrusted. The leaf predicate ``project_trusted``
    can't do this — it doesn't know the workspace root — so bootstrap/builder
    call this once to seed the session's TrustState, and the trust front-ends
    re-call it on change."""
    if explicit is not None:
        return TrustResolution(explicit, "config", False)
    env = trust_env()
    if env is not None:
        return TrustResolution(env, "env", False)
    stored = stored_decision(workspace_root)
    if stored is not None and stored.fingerprint == fingerprint:
        return TrustResolution(stored.trusted, "store", False)
    # No usable decision: untrusted, and worth prompting only when the project
    # actually ships gated content (a stale-fingerprint entry lands here too —
    # the surface changed since the last decision, so re-ask).
    return TrustResolution(False, "default", not surface_empty)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_store.py -q`
Expected: all PASS. Also run `uv run pytest --no-cov tests/test_env_blocklist.py -q` (blocklist behavior must be untouched).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/trust.py tests/test_trust_store.py
git commit -m "feat(trust): persistent per-project trust store + store-aware resolution"
```

---

### Task 2: Project gated-surface scanner (`trust_surface.py`)

**Files:**
- Create: `src/marim_harness/trust_surface.py`
- Modify: `src/marim_harness/plugins/install.py` (one-line public alias)
- Test: `tests/test_trust_surface.py` (create)

**Interfaces:**
- Consumes: `hooks.config.project_hooks_config_path`/`_read_hooks`, `mcp.config.project_mcp_config_path`/`_read_servers`, `plugins.install.executable_surface_fingerprint` internals via the new alias.
- Produces:
  - `ProjectSurface(hook_events: list[str], mcp_servers: list[str], skills: list[str], agents: list[str], plugins: list[str], fingerprint: str)` frozen dataclass with `empty: bool` property and `summary() -> str`
  - `scan_project_surface(workspace_root) -> ProjectSurface`
  - In `plugins/install.py`: `plugin_surface_fingerprint = _surface_fingerprint` (public alias, placed right below `_surface_fingerprint` with a one-line comment that `trust_surface.py` folds it into the project fingerprint).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trust_surface.py`:

```python
"""Project gated-surface scanning: what the trust dialog lists, and the
fingerprint that keys stored decisions."""

import json

from marim_harness.trust_surface import ProjectSurface, scan_project_surface


def _mk_project(tmp_path, *, hooks=None, mcp=None, skills=(), agents=()):
    marim = tmp_path / ".marim"
    marim.mkdir(exist_ok=True)
    if hooks is not None:
        (marim / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    if mcp is not None:
        (marim / "mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
    for name in skills:
        d = marim / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n")
    for name in agents:
        d = marim / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text("---\ndescription: x\n---\n")
    return tmp_path


def test_empty_workspace_is_empty_surface(tmp_path):
    s = scan_project_surface(tmp_path)
    assert isinstance(s, ProjectSurface)
    assert s.empty
    assert s.fingerprint  # even an empty surface fingerprints deterministically


def test_full_surface_enumerated(tmp_path):
    _mk_project(
        tmp_path,
        hooks={"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        mcp={"docs": {"command": "python", "args": ["-m", "server"]}},
        skills=("deploy",), agents=("reviewer",),
    )
    s = scan_project_surface(tmp_path)
    assert not s.empty
    assert s.hook_events == ["SessionStart"]
    assert s.mcp_servers == ["docs"]
    assert s.skills == ["deploy"]
    assert s.agents == ["reviewer"]


def test_skills_alone_make_surface_nonempty(tmp_path):
    _mk_project(tmp_path, skills=("deploy",))
    assert not scan_project_surface(tmp_path).empty


def test_fingerprint_changes_on_executable_change_only(tmp_path):
    _mk_project(tmp_path, mcp={"docs": {"command": "python"}}, skills=("a",))
    fp1 = scan_project_surface(tmp_path).fingerprint
    # Editing a skill must NOT flip the fingerprint (inert content).
    (tmp_path / ".marim" / "skills" / "a" / "SKILL.md").write_text("---\nname: a\ndescription: y\n---\n")
    assert scan_project_surface(tmp_path).fingerprint == fp1
    # Changing the MCP command MUST flip it.
    _mk_project(tmp_path, mcp={"docs": {"command": "python3"}})
    assert scan_project_surface(tmp_path).fingerprint != fp1


def test_malformed_configs_read_as_empty(tmp_path):
    marim = tmp_path / ".marim"
    marim.mkdir()
    (marim / "hooks.json").write_text("{broken", encoding="utf-8")
    (marim / "mcp.json").write_text("[]", encoding="utf-8")
    s = scan_project_surface(tmp_path)
    assert s.hook_events == [] and s.mcp_servers == []


def test_summary_names_counts(tmp_path):
    _mk_project(tmp_path, mcp={"docs": {"command": "python"}}, skills=("a", "b"))
    text = scan_project_surface(tmp_path).summary()
    assert "mcp: 1" in text and "docs" in text and "skills: 2" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_surface.py -q`
Expected: FAIL — `ModuleNotFoundError: marim_harness.trust_surface`.

- [ ] **Step 3: Implement `src/marim_harness/trust_surface.py`**

```python
"""Scan a workspace's *gated* project-local surface: everything that loads
only behind the project trust gate (see ``marim_harness.trust`` and
docs/guides/trust.md). Two consumers, one scan:

- the trust dialog / ``marim trust`` / ``GET .../trust`` list what a grant
  would enable, so the decision is informed rather than an opaque yes/no;
- stored decisions are keyed to ``fingerprint`` — canonical JSON over the
  *executable* surface only (hooks entries, MCP specs, project-plugin
  executable blocks). Inert content (skills/agents text) deliberately does
  NOT feed the fingerprint: editing a skill must not drop trust, the same
  policy the plugin registry applies (see plugins/install.py).

Read-only and tolerant: a missing or malformed config file reads as an empty
section, so any later real content registers as a fingerprint change."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .hooks.config import _read_hooks, project_hooks_config_path
from .mcp.config import _read_servers, project_mcp_config_path
from .plugins.install import plugin_surface_fingerprint


@dataclass(frozen=True)
class ProjectSurface:
    hook_events: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def empty(self) -> bool:
        return not (self.hook_events or self.mcp_servers or self.skills
                    or self.agents or self.plugins)

    def summary(self) -> str:
        """One line for the trust dialog / status readouts, naming counts and
        names so the user knows exactly what a grant enables."""
        parts = []
        if self.hook_events:
            parts.append(f"hooks: {len(self.hook_events)} ({', '.join(self.hook_events)})")
        if self.mcp_servers:
            parts.append(f"mcp: {len(self.mcp_servers)} ({', '.join(self.mcp_servers)})")
        if self.skills:
            parts.append(f"skills: {len(self.skills)}")
        if self.agents:
            parts.append(f"agents: {len(self.agents)}")
        if self.plugins:
            parts.append(f"plugins: {len(self.plugins)} ({', '.join(self.plugins)})")
        return " · ".join(parts) if parts else "none"


def _project_plugin_dirs(workspace_root: Path) -> list[Path]:
    """Project-scope plugin directories, sorted for a stable fingerprint.
    Non-directories (the registry file) are skipped."""
    root = Path(workspace_root) / ".marim" / "plugins"
    try:
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []


def _skill_names(workspace_root: Path) -> list[str]:
    root = Path(workspace_root) / ".marim" / "skills"
    try:
        return sorted(d.name for d in root.iterdir()
                      if d.is_dir() and (d / "SKILL.md").is_file())
    except OSError:
        return []


def _agent_names(workspace_root: Path) -> list[str]:
    root = Path(workspace_root) / ".marim" / "agents"
    try:
        return sorted(p.stem for p in root.iterdir()
                      if p.is_file() and p.suffix == ".md")
    except OSError:
        return []


def scan_project_surface(workspace_root) -> ProjectSurface:
    ws = Path(workspace_root)
    hooks = _read_hooks(project_hooks_config_path(ws))
    servers = _read_servers(project_mcp_config_path(ws))
    plugin_dirs = _project_plugin_dirs(ws)
    fingerprint = json.dumps(
        {
            "hooks": hooks,
            "mcpServers": servers,
            "plugins": {p.name: plugin_surface_fingerprint(p) for p in plugin_dirs},
        },
        sort_keys=True, default=str,
    )
    return ProjectSurface(
        hook_events=sorted(hooks),
        mcp_servers=sorted(servers),
        skills=_skill_names(ws),
        agents=_agent_names(ws),
        plugins=[p.name for p in plugin_dirs],
        fingerprint=fingerprint,
    )
```

In `src/marim_harness/plugins/install.py`, directly below `_surface_fingerprint`'s definition, add:

```python
# Public alias: trust_surface.scan_project_surface folds each project-scope
# plugin's executable surface into the workspace trust fingerprint.
plugin_surface_fingerprint = _surface_fingerprint
```

Note: `_read_servers` in `mcp/config.py` — verify the exact name by reading the file first; if the reader is named differently, import that name (do NOT copy its logic).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_surface.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/trust_surface.py src/marim_harness/plugins/install.py tests/test_trust_surface.py
git commit -m "feat(trust): gated-surface scanner with executable-only fingerprint"
```

---

### Task 3: Tri-state config, `TrustState` on `Deps`, bootstrap wiring

**Files:**
- Modify: `src/marim_harness/config/model.py` (`trust_project_hooks` field ~line 178, env read ~line 316)
- Modify: `src/marim_harness/runtime/deps.py` (add `TrustState` + `Deps.trust` field)
- Modify: `src/marim_harness/runtime/bootstrap.py` (resolution before loaders, thread `trusted`, attach prompt payload)
- Modify: `src/marim_harness/runtime/harness.py` (default `self.trust_prompt = None`, `self.project_surface = None` in `__init__`, near `self.deps = deps` ~line 524)
- Modify: `src/marim_harness/runtime/instructions.py:283,295`, `src/marim_harness/subagents/runner.py:239,390`, `src/marim_harness/interfaces/tui/commands.py:300` (thread live trust into discovery)
- Test: `tests/test_trust_wiring.py` (create)

**Interfaces:**
- Consumes: Task 1's `resolve_project_trust`, Task 2's `scan_project_surface`.
- Produces:
  - `TrustState` dataclass in `runtime/deps.py`: `project: bool = False`, `source: str = "default"`, `fingerprint: str = ""` (mutable — `apply_project_trust` flips it live)
  - `Deps.trust: TrustState = field(default_factory=TrustState)`
  - `Harness.trust_prompt: ProjectSurface | None` (set when the TUI should prompt) and `Harness.project_surface: ProjectSurface | None` (always set by bootstrap)
  - `HarnessConfig.trust_project_hooks: bool | None = None` (tri-state)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trust_wiring.py`:

```python
"""Trust resolution wired through config + bootstrap: tri-state env, store
consultation, prompt flag, and live TrustState on Deps."""

import pytest

from marim_harness.runtime.deps import Deps, TrustState, WorkspaceConfig
from marim_harness.trust import record_decision
from marim_harness.trust_surface import scan_project_surface


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)


def test_deps_carries_trust_state(tmp_path):
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path))
    assert deps.trust == TrustState(project=False, source="default", fingerprint="")


def test_config_tristate_env(monkeypatch):
    from marim_harness.config import load_config
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    assert load_config().trust_project_hooks is None
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    assert load_config().trust_project_hooks is True
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "0")
    assert load_config().trust_project_hooks is False


def test_stored_decision_flows_into_resolution(tmp_path):
    """A stored grant with a fresh fingerprint resolves trusted with no prompt —
    the exact contract bootstrap relies on (headless honors the TUI decision)."""
    from marim_harness.trust import resolve_project_trust
    (tmp_path / ".marim" / "skills" / "s").mkdir(parents=True)
    (tmp_path / ".marim" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: x\n---\n")
    surface = scan_project_surface(tmp_path)
    record_decision(tmp_path, trusted=True, fingerprint=surface.fingerprint, now="t")
    r = resolve_project_trust(tmp_path, explicit=None,
                              fingerprint=surface.fingerprint,
                              surface_empty=surface.empty)
    assert r.trusted and r.source == "store" and not r.prompt_needed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_wiring.py -q`
Expected: FAIL — `ImportError: cannot import name 'TrustState'` and the tri-state assertion fails (today it's `False`, never `None`).

- [ ] **Step 3: Implement**

`config/model.py`:
- Change the field (~line 178) to `trust_project_hooks: bool | None = None` and rewrite its comment: tri-state — `True`/`False` are explicit decisions (an explicit `False` force-untrusts, overriding the store), `None` means "no env decision — consult the trust store".
- In `_common_kwargs()` (~line 316) replace `trust_project_hooks=_bool_env("MARIM_TRUST_PROJECT_HOOKS", False)` with `trust_project_hooks=trust_env()` (import `trust_env` from `..trust`).

`runtime/deps.py` — add above `Deps`:

```python
@dataclass
class TrustState:
    """The session's live project-trust decision. Mutable on purpose:
    Harness.apply_project_trust / revoke flip ``project`` in place so every
    lazy reader (skills/agents discovery, instructions) sees the change on
    its next read — the same live-field pattern as WorkspaceConfig.mode."""

    project: bool = False
    source: str = "default"
    fingerprint: str = ""
```

and the field on `Deps` (place after `workspace`): `trust: TrustState = field(default_factory=TrustState)`.

`runtime/bootstrap.py` — at the top of `build_harness` right after `cfg = load_config()`:

```python
from ..trust import resolve_project_trust
from ..trust_surface import scan_project_surface

surface = scan_project_surface(workspace)
resolution = resolve_project_trust(
    workspace, explicit=cfg.trust_project_hooks,
    fingerprint=surface.fingerprint, surface_empty=surface.empty,
)
trusted = resolution.trusted
```

Then replace every `trust_project=cfg.trust_project_hooks` / `mcp_trust_project=cfg.trust_project_hooks` in this function with `trust_project=trusted` / `mcp_trust_project=trusted` (lines 72, 107, 124, 208 — keep the existing comments, they still hold). Add to the `Deps(...)` construction: `trust=TrustState(project=trusted, source=resolution.source, fingerprint=surface.fingerprint)` (import `TrustState` from `.deps`). After `harness = builder.build()`:

```python
harness.project_surface = surface
if resolution.prompt_needed:
    harness.trust_prompt = surface
```

`runtime/harness.py` — in `__init__` near `self.deps = deps` add:

```python
# Set by bootstrap: the project's gated surface, and — when no trust
# decision exists anywhere and the surface is non-empty — the payload the
# TUI's first-open TrustPanel renders. None for embedders (HarnessBuilder
# does no workspace scanning) and once a decision exists.
self.project_surface = None
self.trust_prompt = None
```

Thread live trust into the lazy readers (each currently omits `trust_project`, falling back to the env-only predicate — exactly the gap this feature closes):
- `runtime/instructions.py:283`: `discover_skills(ctx.deps.workspace.root, trust_project=ctx.deps.trust.project, dirs=ctx.deps.workspace.skill_dirs)`
- `runtime/instructions.py:295`: `discover_agents(ctx.deps.workspace.root, trust_project=ctx.deps.trust.project)`
- `tools/fs_tools.py:62`: same `trust_project=ctx.deps.trust.project` addition to `discover_skills`
- `subagents/runner.py:239`: `find_agent(self.deps.workspace.root, type_, trust_project=self.deps.trust.project)`
- `subagents/runner.py:390`: `discover_agents(self.deps.workspace.root, trust_project=self.deps.trust.project)`
- `interfaces/tui/commands.py:300`: pass `trust_project=app.harness.deps.trust.project` (match the surrounding call's existing arguments)

Also check `runtime/instructions.py:263`'s comment ("No trust flag is in reach here") — if `ctx.deps` is in reach in that closure, thread `ctx.deps.trust.project` there too and update the comment; if not, leave it and its env fallback.

- [ ] **Step 4: Run tests + affected suites**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_wiring.py -q` → PASS.
Run: `uv run pytest --no-cov tests/test_agent.py tests/test_skills.py tests/test_subagent_specs.py tests/test_config.py -q` (or the nearest-named existing files) — no regressions from the tri-state change.

- [ ] **Step 5: Commit**

```bash
git add -A src tests/test_trust_wiring.py
git commit -m "feat(trust): tri-state env config, live TrustState on Deps, store-aware bootstrap"
```

---

### Task 4: The reload seam — `Harness.apply_project_trust()` / `revoke_project_trust()`

**Files:**
- Modify: `src/marim_harness/mcp/manager.py` (new `add_servers` method, after `enable_server`)
- Modify: `src/marim_harness/lsp/manager.py` (new `set_registry` method)
- Modify: `src/marim_harness/runtime/harness.py` (the two new methods)
- Test: `tests/test_trust_apply.py` (create)

**Interfaces:**
- Produces:
  - `McpManager.add_servers(servers: list) -> dict` — async; appends specs not already configured (by name) and connects them; returns `mcp_status.to_dict()`
  - `LspManager.set_registry(registry) -> None` — swaps `self._registry`
  - `Harness.apply_project_trust() -> None` (async) — idempotent grant path
  - `Harness.revoke_project_trust() -> None` (sync) — flips `TrustState`, reloads hooks untrusted; MCP/LSP keep running (restart caveat is the caller's copy)
- Neither method touches the store — **persistence is the caller's job** (panel, `/trust`, CLI, serve all call `record_decision` themselves, then this seam).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trust_apply.py`:

```python
"""The hot-apply seam: granting trust mid-session reloads the gated surface
without a rebuild."""

import json

import pytest

from marim_harness.mcp.manager import McpManager


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)


class _FakeServer:
    """Minimal MCPToolset stand-in: named, async-context-manageable."""

    def __init__(self, id):
        self.id = id
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.anyio
async def test_add_servers_connects_new_only():
    existing = _FakeServer("old")
    mgr = McpManager([existing], set())
    added = _FakeServer("new")
    dup = _FakeServer("old")
    await mgr.add_servers([dup, added])
    assert added.entered and not dup.entered
    assert "new" in mgr.configured_names()
    assert list(mgr.configured_names()).count("old") == 1
    # Status recording: assert the same way existing enable_server tests do —
    # read tests covering enable_server and mirror their mcp_status assertion.


@pytest.mark.anyio
async def test_apply_project_trust_flips_state_and_reloads(tmp_path, monkeypatch):
    """Build a minimal harness against a workspace whose .marim ships a skill
    and a hooks.json; before apply nothing loads, after apply the TrustState
    is flipped, deps.hooks is a live HookRunner, and discovery sees the skill."""
    from marim_harness.runtime.bootstrap import build_harness
    from marim_harness.workspace import discover_skills

    marim = tmp_path / ".marim"
    (marim / "skills" / "deploy").mkdir(parents=True)
    (marim / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: d\n---\n")
    (marim / "hooks.json").write_text(json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
    monkeypatch.setenv("MARIM_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_API_BASE", "http://localhost:9")   # never contacted
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # isolate sessions
    harness = build_harness(tmp_path)
    assert harness.trust_prompt is not None
    assert harness.deps.trust.project is False
    assert harness.deps.hooks is None
    names = [s.name for s in discover_skills(
        tmp_path, trust_project=harness.deps.trust.project)]
    assert "deploy" not in names

    await harness.apply_project_trust()

    assert harness.deps.trust.project is True
    assert harness.trust_prompt is None
    assert harness.deps.hooks is not None
    names = [s.name for s in discover_skills(
        tmp_path, trust_project=harness.deps.trust.project)]
    assert "deploy" in names
    # Idempotent: a second call is a no-op, not an error.
    await harness.apply_project_trust()


@pytest.mark.anyio
async def test_revoke_flips_state_and_drops_project_hooks(tmp_path, monkeypatch):
    from marim_harness.runtime.bootstrap import build_harness

    marim = tmp_path / ".marim"
    marim.mkdir()
    (marim / "hooks.json").write_text(json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
    monkeypatch.setenv("MARIM_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_API_BASE", "http://localhost:9")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    harness = build_harness(tmp_path)
    assert harness.deps.hooks is not None

    harness.revoke_project_trust()

    assert harness.deps.trust.project is False
    assert harness.deps.hooks is None  # only project hooks existed
```

Adjust the local-provider env keys to whatever `tests/` already use to build a no-network harness (grep for an existing `build_harness` test and copy its provider setup verbatim — do not invent new env keys). If `pytest.mark.anyio` isn't the suite's convention, match the existing async test style.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_apply.py -q`
Expected: FAIL — `AttributeError: 'McpManager' object has no attribute 'add_servers'`.

- [ ] **Step 3: Implement**

`mcp/manager.py`, after `enable_server`:

```python
async def add_servers(self, servers: list) -> dict:
    """Register and connect servers that aren't already configured (by name).
    The trust hot-apply path: granting project trust mid-session loads the
    project's .marim/mcp.json servers without a rebuild. Mirrors
    enable_server's per-server bookkeeping; disabled names are registered
    but not connected (a later enable_server picks them up)."""
    known = set(self.configured_names())
    fresh = [s for s in servers if self.server_name(s) not in known]
    self.mcp_servers.extend(fresh)
    for server in fresh:
        name = self.server_name(server)
        if name in self.disabled:
            continue
        err = await self._connect_one(server)
        # Record per-server outcome exactly the way enable_server does —
        # read its body and copy its mcp_status bookkeeping calls verbatim
        # (the status API is enable_server's, not this plan's, to invent).
        _record_status(self.mcp_status, name, err)
    return self.mcp_status.to_dict()
```

(`_record_status` above is a placeholder for enable_server's real status
calls — inline whatever `enable_server` does after `_connect_one`; do not
define a helper of that name unless extracting it from `enable_server`.)

`lsp/manager.py`:

```python
def set_registry(self, registry) -> None:
    """Swap the provider registry live (the trust hot-apply path: project
    trust granted mid-session adds third-party providers). Safe because
    every lookup reads self._registry per call and servers connect lazily."""
    self._registry = registry
```

`runtime/harness.py` (near the other public runtime toggles, e.g. after `set_mode`/`set_advisor_model` — find the section):

```python
async def apply_project_trust(self) -> None:
    """Hot-apply a project-trust grant: flip the live TrustState, then
    eagerly reload what loads at startup (hooks config, project MCP
    servers, LSP registry). Lazy readers (skills/agents discovery,
    instructions) pick the flip up on their next read. Idempotent.
    Persistence is the CALLER's job (record_decision) — this seam is
    pure runtime state, so tests and embedders can drive it without
    touching the operator's store."""
    if self.deps.trust.project:
        return
    from ..hooks import HookRunner, load_hooks_config
    from ..mcp import build_mcp_servers, load_mcp_config
    from .bootstrap import build_lsp_registry  # lazy: bootstrap imports this module

    ws = self.deps.workspace.root
    self.deps.trust.project = True
    self.deps.trust.source = "store"
    self.trust_prompt = None
    hooks_cfg = load_hooks_config(ws, trust_project=True)
    self.deps.hooks = HookRunner(hooks_cfg) if hooks_cfg else None
    if self.mcp is not None:
        specs = load_mcp_config(ws, trust_project=True)
        servers, warnings = build_mcp_servers(specs)
        for warning in warnings:
            logger.warning("MCP config: %s", warning)
        self.mcp.trust_project = True
        await self.mcp.add_servers(servers)
    if self.lsp is not None:
        self.lsp.set_registry(build_lsp_registry(ws, trust_project=True))


def revoke_project_trust(self) -> None:
    """Flip the live TrustState off and drop project hooks. Already-running
    MCP servers / LSP providers keep running until restart — the caller
    owns telling the user that caveat (and persisting the decision)."""
    self.deps.trust.project = False
    self.deps.trust.source = "store"
    self.trust_prompt = None
    from ..hooks import HookRunner, load_hooks_config

    hooks_cfg = load_hooks_config(self.deps.workspace.root, trust_project=False)
    self.deps.hooks = HookRunner(hooks_cfg) if hooks_cfg else None
```

Check `harness.py` already has a module-level `logger`; if the file uses another logging name, match it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_apply.py -q` → PASS.
Run: `uv run pytest --no-cov tests/test_mcp_manager.py -q` (existing manager suite, exact filename may differ) → no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/mcp/manager.py src/marim_harness/lsp/manager.py src/marim_harness/runtime/harness.py tests/test_trust_apply.py
git commit -m "feat(trust): hot-apply reload seam (hooks swap, MCP add_servers, LSP registry)"
```

---

### Task 5: TUI TrustPanel + first-open flow

**Files:**
- Create: `src/marim_harness/interfaces/tui/interactions/trust_panel.py`
- Modify: `src/marim_harness/interfaces/tui/interactions/__init__.py` (export `TrustPanel`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`on_mount` ~line 212: kick off the prompt worker)
- Test: `tests/test_trust_panel.py` (create)

**Interfaces:**
- Consumes: `InteractionPanel`/`run_panel` (`interactions/base.py`), `Harness.trust_prompt` (`ProjectSurface`), `apply_project_trust()`, `record_decision`.
- Produces: `TrustPanel(surface: ProjectSurface)` resolving its future to `True` (trust) / `False` (decline). Keybindings: `t` = trust, `d`/`escape` = don't trust. The panel resolves `False` on escape — an unanswered dialog must not linger as a third state when the user dismisses it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trust_panel.py`. Model the harness/app fixture on an existing panel test (`tests/test_app_present_plan.py` is the closest analogue — copy its app/fake-harness setup pattern, don't invent one):

```python
"""First-open trust prompt: mounts only when bootstrap flagged it, persists
both answers, hot-applies on grant."""

# Fixture setup: copy the fake-harness + HarnessApp pattern from
# tests/test_app_present_plan.py. The fake harness needs: .trust_prompt set to
# a ProjectSurface, .deps with a TrustState, an async apply_project_trust()
# recording it was called, and .deps.workspace.root = tmp_path.


async def test_panel_mounts_when_prompt_pending(...):
    # app with harness.trust_prompt = surface → after on_mount settles,
    # app.query(TrustPanel) is non-empty.


async def test_no_panel_when_no_prompt(...):
    # harness.trust_prompt = None → no TrustPanel mounted.


async def test_trust_key_persists_applies_and_confirms(...):
    # press "t": panel closes; stored_decision(root).trusted is True;
    # fake harness.apply_called is True; a confirmation line was posted.


async def test_decline_key_persists_and_notices(...):
    # press "d": panel closes; stored_decision(root).trusted is False;
    # apply NOT called; transcript contains "/trust" hint notice.
```

Write these as real Pilot tests with the copied fixture — the sketch above defines required behavior, not literal bodies.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_panel.py -q`
Expected: FAIL — `ImportError: TrustPanel`.

- [ ] **Step 3: Implement**

`interactions/trust_panel.py`:

```python
"""First-open project trust prompt. Inline panel (never a modal): lists the
project's gated surface so the decision is informed, resolves True/False.
Both answers persist (the caller records them); escape counts as decline —
an unanswered prompt must not linger while turns run untrusted underneath."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from .base import InteractionPanel


class TrustPanel(InteractionPanel):
    BINDINGS = InteractionPanel.BINDINGS + [
        Binding("t", "trust", "Trust"),
        Binding("d", "decline", "Don't trust"),
        Binding("escape", "decline", "Don't trust", show=False),
    ]

    def __init__(self, surface) -> None:
        super().__init__(id="trust-panel")
        self._surface = surface

    def compose(self) -> ComposeResult:
        yield Static("[b]This project ships configuration that loads on startup[/b]")
        yield Static(self._surface.summary(), classes="muted")
        yield Static(
            "Trust it? Hooks and MCP servers run code with no per-call approval; "
            "skills and agents inject prompt content. docs/guides/trust.md",
            classes="muted",
        )
        yield Static("[b]\\[t][/b] Trust   [b]\\[d][/b] Don't trust")

    def on_mount(self) -> None:
        self.focus()

    def action_trust(self) -> None:
        self.resolve(True)

    def action_decline(self) -> None:
        self.resolve(False)
```

(Adjust markup helpers/classes to match how `plan_card.py` composes its statics — reuse its conventions exactly.)

`app.py` `on_mount` — after existing mount work, add a worker kickoff (mirror how other startup workers are launched in this file):

```python
if getattr(self.harness, "trust_prompt", None) is not None:
    self.run_worker(self._prompt_project_trust(), group="trust", exit_on_error=False)
```

and the method:

```python
async def _prompt_project_trust(self) -> None:
    """First-open trust dialog. Failure to persist must not strand the
    decision: the session still applies it (the user consented), the error
    is surfaced as a system line."""
    from datetime import datetime, timezone

    from ...trust import record_decision
    from .interactions.trust_panel import TrustPanel

    surface = self.harness.trust_prompt
    trusted = bool(await run_panel(self, TrustPanel(surface)))
    try:
        record_decision(
            self.harness.deps.workspace.root, trusted=trusted,
            fingerprint=surface.fingerprint,
            now=datetime.now(timezone.utc).isoformat(),
        )
    except OSError as exc:
        await self.post_system(f"Couldn't save the trust decision: {exc}")
    if trusted:
        await self.harness.apply_project_trust()
        await self.post_system("Project trusted — hooks, MCP, skills and agents are live.")
    else:
        await self.post_system(
            "Project config present but not trusted — `/trust on` to enable."
        )
```

(`run_panel` import: match the module's existing import of it; if approval flows import it from `.interactions.base`, do the same.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_trust_panel.py -q` → PASS.
Run: `uv run pytest --no-cov tests/test_app.py -q` → no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/interactions/ src/marim_harness/interfaces/tui/app.py tests/test_trust_panel.py
git commit -m "feat(tui): first-open project trust panel"
```

---

### Task 6: `/trust` command + settings row

**Files:**
- Modify: `src/marim_harness/interfaces/tui/commands.py` (new `_cmd_trust` + registry entry)
- Modify: `src/marim_harness/interfaces/tui/settings.py` (~line 502: live state + source)
- Test: extend `tests/test_trust_panel.py` or the existing commands test file (`grep -l "_cmd_mode\|dispatch" tests/` and add there)

**Interfaces:**
- Consumes: `Harness.deps.trust`, `Harness.project_surface`, `apply_project_trust`, `revoke_project_trust`, `record_decision`, `scan_project_surface`.

- [ ] **Step 1: Write the failing tests**

In the commands test file (same style as existing `/mode` tests):

```python
async def test_trust_status_reports_state_and_surface(...):
    # /trust with no arg → post_system output contains "untrusted" (or
    # "trusted"), the source, and the surface summary.

async def test_trust_on_persists_and_applies(...):
    # /trust on → stored_decision(root).trusted True, apply_project_trust called,
    # confirmation posted.

async def test_trust_off_persists_and_warns_restart(...):
    # /trust off → stored .trusted False, revoke called, output mentions restart.

async def test_trust_rejects_unknown_arg(...):
    # /trust bananas → usage error posted, nothing persisted.
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest --no-cov -n 0 <that file> -q`.

- [ ] **Step 3: Implement**

`commands.py`:

```python
async def _cmd_trust(app: HarnessApp, arg: str) -> None:
    """Project trust: `/trust` shows the decision and the gated surface;
    `/trust on` grants (persist + hot-apply); `/trust off` revokes (persist;
    running MCP/LSP processes stop only on restart)."""
    from datetime import datetime, timezone

    from ...trust import record_decision
    from ...trust_surface import scan_project_surface

    root = app.harness.deps.workspace.root
    arg = arg.strip().lower()
    if arg not in ("", "on", "off"):
        await app.post_system("Usage: `/trust [on|off]`")
        return
    if not arg:
        trust = app.harness.deps.trust
        surface = app.harness.project_surface or scan_project_surface(root)
        state = "trusted" if trust.project else "untrusted"
        await app.post_system(
            f"Project **{state}** (source: {trust.source}).\n\n"
            f"Gated project config — {surface.summary()}"
        )
        return
    trusted = arg == "on"
    surface = scan_project_surface(root)
    record_decision(root, trusted=trusted, fingerprint=surface.fingerprint,
                    now=datetime.now(timezone.utc).isoformat())
    if trusted:
        await app.harness.apply_project_trust()
        await app.post_system("Project trusted — hooks, MCP, skills and agents are live.")
    else:
        app.harness.revoke_project_trust()
        await app.post_system(
            "Project trust revoked. Skills/agents/hooks drop now; already-running "
            "MCP servers and language servers stop on restart."
        )
```

Registry entry (in `COMMANDS`, near `/mode`):

```python
Command("trust", "show or set project trust: /trust [on|off]", _cmd_trust),
```

`settings.py` (~line 502): replace the env-derived row with the live state —

```python
trust = app.harness.deps.trust  # reach the harness the way neighboring rows do
yield Static(
    f"Project trust: {'on' if trust.project else 'off'} ({trust.source})",
    classes="muted",
)
```

(Match how that settings section actually accesses the harness/env_cfg — keep whatever accessor pattern surrounds line 502.)

- [ ] **Step 4: Run to verify pass** + `uv run pytest --no-cov tests/test_settings_screen.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/commands.py src/marim_harness/interfaces/tui/settings.py tests/
git commit -m "feat(tui): /trust command and live settings trust row"
```

---

### Task 7: `marim trust` CLI subcommand + headless notice

**Files:**
- Create: `src/marim_harness/interfaces/cli/trust_cmd.py`
- Modify: `src/marim_harness/interfaces/cli/router.py` (add `"trust"` to `_MANAGEMENT` set ~line 13 and route it; copy the lazy-import dispatch pattern of `config`)
- Modify: `src/marim_harness/interfaces/cli/headless.py` (one stderr notice after build)
- Test: `tests/test_cli_trust.py` (create)

**Interfaces:**
- Produces: `marim trust [status|grant|revoke] [workspace]` — default subcommand `status`; workspace defaults to cwd. Exit 0 on success, 2 on usage error (match the other command groups' argparse conventions).

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_trust.py` — drive the command function directly (match how `tests/` test the other CLI groups; grep for an existing `config`/`models` CLI test and mirror it):

```python
def test_trust_grant_then_status(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    (tmp_path / ".marim" / "skills" / "s").mkdir(parents=True)
    (tmp_path / ".marim" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: x\n---\n")
    from marim_harness.interfaces.cli.trust_cmd import run
    run(["grant", str(tmp_path)])
    from marim_harness.trust import stored_decision
    assert stored_decision(tmp_path).trusted is True
    run(["status", str(tmp_path)])
    out = capsys.readouterr().out
    assert "trusted" in out and "skills: 1" in out


def test_trust_revoke(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from marim_harness.interfaces.cli.trust_cmd import run
    run(["grant", str(tmp_path)])
    run(["revoke", str(tmp_path)])
    from marim_harness.trust import stored_decision
    assert stored_decision(tmp_path).trusted is False
```

Plus a headless-notice test in the existing headless test file: build against a workspace with a project skill and no decision → the notice line appears on the `err` stream exactly once; with a stored grant → no notice.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

`trust_cmd.py`:

```python
"""`marim trust` — inspect or set the per-project trust decision from the
command line: the headless/CI counterpart of the TUI's first-open dialog
(headless runs never prompt; they honor what this records)."""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from ...trust import record_decision, resolve_project_trust, trust_env
from ...trust_surface import scan_project_surface


def _status(root: Path) -> None:
    surface = scan_project_surface(root)
    r = resolve_project_trust(root, explicit=None, fingerprint=surface.fingerprint,
                              surface_empty=surface.empty)
    env = trust_env()
    state = "trusted" if r.trusted else "untrusted"
    print(f"{root}: {state} (source: {r.source})")
    print(f"gated project config — {surface.summary()}")
    if env is not None:
        print("note: MARIM_TRUST_PROJECT_HOOKS is set and overrides the store")


def run(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="marim trust")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "grant", "revoke"])
    p.add_argument("workspace", nargs="?", default=".")
    args = p.parse_args(argv)
    root = Path(args.workspace).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        raise SystemExit(2)
    if args.action == "status":
        _status(root)
        return
    surface = scan_project_surface(root)
    record_decision(root, trusted=args.action == "grant",
                    fingerprint=surface.fingerprint,
                    now=datetime.now(timezone.utc).isoformat())
    verb = "granted" if args.action == "grant" else "revoked"
    print(f"trust {verb} for {root}")
    if args.action == "grant":
        print(f"will load — {surface.summary()}")
```

`router.py`: add `"trust"` to `_MANAGEMENT` and route it exactly like the existing groups (read how `config` dispatches and copy the shape).

`headless.py` in `run_headless` (it already takes `err=sys.stderr`), after the harness is available:

```python
if getattr(harness, "trust_prompt", None) is not None:
    print(
        "note: project config present but not trusted; run `marim trust grant` "
        "or set MARIM_TRUST_PROJECT_HOOKS=1",
        file=err,
    )
```

- [ ] **Step 4: Run to verify pass** + existing `tests/test_headless*.py`.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/cli/ tests/
git commit -m "feat(cli): marim trust subcommand + headless untrusted notice"
```

---

### Task 8: Serve API — `GET/POST /v1/workspaces/{ws}/trust` + `trust_prompt_pending`

**Files:**
- Modify: `src/marim_harness/server/http.py` (two handlers + two `Route(...)` entries in the routes list ~line 587)
- Modify: `src/marim_harness/server/supervisor.py` (helper to enumerate live hosts of one workspace)
- Modify: wherever the session payload dict is built (grep `def get_session` in `http.py` / `server/schema.py`) — add `trust_prompt_pending`
- Modify: `docs/reference/serve-api.md` (document both endpoints + the session field)
- Test: `tests/test_server_trust.py` (create; model fixtures on the existing `tests/test_server*.py` TestClient setup)

**Interfaces:**
- `GET /v1/workspaces/{ws}/trust` → 200 `{"trusted": bool, "source": str, "fingerprint_fresh": bool, "surface": {"hook_events": [...], "mcp_servers": [...], "skills": [...], "agents": [...], "plugins": [...], "summary": str}}`, `Cache-Control: no-cache`; 404 unknown workspace.
- `POST /v1/workspaces/{ws}/trust` body `{"trusted": true|false}` → 200 `{"trusted": bool, "applied_sessions": int, "restart_note": str | null}`; 400 malformed body; 404 unknown workspace. Persists against the *current* fingerprint, then for every live session host of that workspace calls `apply_project_trust()` (grant) or `revoke_project_trust()` (revoke — `restart_note` set).
- `SessionSupervisor.hosts_for(ws_id) -> list[SessionHost]` (live hosts only, no spawning).
- Session payload gains `"trust_prompt_pending": bool` (`harness.trust_prompt is not None`).

- [ ] **Step 1: Write the failing tests** — TestClient tests: GET shape on a workspace with a project skill (untrusted, `trusted: false`, summary mentions skills); POST grant → 200, `stored_decision` recorded, subsequent GET `trusted: true, source: "store"`; POST revoke → `restart_note` non-null; POST garbage body → 400; session payload includes `trust_prompt_pending`. Copy the existing server test fixture (app + tmp workspace registration) verbatim from `tests/test_server_sessions.py` or nearest.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** — handlers in `http.py` following the file's existing handler idioms (json body parse guard, `_json(...)`/Response helpers, no-cache header helper added for the cache-headers feature). Resolution for GET:

```python
surface = scan_project_surface(root)
stored = stored_decision(root)
resolution = resolve_project_trust(root, explicit=None,
                                   fingerprint=surface.fingerprint,
                                   surface_empty=surface.empty)
fingerprint_fresh = stored is not None and stored.fingerprint == surface.fingerprint
```

POST grant path:

```python
record_decision(root, trusted=want, fingerprint=surface.fingerprint,
                now=datetime.now(timezone.utc).isoformat())
applied = 0
for host in supervisor.hosts_for(ws_id):
    if want:
        await host.harness.apply_project_trust()
    else:
        host.harness.revoke_project_trust()
    applied += 1
```

`supervisor.py`:

```python
def hosts_for(self, ws_id: str) -> list["SessionHost"]:
    """Live hosts of one workspace (no spawning) — the serve trust
    endpoint hot-applies a decision to every running session."""
    return [h for (w, _sid), h in self._hosts.items() if w == ws_id]
```

(Adjust `_hosts` to the supervisor's real dict name — read the class first.)

`serve-api.md`: document both endpoints, the payload shapes above, and the honest limit verbatim from the spec: *"`POST .../trust` lets a remote client enable startup code execution; serve already exposes turn execution to whoever can reach it, so trust adds no new exposure class."*

- [ ] **Step 4: Run to verify pass** + `uv run pytest --no-cov tests/test_server_sessions.py -q` (or nearest existing server suite).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/server/ docs/reference/serve-api.md tests/test_server_trust.py
git commit -m "feat(serve): workspace trust endpoints + trust_prompt_pending"
```

---

### Task 9: Docs, changelog, full gate

**Files:**
- Modify: `docs/guides/trust.md` (rewrite "The project trust gate" section: store, first-open panel, `/trust`, `marim trust`, serve endpoints, force-untrusted env semantics, fingerprint invalidation; keep the threat-model framing and the "what trust does not cover" section intact)
- Modify: `docs/reference/configuration.md` (the `MARIM_TRUST_PROJECT_HOOKS` row: now a tri-state override over the per-project store)
- Modify: `.env.example` (same wording update at its `MARIM_TRUST_PROJECT_HOOKS` comment)
- Modify: `CHANGELOG.md` (one `## [Unreleased]` entry: interactive per-project trust — first-open TUI dialog, persistent store, `/trust`, `marim trust`, serve endpoints, hot-apply)
- Modify: `CLAUDE.md` (the trust-gate sentences in the hooks/mcp bullet points: mention the store + prompt now back the same gate)

**Steps:**

- [ ] **Step 1: Write the doc edits** (content per the spec; keep `docs/guides/trust.md`'s existing tone — layered, honest about limits).
- [ ] **Step 2: Run the doc-lint test**: `uv run pytest --no-cov tests/test_docs.py -q` (env-var table checker + link checker must stay green; exact filename — grep for `every_env_var`).
- [ ] **Step 3: Full gate**: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: ruff clean, pyright 0 errors, full suite green with coverage ≥90%.
- [ ] **Step 4: Commit**

```bash
git add docs CHANGELOG.md CLAUDE.md .env.example
git commit -m "docs(trust): document the interactive per-project trust flow"
```

---

## Self-review notes (already applied)

- Serve endpoints are workspace-scoped (`/v1/workspaces/{ws}/trust`) — the spec's `/v1/trust` was written before confirming serve is multi-workspace; workspace-level intent is preserved.
- `apply_project_trust`/`revoke_project_trust` deliberately do NOT persist; every caller (panel, `/trust`, CLI, serve) records first, then applies — keeping the seam usable by embedders/tests without touching the operator's store.
- Tasks 5–8 tests reference existing fixture patterns by file name instead of inlining full fixtures — those fixtures are project-specific plumbing the implementer must copy from the named files, not invent; the behavioral contracts to assert are spelled out per test.
- `trust.py` docstring's "imports only the stdlib" claim must be updated in Task 1 (it gains `atomic_io`, itself stdlib-only).
