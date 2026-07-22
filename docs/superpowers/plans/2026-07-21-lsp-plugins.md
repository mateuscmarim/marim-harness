# LSP Language Servers as Installable Plugins — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace marim's closed, hard-coded multilspy language set with a provider model assembled from bundled + third-party plugins, so any LSP server can be added by installing a plugin.

**Architecture:** A new `LspProvider` value object describes one language's server (extensions, probe binaries, install hint, and either a generic stdio `command` or a bundled-only in-tree `backend`). An `LspRegistry` is built per session from four bundled providers (loaded from in-tree bundle manifests) plus third-party providers discovered through the existing plugin system under the same MCP trust gate. `LspManager` and the bootstrap tool-registration gate take the injected registry instead of importing module globals. A new `GenericStdioServer(multilspy.LanguageServer)` launches declarative servers, reusing all of multilspy's client plumbing.

**Tech Stack:** Python ≥3.10, multilspy, pydantic-ai, pytest. No new runtime dependencies.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity ≤ 10 (no blanket `# noqa: C901`).
- Pure decision/parse helpers stay side-effect-free and unit-tested directly; effectful I/O tested against a tmp workspace.
- Run `uv run ruff check src tests` → `uv run pyright` → `uv run pytest` before claiming any task done. Use `uv` for everything.
- marim NEVER downloads language-server binaries. Declare + probe PATH + surface install hint only.
- Third-party plugins may use ONLY the declarative (`command`/`args`) form. The `backend` and named-`diagnostics` keys are bundled-only and MUST be rejected (strict) / ignored (lenient) for non-bundled sources.
- Third-party LSP providers follow the exact trust rule as plugin MCP servers: project-scope requires both the per-plugin `trusted` bit AND `MARIM_TRUST_PROJECT_HOOKS`; global-scope requires the per-plugin `trusted` bit. Bundled providers are always trusted.
- Preserve existing behavior byte-for-byte: python routes to `BasedPyrightServer` when `basedpyright-langserver` is present (jedi fallback) and its diagnostics go through `lsp/checks.py` (ruff always + pyright on deep).

---

## File Structure

**New files:**
- `src/marim_harness/lsp/provider.py` — `LspProvider` value object, `parse_lsp_providers()`, and `LspRegistry`.
- `src/marim_harness/lsp/bundled.py` — loader for the four in-tree bundled language plugins.
- `src/marim_harness/lsp/generic.py` — `GenericStdioServer(LanguageServer)`.
- `src/marim_harness/lsp/bundled/{python,typescript,cpp,java}/.marim-plugin/plugin.json` — bundled plugin manifests.
- `tests/test_lsp_provider.py`, `tests/test_lsp_bundled.py`, `tests/test_lsp_generic.py`, `tests/test_lsp_plugin_discovery.py`, `tests/fake_lsp_server.py`.

**Modified files:**
- `src/marim_harness/lsp/registry.py` — pure helpers parametrized by provider-derived maps; drop module globals `_EXT_TO_LANG`/`_PROBES`.
- `src/marim_harness/lsp/manager.py` — inject `registry`; registry-driven `_default_factory`; provider-driven `diagnostics()`.
- `src/marim_harness/plugins/manifest.py` — `PluginManifest.lsp_block()` accessor.
- `src/marim_harness/plugins/discovery.py` — `plugin_lsp_providers()`.
- `src/marim_harness/runtime/builder.py` — `with_lsp(...)` carries the provider list; thread into `HarnessConfig`.
- `src/marim_harness/runtime/bootstrap.py` — build registry from bundled + discovered providers; use it for the gate.
- `src/marim_harness/runtime/harness.py` — construct `LspManager(root, registry=...)`.
- `src/marim_harness/runtime/deps.py` — no change to `lsp` field type (still `LspManager | None`).
- `docs/plugins.md`, `CLAUDE.md`, `.env.example` — docs.

---

## Task 1: `LspProvider` value object + provider parsing

