# Plugin System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Claude-Code-style plugin system to marim — a directory + `.marim-plugin/plugin.json` manifest that bundles skills, subagents, hooks, MCP servers, and optional instructions into one installable, versioned unit, installable from a local path or git URL into a global or project scope, with a trust gate on executable parts.

**Architecture:** A new `src/marim_harness/plugins/` package owns manifest parsing, an on-disk state registry, discovery, and install/lifecycle. The existing discovery seams are extended to *also* surface plugin-provided content: `discover_skills`/`discover_agents` consult the plugin registry directly (they are called from many turn-time call sites that only hold `workspace_root`, so reading the small registry inline matches marim's existing "re-read config each turn" model and lets skills/agents refresh without a restart); `load_hooks_config`/`load_mcp_config`/`register_instructions` merge plugin contributions (hooks/MCP gated by per-plugin trust). Plugin skills/agents are namespaced `plugin:name` via a new `plugin` field and a `qualified_name` property — bare-name validation is untouched.

**Tech Stack:** Python 3.10+, `dataclasses`, `pyyaml` (already used), stdlib `json`/`shutil`/`subprocess`, `argparse` (CLI pattern), Textual (TUI commands), pytest + `uv` for tests.

## Global Constraints

- **Python:** 3.10+ (use `X | None`, `list[...]` natively; no `from __future__` needed except where a module already uses it).
- **Manifest location:** `.marim-plugin/plugin.json` — the ONLY file inside `.marim-plugin/`; all component dirs (`skills/`, `agents/`, `hooks/`, `mcp.json`, `AGENTS.md`) live at the plugin root.
- **Manifest field names mirror Claude Code:** `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, `skills`, `agents`, `hooks`, `mcpServers`. Only `name` is required.
- **Path variable:** `${MARIM_PLUGIN_ROOT}` substitutes to the plugin's absolute install dir inside hook commands and MCP specs.
- **Fail-safe discovery, strict install:** a malformed manifest/component is skipped-with-warning at discovery time (never breaks a turn); rejected with a clear error at install/validate time.
- **Namespacing:** plugin-provided skills/agents are exposed/resolved as `plugin-name:item-name`. User content uses bare names and always wins (distinct key).
- **Precedence (highest first):** project user dirs → global user dirs → project plugins → global plugins. Built-in subagents remain the final fallback.
- **Trust:** skills/agents/instructions load for any *enabled* plugin; hooks/MCP merge only for *enabled + trusted* plugins. Default scope is `global`. Headless install defaults to untrusted; `--trust` required non-interactively.
- **Identifier rule (existing):** names match `^[a-z0-9]+(?:-[a-z0-9]+)*$`, ≤64 chars. Plugin names follow the same rule.
- **Tests:** run with `uv run pytest`. Lint with `uv run ruff check` and `uv run pyright` before each commit.

## Test File Conventions (AUTHORITATIVE — overrides per-task paths)

The repo uses a **flat** `tests/` layout (no subdirectories, no per-dir `__init__.py`). Several module test files already exist. To avoid editing large existing files and to keep all plugin tests grouped, **every test in this plan goes in a new dedicated flat file**. Where a task body names a `tests/plugins/…`, `tests/cli/…`, `tests/tui/…`, or existing module path, use the mapping below instead, and **skip any step that creates a `tests/<subdir>/__init__.py`** (not needed in a flat layout):

| Task | Plan says | Create this file instead |
|------|-----------|--------------------------|
| 1 | `tests/plugins/test_manifest.py` | `tests/test_plugin_manifest.py` |
| 2 | `tests/plugins/test_state.py` | `tests/test_plugin_state.py` |
| 3 | `tests/plugins/test_discovery.py` | `tests/test_plugin_discovery.py` |
| 4 | `tests/test_skills.py` (append) | `tests/test_plugin_skills.py` (new) |
| 5 | `tests/test_agents.py` (append) | `tests/test_plugin_agents.py` (new) |
| 6 | `tests/test_hooks_config.py` (append) | `tests/test_plugin_hooks.py` (new) |
| 7 | `tests/test_mcp_config.py` | `tests/test_plugin_mcp.py` |
| 8 | `tests/test_instructions.py` (append) | `tests/test_plugin_instructions.py` (new) |
| 9 | `tests/plugins/test_install.py` | `tests/test_plugin_install.py` |
| 10 | `tests/cli/test_plugin_cmd.py` | `tests/test_plugin_cli.py` |
| 11 | `tests/tui/test_plugin_command.py` | `tests/test_plugin_command.py` |
| 12 | `tests/plugins/test_integration.py` | `tests/test_plugin_integration.py` |

The fixture plugin (Task 12) lives at `tests/fixtures/plugins/demo-plugin/` (create the `tests/fixtures/` dir; it does not exist yet). In Task 12's integration test, the fixture path is therefore `Path(__file__).parent / "fixtures" / "plugins" / "demo-plugin"` (single `.parent`, since the test file is directly under `tests/`). Update the `pytest` run commands and `git add` lines in each task to the mapped flat paths accordingly. The test *code* in each task is self-contained and correct regardless of file location — only the path changes.

---

## File Structure

**New package `src/marim_harness/plugins/`:**
- `__init__.py` — public exports.
- `manifest.py` — `PluginManifest`, `load_manifest` (strict) / `try_load_manifest` (lenient), `${MARIM_PLUGIN_ROOT}` substitution, path resolution with traversal guard.
- `state.py` — `InstalledPlugin`, scope dir helpers, `load_state`/`save_state` over `plugins.json`.
- `discovery.py` — `ResolvedPlugin`, `discover_plugins`, and the contribution helpers (`plugin_skill_roots`, `plugin_agent_roots`, `plugin_hook_entries`, `plugin_mcp_specs`, `plugin_instruction_texts`, `plugin_bundle_summary`).
- `install.py` — `install_plugin`, `remove_plugin`, `set_enabled`, `set_trusted`, `update_plugin`, git/local source handling.

**Modified:**
- `workspace/skills.py` — `Skill.plugin` field + `qualified_name`; plugin roots in `discover_skills`; `find_skill`/index on qualified name.
- `workspace/agents.py` — `AgentDef.plugin` field + `qualified_name`; plugin roots in `discover_agents`; `find_agent`/index on qualified name.
- `workspace/__init__.py` — no new exports required (helpers imported from `..plugins`), but re-exports stay valid.
- `hooks/config.py` — merge `plugin_hook_entries`.
- `mcp/config.py` — merge `plugin_mcp_specs` (under user specs).
- `instructions.py` — `_plugin_instructions` closure.
- `interfaces/cli/router.py` — add `"plugin"` to `_MANAGEMENT`.
- `interfaces/cli/plugin.py` — **new** `marim plugin …` command group.
- `interfaces/tui/commands.py` — `/plugin` command.
- `subagents.py` — error listing uses `qualified_name`.

**New tests** under `tests/plugins/` and additions to existing `tests/` for the modified modules. Fixtures under `tests/fixtures/plugins/`.

---

### Task 1: Plugin manifest parsing & validation

**Files:**
- Create: `src/marim_harness/plugins/__init__.py`
- Create: `src/marim_harness/plugins/manifest.py`
- Test: `tests/plugins/test_manifest.py`

**Interfaces:**
- Produces:
  - `MANIFEST_DIR = ".marim-plugin"`, `MANIFEST_FILE = "plugin.json"`
  - `class ManifestError(Exception)`
  - `@dataclass(frozen=True) class PluginManifest` with fields `name: str`, `root: Path`, `raw: dict`; properties `version: str | None`, `description: str`, `author: dict`, `homepage: str | None`, `repository: str | None`, `license: str | None`, `keywords: list[str]`; methods `skills_dir() -> Path`, `agents_dir() -> Path`, `hooks_source() -> Path | dict | None`, `mcp_source() -> Path | dict | None`, `instructions_path() -> Path`.
  - `load_manifest(plugin_dir: Path) -> PluginManifest` (strict; raises `ManifestError`)
  - `try_load_manifest(plugin_dir: Path) -> PluginManifest | None` (lenient; logs + returns None)
  - `substitute_root(value, plugin_root: Path)` — recursive `${MARIM_PLUGIN_ROOT}` substitution for str/list/dict.
  - `_NAME_RE` reused rule for plugin name validity via `valid_plugin_name(name) -> bool`.

- [ ] **Step 1: Create the test directory marker and write the failing test**

Create `tests/plugins/__init__.py` (empty) and `tests/plugins/test_manifest.py`:

```python
import json
from pathlib import Path

import pytest

from marim_harness.plugins.manifest import (
    ManifestError,
    PluginManifest,
    load_manifest,
    substitute_root,
    try_load_manifest,
    valid_plugin_name,
)


def _write_manifest(plugin_dir: Path, data: dict) -> None:
    md = plugin_dir / ".marim-plugin"
    md.mkdir(parents=True, exist_ok=True)
    (md / "plugin.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_minimal_manifest(tmp_path):
    _write_manifest(tmp_path, {"name": "my-plugin"})
    m = load_manifest(tmp_path)
    assert m.name == "my-plugin"
    assert m.version is None
    assert m.description == ""
    assert m.skills_dir() == (tmp_path / "skills").resolve()
    assert m.agents_dir() == (tmp_path / "agents").resolve()


def test_load_full_manifest_fields(tmp_path):
    _write_manifest(
        tmp_path,
        {
            "name": "full",
            "version": "1.2.0",
            "description": "does things",
            "author": {"name": "A", "email": "a@example.com"},
            "homepage": "https://h",
            "repository": "https://r",
            "license": "MIT",
            "keywords": ["x", "y"],
        },
    )
    m = load_manifest(tmp_path)
    assert m.version == "1.2.0"
    assert m.description == "does things"
    assert m.author == {"name": "A", "email": "a@example.com"}
    assert m.keywords == ["x", "y"]


def test_custom_component_paths(tmp_path):
    _write_manifest(
        tmp_path,
        {"name": "p", "skills": "./custom/skills/", "hooks": "./h/hooks.json"},
    )
    m = load_manifest(tmp_path)
    assert m.skills_dir() == (tmp_path / "custom" / "skills").resolve()
    assert m.hooks_source() == (tmp_path / "h" / "hooks.json").resolve()


def test_inline_mcp_servers(tmp_path):
    _write_manifest(tmp_path, {"name": "p", "mcpServers": {"web": {"url": "https://x"}}})
    m = load_manifest(tmp_path)
    assert m.mcp_source() == {"web": {"url": "https://x"}}


def test_missing_manifest_strict_raises(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_missing_name_strict_raises(tmp_path):
    _write_manifest(tmp_path, {"version": "1.0"})
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_invalid_name_strict_raises(tmp_path):
    _write_manifest(tmp_path, {"name": "Bad Name"})
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_path_traversal_rejected(tmp_path):
    _write_manifest(tmp_path, {"name": "p", "skills": "../../etc"})
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_try_load_returns_none_on_bad(tmp_path):
    assert try_load_manifest(tmp_path) is None
    _write_manifest(tmp_path, {"name": "ok"})
    assert try_load_manifest(tmp_path).name == "ok"


def test_substitute_root_recurses(tmp_path):
    out = substitute_root(
        {"command": "${MARIM_PLUGIN_ROOT}/bin/x", "args": ["${MARIM_PLUGIN_ROOT}/y"]},
        Path("/plugins/p"),
    )
    assert out == {"command": "/plugins/p/bin/x", "args": ["/plugins/p/y"]}


def test_valid_plugin_name():
    assert valid_plugin_name("my-plugin")
    assert not valid_plugin_name("My-Plugin")
    assert not valid_plugin_name("-x")
    assert not valid_plugin_name("")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/plugins/test_manifest.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.plugins'`.

- [ ] **Step 3: Create the package init**

Create `src/marim_harness/plugins/__init__.py`:

```python
"""Plugin system: installable bundles of skills, sub-agents, hooks, MCP
servers, and instructions, modeled on Claude Code's plugin format.

A plugin is a directory with a ``.marim-plugin/plugin.json`` manifest. It is
installed (copied or linked) into a global (``~/.config/marim/plugins/``) or
project (``<ws>/.marim/plugins/``) scope and tracked in a per-scope
``plugins.json`` registry. Discovery contributes a plugin's content into
marim's existing systems: skills/sub-agents/instructions for any *enabled*
plugin, hooks/MCP only for *enabled + trusted* ones.
"""

from .manifest import (
    MANIFEST_DIR,
    MANIFEST_FILE,
    ManifestError,
    PluginManifest,
    load_manifest,
    substitute_root,
    try_load_manifest,
    valid_plugin_name,
)

__all__ = [
    "MANIFEST_DIR",
    "MANIFEST_FILE",
    "ManifestError",
    "PluginManifest",
    "load_manifest",
    "substitute_root",
    "try_load_manifest",
    "valid_plugin_name",
]
```

- [ ] **Step 4: Implement `manifest.py`**

Create `src/marim_harness/plugins/manifest.py`:

```python
"""Parse and validate a plugin's ``.marim-plugin/plugin.json`` manifest.

Strict (``load_manifest``) raises ``ManifestError`` on any problem — used at
install/validate time. Lenient (``try_load_manifest``) returns ``None`` and
logs — used at discovery time so a broken plugin never breaks a turn.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_DIR = ".marim-plugin"
MANIFEST_FILE = "plugin.json"

# Same identifier rule as skills/agents.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ManifestError(Exception):
    """A manifest is missing, unreadable, or invalid."""


def valid_plugin_name(name) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name) <= 64
        and _NAME_RE.match(name) is not None
    )


@dataclass(frozen=True)
class PluginManifest:
    """A parsed manifest. ``root`` is the plugin's directory; ``raw`` is the
    decoded JSON object."""

    name: str
    root: Path
    raw: dict

    @property
    def version(self) -> str | None:
        v = self.raw.get("version")
        return str(v) if v is not None else None

    @property
    def description(self) -> str:
        return str(self.raw.get("description", "") or "")

    @property
    def author(self) -> dict:
        a = self.raw.get("author")
        return a if isinstance(a, dict) else {}

    @property
    def homepage(self) -> str | None:
        v = self.raw.get("homepage")
        return str(v) if v else None

    @property
    def repository(self) -> str | None:
        v = self.raw.get("repository")
        return str(v) if v else None

    @property
    def license(self) -> str | None:
        v = self.raw.get("license")
        return str(v) if v else None

    @property
    def keywords(self) -> list[str]:
        k = self.raw.get("keywords")
        return [str(x) for x in k] if isinstance(k, list) else []

    def _resolve(self, value, default: str) -> Path:
        rel = value if isinstance(value, str) and value.strip() else default
        root = self.root.resolve()
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            raise ManifestError(f"path escapes plugin root: {rel!r}")
        return target

    def skills_dir(self) -> Path:
        return self._resolve(self.raw.get("skills"), "skills")

    def agents_dir(self) -> Path:
        return self._resolve(self.raw.get("agents"), "agents")

    def instructions_path(self) -> Path:
        return self._resolve(None, "AGENTS.md")

    def hooks_source(self) -> Path | dict | None:
        v = self.raw.get("hooks")
        if isinstance(v, dict):
            return v
        return self._resolve(v, "hooks/hooks.json")

    def mcp_source(self) -> Path | dict | None:
        v = self.raw.get("mcpServers")
        if isinstance(v, dict):
            return v
        return self._resolve(v, "mcp.json")


def _read_raw(plugin_dir: Path) -> dict:
    path = Path(plugin_dir) / MANIFEST_DIR / MANIFEST_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"no readable manifest at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"manifest must be a JSON object: {path}")
    return data


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """Strict parse. Raises ``ManifestError`` on any problem."""
    data = _read_raw(plugin_dir)
    name = data.get("name")
    if not valid_plugin_name(name):
        raise ManifestError(f"invalid or missing plugin name: {name!r}")
    manifest = PluginManifest(name=name, root=Path(plugin_dir), raw=data)
    # Surface path-traversal problems eagerly for configured component paths.
    for key, getter in (
        ("skills", manifest.skills_dir),
        ("agents", manifest.agents_dir),
        ("hooks", manifest.hooks_source),
        ("mcpServers", manifest.mcp_source),
    ):
        if key in data and not isinstance(data[key], dict):
            getter()
    return manifest


def try_load_manifest(plugin_dir: Path) -> PluginManifest | None:
    """Lenient parse. Returns ``None`` (and logs) on any problem."""
    try:
        return load_manifest(plugin_dir)
    except ManifestError as exc:
        logger.warning("skipping plugin at %s: %s", plugin_dir, exc)
        return None


def substitute_root(value, plugin_root: Path):
    """Recursively replace ``${MARIM_PLUGIN_ROOT}`` with ``plugin_root`` in
    strings inside ``value`` (str/list/dict pass-through for other types)."""
    token = "${MARIM_PLUGIN_ROOT}"
    root = str(plugin_root)
    if isinstance(value, str):
        return value.replace(token, root)
    if isinstance(value, list):
        return [substitute_root(v, plugin_root) for v in value]
    if isinstance(value, dict):
        return {k: substitute_root(v, plugin_root) for k, v in value.items()}
    return value
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/plugins/test_manifest.py -q`
Expected: PASS (all tests).

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/marim_harness/plugins/ tests/plugins/ && uv run pyright src/marim_harness/plugins/`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/plugins/__init__.py src/marim_harness/plugins/manifest.py tests/plugins/__init__.py tests/plugins/test_manifest.py
git commit -m "feat(plugins): plugin manifest parsing and validation"
```

---

### Task 2: Plugin state registry (`plugins.json`)

**Files:**
- Create: `src/marim_harness/plugins/state.py`
- Modify: `src/marim_harness/plugins/__init__.py`
- Test: `tests/plugins/test_state.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `@dataclass class InstalledPlugin` with fields `name: str`, `version: str | None`, `source: dict`, `enabled: bool = True`, `trusted: bool = False`, `linked: bool = False`, `installed_at: str = ""`; methods `to_dict() -> dict`, classmethod `from_dict(name, data) -> InstalledPlugin`.
  - `global_plugins_dir() -> Path` → `config_dir() / "plugins"`
  - `project_plugins_dir(workspace_root) -> Path` → `<ws>/.marim/plugins`
  - `state_path(plugins_dir: Path) -> Path` → `plugins_dir / "plugins.json"`
  - `load_state(plugins_dir: Path) -> dict[str, InstalledPlugin]` (missing/malformed → `{}`)
  - `save_state(plugins_dir: Path, state: dict[str, InstalledPlugin]) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/plugins/test_state.py`:

```python
from marim_harness.plugins.state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    save_state,
    state_path,
)


def test_roundtrip(tmp_path):
    pdir = tmp_path / "plugins"
    rec = InstalledPlugin(
        name="p",
        version="1.0.0",
        source={"type": "local", "path": "/src/p"},
        enabled=True,
        trusted=False,
        linked=False,
        installed_at="2026-06-22T00:00:00Z",
    )
    save_state(pdir, {"p": rec})
    loaded = load_state(pdir)
    assert loaded["p"] == rec


def test_missing_state_is_empty(tmp_path):
    assert load_state(tmp_path / "nope") == {}


def test_malformed_state_is_empty(tmp_path):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    state_path(pdir).write_text("{ not json", encoding="utf-8")
    assert load_state(pdir) == {}


def test_from_dict_defaults():
    rec = InstalledPlugin.from_dict("x", {"source": {"type": "local"}})
    assert rec.name == "x"
    assert rec.enabled is True
    assert rec.trusted is False
    assert rec.version is None


def test_scope_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert global_plugins_dir() == tmp_path / "cfg" / "marim" / "plugins"
    assert project_plugins_dir(tmp_path) == tmp_path / ".marim" / "plugins"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/plugins/test_state.py -q`
Expected: FAIL — `ModuleNotFoundError: ... plugins.state`.

- [ ] **Step 3: Implement `state.py`**

Create `src/marim_harness/plugins/state.py`:

```python
"""The per-scope installed-plugin registry, stored as ``plugins.json`` inside
each scope's ``plugins/`` directory. A missing or malformed registry reads as
empty — never fatal."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import config_dir


@dataclass
class InstalledPlugin:
    """One registry entry: where the plugin came from and its current state."""

    name: str
    version: str | None
    source: dict
    enabled: bool = True
    trusted: bool = False
    linked: bool = False
    installed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source": self.source,
            "enabled": self.enabled,
            "trusted": self.trusted,
            "linked": self.linked,
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "InstalledPlugin":
        return cls(
            name=name,
            version=data.get("version"),
            source=data.get("source") or {},
            enabled=bool(data.get("enabled", True)),
            trusted=bool(data.get("trusted", False)),
            linked=bool(data.get("linked", False)),
            installed_at=str(data.get("installed_at", "") or ""),
        )


def global_plugins_dir() -> Path:
    """Global plugin cache + registry (``~/.config/marim/plugins/``)."""
    return config_dir() / "plugins"


def project_plugins_dir(workspace_root) -> Path:
    """Project plugin cache + registry (``<ws>/.marim/plugins/``)."""
    return Path(workspace_root) / ".marim" / "plugins"


def state_path(plugins_dir: Path) -> Path:
    return Path(plugins_dir) / "plugins.json"


def load_state(plugins_dir: Path) -> dict[str, "InstalledPlugin"]:
    path = state_path(plugins_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return {}
    out: dict[str, InstalledPlugin] = {}
    for name, entry in plugins.items():
        if isinstance(entry, dict):
            out[name] = InstalledPlugin.from_dict(name, entry)
    return out


def save_state(plugins_dir: Path, state: dict[str, "InstalledPlugin"]) -> None:
    path = state_path(plugins_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plugins": {name: rec.to_dict() for name, rec in state.items()}}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# Re-export to satisfy unused-import linters in modules that only use field().
__all__ = [
    "InstalledPlugin",
    "global_plugins_dir",
    "project_plugins_dir",
    "state_path",
    "load_state",
    "save_state",
]
_ = field  # keep import if a future field default is added
```

> Note: remove the trailing `_ = field` / `field` import if ruff flags it as unused — it is only a placeholder. Simpler: drop `field` from the import line entirely. Use `from dataclasses import dataclass` only.

Apply that simplification now — the import line should read:

```python
from dataclasses import dataclass
```

and delete the `_ = field` line and the comment above it.

- [ ] **Step 4: Add exports to `plugins/__init__.py`**

Add to the imports and `__all__` in `src/marim_harness/plugins/__init__.py`:

```python
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    save_state,
    state_path,
)
```

And extend `__all__` with: `"InstalledPlugin", "global_plugins_dir", "load_state", "project_plugins_dir", "save_state", "state_path"`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/plugins/test_state.py -q`
Expected: PASS.

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/marim_harness/plugins/ && uv run pyright src/marim_harness/plugins/`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/plugins/state.py src/marim_harness/plugins/__init__.py tests/plugins/test_state.py
git commit -m "feat(plugins): per-scope installed-plugin state registry"
```

---

### Task 3: Plugin discovery & contribution helpers

**Files:**
- Create: `src/marim_harness/plugins/discovery.py`
- Modify: `src/marim_harness/plugins/__init__.py`
- Test: `tests/plugins/test_discovery.py`

**Interfaces:**
- Consumes: `PluginManifest`/`try_load_manifest`/`substitute_root` (Task 1); `InstalledPlugin`/`load_state`/`global_plugins_dir`/`project_plugins_dir` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class ResolvedPlugin` with `name: str`, `scope: str` (`"project"`/`"global"`), `root: Path`, `record: InstalledPlugin`, `manifest: PluginManifest`. Property `enabled: bool`, `trusted: bool`.
  - `discover_plugins(workspace_root) -> list[ResolvedPlugin]` — both scopes, project shadows global by name; only entries whose dir + manifest load; **all** installed (enabled and disabled) returned, sorted by name (callers filter).
  - `plugin_skill_roots(workspace_root) -> list[tuple[str, Path]]` — `(plugin_name, skills_dir)` for **enabled** plugins.
  - `plugin_agent_roots(workspace_root) -> list[tuple[str, Path]]` — `(plugin_name, agents_dir)` for **enabled** plugins.
  - `plugin_instruction_texts(workspace_root) -> list[tuple[str, str]]` — `(plugin_name, text)` for **enabled** plugins with a readable `AGENTS.md`.
  - `plugin_hook_entries(workspace_root) -> dict` — merged `{event: [entry,...]}` from **enabled + trusted** plugins, `${MARIM_PLUGIN_ROOT}` substituted.
  - `plugin_mcp_specs(workspace_root) -> dict` — `{namespaced_name: spec}` from **enabled + trusted** plugins, `${MARIM_PLUGIN_ROOT}` substituted; server names namespaced `<plugin>_<server>`.
  - `plugin_bundle_summary(manifest) -> dict` — counts `{"skills": n, "agents": n, "hooks": n, "mcpServers": n}`; `has_executable(summary) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/plugins/test_discovery.py`:

```python
import json
from pathlib import Path

from marim_harness.plugins.discovery import (
    discover_plugins,
    has_executable,
    plugin_agent_roots,
    plugin_bundle_summary,
    plugin_hook_entries,
    plugin_instruction_texts,
    plugin_mcp_specs,
    plugin_skill_roots,
)
from marim_harness.plugins.manifest import load_manifest
from marim_harness.plugins.state import InstalledPlugin, save_state


def _make_plugin(plugins_dir: Path, name: str, *, manifest: dict, files: dict):
    pdir = plugins_dir / name
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, **manifest}), encoding="utf-8"
    )
    for rel, content in files.items():
        fp = pdir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    return pdir


def _install(plugins_dir: Path, name: str, **kw):
    state = {name: InstalledPlugin(name=name, version=None, source={"type": "local"}, **kw)}
    save_state(plugins_dir, state)


def _ws(tmp_path, monkeypatch):
    # Isolate both scopes inside tmp_path.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_discover_enabled_and_disabled(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "p1", manifest={}, files={})
    _install(gdir, "p1", enabled=True)
    found = discover_plugins(ws)
    assert [p.name for p in found] == ["p1"]
    assert found[0].scope == "global"
    assert found[0].enabled is True


def test_project_shadows_global(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    pdir = ws / ".marim" / "plugins"
    _make_plugin(gdir, "dup", manifest={"description": "global"}, files={})
    _install(gdir, "dup", enabled=True)
    _make_plugin(pdir, "dup", manifest={"description": "project"}, files={})
    _install(pdir, "dup", enabled=True)
    found = discover_plugins(ws)
    assert len(found) == 1
    assert found[0].scope == "project"
    assert found[0].manifest.description == "project"


def test_skill_and_agent_roots_only_enabled(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "on", manifest={}, files={"skills/.keep": ""})
    _install(gdir, "on", enabled=True)
    _make_plugin(gdir, "off", manifest={}, files={"skills/.keep": ""})
    _install(gdir, "off", enabled=False)
    roots = dict(plugin_skill_roots(ws))
    assert "on" in roots and "off" not in roots
    assert roots["on"] == (gdir / "on" / "skills").resolve()
    assert dict(plugin_agent_roots(ws)).get("on") == (gdir / "on" / "agents").resolve()


def test_hooks_and_mcp_require_trust(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    hooks = {"hooks": {"Stop": [{"type": "command", "command": "${MARIM_PLUGIN_ROOT}/x.sh"}]}}
    _make_plugin(
        gdir, "untrusted",
        manifest={},
        files={"hooks/hooks.json": json.dumps(hooks),
               "mcp.json": json.dumps({"mcpServers": {"web": {"url": "https://u"}}})},
    )
    _install(gdir, "untrusted", enabled=True, trusted=False)
    assert plugin_hook_entries(ws) == {}
    assert plugin_mcp_specs(ws) == {}

    _install(gdir, "untrusted", enabled=True, trusted=True)
    entries = plugin_hook_entries(ws)
    assert entries["Stop"][0]["command"] == str((gdir / "untrusted").resolve()) + "/x.sh"
    specs = plugin_mcp_specs(ws)
    assert "untrusted_web" in specs and specs["untrusted_web"]["url"] == "https://u"


def test_instruction_texts(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _make_plugin(gdir, "p", manifest={}, files={"AGENTS.md": "do the thing"})
    _install(gdir, "p", enabled=True)
    assert plugin_instruction_texts(ws) == [("p", "do the thing")]


def test_bundle_summary_and_has_executable(tmp_path):
    pdir = _make_plugin(
        tmp_path, "p",
        manifest={},
        files={"skills/s/SKILL.md": "x", "hooks/hooks.json": json.dumps({"hooks": {"Stop": [{}]}})},
    )
    m = load_manifest(pdir)
    summary = plugin_bundle_summary(m)
    assert summary["skills"] == 1
    assert summary["hooks"] == 1
    assert has_executable(summary) is True
    assert has_executable({"skills": 2, "agents": 1, "hooks": 0, "mcpServers": 0}) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/plugins/test_discovery.py -q`
Expected: FAIL — `ModuleNotFoundError: ... plugins.discovery`.

- [ ] **Step 3: Implement `discovery.py`**

Create `src/marim_harness/plugins/discovery.py`:

```python
"""Resolve installed plugins and turn their bundled content into contributions
for marim's existing discovery systems.

Skills, sub-agents, and instructions are contributed for any *enabled* plugin
(inert text the model reads). Hooks and MCP servers are contributed only for
*enabled + trusted* plugins, since they execute code. Project plugins shadow
global plugins of the same name."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .manifest import PluginManifest, substitute_root, try_load_manifest
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedPlugin:
    """An installed plugin whose directory and manifest both loaded."""

    name: str
    scope: str  # "project" or "global"
    root: Path
    record: InstalledPlugin
    manifest: PluginManifest

    @property
    def enabled(self) -> bool:
        return self.record.enabled

    @property
    def trusted(self) -> bool:
        return self.record.trusted


def _scope_dirs(workspace_root) -> list[tuple[str, Path]]:
    # Highest precedence first: project shadows global.
    return [
        ("project", project_plugins_dir(workspace_root)),
        ("global", global_plugins_dir()),
    ]


def discover_plugins(workspace_root) -> list[ResolvedPlugin]:
    """All installed plugins across both scopes (enabled and disabled), project
    shadowing global by name, sorted by name. Entries whose directory or
    manifest fails to load are skipped with a warning."""
    seen: dict[str, ResolvedPlugin] = {}
    for scope, plugins_dir in _scope_dirs(workspace_root):
        for name, record in load_state(plugins_dir).items():
            if name in seen:
                continue
            root = plugins_dir / name
            manifest = try_load_manifest(root)
            if manifest is None:
                continue
            seen[name] = ResolvedPlugin(name, scope, root, record, manifest)
    return sorted(seen.values(), key=lambda p: p.name)


def _enabled(workspace_root) -> list[ResolvedPlugin]:
    return [p for p in discover_plugins(workspace_root) if p.enabled]


def _enabled_trusted(workspace_root) -> list[ResolvedPlugin]:
    return [p for p in discover_plugins(workspace_root) if p.enabled and p.trusted]


def plugin_skill_roots(workspace_root) -> list[tuple[str, Path]]:
    return [(p.name, p.manifest.skills_dir()) for p in _enabled(workspace_root)]


def plugin_agent_roots(workspace_root) -> list[tuple[str, Path]]:
    return [(p.name, p.manifest.agents_dir()) for p in _enabled(workspace_root)]


def plugin_instruction_texts(workspace_root) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in _enabled(workspace_root):
        try:
            text = p.manifest.instructions_path().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            out.append((p.name, text))
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_hook_entries(workspace_root) -> dict:
    """Merged ``{event: [entry,...]}`` from enabled+trusted plugins, with
    ``${MARIM_PLUGIN_ROOT}`` substituted in each entry."""
    merged: dict = {}
    for p in _enabled_trusted(workspace_root):
        source = p.manifest.hooks_source()
        if isinstance(source, dict):
            hooks = source.get("hooks") if "hooks" in source else source
        else:
            hooks = _read_json(source).get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            merged.setdefault(event, []).extend(
                substitute_root(e, p.root) for e in entries
            )
    return merged


def plugin_mcp_specs(workspace_root) -> dict:
    """Merged ``{namespaced_name: spec}`` from enabled+trusted plugins. Server
    names are namespaced ``<plugin>_<server>`` so two plugins never collide on
    a tool prefix. ``${MARIM_PLUGIN_ROOT}`` is substituted in each spec."""
    merged: dict = {}
    for p in _enabled_trusted(workspace_root):
        source = p.manifest.mcp_source()
        if isinstance(source, dict):
            servers = source.get("mcpServers") if "mcpServers" in source else source
        else:
            servers = _read_json(source).get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for server_name, spec in servers.items():
            merged[f"{p.name}_{server_name}"] = substitute_root(spec, p.root)
    return merged


def plugin_bundle_summary(manifest: PluginManifest) -> dict:
    """Count what a plugin bundles, for the install-time trust prompt."""
    skills = _count_dirs_with(manifest.skills_dir(), "SKILL.md")
    agents = _count_files(manifest.agents_dir(), ".md")
    hooks = _count_hooks(manifest)
    mcp = _count_mcp(manifest)
    return {"skills": skills, "agents": agents, "hooks": hooks, "mcpServers": mcp}


def has_executable(summary: dict) -> bool:
    """Whether a bundle summary contains code-executing parts (hooks/MCP)."""
    return bool(summary.get("hooks")) or bool(summary.get("mcpServers"))


def _count_dirs_with(root: Path, marker: str) -> int:
    try:
        return sum(1 for d in root.iterdir() if d.is_dir() and (d / marker).is_file())
    except OSError:
        return 0


def _count_files(root: Path, suffix: str) -> int:
    try:
        return sum(1 for f in root.iterdir() if f.is_file() and f.suffix == suffix)
    except OSError:
        return 0


def _count_hooks(manifest: PluginManifest) -> int:
    source = manifest.hooks_source()
    hooks = source.get("hooks", source) if isinstance(source, dict) else _read_json(source).get("hooks")
    if not isinstance(hooks, dict):
        return 0
    return sum(len(v) for v in hooks.values() if isinstance(v, list))


def _count_mcp(manifest: PluginManifest) -> int:
    source = manifest.mcp_source()
    servers = source.get("mcpServers", source) if isinstance(source, dict) else _read_json(source).get("mcpServers")
    return len(servers) if isinstance(servers, dict) else 0
```

- [ ] **Step 4: Export discovery helpers from `plugins/__init__.py`**

Add:

```python
from .discovery import (
    ResolvedPlugin,
    discover_plugins,
    has_executable,
    plugin_agent_roots,
    plugin_bundle_summary,
    plugin_hook_entries,
    plugin_instruction_texts,
    plugin_mcp_specs,
    plugin_skill_roots,
)
```

and add each name to `__all__`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/plugins/test_discovery.py -q`
Expected: PASS.

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/marim_harness/plugins/ tests/plugins/ && uv run pyright src/marim_harness/plugins/`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/plugins/discovery.py src/marim_harness/plugins/__init__.py tests/plugins/test_discovery.py
git commit -m "feat(plugins): discovery and contribution helpers"
```

---

### Task 4: Surface plugin skills (namespaced) in `discover_skills`

**Files:**
- Modify: `src/marim_harness/workspace/skills.py`
- Test: `tests/test_skills.py` (add cases; create if absent under `tests/`)

**Interfaces:**
- Consumes: `plugin_skill_roots(workspace_root)` (Task 3).
- Produces: `Skill.plugin: str | None` field; `Skill.qualified_name` property; `discover_skills`/`find_skill`/`skills_index_text` keyed and matched on `qualified_name`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_skills.py` (create the file with this content if it does not exist):

```python
import json
from pathlib import Path

from marim_harness.plugins.state import InstalledPlugin, save_state
from marim_harness.workspace.skills import discover_skills, find_skill, skills_index_text


def _skill(root: Path, name: str, desc: str):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody", encoding="utf-8")


def _install_plugin_with_skill(plugins_dir: Path, plugin: str, skill: str):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(json.dumps({"name": plugin}), encoding="utf-8")
    _skill(pdir / "skills", skill, "from plugin")
    save_state(plugins_dir, {plugin: InstalledPlugin(name=plugin, version=None, source={"type": "local"}, enabled=True)})


def test_plugin_skill_is_namespaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_skill(gdir, "myplugin", "review")
    names = [s.qualified_name for s in discover_skills(ws)]
    assert "myplugin:review" in names
    found = find_skill(ws, "myplugin:review")
    assert found is not None and found.plugin == "myplugin"
    assert "- myplugin:review — from plugin" in skills_index_text(discover_skills(ws))


def test_user_skill_beats_plugin_same_bare_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    (ws / ".marim" / "skills").mkdir(parents=True)
    _skill(ws / ".marim" / "skills", "review", "user owned")
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_skill(gdir, "myplugin", "review")
    by_name = {s.qualified_name: s for s in discover_skills(ws)}
    assert by_name["review"].description == "user owned"
    assert by_name["myplugin:review"].description == "from plugin"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_skills.py -q`
Expected: FAIL — `AttributeError: 'Skill' object has no attribute 'qualified_name'`.

- [ ] **Step 3: Add the `plugin` field and `qualified_name` to `Skill`**

In `src/marim_harness/workspace/skills.py`, modify the `Skill` dataclass (after `metadata`):

```python
@dataclass(frozen=True)
class Skill:
    """One discovered skill: its identity, where it lives, and its metadata.
    ``root`` is the skill's own (absolute) directory; ``source`` names the
    discovery root it came from (e.g. ``project`` or ``global``). ``plugin`` is
    the owning plugin's name when the skill came from a plugin, else None."""

    name: str
    description: str
    root: Path
    source: str
    disable_model_invocation: bool = False
    allowed_tools: str = ""  # parsed but not enforced in v1
    metadata: dict = field(default_factory=dict)
    plugin: str | None = None

    @property
    def qualified_name(self) -> str:
        """The name used for display and lookup: ``plugin:name`` for plugin
        skills, the bare name otherwise."""
        return f"{self.plugin}:{self.name}" if self.plugin else self.name
```

- [ ] **Step 4: Thread `plugin` through `_parse_skill`**

Change the signature and the returned `Skill`:

```python
def _parse_skill(source: str, directory: Path, plugin: str | None = None) -> Skill | None:
```

and in the `return Skill(...)` add `plugin=plugin,` as the final argument.

- [ ] **Step 5: Add plugin roots to `discover_skills` keyed on the qualified name**

Replace `discover_skills` with:

```python
def discover_skills(workspace_root) -> list[Skill]:
    """All effective skills for a workspace, deduped by qualified name with the
    first root in precedence order winning, sorted for stable display. User
    roots (bare names) come first, then plugin roots (``plugin:name``), so a
    user's own skill always beats a plugin's same-named one."""
    from ..plugins import plugin_skill_roots

    seen: dict[str, Skill] = {}
    for source, root in skill_roots(workspace_root):
        _collect_skills(seen, source, root, plugin=None)
    for plugin_name, root in plugin_skill_roots(workspace_root):
        _collect_skills(seen, f"plugin:{plugin_name}", root, plugin=plugin_name)
    return sorted(seen.values(), key=lambda s: s.qualified_name)


def _collect_skills(seen: dict, source: str, root: Path, *, plugin: str | None) -> None:
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for directory in entries:
        skill = _parse_skill(source, directory, plugin=plugin)
        if skill is None:
            continue
        if skill.qualified_name in seen:
            continue  # a higher-precedence root already claimed this name
        seen[skill.qualified_name] = skill
```

- [ ] **Step 6: Match `find_skill` and the index on the qualified name**

Replace `find_skill`:

```python
def find_skill(workspace_root, name: str) -> Skill | None:
    """The effective skill whose qualified name is ``name``, or None."""
    for skill in discover_skills(workspace_root):
        if skill.qualified_name == name:
            return skill
    return None
```

Replace the comprehension in `skills_index_text` to use `s.qualified_name`:

```python
    lines = [
        f"- {s.qualified_name} — {s.description}"
        for s in skills
        if not s.disable_model_invocation
    ]
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_skills.py -q`
Expected: PASS.

- [ ] **Step 8: Run the full suite to catch regressions**

Run: `uv run pytest -q`
Expected: PASS (no existing test relies on `discover_skills` excluding plugin roots; the new `plugin` field defaults to None so existing `Skill(...)` constructions are unaffected).

- [ ] **Step 9: Lint**

Run: `uv run ruff check src/marim_harness/workspace/skills.py tests/test_skills.py && uv run pyright src/marim_harness/workspace/skills.py`
Expected: no errors.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/workspace/skills.py tests/test_skills.py
git commit -m "feat(plugins): surface plugin skills namespaced in discover_skills"
```

---

### Task 5: Surface plugin sub-agents (namespaced) in `discover_agents`

**Files:**
- Modify: `src/marim_harness/workspace/agents.py`
- Modify: `src/marim_harness/subagents.py`
- Test: `tests/test_agents.py` (add cases; create if absent)

**Interfaces:**
- Consumes: `plugin_agent_roots(workspace_root)` (Task 3).
- Produces: `AgentDef.plugin: str | None`; `AgentDef.qualified_name`; `discover_agents`/`find_agent`/`agents_index_text` keyed and matched on `qualified_name`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agents.py` (create the file with this content if it does not exist):

```python
import json
from pathlib import Path

from marim_harness.plugins.state import InstalledPlugin, save_state
from marim_harness.workspace.agents import (
    agents_index_text,
    discover_agents,
    find_agent,
)


def _install_plugin_with_agent(plugins_dir: Path, plugin: str, agent: str):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(json.dumps({"name": plugin}), encoding="utf-8")
    adir = pdir / "agents"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{agent}.md").write_text(
        f"---\nname: {agent}\ndescription: plugin agent\n---\nYou are {agent}.", encoding="utf-8"
    )
    save_state(plugins_dir, {plugin: InstalledPlugin(name=plugin, version=None, source={"type": "local"}, enabled=True)})


def test_plugin_agent_is_namespaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_agent(gdir, "myplugin", "reviewer")
    names = [a.qualified_name for a in discover_agents(ws)]
    assert "myplugin:reviewer" in names
    # built-ins still present with bare names
    assert "explore" in names and "general" in names
    found = find_agent(ws, "myplugin:reviewer")
    assert found is not None and found.plugin == "myplugin"
    assert "- myplugin:reviewer — plugin agent" in agents_index_text(discover_agents(ws))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agents.py -q`
Expected: FAIL — `AttributeError: 'AgentDef' object has no attribute 'qualified_name'`.

- [ ] **Step 3: Add `plugin` field and `qualified_name` to `AgentDef`**

In `src/marim_harness/workspace/agents.py`, modify the dataclass:

```python
@dataclass(frozen=True)
class AgentDef:
    """One sub-agent role: its identity, the system prompt that shapes it, and
    the tool names it may use (before the mode-based gating in effective_tools).
    ``source`` is ``built-in`` or the discovery root the file came from.
    ``plugin`` is the owning plugin's name when the agent came from a plugin."""

    name: str
    description: str
    prompt: str
    tools: frozenset[str]
    source: str
    plugin: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}:{self.name}" if self.plugin else self.name