**Files:**
- Create: `src/marim_harness/lsp/provider.py` (provider + parser only; registry added in Task 2)
- Test: `tests/test_lsp_provider.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) LspProvider` with fields: `language: str`, `extensions: tuple[str, ...]`, `probe: tuple[str, ...]`, `install_hint: str`, `command: str | None`, `args: tuple[str, ...]`, `root_markers: tuple[str, ...]`, `env: tuple[tuple[str, str], ...]`, `backend: str | None`, `diagnostics: str`, `source: str`, `plugin_root: "Path | None"`.
  - `parse_lsp_providers(block, *, bundled: bool, source: str, plugin_root: "Path | None", strict: bool) -> list[LspProvider]`
  - `class LspProviderError(Exception)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lsp_provider.py
import pytest
from marim_harness.lsp.provider import (
    LspProvider,
    LspProviderError,
    parse_lsp_providers,
)


def test_parse_declarative_single():
    block = {
        "language": "go",
        "extensions": [".go"],
        "command": "gopls",
        "args": [],
        "rootMarkers": ["go.mod"],
        "env": {"GOFLAGS": "-mod=mod"},
        "probe": ["gopls"],
        "installHint": "install gopls",
    }
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert p.language == "go"
    assert p.extensions == (".go",)
    assert p.command == "gopls"
    assert p.root_markers == ("go.mod",)
    assert p.env == (("GOFLAGS", "-mod=mod"),)
    assert p.probe == ("gopls",)
    assert p.diagnostics == "lsp"
    assert p.backend is None
    assert p.source == "global"


def test_probe_defaults_to_command():
    block = {"language": "go", "extensions": [".go"], "command": "gopls"}
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert p.probe == ("gopls",)


def test_list_form():
    block = [
        {"language": "go", "extensions": [".go"], "command": "gopls"},
        {"language": "zig", "extensions": [".zig"], "command": "zls"},
    ]
    ps = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert [p.language for p in ps] == ["go", "zig"]


def test_backend_rejected_for_third_party_strict():
    block = {"language": "python", "extensions": [".py"], "backend": "basedpyright"}
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            block, bundled=False, source="global", plugin_root=None, strict=True
        )


def test_backend_ignored_for_third_party_lenient():
    block = {"language": "python", "extensions": [".py"], "backend": "basedpyright"}
    assert parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=False
    ) == []


def test_backend_allowed_for_bundled():
    block = {
        "language": "python",
        "extensions": [".py"],
        "backend": "basedpyright",
        "diagnostics": "python-checks",
        "probe": ["basedpyright-langserver", "jedi-language-server"],
        "installHint": "install basedpyright",
    }
    (p,) = parse_lsp_providers(
        block, bundled=True, source="bundled", plugin_root=None, strict=True
    )
    assert p.backend == "basedpyright"
    assert p.diagnostics == "python-checks"


def test_command_and_backend_mutually_exclusive():
    block = {
        "language": "python",
        "extensions": [".py"],
        "backend": "basedpyright",
        "command": "basedpyright-langserver",
    }
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            block, bundled=True, source="bundled", plugin_root=None, strict=True
        )


def test_missing_language_or_extensions_rejected_strict():
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            {"extensions": [".go"], "command": "gopls"},
            bundled=False, source="global", plugin_root=None, strict=True,
        )
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            {"language": "go", "command": "gopls"},
            bundled=False, source="global", plugin_root=None, strict=True,
        )


def test_no_launch_rejected_strict():
    # Neither command nor backend: nothing to launch.
    with pytest.raises(LspProviderError):
        parse_lsp_providers(
            {"language": "go", "extensions": [".go"]},
            bundled=False, source="global", plugin_root=None, strict=True,
        )


def test_extension_normalized_to_lowercase_with_dot():
    block = {"language": "go", "extensions": ["GO", ".Go"], "command": "gopls"}
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    assert p.extensions == (".go", ".go")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_provider.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.lsp.provider'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/lsp/provider.py
"""LSP providers: one language's server contribution, and the per-session
registry assembled from them.

A provider is parsed from a plugin manifest's ``lsp`` block. Third-party
plugins may use only the declarative ``command``/``args`` form; the ``backend``
and named-``diagnostics`` keys are a bundled-only seam into in-tree tuned code
(BasedPyrightServer, lsp/checks.py). This module is pure stdlib — no multilspy
import — so importing it (from the tools/bootstrap layer) never drags in the
heavy dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Recognized bundled-only backend keys. `multilspy:<lang>` is validated by prefix.
_BUNDLED_BACKENDS = frozenset({"basedpyright"})
_MULTILSPY_PREFIX = "multilspy:"
_DIAGNOSTICS_STRATEGIES = frozenset({"lsp", "python-checks"})


class LspProviderError(Exception):
    """A manifest ``lsp`` block is malformed or uses a bundled-only key."""


@dataclass(frozen=True)
class LspProvider:
    language: str
    extensions: tuple[str, ...]
    probe: tuple[str, ...]
    install_hint: str
    command: str | None
    args: tuple[str, ...]
    root_markers: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    backend: str | None
    diagnostics: str
    source: str  # "bundled" | "global" | "project"
    plugin_root: Path | None


def _norm_ext(ext: str) -> str:
    e = str(ext).strip().lower()
    return e if e.startswith(".") else f".{e}"


def _valid_backend(backend: str) -> bool:
    return backend in _BUNDLED_BACKENDS or backend.startswith(_MULTILSPY_PREFIX)


def _parse_one(
    raw: dict, *, bundled: bool, source: str, plugin_root: Path | None, strict: bool
) -> LspProvider | None:
    def fail(msg: str) -> LspProvider | None:
        if strict:
            raise LspProviderError(msg)
        logger.warning("skipping lsp provider: %s", msg)
        return None

    if not isinstance(raw, dict):
        return fail(f"lsp provider must be an object, got {type(raw).__name__}")
    language = raw.get("language")
    if not isinstance(language, str) or not language.strip():
        return fail("lsp provider missing 'language'")
    exts = raw.get("extensions")
    if not isinstance(exts, list) or not exts:
        return fail(f"lsp provider {language!r} missing non-empty 'extensions'")

    backend = raw.get("backend")
    command = raw.get("command")
    if backend is not None:
        if not bundled:
            return fail(f"'backend' is bundled-only (provider {language!r})")
        if command is not None:
            return fail(f"provider {language!r}: 'backend' and 'command' are exclusive")
        if not isinstance(backend, str) or not _valid_backend(backend):
            return fail(f"provider {language!r}: unknown backend {backend!r}")
    elif command is not None:
        if not isinstance(command, str) or not command.strip():
            return fail(f"provider {language!r}: 'command' must be a non-empty string")
    else:
        return fail(f"provider {language!r}: needs 'command' or 'backend'")

    diagnostics = raw.get("diagnostics", "lsp")
    if diagnostics not in _DIAGNOSTICS_STRATEGIES:
        return fail(f"provider {language!r}: unknown diagnostics {diagnostics!r}")
    if diagnostics != "lsp" and not bundled:
        return fail(f"named diagnostics {diagnostics!r} is bundled-only ({language!r})")

    probe_raw = raw.get("probe")
    if isinstance(probe_raw, list):
        probe = tuple(str(b) for b in probe_raw)
    elif command is not None:
        probe = (command.split()[0],)  # default to the command's binary
    else:
        probe = ()  # backend providers carry their own probe or auto-provide

    env_raw = raw.get("env")
    env = (
        tuple((str(k), str(v)) for k, v in env_raw.items())
        if isinstance(env_raw, dict)
        else ()
    )
    markers_raw = raw.get("rootMarkers")
    root_markers = (
        tuple(str(m) for m in markers_raw) if isinstance(markers_raw, list) else ()
    )
    args_raw = raw.get("args")
    args = tuple(str(a) for a in args_raw) if isinstance(args_raw, list) else ()

    return LspProvider(
        language=language,
        extensions=tuple(_norm_ext(e) for e in exts),
        probe=probe,
        install_hint=str(raw.get("installHint", "") or ""),
        command=command,
        args=args,
        root_markers=root_markers,
        env=env,
        backend=backend,
        diagnostics=diagnostics,
        source=source,
        plugin_root=plugin_root,
    )


def parse_lsp_providers(
    block, *, bundled: bool, source: str, plugin_root: Path | None, strict: bool
) -> list[LspProvider]:
    """Parse an ``lsp`` manifest value (object or list of objects) into providers.
    ``strict`` raises on any problem (install/validate time); non-strict logs and
    drops the bad entry (discovery time)."""
    entries = block if isinstance(block, list) else [block]
    out: list[LspProvider] = []
    for entry in entries:
        p = _parse_one(
            entry, bundled=bundled, source=source, plugin_root=plugin_root, strict=strict
        )
        if p is not None:
            out.append(p)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_lsp_provider.py -q`
Expected: PASS (all cases green)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/lsp/provider.py tests/test_lsp_provider.py
git commit -m "feat(lsp): LspProvider value object + manifest lsp-block parser"
```

---

## Task 2: `LspRegistry` — merge providers into the query surface

**Files:**
- Modify: `src/marim_harness/lsp/registry.py` — parametrize the pure helpers; drop `_EXT_TO_LANG`/`_PROBES` module globals.
- Modify: `src/marim_harness/lsp/provider.py` — add `LspRegistry`.
- Modify: `tests/test_lsp_provider.py` — add registry tests.
- Check: existing `tests/test_lsp_registry.py` (if present) — update calls to the new parametrized helper signatures.

**Interfaces:**
- Consumes (Task 1): `LspProvider`.
- Produces:
  - `class LspRegistry` with `__init__(self, providers: list[LspProvider])` and methods:
    - `language_for(self, path: str) -> str | None`
    - `availability(self, language: str) -> registry.Availability`
    - `workspace_languages(self, root, *, max_entries: int = 50_000) -> set[str]`
    - `locally_installed_languages(self) -> set[str]`
    - `provider_for(self, language: str) -> LspProvider | None`
  - `registry.language_for(path, ext_to_lang)`, `registry.availability(language, probes)`, `registry.workspace_languages(root, ext_to_lang, *, max_entries)`, `registry.locally_installed_languages(probes)` — now take their maps as arguments.

- [ ] **Step 1: Write the failing test (append to tests/test_lsp_provider.py)**

```python
from marim_harness.lsp.provider import LspRegistry


def _prov(language, exts, *, command=None, backend=None, probe=None,
          diagnostics="lsp", source="global"):
    from marim_harness.lsp.provider import parse_lsp_providers
    block = {"language": language, "extensions": exts, "diagnostics": diagnostics}
    if command:
        block["command"] = command
    if backend:
        block["backend"] = backend
    if probe is not None:
        block["probe"] = probe
    (p,) = parse_lsp_providers(
        block, bundled=backend is not None, source=source,
        plugin_root=None, strict=True,
    )
    return p


def test_registry_language_for():
    reg = LspRegistry([_prov("go", [".go"], command="gopls")])
    assert reg.language_for("main.go") == "go"
    assert reg.language_for("main.py") is None
    assert reg.language_for("src.v2/Makefile") is None  # dotted dir, no ext


def test_registry_availability_probe_present(monkeypatch):
    reg = LspRegistry([_prov("go", [".go"], command="gopls", probe=["gopls"])])
    monkeypatch.setattr(
        "marim_harness.lsp.registry.shutil.which", lambda b: "/usr/bin/gopls"
    )
    assert reg.availability("go").available is True


def test_registry_availability_probe_missing():
    reg = LspRegistry(
        [_prov("go", [".go"], command="gopls", probe=["definitely-not-on-path-xyz"])]
    )
    a = reg.availability("go")
    assert a.available is False


def test_registry_empty_probe_always_available():
    reg = LspRegistry([_prov("java", [".java"], backend="multilspy:java", probe=[])])
    assert reg.availability("java").available is True


def test_registry_unknown_language():
    reg = LspRegistry([])
    assert reg.availability("nope").available is False


def test_registry_provider_for():
    reg = LspRegistry([_prov("go", [".go"], command="gopls")])
    assert reg.provider_for("go").command == "gopls"
    assert reg.provider_for("py") is None


def test_registry_workspace_languages(tmp_path):
    (tmp_path / "a.go").write_text("package main")
    (tmp_path / "b.go").write_text("package main")
    reg = LspRegistry([_prov("go", [".go"], command="gopls")])
    assert reg.workspace_languages(tmp_path) == {"go"}


def test_registry_last_provider_wins_on_extension_conflict():
    # A later provider (e.g. project plugin) overriding the same extension wins.
    reg = LspRegistry([
        _prov("python", [".py"], backend="basedpyright", source="bundled"),
        _prov("python2", [".py"], command="custom-py-ls", source="project"),
    ])
    assert reg.language_for("x.py") == "python2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_provider.py -q`
Expected: FAIL — `ImportError: cannot import name 'LspRegistry'`

- [ ] **Step 3a: Refactor `registry.py` to parametrized helpers**

Replace the module-global maps and the functions that used them. Delete `_EXT_TO_LANG` and `_PROBES`. Keep `Availability`, `_MIN_SHARE`, `_MIN_COUNT`, `_SCAN_IGNORED_DIRS`. Rewrite the four functions to take their maps as parameters:

```python
# src/marim_harness/lsp/registry.py  (replace the marked regions)
# --- DELETE the _EXT_TO_LANG and _PROBES module dicts entirely. ---

def language_for(path: str, ext_to_lang: dict[str, str]) -> str | None:
    """Return the language for ``path`` per ``ext_to_lang``, or None."""
    _stem, ext = os.path.splitext(os.path.basename(path))
    if not ext:
        return None
    return ext_to_lang.get(ext.lower())


def availability(language: str, probes: dict[str, tuple[tuple[str, ...], str]]) -> Availability:
    """Whether a server for ``language`` can start, with an install hint.
    ``probes`` maps language -> (probe binaries, install hint). An empty probe
    tuple means auto-provided (always available)."""
    entry = probes.get(language)
    if entry is None:
        return Availability(False, "unsupported language")
    probe_bins, hint = entry
    if not probe_bins:
        return Availability(True, hint)
    found = any(shutil.which(b) for b in probe_bins)
    return Availability(found, hint)


def workspace_languages(
    root, ext_to_lang: dict[str, str], *, max_entries: int = 50_000
) -> set[str]:
    """Languages significantly present under ``root`` per ``ext_to_lang``.
    (Body unchanged from the original except language_for takes ext_to_lang.)"""
    counts: dict[str, int] = {}
    seen = 0
    capped = False
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SCAN_IGNORED_DIRS
        )
        for name in sorted(filenames):
            seen += 1
            if seen > max_entries:
                capped = True
                break
            language = language_for(name, ext_to_lang)
            if language is not None:
                counts[language] = counts.get(language, 0) + 1
        if capped:
            break
    total = sum(counts.values())
    if not total:
        return set()
    return {
        language
        for language, count in counts.items()
        if count >= _MIN_COUNT or count / total >= _MIN_SHARE
    }


def locally_installed_languages(
    probes: dict[str, tuple[tuple[str, ...], str]]
) -> set[str]:
    """Languages whose server binary is on PATH right now (excludes auto-download
    -only languages, whose probe tuple is empty)."""
    out: set[str] = set()
    for language, (probe_bins, _hint) in probes.items():
        if probe_bins and any(shutil.which(b) for b in probe_bins):
            out.add(language)
    return out
```

- [ ] **Step 3b: Add `LspRegistry` to `provider.py`**

```python
# append to src/marim_harness/lsp/provider.py
from . import registry as _registry


class LspRegistry:
    """The per-session merged view of all LSP providers. Later providers win on
    an extension or language collision (project plugins shadow global shadow
    bundled — callers order the list accordingly)."""

    def __init__(self, providers: list[LspProvider]) -> None:
        self._providers = list(providers)
        self._ext_to_lang: dict[str, str] = {}
        self._by_language: dict[str, LspProvider] = {}
        self._probes: dict[str, tuple[tuple[str, ...], str]] = {}
        for p in self._providers:
            self._by_language[p.language] = p
            self._probes[p.language] = (p.probe, p.install_hint)
            for ext in p.extensions:
                self._ext_to_lang[ext] = p.language

    def language_for(self, path: str) -> str | None:
        return _registry.language_for(path, self._ext_to_lang)

    def availability(self, language: str) -> "_registry.Availability":
        return _registry.availability(language, self._probes)

    def workspace_languages(self, root, *, max_entries: int = 50_000) -> set[str]:
        return _registry.workspace_languages(root, self._ext_to_lang, max_entries=max_entries)

    def locally_installed_languages(self) -> set[str]:
        return _registry.locally_installed_languages(self._probes)

    def provider_for(self, language: str) -> LspProvider | None:
        return self._by_language.get(language)
```

- [ ] **Step 3c: Update any existing `tests/test_lsp_registry.py`**