```

- [ ] **Step 4: Thread `plugin` through `_parse_agent`**

Change signature and return:

```python
def _parse_agent(source: str, path: Path, plugin: str | None = None) -> AgentDef | None:
```

and add `plugin=plugin,` to the returned `AgentDef(...)`.

- [ ] **Step 5: Add plugin roots to `discover_agents` keyed on qualified name**

Replace `discover_agents`:

```python
def discover_agents(workspace_root) -> list[AgentDef]:
    """All effective sub-agents: custom definitions (user roots first, then
    plugin roots as ``plugin:name``) layered over the built-ins, deduped by
    qualified name with the highest-precedence root winning. Sorted by
    qualified name for stable display."""
    from ..plugins import plugin_agent_roots

    seen: dict[str, AgentDef] = {}
    for source, root in agent_roots(workspace_root):
        _collect_agents(seen, source, root, plugin=None)
    for plugin_name, root in plugin_agent_roots(workspace_root):
        _collect_agents(seen, f"plugin:{plugin_name}", root, plugin=plugin_name)
    for name, agent in _builtins().items():
        seen.setdefault(name, agent)
    return sorted(seen.values(), key=lambda a: a.qualified_name)


def _collect_agents(seen: dict, source: str, root: Path, *, plugin: str | None) -> None:
    try:
        files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix == ".md")
    except OSError:
        return
    for path in files:
        agent = _parse_agent(source, path, plugin=plugin)
        if agent is None:
            continue
        if agent.qualified_name in seen:
            continue  # a higher-precedence root already claimed this name
        seen[agent.qualified_name] = agent