If `tests/test_lsp_registry.py` exists, update every `registry.language_for(x)` call to `registry.language_for(x, EXT_MAP)` etc., defining a small local `EXT_MAP`/`PROBES` fixture mirroring the old built-in values. Run it to confirm green. (If the file does not exist, skip.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_lsp_provider.py tests/test_lsp_registry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/lsp/provider.py src/marim_harness/lsp/registry.py tests/
git commit -m "feat(lsp): LspRegistry over providers; parametrize registry helpers"
```

---

## Task 3: Bundled provider loader + the four bundled manifests

**Files:**
- Create: `src/marim_harness/lsp/bundled.py`
- Create: `src/marim_harness/lsp/bundled/python/.marim-plugin/plugin.json`
- Create: `src/marim_harness/lsp/bundled/typescript/.marim-plugin/plugin.json`
- Create: `src/marim_harness/lsp/bundled/cpp/.marim-plugin/plugin.json`
- Create: `src/marim_harness/lsp/bundled/java/.marim-plugin/plugin.json`
- Modify: `pyproject.toml` — ensure the bundled JSON ships in the wheel (package-data / include).
- Test: `tests/test_lsp_bundled.py`

**Interfaces:**
- Consumes (Task 1): `parse_lsp_providers`, `LspProvider`.
- Produces: `bundled_lsp_providers() -> list[LspProvider]` and `bundled_lsp_dir() -> Path`.

- [ ] **Step 1: Write the four bundled manifests**

`src/marim_harness/lsp/bundled/python/.marim-plugin/plugin.json`:
```json
{
  "name": "lsp-python",
  "description": "Python language server (basedpyright, jedi fallback) with ruff/pyright diagnostics.",
  "lsp": {
    "language": "python",
    "extensions": [".py"],
    "backend": "basedpyright",
    "diagnostics": "python-checks",
    "probe": ["basedpyright-langserver", "jedi-language-server"],
    "installHint": "install basedpyright (pip install basedpyright) or jedi-language-server (pip install jedi-language-server)"
  }
}
```

`src/marim_harness/lsp/bundled/typescript/.marim-plugin/plugin.json`:
```json
{
  "name": "lsp-typescript",
  "description": "TypeScript/JavaScript language server (typescript-language-server).",
  "lsp": {
    "language": "typescript",
    "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"],
    "backend": "multilspy:typescript",
    "diagnostics": "lsp",
    "probe": ["typescript-language-server"],
    "installHint": "install typescript-language-server (npm i -g typescript-language-server typescript)"
  }
}
```

`src/marim_harness/lsp/bundled/cpp/.marim-plugin/plugin.json`:
```json
{
  "name": "lsp-cpp",
  "description": "C/C++ language server (clangd).",
  "lsp": {
    "language": "cpp",
    "extensions": [".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"],
    "backend": "multilspy:cpp",
    "diagnostics": "lsp",
    "probe": ["clangd"],
    "installHint": "install clangd (e.g. pacman -S clang)"
  }
}
```

`src/marim_harness/lsp/bundled/java/.marim-plugin/plugin.json`:
```json
{
  "name": "lsp-java",
  "description": "Java language server (eclipse.jdt.ls, auto-downloaded by multilspy).",
  "lsp": {
    "language": "java",
    "extensions": [".java"],
    "backend": "multilspy:java",
    "diagnostics": "lsp",
    "probe": [],
    "installHint": "auto-downloaded by multilspy on first use"
  }
}
```

Note: `typescript` maps both `.js`/`.jsx`/etc. and `.ts`/`.tsx` to one `typescript` language. This differs cosmetically from the old two-language (`typescript` + `javascript`) split but is behavior-equivalent — multilspy's typescript-language-server handles both, and the old `javascript` entry pointed at the same binary. Task 3's migration-guard test asserts the *extension coverage* and *probe/availability* match; it does not require the old language *names*.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_lsp_bundled.py
from marim_harness.lsp.bundled import bundled_lsp_providers


def test_bundled_covers_expected_extensions():
    provs = bundled_lsp_providers()
    ext_to_lang = {e: p.language for p in provs for e in p.extensions}
    # Every extension the old _EXT_TO_LANG covered must still resolve.
    for ext in [".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                ".java", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"]:
        assert ext in ext_to_lang, ext


def test_bundled_python_uses_basedpyright_and_python_checks():
    py = next(p for p in bundled_lsp_providers() if p.language == "python")
    assert py.backend == "basedpyright"
    assert py.diagnostics == "python-checks"
    assert py.probe == ("basedpyright-langserver", "jedi-language-server")
    assert py.source == "bundled"


def test_bundled_java_is_auto_provided():
    java = next(p for p in bundled_lsp_providers() if p.language == "java")
    assert java.probe == ()
    assert java.backend == "multilspy:java"


def test_all_bundled_are_bundled_source():
    assert all(p.source == "bundled" for p in bundled_lsp_providers())
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_bundled.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.lsp.bundled'`

- [ ] **Step 4: Write the loader**

```python
# src/marim_harness/lsp/bundled.py
"""Load marim's four bundled LSP language plugins from in-tree manifests.

These ship inside the wheel and are always trusted (shipped with marim). They
reuse the exact ``lsp`` manifest schema third-party plugins use, but bypass the
install/registry/trust discovery machinery — they are marim's defaults, always
on. Bundled manifests may use the bundled-only ``backend``/``diagnostics`` keys.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .provider import LspProvider, parse_lsp_providers

logger = logging.getLogger(__name__)


def bundled_lsp_dir() -> Path:
    return Path(__file__).resolve().parent / "bundled"


def bundled_lsp_providers() -> list[LspProvider]:
    """All bundled language providers, parsed strictly (a broken bundled
    manifest is a marim packaging bug, so surface it via a warning and skip)."""
    out: list[LspProvider] = []
    root = bundled_lsp_dir()
    try:
        subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return out
    for d in subdirs:
        manifest = d / ".marim-plugin" / "plugin.json"
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("skipping bundled lsp plugin at %s: %s", d, exc)
            continue
        block = raw.get("lsp") if isinstance(raw, dict) else None
        if block is None:
            continue
        try:
            out.extend(
                parse_lsp_providers(
                    block, bundled=True, source="bundled",
                    plugin_root=d, strict=True,
                )
            )
        except Exception as exc:  # noqa: BLE001 — bad bundled manifest ⇒ skip
            logger.warning("invalid bundled lsp plugin at %s: %s", d, exc)
    return out
```

- [ ] **Step 5: Ship the JSON in the wheel**

In `pyproject.toml`, confirm the build backend includes `src/marim_harness/lsp/bundled/**/*.json`. For hatchling (check `[tool.hatch.build]`), add if missing:
```toml
[tool.hatch.build.targets.wheel]
include = ["src/marim_harness"]
[tool.hatch.build.targets.wheel.force-include]
"src/marim_harness/lsp/bundled" = "marim_harness/lsp/bundled"
```
Match whatever mechanism already ships `src/marim_harness/builtin` (grep `pyproject.toml` for `builtin` and mirror it). Then verify: `uv build 2>&1 | tail -3` succeeds and `python -m zipfile -l dist/*.whl | grep bundled/python` lists the manifest.

- [ ] **Step 6: Run tests**

Run: `uv run pytest --no-cov tests/test_lsp_bundled.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/lsp/bundled.py src/marim_harness/lsp/bundled/ tests/test_lsp_bundled.py pyproject.toml
git commit -m "feat(lsp): bundled language plugins (python/ts/cpp/java) + loader"
```

---

## Task 4: Third-party `lsp` manifest block + discovery collection

**Files:**
- Modify: `src/marim_harness/plugins/manifest.py` — add `PluginManifest.lsp_block()`.
- Modify: `src/marim_harness/plugins/discovery.py` — add `plugin_lsp_providers()`.
- Test: `tests/test_lsp_plugin_discovery.py`

**Interfaces:**
- Consumes: `parse_lsp_providers` (Task 1), `_enabled_trusted` + `ResolvedPlugin` + `substitute_root` (existing discovery internals).
- Produces:
  - `PluginManifest.lsp_block(self) -> dict | list | None` (the raw `lsp` value, `${MARIM_PLUGIN_ROOT}`-substituted).
  - `plugin_lsp_providers(workspace_root, *, trust_project: bool = False) -> list[LspProvider]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lsp_plugin_discovery.py
import json
from pathlib import Path

from marim_harness.plugins.discovery import plugin_lsp_providers
from marim_harness.plugins.state import (
    InstalledPlugin, project_plugins_dir, save_state,
)


def _install_project_plugin(ws: Path, name: str, lsp: dict, *, trusted: bool):
    pdir = project_plugins_dir(ws) / name
    (pdir / ".marim-plugin").mkdir(parents=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "lsp": lsp})
    )
    save_state(
        project_plugins_dir(ws),
        {name: InstalledPlugin(name=name, version=None, source={},
                               enabled=True, trusted=trusted)},
    )


def test_project_plugin_lsp_gated_by_trust(tmp_path):
    _install_project_plugin(
        tmp_path, "go-lsp",
        {"language": "go", "extensions": [".go"], "command": "gopls"},
        trusted=True,
    )
    # Per-plugin trusted bit set, but project gate off ⇒ withheld.
    assert plugin_lsp_providers(tmp_path, trust_project=False) == []
    # Project gate on ⇒ contributed.
    provs = plugin_lsp_providers(tmp_path, trust_project=True)
    assert [p.language for p in provs] == ["go"]
    assert provs[0].source == "project"


def test_untrusted_project_plugin_withheld_even_with_gate(tmp_path):
    _install_project_plugin(
        tmp_path, "go-lsp",
        {"language": "go", "extensions": [".go"], "command": "gopls"},
        trusted=False,
    )
    assert plugin_lsp_providers(tmp_path, trust_project=True) == []


def test_third_party_backend_key_ignored(tmp_path):
    # A malicious/mistaken third-party plugin using the bundled-only backend key
    # contributes nothing (lenient parse drops it), not a basedpyright hijack.
    _install_project_plugin(
        tmp_path, "evil",
        {"language": "python", "extensions": [".py"], "backend": "basedpyright"},
        trusted=True,
    )
    assert plugin_lsp_providers(tmp_path, trust_project=True) == []


def test_plugin_root_substitution(tmp_path):
    _install_project_plugin(
        tmp_path, "wrapped",
        {"language": "go", "extensions": [".go"],
         "command": "${MARIM_PLUGIN_ROOT}/bin/gopls-wrapper"},
        trusted=True,
    )
    (prov,) = plugin_lsp_providers(tmp_path, trust_project=True)
    assert prov.command.endswith("/bin/gopls-wrapper")
    assert "${MARIM_PLUGIN_ROOT}" not in prov.command
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_plugin_discovery.py -q`
Expected: FAIL — `ImportError: cannot import name 'plugin_lsp_providers'`

- [ ] **Step 3a: Add `lsp_block()` to `manifest.py`**

```python
# in class PluginManifest (src/marim_harness/plugins/manifest.py)
    def lsp_block(self):
        """The raw ``lsp`` manifest value (object or list), or None. Not path-
        resolved here — LSP providers carry a plugin_root and substitute
        ${MARIM_PLUGIN_ROOT} at collection time (see discovery.plugin_lsp_providers)."""
        return self.raw.get("lsp")
```

- [ ] **Step 3b: Add `plugin_lsp_providers()` to `discovery.py`**

```python
# src/marim_harness/plugins/discovery.py — add near plugin_mcp_specs
from ..lsp.provider import LspProvider, parse_lsp_providers


def plugin_lsp_providers(
    workspace_root, *, trust_project: bool = False
) -> list["LspProvider"]:
    """LSP providers contributed by enabled+trusted plugins. Follows the exact
    trust rule as plugin_mcp_specs — an LSP server launches code on connect, so
    project-scope plugins need both the per-plugin trust bit AND the project
    trust gate. Third-party providers are declarative only: the bundled-only
    ``backend``/named-``diagnostics`` keys are dropped by the lenient parse.
    ``${MARIM_PLUGIN_ROOT}`` is substituted in each provider's command/args."""
    out: list[LspProvider] = []
    for p in _enabled_trusted(workspace_root, trust_project=trust_project):
        block = p.manifest.lsp_block()
        if block is None:
            continue
        block = substitute_root(block, p.root)
        out.extend(
            parse_lsp_providers(
                block, bundled=False, source=p.scope,
                plugin_root=p.root, strict=False,
            )
        )
    return out
```

Note: importing `marim_harness.lsp.provider` at the top of `discovery.py` is safe — `provider.py` is pure stdlib (no multilspy). Confirm no import cycle with `uv run python -c "import marim_harness.plugins.discovery"`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_lsp_plugin_discovery.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/plugins/manifest.py src/marim_harness/plugins/discovery.py tests/test_lsp_plugin_discovery.py
git commit -m "feat(lsp): collect third-party lsp providers under the MCP trust gate"
```

---

## Task 5: `GenericStdioServer` for declarative servers

**Files:**
- Create: `src/marim_harness/lsp/generic.py`
- Create: `tests/fake_lsp_server.py` (a minimal stdio LSP server for tests)
- Test: `tests/test_lsp_generic.py`

**Interfaces:**
- Consumes: `LspProvider` (Task 1); multilspy `LanguageServer`, `ProcessLaunchInfo`, `MultilspyConfig`, `MultilspyLogger`, `InitializeParams`.
- Produces: `GenericStdioServer(LanguageServer)` with classmethod `from_provider(cls, provider: LspProvider, root: "Path") -> GenericStdioServer`.

- [ ] **Step 1: Write the fake LSP server fixture**

```python
# tests/fake_lsp_server.py
"""A minimal LSP server over stdio for tests: answers initialize, acks
initialized/shutdown, and returns one definition location. Speaks the
Content-Length framing multilspy's client uses. No third-party deps."""
import json
import sys