```

- [ ] **Step 6: Match `find_agent` and the index on the qualified name**

Replace `find_agent`:

```python
def find_agent(workspace_root, name: str) -> AgentDef | None:
    """The effective sub-agent whose qualified name is ``name``, or None."""
    for agent in discover_agents(workspace_root):
        if agent.qualified_name == name:
            return agent
    return None
```

Change `agents_index_text` to use `a.qualified_name`:

```python
    return "\n".join(f"- {a.qualified_name} — {a.description}" for a in defs)
```

- [ ] **Step 7: Update the spawn error listing in `subagents.py`**

In `src/marim_harness/subagents.py` around line 73, change the unknown-agent error to list qualified names:

```python
            names = ", ".join(a.qualified_name for a in discover_agents(self.deps.workspace_root))
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agents.py -q`
Expected: PASS.

- [ ] **Step 9: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/marim_harness/workspace/agents.py src/marim_harness/subagents.py && uv run pyright src/marim_harness/workspace/agents.py`
Expected: PASS, no lint errors.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/workspace/agents.py src/marim_harness/subagents.py tests/test_agents.py
git commit -m "feat(plugins): surface plugin sub-agents namespaced in discover_agents"
```

---

### Task 6: Merge plugin hooks into `load_hooks_config`

**Files:**
- Modify: `src/marim_harness/hooks/config.py`
- Test: `tests/test_hooks_config.py` (add a case; create if absent)

**Interfaces:**
- Consumes: `plugin_hook_entries(workspace_root)` (Task 3).
- Produces: `load_hooks_config` additionally concatenates plugin hook entries (enabled+trusted) regardless of `trust_project`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hooks_config.py`:

```python
import json
from pathlib import Path

from marim_harness.hooks.config import load_hooks_config
from marim_harness.plugins.state import InstalledPlugin, save_state


def _install_plugin_with_hooks(plugins_dir: Path, plugin: str, trusted: bool):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(json.dumps({"name": plugin}), encoding="utf-8")
    (pdir / "hooks").mkdir(parents=True, exist_ok=True)
    (pdir / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"Stop": [{"type": "command", "command": "echo hi"}]}}), encoding="utf-8"
    )
    save_state(plugins_dir, {plugin: InstalledPlugin(
        name=plugin, version=None, source={"type": "local"}, enabled=True, trusted=trusted)})


def test_trusted_plugin_hooks_merged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_hooks(gdir, "p", trusted=True)
    cfg = load_hooks_config(ws, trust_project=False)
    assert cfg["Stop"][0]["command"] == "echo hi"


def test_untrusted_plugin_hooks_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_hooks(gdir, "p", trusted=False)
    cfg = load_hooks_config(ws, trust_project=False)
    assert cfg == {}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_hooks_config.py -q`
Expected: FAIL — `test_trusted_plugin_hooks_merged` fails with `KeyError: 'Stop'` (plugin hooks not yet merged).

- [ ] **Step 3: Merge plugin hook entries in `load_hooks_config`**