def _read_message(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("utf-8").rstrip("\r\n")
        if line == "":
            break
        key, _, val = line.partition(":")
        headers[key.strip().lower()] = val.strip()
    length = int(headers.get("content-length", "0"))
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(stream, payload):
    data = json.dumps(payload).encode("utf-8")
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    stream.write(data)
    stream.flush()


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        msg = _read_message(stdin)
        if msg is None:
            return
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _write_message(stdout, {
                "jsonrpc": "2.0", "id": mid,
                "result": {"capabilities": {"definitionProvider": True,
                                            "referencesProvider": True,
                                            "documentSymbolProvider": True}},
            })
        elif method == "textDocument/definition":
            _write_message(stdout, {
                "jsonrpc": "2.0", "id": mid,
                "result": [{"uri": msg["params"]["textDocument"]["uri"],
                            "range": {"start": {"line": 0, "character": 0},
                                      "end": {"line": 0, "character": 1}}}],
            })
        elif method == "shutdown":
            _write_message(stdout, {"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            return
        # notifications (initialized, didOpen, ...) need no reply


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_lsp_generic.py
import sys
from pathlib import Path

import pytest

from marim_harness.lsp.generic import GenericStdioServer
from marim_harness.lsp.provider import parse_lsp_providers


def _fake_provider():
    fake = Path(__file__).parent / "fake_lsp_server.py"
    block = {
        "language": "fake",
        "extensions": [".fake"],
        "command": f"{sys.executable} {fake}",
        "rootMarkers": ["fake.toml"],
    }
    (p,) = parse_lsp_providers(
        block, bundled=False, source="global", plugin_root=None, strict=True
    )
    return p


def test_launch_info_uses_command_and_args(tmp_path):
    srv = GenericStdioServer.from_provider(_fake_provider(), tmp_path)
    # ProcessLaunchInfo carries the composed command string.
    assert "fake_lsp_server.py" in srv._launch_cmd  # see impl note below
    assert srv.language_id == "fake"


@pytest.mark.asyncio
async def test_generic_definition_round_trip(tmp_path):
    (tmp_path / "x.fake").write_text("hello\n")
    srv = GenericStdioServer.from_provider(_fake_provider(), tmp_path)
    async with srv.start_server():
        locs = await srv.request_definition("x.fake", 0, 0)
    assert locs and locs[0]["range"]["start"]["line"] == 0
```

(If the repo does not already enable `pytest-asyncio` auto mode, mark with the same mechanism other async tests in `tests/` use — grep `tests/` for `asyncio_mode` or `@pytest.mark.asyncio` and match it.)

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_generic.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.lsp.generic'`

- [ ] **Step 4: Write `GenericStdioServer`**

Model exactly on `basedpyright.py`'s subclass shape (initialize params, `workspace/configuration` handler, `start_server` handshake), but drive command/env/markers from the provider.

```python
# src/marim_harness/lsp/generic.py
"""A generic multilspy LanguageServer subclass for plugin-declared servers.

Third-party LSP plugins declare a launch command; this subclass runs it over
stdio and reuses all of multilspy's client plumbing (framing, initialize
handshake, request_definition/references/document_symbols, publishDiagnostics
collection). It mirrors BasedPyrightServer's handshake — answer
workspace/configuration so servers that request settings don't stall, signal
readiness once ``initialized`` is acked — with sensible defaults for a server
we know nothing specific about.

Imported lazily from the manager's factory, so multilspy stays off the
``import marim_harness`` path.
"""

from __future__ import annotations

import logging
import os
import pathlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from multilspy.language_server import LanguageServer
from multilspy.lsp_protocol_handler.lsp_types import InitializeParams
from multilspy.lsp_protocol_handler.server import ProcessLaunchInfo
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

from .provider import LspProvider

logger = logging.getLogger(__name__)


class GenericStdioServer(LanguageServer):
    """Any LSP server over stdio, driven through multilspy's client plumbing."""

    def __init__(
        self,
        config: MultilspyConfig,
        logger_: MultilspyLogger,
        repository_root_path: str,
        *,
        cmd: str,
        language_id: str,
        env: dict[str, str] | None = None,
    ):
        launch = ProcessLaunchInfo(
            cmd=cmd, cwd=repository_root_path, env={**os.environ, **(env or {})}
        )
        super().__init__(config, logger_, repository_root_path, launch, language_id)
        self._launch_cmd = cmd  # exposed for tests / debugging

    @classmethod
    def from_provider(cls, provider: LspProvider, root: Path) -> "GenericStdioServer":
        assert provider.command is not None  # declarative providers only
        cmd = " ".join([provider.command, *provider.args]).strip()
        config = MultilspyConfig.from_dict({"code_language": provider.language})
        return cls(
            config,
            MultilspyLogger(),
            str(root),
            cmd=cmd,
            language_id=provider.language,
            env=dict(provider.env),
        )

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        root_uri = pathlib.Path(repository_absolute_path).as_uri()
        return cast(InitializeParams, {
            "processId": os.getpid(),
            "rootPath": repository_absolute_path,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "publishDiagnostics": {"versionSupport": True},
                },
                "workspace": {"workspaceFolders": True, "configuration": True},
            },
            "initializationOptions": {},
            "workspaceFolders": [
                {"uri": root_uri, "name": os.path.basename(repository_absolute_path)}
            ],
        })

    @asynccontextmanager
    async def start_server(self) -> AsyncGenerator["GenericStdioServer", None]:
        async def workspace_configuration(params):
            return [{}] * len(params.get("items", []))

        async def do_nothing(params):
            return

        async def window_log_message(msg):
            self.logger.log(f"LSP: window/logMessage: {msg}", logging.INFO)

        self.server.on_request("workspace/configuration", workspace_configuration)
        self.server.on_request("client/registerCapability", do_nothing)
        self.server.on_request("window/workDoneProgress/create", do_nothing)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", do_nothing)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        async with super().start_server():
            self.logger.log(f"Starting generic LSP process: {self._launch_cmd}", logging.INFO)
            await self.server.start()
            init_params = self._get_initialize_params(self.repository_root_path)
            await self.server.send.initialize(init_params)
            self.server.notify.initialized({})
            self.completions_available.set()
            yield self
            await self.server.shutdown()
            await self.server.stop()
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest --no-cov tests/test_lsp_generic.py -q`
Expected: PASS. If `test_generic_definition_round_trip` is flaky on a slow CI leg, that's the only test allowed a short per-request timeout bump — never a `sleep`.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/lsp/generic.py tests/test_lsp_generic.py tests/fake_lsp_server.py
git commit -m "feat(lsp): GenericStdioServer for plugin-declared language servers"
```

---

## Task 6: Wire the registry into `LspManager`

**Files:**
- Modify: `src/marim_harness/lsp/manager.py`
- Test: existing `tests/test_lsp*.py` manager tests (update construction) + new cases.

**Interfaces:**
- Consumes: `LspRegistry`, `LspProvider` (Tasks 1-2); `GenericStdioServer` (Task 5); `BasedPyrightServer` (existing); `checks` (existing).
- Produces: `LspManager(root, *, registry: LspRegistry, disabled=frozenset(), server_factory=None, request_timeout=15.0, start_timeout=60.0)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lsp_manager_registry.py
import pytest

from marim_harness.lsp.manager import LspManager
from marim_harness.lsp.provider import LspRegistry, parse_lsp_providers


def _reg(*blocks_bundled):
    provs = []
    for block, bundled in blocks_bundled:
        provs += parse_lsp_providers(
            block, bundled=bundled, source="bundled" if bundled else "global",
            plugin_root=None, strict=True,
        )
    return LspRegistry(provs)


@pytest.mark.asyncio
async def test_unsupported_file_message(tmp_path):
    mgr = LspManager(tmp_path, registry=_reg())
    out = await mgr.goto_definition("x.zzz", 1, 1)
    assert "unsupported file type" in out


@pytest.mark.asyncio
async def test_disabled_language_message(tmp_path):
    reg = _reg((
        {"language": "go", "extensions": [".go"], "command": "gopls"}, False,
    ))
    mgr = LspManager(tmp_path, registry=reg, disabled=frozenset({"go"}))
    out = await mgr.hover("x.go", 1, 1)
    assert "disabled for go" in out


@pytest.mark.asyncio
async def test_python_diagnostics_routes_to_checks(tmp_path, monkeypatch):
    reg = _reg((
        {"language": "python", "extensions": [".py"], "backend": "basedpyright",
         "diagnostics": "python-checks", "probe": ["basedpyright-langserver"]}, True,
    ))
    called = {}
    async def fake_python_diagnostics(root, path, *, deep):
        called["hit"] = (path, deep)
        return []
    monkeypatch.setattr(
        "marim_harness.lsp.manager.checks.python_diagnostics", fake_python_diagnostics
    )
    monkeypatch.setattr(
        "marim_harness.lsp.manager.checks.format_checks", lambda path, diags: "clean"
    )
    mgr = LspManager(tmp_path, registry=reg)
    out = await mgr.diagnostics("m.py")
    assert out == "clean"
    assert called["hit"] == ("m.py", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_manager_registry.py -q`
Expected: FAIL — `TypeError: __init__() missing 1 required keyword-only argument: 'registry'`

- [ ] **Step 3a: Inject the registry into `__init__`**

In `manager.py`, change the constructor signature and store the registry:
```python
    def __init__(
        self,
        root: Path,
        *,
        registry: "LspRegistry",
        disabled: frozenset[str] = frozenset(),
        server_factory: Callable[[str, Path], Any] | None = None,
        request_timeout: float = 15.0,
        start_timeout: float = 60.0,
    ) -> None:
        self.root = root
        self._registry = registry
        self._disabled = disabled
        self._factory = server_factory or self._default_factory
        # ... rest unchanged
```
Add the import at top: `from .provider import LspProvider, LspRegistry` (under `TYPE_CHECKING` is fine — provider.py is pure, so a direct import is also OK).

- [ ] **Step 3b: Replace module `registry.*` calls with `self._registry.*`**

- `_server_for`: `language = self._registry.language_for(path)`; `avail = self._registry.availability(language)`.
- `diagnostics`: replace `if registry.language_for(path) == "python":` with a provider-strategy check (Step 3d).
- `workspace_symbols`: `for language in sorted(self._registry.locally_installed_languages()):`.
- Delete the module-level `from . import checks, registry` line's `registry` (keep `checks`). Import `registry` is no longer used directly here.

- [ ] **Step 3c: Make `_default_factory` registry/provider-driven**

Convert `_default_factory` from a module function to a method that dispatches on the provider's `backend`/`command`:
```python
    def _default_factory(self, language: str, root: Path):
        """Build a server for ``language`` from its provider. `backend:
        basedpyright` → BasedPyrightServer (jedi fallback via multilspy);
        `backend: multilspy:<lang>` → multilspy's tuned server; a declarative
        command → GenericStdioServer. Imported lazily so multilspy loads only
        when a server actually starts."""
        provider = self._registry.provider_for(language)
        if provider is None:
            raise ValueError(f"no LSP provider for {language!r}")
        if provider.backend == "basedpyright":
            import shutil
            from multilspy.multilspy_config import MultilspyConfig
            from multilspy.multilspy_logger import MultilspyLogger
            config = MultilspyConfig.from_dict({"code_language": "python"})
            if shutil.which("basedpyright-langserver"):
                from .basedpyright import BasedPyrightServer
                return BasedPyrightServer(config, MultilspyLogger(), str(root))
            from multilspy import LanguageServer
            return LanguageServer.create(config, MultilspyLogger(), str(root))
        if provider.backend and provider.backend.startswith("multilspy:"):
            from multilspy import LanguageServer
            from multilspy.multilspy_config import MultilspyConfig
            from multilspy.multilspy_logger import MultilspyLogger
            lang = provider.backend.split(":", 1)[1]
            config = MultilspyConfig.from_dict({"code_language": lang})
            return LanguageServer.create(config, MultilspyLogger(), str(root))
        # Declarative third-party server.
        from .generic import GenericStdioServer
        return GenericStdioServer.from_provider(provider, root)
```
Delete the old module-level `_default_factory` function. Keep its jedi-fallback docstring intent (now in the `basedpyright` branch). Note: `_factory` is now a bound method; `server_factory` injection for tests still overrides it (a plain `(language, root)` callable), so `self._factory(language, self.root)` in `_start_language` still works — confirm that call site passes exactly `(language, self.root)`.

If the resulting `_default_factory` trips C901 (complexity ≤ 10), extract each backend branch into a small helper (`_make_basedpyright`, `_make_multilspy`, `_make_generic`).

- [ ] **Step 3d: Provider-driven diagnostics strategy**

Replace the python hard-check at the top of `diagnostics()`:
```python
    async def diagnostics(self, path: str, *, settle: float = 1.5, deep: bool = False) -> str:
        language = self._registry.language_for(path)
        provider = self._registry.provider_for(language) if language else None
        if provider is not None and provider.diagnostics == "python-checks":
            if language in self._disabled:
                return f"LSP is disabled for {language}."
            diags = await checks.python_diagnostics(self.root, path, deep=deep)
            return checks.format_checks(path, diags)
        # ... unchanged LSP publishDiagnostics path below (server_for, collector, etc.)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_lsp_manager_registry.py tests/test_lsp_generic.py tests/test_lsp_bundled.py tests/test_lsp_provider.py -q`
Expected: PASS. Also run any existing manager test file and fix its `LspManager(root)` constructions to pass `registry=`.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/lsp/manager.py tests/
git commit -m "feat(lsp): inject LspRegistry into manager; provider-driven factory + diagnostics"
```

---

## Task 7: Assemble the registry in bootstrap/builder/harness

**Files:**
- Modify: `src/marim_harness/runtime/bootstrap.py`
- Modify: `src/marim_harness/runtime/builder.py`
- Modify: `src/marim_harness/runtime/harness.py`
- Test: `tests/test_lsp_bootstrap_gate.py`

**Interfaces:**
- Consumes: `bundled_lsp_providers` (Task 3), `plugin_lsp_providers` (Task 4), `LspRegistry` (Task 2).
- Produces: a single `LspRegistry` assembled as `bundled + global-plugin + project-plugin` providers (bundled lowest precedence, so plugins can override), threaded to both the bootstrap gate and `LspManager`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lsp_bootstrap_gate.py
from marim_harness.runtime.bootstrap import build_lsp_registry


def test_registry_includes_bundled(tmp_path):
    reg = build_lsp_registry(tmp_path, trust_project=False)
    assert reg.provider_for("python") is not None
    assert reg.provider_for("java") is not None


def test_gate_uses_registry(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    reg = build_lsp_registry(tmp_path, trust_project=False)
    # python present in workspace; its provider exists (availability depends on
    # PATH, but workspace_languages must detect it).
    assert "python" in reg.workspace_languages(tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_bootstrap_gate.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_lsp_registry'`

- [ ] **Step 3a: Add `build_lsp_registry` to `bootstrap.py`**

Replace the `from ..lsp import registry as lsp_registry` import and rework the gate region (lines ~104-125):
```python
# src/marim_harness/runtime/bootstrap.py
from ..lsp.bundled import bundled_lsp_providers
from ..lsp.provider import LspRegistry
from ..plugins.discovery import plugin_lsp_providers


def build_lsp_registry(workspace, *, trust_project: bool) -> LspRegistry:
    """Assemble the session LSP registry: bundled providers first (lowest
    precedence), then trusted third-party plugin providers, so a project/global
    plugin can override a bundled language by declaring the same extension."""
    providers = list(bundled_lsp_providers())
    providers += plugin_lsp_providers(workspace, trust_project=trust_project)
    return LspRegistry(providers)
```
Then in the gate:
```python
    lsp_reg = build_lsp_registry(workspace, trust_project=cfg.trust_project_hooks)
    register_lsp_tools = cfg.lsp_enabled and cfg.lsp_tools_enabled
    if register_lsp_tools:
        found = lsp_reg.workspace_languages(workspace)
        if not any(lsp_reg.availability(lang).available for lang in found):
            register_lsp_tools = False
            logger.info(
                "LSP tools disabled: no language server available for "
                "workspace languages %s",
                sorted(found) if found else "(none detected)",
            )
```
(Use the correct config attribute for the project-trust flag — grep `bootstrap.py`/`cfg` for `trust_project`; it may be `cfg.trust_project_hooks`.) Pass `lsp_reg` into the builder in Step 3b.

- [ ] **Step 3b: Thread the registry through `builder.py`**

`with_lsp` gains a `providers`/`registry` param; store it and put it in `HarnessConfig`:
```python
    def with_lsp(self, *, enabled: bool = True, tools: bool = True,
                 registry: "LspRegistry | None" = None) -> "HarnessBuilder":
        self._lsp = enabled
        self._lsp_tools = tools
        self._lsp_registry = registry
        return self
```
In `build()`, put `lsp_registry=self._lsp_registry` into `config_fields`. Add `lsp_registry: "LspRegistry | None" = None` to `HarnessConfig` (harness.py). Bootstrap calls `.with_lsp(enabled=cfg.lsp_enabled, tools=register_lsp_tools, registry=lsp_reg)`.

For the embedding path (HarnessBuilder used directly, no bootstrap): when `registry is None` and `enabled`, `build()` defaults it to `LspRegistry(bundled_lsp_providers())` so a bare builder still gets the bundled languages. Add that default in `build()`.

- [ ] **Step 3c: Use the registry in `harness.py`**

In `build_collaborators` (harness.py ~line 336):
```python
    lsp = (
        LspManager(deps.workspace.root, registry=cfg.lsp_registry)
        if cfg.lsp_enabled and cfg.lsp_registry is not None
        else None
    )
```

- [ ] **Step 4: Run tests + full suite**

Run: `uv run pytest --no-cov tests/test_lsp_bootstrap_gate.py -q`
Expected: PASS
Then: `uv run pytest -q` (full suite with coverage) — fix any construction sites that still call `LspManager(root)` or the old `registry.*`/`with_lsp` signatures.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/bootstrap.py src/marim_harness/runtime/builder.py src/marim_harness/runtime/harness.py tests/test_lsp_bootstrap_gate.py
git commit -m "feat(lsp): assemble session registry from bundled + plugin providers"
```

---

## Task 8: Docs, CLAUDE.md, and full quality gate

**Files:**
- Modify: `docs/plugins.md` — document the `lsp` manifest block (declarative form, trust rule, that `backend` is bundled-only).
- Create: `docs/lsp-plugins.md` — short reference: how to add a language via a plugin, the four bundled languages, the declare/probe/hint model.
- Modify: `CLAUDE.md` — update the `lsp/` subsystem bullet to describe the provider/registry/bundled-plugin model.
- Modify: `.env.example` — note (if relevant) that bundled languages are always present; third-party LSP plugins follow `MARIM_TRUST_PROJECT_HOOKS`.

- [ ] **Step 1: Update `CLAUDE.md` `lsp/` bullet**

Replace the existing `lsp/` bullet with:
```markdown
- `lsp/` — multilspy-backed language servers, now assembled from **LSP providers**
  (`provider.py`: `LspProvider` + `LspRegistry`) rather than a hard-coded set. Four
  bundled language plugins (`lsp/bundled/{python,typescript,cpp,java}`) ship in-tree
  and always load; third-party plugins add languages via an `lsp` manifest block
  (declarative `command`/`args` only) under the same `MARIM_TRUST_PROJECT_HOOKS` gate
  as MCP. `backend:`/named-`diagnostics:` keys are a bundled-only seam to in-tree tuned
  code (`basedpyright.py`, ruff/pyright via `checks.py`). Declarative servers launch
  through `GenericStdioServer` (`generic.py`). Two switches still gate the whole thing:
  `lsp_enabled` (manager + diagnostics-on-edit) and `lsp_tools_enabled` (the six nav
  tools). marim never downloads server binaries — it probes PATH and surfaces the
  provider's install hint.
```

- [ ] **Step 2: Write `docs/lsp-plugins.md`** (adding a language via plugin, with the declarative manifest example from the spec; the four bundled languages table; the trust rule).

- [ ] **Step 3: Update `docs/plugins.md`** to list `lsp` alongside skills/agents/hooks/mcpServers in the manifest reference, noting the bundled-only keys and the trust gate.

- [ ] **Step 4: Run the full quality gate**

Run in order (matches CI):
```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: all green, coverage not regressed. Fix anything that fails before committing.

- [ ] **Step 5: Commit**

```bash
git add docs/ CLAUDE.md .env.example
git commit -m "docs(lsp): document LSP-as-plugins provider model"
```

---

## Self-Review

**Spec coverage:**
- Provider model / `LspRegistry` replacing module globals → Tasks 1, 2. ✓
- Generic stdio launcher for any server → Task 5. ✓
- Migrate built-ins to bundled plugins (no hard-coded set) → Tasks 2 (drop globals), 3 (bundled). ✓
- Declare + probe + hint; no binary downloads → Tasks 1 (probe/hint fields), 6 (availability gates cold start only). ✓
- Manifest `lsp` block, bundled-only `backend`/`diagnostics` rejection → Tasks 1, 4. ✓
- MCP-style trust gate → Task 4 (`_enabled_trusted` reuse). ✓
- Manager registry injection + generic factory + diagnostics strategy → Task 6. ✓
- Preserve basedpyright + ruff/pyright python path → Task 3 (bundled python manifest), Task 6 (basedpyright branch, python-checks strategy). ✓
- Build-time tool-registration gate over the new registry → Task 7. ✓
- Testing (pure/unit, generic launcher vs stub, migration guard, integration) → Tasks 1-7 tests. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The two "grep the repo and match" notes (pyproject packaging mechanism, pytest-asyncio mode, exact `cfg` trust attribute) are deliberate — they adapt to existing repo conventions the implementer must not guess at; each names the exact symbol to find.

**Type consistency:** `LspProvider` fields, `parse_lsp_providers(...)` signature, `LspRegistry` method names, `LspManager(root, *, registry=...)`, `bundled_lsp_providers()`, `plugin_lsp_providers(...)`, `build_lsp_registry(...)`, `GenericStdioServer.from_provider(...)` are used consistently across Tasks 1-7. The manager's `_factory` remains a `(language, root)` callable so test injection is unchanged.