In `src/marim_harness/hooks/config.py`, replace `load_hooks_config`:

```python
def load_hooks_config(workspace_root: Path, *, trust_project: bool) -> dict:
    """Merge global hook entries, project entries (only when ``trust_project``),
    and entries from enabled+trusted plugins into one ``{event: [entry, ...]}``
    map. Per-event lists are concatenated. Plugin hooks are gated by per-plugin
    trust, independent of ``trust_project`` (which governs ``.marim/hooks.json``)."""
    from ..plugins import plugin_hook_entries

    merged: dict = {}
    _merge_into(merged, _read_hooks(global_hooks_config_path()))
    if trust_project:
        _merge_into(merged, _read_hooks(project_hooks_config_path(workspace_root)))
    _merge_into(merged, plugin_hook_entries(workspace_root))
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_hooks_config.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/marim_harness/hooks/config.py && uv run pyright src/marim_harness/hooks/config.py`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/hooks/config.py tests/test_hooks_config.py
git commit -m "feat(plugins): merge trusted plugin hooks into load_hooks_config"
```

---

### Task 7: Merge plugin MCP servers into `load_mcp_config`

**Files:**
- Modify: `src/marim_harness/mcp/config.py`
- Test: `tests/test_mcp_config.py` (add a case; create if absent)

**Interfaces:**
- Consumes: `plugin_mcp_specs(workspace_root)` (Task 3).
- Produces: `load_mcp_config` merges plugin MCP specs *under* the user (global/project) specs, so user-defined servers win on name.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_config.py`:

```python
import json
from pathlib import Path

from marim_harness.mcp.config import load_mcp_config
from marim_harness.plugins.state import InstalledPlugin, save_state


def _install_plugin_with_mcp(plugins_dir: Path, plugin: str, trusted: bool):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(json.dumps({"name": plugin}), encoding="utf-8")
    (pdir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"web": {"url": "https://plugin"}}}), encoding="utf-8"
    )
    save_state(plugins_dir, {plugin: InstalledPlugin(
        name=plugin, version=None, source={"type": "local"}, enabled=True, trusted=trusted)})


def test_trusted_plugin_mcp_merged_and_namespaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_mcp(gdir, "p", trusted=True)
    specs = load_mcp_config(ws)
    assert "p_web" in specs
    assert specs["p_web"]["url"] == "https://plugin"


def test_untrusted_plugin_mcp_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_mcp(gdir, "p", trusted=False)
    assert load_mcp_config(ws) == {}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_mcp_config.py -q`
Expected: FAIL — `test_trusted_plugin_mcp_merged_and_namespaced` fails (`p_web` missing).

- [ ] **Step 3: Merge plugin MCP specs under user specs in `load_mcp_config`**

In `src/marim_harness/mcp/config.py`, replace `load_mcp_config`:

```python
def load_mcp_config(workspace_root: Path) -> dict:
    """Merge MCP server specs into one name->spec mapping. Precedence, lowest
    first: enabled+trusted plugin servers (namespaced ``<plugin>_<server>``),
    then global, then project — so a user's own server wins on name."""
    from ..plugins import plugin_mcp_specs

    merged = dict(plugin_mcp_specs(workspace_root))
    merged.update(_read_servers(global_mcp_config_path()))
    merged.update(_read_servers(project_mcp_config_path(workspace_root)))
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mcp_config.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/marim_harness/mcp/config.py && uv run pyright src/marim_harness/mcp/config.py`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/mcp/config.py tests/test_mcp_config.py
git commit -m "feat(plugins): merge trusted plugin MCP servers into load_mcp_config"
```

---

### Task 8: Inject plugin instructions (`AGENTS.md`)

**Files:**
- Modify: `src/marim_harness/instructions.py`
- Test: `tests/test_instructions.py` (add a case; create if absent)

**Interfaces:**
- Consumes: `plugin_instruction_texts(workspace_root)` (Task 3).
- Produces: a new `@agent.instructions` closure `_plugin_instructions` injecting each enabled plugin's `AGENTS.md`, labeled by plugin name.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_instructions.py` a closure-level test that does not require a full agent. Since the closures are defined inside `register_instructions`, test the underlying helper instead by asserting `plugin_instruction_texts` is wired. Add:

```python
import json
from pathlib import Path

from marim_harness.plugins.discovery import plugin_instruction_texts
from marim_harness.plugins.state import InstalledPlugin, save_state


def test_plugin_instruction_texts_used(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    pdir = gdir / "p"
    (pdir / ".marim-plugin").mkdir(parents=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(json.dumps({"name": "p"}), encoding="utf-8")
    (pdir / "AGENTS.md").write_text("plugin says hi", encoding="utf-8")
    save_state(gdir, {"p": InstalledPlugin(name="p", version=None, source={"type": "local"}, enabled=True)})
    assert plugin_instruction_texts(ws) == [("p", "plugin says hi")]
```

> This validates the data source the closure consumes. The closure itself is exercised by the integration test in Task 10 via a real harness build.

- [ ] **Step 2: Run the test to verify it passes for the helper but the closure is absent**

Run: `uv run pytest tests/test_instructions.py -q`
Expected: PASS for the helper test (the helper exists from Task 3). Proceed to add the closure so plugin instructions actually reach the model.

- [ ] **Step 3: Add the `_plugin_instructions` closure**

In `src/marim_harness/instructions.py`, add the import near the top (after the existing `from .workspace import (...)` block):

```python
from .plugins import plugin_instruction_texts
```

Inside `register_instructions`, add a new closure after `_project_instructions`:

```python
    @agent.instructions
    def _plugin_instructions(ctx: RunContext[Deps]) -> str:
        texts = plugin_instruction_texts(ctx.deps.workspace_root)
        if not texts:
            return ""
        blocks = [f"## From plugin '{name}'\n\n{text}" for name, text in texts]
        return (
            "Instructions contributed by installed plugins (treat like "
            "project instructions):\n\n" + "\n\n".join(blocks)
        )
```

- [ ] **Step 4: Add a regression test that the closure is registered**

Add to `tests/test_instructions.py`:

```python
def test_plugin_instructions_closure_registered():
    import inspect
    from marim_harness import instructions as mod

    src = inspect.getsource(mod.register_instructions)
    assert "_plugin_instructions" in src
    assert "plugin_instruction_texts" in src
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_instructions.py -q`
Expected: PASS.

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/marim_harness/instructions.py && uv run pyright src/marim_harness/instructions.py`
Expected: PASS, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/instructions.py tests/test_instructions.py
git commit -m "feat(plugins): inject enabled plugins' AGENTS.md into instructions"
```

---

### Task 9: Install / lifecycle core (`install.py`)

**Files:**
- Create: `src/marim_harness/plugins/install.py`
- Modify: `src/marim_harness/plugins/__init__.py`
- Test: `tests/plugins/test_install.py`

**Interfaces:**
- Consumes: `load_manifest`/`ManifestError` (Task 1); `InstalledPlugin`/`load_state`/`save_state`/`global_plugins_dir`/`project_plugins_dir` (Task 2); `plugin_bundle_summary`/`has_executable`/`discover_plugins` (Task 3).
- Produces:
  - `class InstallError(Exception)`
  - `scope_dir(scope: str, workspace_root) -> Path` (`"global"`/`"project"`)
  - `is_git_source(source: str) -> bool`
  - `install_plugin(source: str, *, scope: str, workspace_root, trust: bool, link: bool = False, name_override: str | None = None, now: str) -> InstalledPlugin`
  - `remove_plugin(name: str, *, scope: str, workspace_root) -> bool`
  - `set_enabled(name: str, *, scope: str, workspace_root, enabled: bool) -> bool`
  - `set_trusted(name: str, *, scope: str, workspace_root, trusted: bool) -> bool`
  - `update_plugin(name: str, *, scope: str, workspace_root, now: str) -> InstalledPlugin`
  - `_run_git(args: list[str], cwd: Path | None = None) -> str` (monkeypatchable seam)

> `now` is an ISO-8601 timestamp passed in by the caller (the CLI), keeping `install.py` free of clock calls for deterministic tests.

- [ ] **Step 1: Write the failing test**

Create `tests/plugins/test_install.py`:

```python
import json
from pathlib import Path

import pytest

from marim_harness.plugins.install import (
    InstallError,
    install_plugin,
    is_git_source,
    remove_plugin,
    set_enabled,
    set_trusted,
)
from marim_harness.plugins.state import load_state


def _make_source(src: Path, name: str, *, with_hooks: bool = False):
    (src / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (src / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    sk = src / "skills" / "demo"
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nx", encoding="utf-8")
    if with_hooks:
        (src / "hooks").mkdir(parents=True, exist_ok=True)
        (src / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": [{"type": "command", "command": "echo"}]}}), encoding="utf-8"
        )


def test_is_git_source():
    assert is_git_source("https://github.com/a/b.git")
    assert is_git_source("git@github.com:a/b.git")
    assert not is_git_source("/local/path")
    assert not is_git_source("./rel")


def test_install_local_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    rec = install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")
    assert rec.name == "demo"
    assert rec.version == "1.0.0"
    assert rec.trusted is True  # no executable parts -> auto-trusted
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    assert (gdir / "demo" / ".marim-plugin" / "plugin.json").is_file()
    assert "demo" in load_state(gdir)


def test_install_with_hooks_respects_trust_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "exec", with_hooks=True)
    rec = install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")
    assert rec.trusted is False  # executable, not trusted unless asked
    rec2 = install_plugin(str(src), scope="global", workspace_root=ws, trust=True, now="T")
    assert rec2.trusted is True


def test_install_rejects_bad_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(InstallError):
        install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")


def test_install_link_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    rec = install_plugin(str(src), scope="global", workspace_root=ws, trust=False, link=True, now="T")
    assert rec.linked is True
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    assert (gdir / "demo").is_symlink()


def test_enable_disable_trust_and_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")
    gdir = tmp_path / "cfg" / "marim" / "plugins"

    assert set_enabled("demo", scope="global", workspace_root=ws, enabled=False) is True
    assert load_state(gdir)["demo"].enabled is False
    assert set_trusted("demo", scope="global", workspace_root=ws, trusted=True) is True
    assert load_state(gdir)["demo"].trusted is True
    assert remove_plugin("demo", scope="global", workspace_root=ws) is True
    assert "demo" not in load_state(gdir)
    assert not (gdir / "demo").exists()


def test_install_from_local_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    # Build a real local git repo containing a plugin.
    repo = tmp_path / "repo"
    _make_source(repo, "gitdemo")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    url = repo.as_uri() + "/.git" if False else str(repo)  # local path clone
    rec = install_plugin(url, scope="global", workspace_root=ws, trust=False, now="T", _force_git=True)
    assert rec.name == "gitdemo"
    assert rec.source["type"] == "git"
    assert rec.source.get("sha")
```

> The final test uses `git` on a local path with a `_force_git=True` test hook so we exercise the clone path without a network. Implement `install_plugin` to accept `_force_git` (default False) that treats `source` as git even when it is a local path.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/plugins/test_install.py -q`
Expected: FAIL — `ModuleNotFoundError: ... plugins.install`.

- [ ] **Step 3: Implement `install.py`**

Create `src/marim_harness/plugins/install.py`:

```python
"""Install, update, and manage the lifecycle of plugins.

Installing copies (or, with ``link``, symlinks) a plugin's directory into the
target scope's ``plugins/`` cache, validates its manifest strictly, and records
it in the scope registry. A plugin with no executable parts (hooks/MCP) is
auto-trusted; one with executable parts is trusted only when ``trust`` is set.
Git sources are shallow-cloned to a temp dir and copied in, recording the
resolved commit SHA."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .discovery import has_executable, plugin_bundle_summary
from .manifest import ManifestError, load_manifest
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    save_state,
)

logger = logging.getLogger(__name__)


class InstallError(Exception):
    """An install/update/lifecycle operation failed."""


def scope_dir(scope: str, workspace_root) -> Path:
    if scope == "global":
        return global_plugins_dir()
    if scope == "project":
        return project_plugins_dir(workspace_root)
    raise InstallError(f"unknown scope: {scope!r} (use 'global' or 'project')")


def is_git_source(source: str) -> bool:
    s = source.strip()
    if s.startswith(("http://", "https://", "git://", "ssh://")):
        return True
    if s.startswith("git@"):
        return True
    return s.endswith(".git")


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise InstallError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise InstallError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return out.stdout.strip()


def _clone_git(source: str, dest: Path, ref: str | None) -> dict:
    """Clone ``source`` into ``dest`` and return a source record with the
    resolved SHA."""
    args = ["clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += [source, str(dest)]
    _run_git(args)
    sha = _run_git(["rev-parse", "HEAD"], cwd=dest)
    record = {"type": "git", "url": source, "sha": sha}
    if ref:
        record["ref"] = ref
    return record


def _validated_manifest(plugin_dir: Path):
    try:
        return load_manifest(plugin_dir)
    except ManifestError as exc:
        raise InstallError(str(exc)) from exc


def _materialize(src_dir: Path, dest: Path, *, link: bool) -> None:
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if link:
        dest.symlink_to(src_dir.resolve(), target_is_directory=True)
    else:
        shutil.copytree(src_dir, dest)


def install_plugin(
    source: str,
    *,
    scope: str,
    workspace_root,
    trust: bool,
    link: bool = False,
    name_override: str | None = None,
    now: str,
    _force_git: bool = False,
) -> InstalledPlugin:
    """Install ``source`` (a local dir or git URL) into ``scope``. Returns the
    written registry record."""
    target_root = scope_dir(scope, workspace_root)
    use_git = _force_git or is_git_source(source)

    with tempfile.TemporaryDirectory() as tmp:
        if use_git:
            staging = Path(tmp) / "clone"
            source_record = _clone_git(source, staging, ref=None)
            if link:
                raise InstallError("--link is only valid for local sources")
        else:
            staging = Path(source)
            if not staging.is_dir():
                raise InstallError(f"not a directory: {source}")
            source_record = {"type": "local", "path": str(staging.resolve())}

        manifest = _validated_manifest(staging)
        name = name_override or manifest.name
        summary = plugin_bundle_summary(manifest)
        trusted = True if not has_executable(summary) else bool(trust)

        dest = target_root / name
        # For git, copy regardless of link (link only applies to local sources).
        _materialize(staging, dest, link=link and not use_git)

    record = InstalledPlugin(
        name=name,
        version=manifest.version,
        source=source_record,
        enabled=True,
        trusted=trusted,
        linked=bool(link and not use_git),
        installed_at=now,
    )
    state = load_state(target_root)
    state[name] = record
    save_state(target_root, state)
    return record


def _mutate(name: str, scope: str, workspace_root, fn) -> bool:
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    rec = state.get(name)
    if rec is None:
        return False
    fn(rec)
    save_state(target_root, state)
    return True


def set_enabled(name: str, *, scope: str, workspace_root, enabled: bool) -> bool:
    return _mutate(name, scope, workspace_root, lambda r: setattr(r, "enabled", enabled))


def set_trusted(name: str, *, scope: str, workspace_root, trusted: bool) -> bool:
    return _mutate(name, scope, workspace_root, lambda r: setattr(r, "trusted", trusted))


def remove_plugin(name: str, *, scope: str, workspace_root) -> bool:
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    if name not in state:
        return False
    dest = target_root / name
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    del state[name]
    save_state(target_root, state)
    return True


def update_plugin(name: str, *, scope: str, workspace_root, now: str) -> InstalledPlugin:
    """Re-fetch a git-sourced plugin to the latest of its ref. Local/linked
    plugins cannot be updated this way."""
    target_root = scope_dir(scope, workspace_root)
    state = load_state(target_root)
    rec = state.get(name)
    if rec is None:
        raise InstallError(f"plugin not installed: {name}")
    if rec.source.get("type") != "git":
        raise InstallError(f"{name} was not installed from git; reinstall to update")
    url = rec.source["url"]
    ref = rec.source.get("ref")
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "clone"
        source_record = _clone_git(url, staging, ref=ref)
        manifest = _validated_manifest(staging)
        _materialize(staging, target_root / name, link=False)
    rec.version = manifest.version
    rec.source = source_record
    rec.installed_at = now
    save_state(target_root, state)
    return rec
```

- [ ] **Step 4: Export install API from `plugins/__init__.py`**

Add:

```python
from .install import (
    InstallError,
    install_plugin,
    is_git_source,
    remove_plugin,
    scope_dir,
    set_enabled,
    set_trusted,
    update_plugin,
)
```

and add each to `__all__`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/plugins/test_install.py -q`
Expected: PASS (the git test requires `git` on PATH; it is present in this dev environment).

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/marim_harness/plugins/ tests/plugins/ && uv run pyright src/marim_harness/plugins/`
Expected: PASS, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/plugins/install.py src/marim_harness/plugins/__init__.py tests/plugins/test_install.py
git commit -m "feat(plugins): install/update/lifecycle core"
```

---

### Task 10: `marim plugin …` CLI

**Files:**
- Create: `src/marim_harness/interfaces/cli/plugin.py`
- Modify: `src/marim_harness/interfaces/cli/router.py`
- Test: `tests/cli/test_plugin_cmd.py`

**Interfaces:**
- Consumes: install API (Task 9); `discover_plugins`/`plugin_bundle_summary`/`has_executable` (Task 3).
- Produces: `main(argv: list[str], *, out=sys.stdout, err=sys.stderr, input_fn=input, now_fn=...) -> int` with subcommands `install`, `list`, `info`, `enable`, `disable`, `trust`, `remove`, `update`, `validate`. Router routes the `plugin` keyword.

- [ ] **Step 1: Write the failing test**

Create `tests/cli/__init__.py` (empty) if missing, and `tests/cli/test_plugin_cmd.py`:

```python
import io
import json
from pathlib import Path

from marim_harness.interfaces.cli import plugin as plugin_cmd


def _make_source(src: Path, name: str, *, with_hooks: bool = False):
    (src / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (src / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "description": "d"}), encoding="utf-8"
    )
    sk = src / "skills" / "demo"
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nx", encoding="utf-8")
    if with_hooks:
        (src / "hooks").mkdir(parents=True, exist_ok=True)
        (src / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": [{"type": "command", "command": "echo"}]}}), encoding="utf-8"
        )


def _run(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = plugin_cmd.main(argv, out=out, err=err, now_fn=lambda: "T", **kw)
    return code, out.getvalue(), err.getvalue()


def test_install_inert_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "demo")
    code, out, err = _run(["install", str(src)])
    assert code == 0, err
    code, out, err = _run(["list"])
    assert "demo" in out
    assert "enabled" in out


def test_install_executable_prompts_for_trust(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "exec", with_hooks=True)
    # Decline trust at the prompt.
    code, out, err = _run(["install", str(src)], input_fn=lambda _p: "n")
    assert code == 0
    code, out, err = _run(["list", "--json"])
    rec = {p["name"]: p for p in json.loads(out)}["exec"]
    assert rec["trusted"] is False


def test_install_trust_flag_headless(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "exec", with_hooks=True)

    def _no_input(_p):
        raise AssertionError("must not prompt when --trust is given")

    code, out, err = _run(["install", str(src), "--trust"], input_fn=_no_input)
    assert code == 0
    code, out, err = _run(["list", "--json"])
    rec = {p["name"]: p for p in json.loads(out)}["exec"]
    assert rec["trusted"] is True


def test_enable_disable_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    _make_source(src, "demo")
    _run(["install", str(src)])
    assert _run(["disable", "demo"])[0] == 0
    assert _run(["enable", "demo"])[0] == 0
    assert _run(["remove", "demo"])[0] == 0
    code, out, err = _run(["info", "demo"])
    assert code != 0  # gone


def test_validate(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _make_source(src, "demo")
    code, out, err = _run(["validate", str(src)])
    assert code == 0
    bad = tmp_path / "bad"
    bad.mkdir()
    assert _run(["validate", str(bad)])[0] != 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/cli/test_plugin_cmd.py -q`
Expected: FAIL — `ModuleNotFoundError: ... cli.plugin`.

- [ ] **Step 3: Implement `interfaces/cli/plugin.py`**

Create `src/marim_harness/interfaces/cli/plugin.py`:

```python
"""``marim plugin ...`` — install and manage plugins."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ...plugins import (
    InstallError,
    ManifestError,
    discover_plugins,
    has_executable,
    install_plugin,
    load_manifest,
    plugin_bundle_summary,
    remove_plugin,
    scope_dir,
    set_enabled,
    set_trusted,
    update_plugin,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marim plugin", add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    inst = sub.add_parser("install", help="Install a plugin from a path or git URL.")
    inst.add_argument("source")
    inst.add_argument("--scope", choices=("global", "project"), default="global")
    inst.add_argument("--trust", action="store_true", help="Trust executable parts (hooks/MCP).")
    inst.add_argument("--link", action="store_true", help="Symlink a local source instead of copying.")
    inst.add_argument("--name", default=None, help="Override the installed name.")

    lst = sub.add_parser("list", help="List installed plugins.")
    lst.add_argument("--json", action="store_true")

    for name, help_ in (
        ("info", "Show one plugin's details."),
        ("enable", "Enable a plugin."),
        ("disable", "Disable a plugin."),
        ("trust", "Trust a plugin's executable parts."),
        ("remove", "Uninstall a plugin."),
        ("update", "Re-fetch a git-sourced plugin."),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("name")
        p.add_argument("--scope", choices=("global", "project"), default="global")

    val = sub.add_parser("validate", help="Validate a plugin directory's manifest.")
    val.add_argument("path")
    return parser


def _scope_of(name, workspace_root, preferred):
    """Find which scope a plugin is installed in, preferring ``preferred``."""
    for p in discover_plugins(workspace_root):
        if p.name == name and (preferred is None or p.scope == preferred):
            return p.scope
    for p in discover_plugins(workspace_root):
        if p.name == name:
            return p.scope
    return None


def _cmd_install(args, *, ws, out, err, input_fn, now_fn) -> int:
    source = args.source
    trust = args.trust
    # If executable and trust not pre-granted, prompt (interactive only).
    if not trust:
        manifest_dir = Path(source)
        try:
            manifest = load_manifest(manifest_dir) if manifest_dir.is_dir() else None
        except ManifestError:
            manifest = None
        if manifest is not None:
            summary = plugin_bundle_summary(manifest)
            if has_executable(summary):
                print(
                    f"Plugin {manifest.name!r} bundles "
                    f"{summary['skills']} skills, {summary['agents']} agents, "
                    f"{summary['hooks']} hooks, {summary['mcpServers']} MCP servers.",
                    file=out,
                )
                answer = input_fn("Trust this plugin's hooks/MCP servers? [y/N] ").strip().lower()
                trust = answer in ("y", "yes")
    try:
        rec = install_plugin(
            source, scope=args.scope, workspace_root=ws, trust=trust,
            link=args.link, name_override=args.name, now=now_fn(),
        )
    except InstallError as exc:
        print(f"error: {exc}", file=err)
        return 1
    state = "trusted" if rec.trusted else "untrusted"
    print(f"installed {rec.name} ({rec.version or 'unknown'}) [{args.scope}, {state}]", file=out)
    return 0


def _cmd_list(args, *, ws, out, err) -> int:
    plugins = discover_plugins(ws)
    if args.json:
        print(json.dumps([
            {"name": p.name, "scope": p.scope, "version": p.record.version,
             "enabled": p.record.enabled, "trusted": p.record.trusted}
            for p in plugins
        ]), file=out)
        return 0
    if not plugins:
        print("no plugins installed", file=out)
        return 0
    for p in plugins:
        flags = "enabled" if p.record.enabled else "disabled"
        flags += ", trusted" if p.record.trusted else ", untrusted"
        print(f"{p.name}  [{p.scope}, {flags}]  {p.manifest.description}", file=out)
    return 0


def _cmd_info(args, *, ws, out, err) -> int:
    for p in discover_plugins(ws):
        if p.name == args.name:
            summary = plugin_bundle_summary(p.manifest)
            print(f"name:        {p.name}", file=out)
            print(f"version:     {p.record.version or 'unknown'}", file=out)
            print(f"scope:       {p.scope}", file=out)
            print(f"enabled:     {p.record.enabled}", file=out)
            print(f"trusted:     {p.record.trusted}", file=out)
            print(f"description: {p.manifest.description}", file=out)
            print(f"bundles:     {summary['skills']} skills, {summary['agents']} agents, "
                  f"{summary['hooks']} hooks, {summary['mcpServers']} MCP servers", file=out)
            print(f"source:      {p.record.source}", file=out)
            return 0
    print(f"error: plugin not found: {args.name}", file=err)
    return 1


def _cmd_toggle(args, *, ws, out, err, action) -> int:
    scope = _scope_of(args.name, ws, args.scope)
    if scope is None:
        print(f"error: plugin not found: {args.name}", file=err)
        return 1
    ok = action(scope)
    if not ok:
        print(f"error: could not update {args.name}", file=err)
        return 1
    print(f"{args.name}: ok", file=out)
    return 0


def _cmd_validate(args, *, out, err) -> int:
    try:
        manifest = load_manifest(Path(args.path))
    except ManifestError as exc:
        print(f"invalid: {exc}", file=err)
        return 1
    summary = plugin_bundle_summary(manifest)
    print(f"valid: {manifest.name} ({manifest.version or 'unknown'}) — "
          f"{summary['skills']} skills, {summary['agents']} agents, "
          f"{summary['hooks']} hooks, {summary['mcpServers']} MCP servers", file=out)
    return 0


def main(argv, *, out=sys.stdout, err=sys.stderr, input_fn=input, now_fn=_now) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    ws = Path.cwd()
    if args.cmd == "install":
        return _cmd_install(args, ws=ws, out=out, err=err, input_fn=input_fn, now_fn=now_fn)
    if args.cmd == "list":
        return _cmd_list(args, ws=ws, out=out, err=err)
    if args.cmd == "info":
        return _cmd_info(args, ws=ws, out=out, err=err)
    if args.cmd == "enable":
        return _cmd_toggle(args, ws=ws, out=out, err=err,
                           action=lambda s: set_enabled(args.name, scope=s, workspace_root=ws, enabled=True))
    if args.cmd == "disable":
        return _cmd_toggle(args, ws=ws, out=out, err=err,
                           action=lambda s: set_enabled(args.name, scope=s, workspace_root=ws, enabled=False))
    if args.cmd == "trust":
        return _cmd_toggle(args, ws=ws, out=out, err=err,
                           action=lambda s: set_trusted(args.name, scope=s, workspace_root=ws, trusted=True))
    if args.cmd == "remove":
        return _cmd_toggle(args, ws=ws, out=out, err=err,
                           action=lambda s: remove_plugin(args.name, scope=s, workspace_root=ws))
    if args.cmd == "update":
        scope = _scope_of(args.name, ws, args.scope)
        if scope is None:
            print(f"error: plugin not found: {args.name}", file=err)
            return 1
        try:
            rec = update_plugin(args.name, scope=scope, workspace_root=ws, now=now_fn())
        except InstallError as exc:
            print(f"error: {exc}", file=err)
            return 1
        print(f"updated {rec.name} to {rec.version or 'unknown'}", file=out)
        return 0
    if args.cmd == "validate":
        return _cmd_validate(args, out=out, err=err)
    parser.print_help(err)
    return 2
```

> Add `load_manifest` and `ManifestError` to the `plugins/__init__.py` exports if not already present (Task 1 exported them).

- [ ] **Step 4: Register the `plugin` keyword in the router**

In `src/marim_harness/interfaces/cli/router.py`, change line 13:

```python
_MANAGEMENT = {"sessions", "config", "models", "plugin"}
```

(The existing dispatch at lines 30-36 imports `.plugin` and calls its `main(argv[1:])` automatically.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/cli/test_plugin_cmd.py -q`
Expected: PASS.

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/marim_harness/interfaces/cli/plugin.py src/marim_harness/interfaces/cli/router.py tests/cli/ && uv run pyright src/marim_harness/interfaces/cli/plugin.py`
Expected: PASS, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/cli/plugin.py src/marim_harness/interfaces/cli/router.py tests/cli/__init__.py tests/cli/test_plugin_cmd.py
git commit -m "feat(plugins): marim plugin CLI (install/list/info/enable/disable/trust/remove/update/validate)"
```

---

### Task 11: `/plugin` TUI command

**Files:**
- Modify: `src/marim_harness/interfaces/tui/commands.py`
- Test: `tests/tui/test_plugin_command.py`

**Interfaces:**
- Consumes: `discover_plugins` (Task 3); `set_enabled` (Task 9).
- Produces: a `_cmd_plugin(app, arg)` handler and a `Command("plugin", ...)` entry. Supports `/plugin` or `/plugin list`, `/plugin enable <name>`, `/plugin disable <name>`.

- [ ] **Step 1: Write the failing test**

Create `tests/tui/__init__.py` (empty) if missing, and `tests/tui/test_plugin_command.py`. The handler is async and uses a small fake app exposing `post_system` and `harness.deps.workspace_root`:

```python
import asyncio
import json
from pathlib import Path

from marim_harness.interfaces.tui.commands import _cmd_plugin
from marim_harness.plugins.state import InstalledPlugin, save_state


class _FakeDeps:
    def __init__(self, ws):
        self.workspace_root = ws


class _FakeHarness:
    def __init__(self, ws):
        self.deps = _FakeDeps(ws)


class _FakeApp:
    def __init__(self, ws):
        self.harness = _FakeHarness(ws)
        self.messages = []

    async def post_system(self, text):
        self.messages.append(text)


def _install(plugins_dir: Path, name: str, enabled=True):
    pdir = plugins_dir / name
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "description": "d"}), encoding="utf-8"
    )
    save_state(plugins_dir, {name: InstalledPlugin(
        name=name, version=None, source={"type": "local"}, enabled=enabled)})


def test_plugin_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    _install(tmp_path / "cfg" / "marim" / "plugins", "demo")
    app = _FakeApp(ws)
    asyncio.run(_cmd_plugin(app, "list"))
    assert any("demo" in m for m in app.messages)


def test_plugin_disable_then_enable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install(gdir, "demo")
    app = _FakeApp(ws)
    asyncio.run(_cmd_plugin(app, "disable demo"))
    from marim_harness.plugins.state import load_state
    assert load_state(gdir)["demo"].enabled is False
    asyncio.run(_cmd_plugin(app, "enable demo"))
    assert load_state(gdir)["demo"].enabled is True
    assert any("next launch" in m.lower() for m in app.messages)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/tui/test_plugin_command.py -q`
Expected: FAIL — `ImportError: cannot import name '_cmd_plugin'`.

- [ ] **Step 3: Implement the handler and register the command**

In `src/marim_harness/interfaces/tui/commands.py`, add the handler near the other `_cmd_*` functions (before the `COMMANDS` list):

```python
async def _cmd_plugin(app: HarnessApp, arg: str) -> None:
    from ...plugins import discover_plugins, set_enabled

    ws = app.harness.deps.workspace_root
    sub, _, rest = arg.partition(" ")
    sub = sub.strip().lower()
    name = rest.strip()

    if sub in ("", "list"):
        plugins = discover_plugins(ws)
        if not plugins:
            await app.post_system("No plugins installed. Install with `marim plugin install <path|git-url>`.")
            return
        lines = ["**Plugins**", ""]
        for p in plugins:
            state = "enabled" if p.record.enabled else "disabled"
            state += ", trusted" if p.record.trusted else ", untrusted"
            lines.append(f"- `{p.name}` [{p.scope}, {state}] — {p.manifest.description}")
        await app.post_system("\n".join(lines))
        return

    if sub in ("enable", "disable"):
        if not name:
            await app.post_system(f"Usage: `/plugin {sub} <name>`")
            return
        target = next((p for p in discover_plugins(ws) if p.name == name), None)
        if target is None:
            await app.post_system(f"Plugin not found: `{name}`")
            return
        set_enabled(name, scope=target.scope, workspace_root=ws, enabled=(sub == "enable"))
        await app.post_system(
            f"`{name}` {sub}d. Hooks/MCP changes take effect on next launch; "
            "skills and sub-agents refresh next turn."
        )
        return

    await app.post_system("Usage: `/plugin [list | enable <name> | disable <name>]`")
```

Add to the `COMMANDS` list (before the `settings`/`exit` entries):

```python
    Command("plugin", "list or toggle plugins: /plugin [list | enable <name> | disable <name>]", _cmd_plugin),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/tui/test_plugin_command.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/marim_harness/interfaces/tui/commands.py tests/tui/ && uv run pyright src/marim_harness/interfaces/tui/commands.py`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/commands.py tests/tui/__init__.py tests/tui/test_plugin_command.py
git commit -m "feat(plugins): /plugin TUI command (list/enable/disable)"
```

---

### Task 12: End-to-end integration test + docs

**Files:**
- Create: `tests/plugins/test_integration.py`
- Create: `tests/fixtures/plugins/demo-plugin/` (a fully-formed fixture plugin)
- Modify: `docs/` — add a short `docs/plugins.md` usage note; update `README` plugin section if one exists.

**Interfaces:**
- Consumes: everything.
- Produces: a test that installs a fixture plugin and asserts skills/agents surface (namespaced) and that hooks/MCP gate on trust; plus user docs.

- [ ] **Step 1: Create the fixture plugin**

Create these files:

`tests/fixtures/plugins/demo-plugin/.marim-plugin/plugin.json`:

```json
{
  "name": "demo-plugin",
  "version": "1.0.0",
  "description": "A demo plugin bundling a skill, an agent, a hook, and an MCP server.",
  "author": {"name": "marim"},
  "license": "MIT"
}
```

`tests/fixtures/plugins/demo-plugin/skills/greet/SKILL.md`:

```markdown
---
name: greet
description: Greet the user warmly.
---

Say hello to the user by name.
```

`tests/fixtures/plugins/demo-plugin/agents/reviewer.md`:

```markdown
---
name: reviewer
description: Reviews a diff for obvious mistakes.
---

You are a focused code reviewer. Report only concrete issues.
```

`tests/fixtures/plugins/demo-plugin/hooks/hooks.json`:

```json
{
  "hooks": {
    "Stop": [
      {"type": "command", "command": "${MARIM_PLUGIN_ROOT}/bin/notify.sh"}
    ]
  }
}
```

`tests/fixtures/plugins/demo-plugin/mcp.json`:

```json
{
  "mcpServers": {
    "docs": {"url": "https://example.com/mcp"}
  }
}
```

`tests/fixtures/plugins/demo-plugin/AGENTS.md`:

```markdown
When greeting, be concise and friendly.
```

- [ ] **Step 2: Write the integration test**

Create `tests/plugins/test_integration.py`:

```python
from pathlib import Path

from marim_harness.hooks.config import load_hooks_config
from marim_harness.mcp.config import load_mcp_config
from marim_harness.plugins.install import install_plugin, set_trusted
from marim_harness.workspace.agents import discover_agents
from marim_harness.workspace.skills import discover_skills

FIXTURE = Path(__file__).parent.parent / "fixtures" / "plugins" / "demo-plugin"


def test_full_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()

    # Install untrusted (it has hooks + MCP -> stays untrusted without --trust).
    rec = install_plugin(str(FIXTURE), scope="global", workspace_root=ws, trust=False, now="T")
    assert rec.trusted is False

    # Inert content surfaces immediately, namespaced.
    skills = {s.qualified_name for s in discover_skills(ws)}
    agents = {a.qualified_name for a in discover_agents(ws)}
    assert "demo-plugin:greet" in skills
    assert "demo-plugin:reviewer" in agents

    # Executable content is gated off while untrusted.
    assert load_hooks_config(ws, trust_project=False) == {}
    assert load_mcp_config(ws) == {}

    # Trusting turns on hooks + MCP, with ${MARIM_PLUGIN_ROOT} resolved and
    # the MCP server namespaced.
    set_trusted("demo-plugin", scope="global", workspace_root=ws, trusted=True)
    hooks = load_hooks_config(ws, trust_project=False)
    assert hooks["Stop"][0]["command"].endswith("/bin/notify.sh")
    assert "${MARIM_PLUGIN_ROOT}" not in hooks["Stop"][0]["command"]
    assert "demo-plugin_docs" in load_mcp_config(ws)
```

- [ ] **Step 3: Run the integration test**

Run: `uv run pytest tests/plugins/test_integration.py -q`
Expected: PASS.

- [ ] **Step 4: Write user docs**

Create `docs/plugins.md`:

```markdown
# Plugins

A plugin bundles skills, sub-agents, hooks, MCP servers, and optional
`AGENTS.md` instructions into one installable directory.

## Layout

    my-plugin/
    ├── .marim-plugin/plugin.json   # manifest (name required)
    ├── skills/<name>/SKILL.md
    ├── agents/<name>.md
    ├── hooks/hooks.json
    ├── mcp.json
    └── AGENTS.md

## Install

    marim plugin install <path|git-url> [--scope global|project] [--trust] [--link]
    marim plugin list
    marim plugin enable|disable|trust|remove|update <name>
    marim plugin validate <path>

In the TUI: `/plugin [list | enable <name> | disable <name>]`.

## Trust

Skills, sub-agents, and instructions load for any enabled plugin. Hooks and MCP
servers execute code, so they load only for plugins you trust. Installing a
plugin with hooks/MCP prompts for trust; pass `--trust` to grant it
non-interactively (e.g. in CI). Trust is recorded per plugin.

## Naming

Plugin skills and sub-agents are namespaced `plugin-name:item-name`, so they
never collide with your own. Your own skills/agents always take precedence.

## Scopes

`--scope global` (default) installs to `~/.config/marim/plugins/`; `--scope
project` installs to `<workspace>/.marim/plugins/` for sharing via git.
Enable/disable and trust changes to hooks/MCP take effect on next launch.
```

- [ ] **Step 5: Run the whole suite + lint a final time**

Run: `uv run pytest -q && uv run ruff check src tests && uv run pyright src`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add tests/plugins/test_integration.py tests/fixtures/plugins/ docs/plugins.md
git commit -m "test(plugins): end-to-end integration + user docs"
```

---

## Self-Review

**Spec coverage check (each spec section → task):**
- Plugin layout & manifest (`.marim-plugin/plugin.json`, component dirs, field names, `${MARIM_PLUGIN_ROOT}`) → Task 1. ✔
- Disk layout / state registry / scopes → Task 2 (state), Task 9 (install writes cache + state). ✔
- Precedence (user > project-plugin > global-plugin) → Tasks 3, 4, 5 (user roots iterated first; project scope shadows global in `discover_plugins`). ✔
- Trust model (inert always; hooks/MCP gated; auto-trust when no executable; headless `--trust`) → Tasks 3 (gating helpers), 9 (auto-trust logic), 10 (prompt + `--trust`). ✔
- Integration seams (skills, agents, hooks, MCP, instructions) → Tasks 4, 5, 6, 7, 8. ✔
- Namespacing (`plugin:name`, end-to-end resolution) → Tasks 4, 5 (+ subagents.py error listing). ✔
- Fail-safe discovery vs strict install → Task 1 (`try_load_manifest` vs `load_manifest`), Task 3 (skip-with-warning), Task 9/10 (`InstallError`). ✔
- CLI surface → Task 10. ✔
- TUI `/plugin` → Task 11. ✔
- Git + local install, `--link`, `update` → Task 9. ✔
- Testing (unit + integration + fixtures) → every task + Task 12. ✔

**Placeholder scan:** No "TBD"/"handle errors"/"similar to" placeholders; every code step shows complete code. One explicit cleanup instruction is called out in Task 2 Step 3 (drop the `field` import). ✔

**Type consistency:**
- `qualified_name` used consistently in Tasks 4 (`Skill`) and 5 (`AgentDef`), and in `find_skill`/`find_agent`/index/`subagents.py`. ✔
- `install_plugin(..., now=...)` signature matches CLI call in Task 10 (`now=now_fn()`). ✔
- `set_enabled`/`set_trusted`/`remove_plugin`/`update_plugin` keyword-only `scope`/`workspace_root` signatures match call sites in Tasks 10 and 11. ✔
- `plugin_skill_roots`/`plugin_agent_roots` return `list[tuple[str, Path]]`, consumed as `(plugin_name, root)` in Tasks 4/5. ✔
- `plugin_hook_entries`/`plugin_mcp_specs` return dicts merged in Tasks 6/7. ✔

**Note for the implementer on test files:** Follow the **Test File Conventions** table near the top of this plan — every plugin test goes in a new dedicated flat `tests/test_plugin_*.py` file, so there is no collision with the existing `tests/test_skills.py`, `tests/test_agents.py`, `tests/test_hooks_config.py`, `tests/test_mcp.py`, or `tests/test_instructions.py`. Do not append to or overwrite those existing files, and skip any `tests/<subdir>/__init__.py` creation step.
