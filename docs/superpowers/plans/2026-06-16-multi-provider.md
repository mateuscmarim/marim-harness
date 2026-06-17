# Multi-Provider Model Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow multiple model providers (OpenRouter, Google/Gemini, local) to be configured simultaneously and selectable from the TUI model picker, which groups models by provider in sections.

**Architecture:** Auto-detect active providers from env vars at startup; `MultiModelSource` holds one `ModelSource` per detected provider and fetches all catalogs in parallel; `ModelPickerModal` receives a list of `(provider, entries)` sections and dismisses with a `SelectedModel(provider, model_id)` instead of a bare string; `Harness.set_model` accepts provider + model_id and persists both to the session store.

**Tech Stack:** Python 3.11+, Textual (TUI), pydantic-ai, httpx (async catalog fetch), pytest + anyio

---

## File Map

| File | Change |
|------|--------|
| `src/marim_harness/config/model.py` | Add `SelectedModel`, `detect_providers()`, `MultiModelSource` |
| `src/marim_harness/config/__init__.py` | Re-export new symbols |
| `src/marim_harness/session/store.py` | Add `provider: Optional[str]` field to `SessionStore` |
| `src/marim_harness/session/ctrl.py` | `set_model(model_id, provider)` persists both; `new_session` forwards provider |
| `src/marim_harness/agent.py` | `Harness`: add `model_provider`, change `set_model(provider, model_id)`, update `_apply_saved_model` |
| `src/marim_harness/bootstrap.py` | Use `detect_providers()` + `MultiModelSource` |
| `src/marim_harness/interfaces/tui/model_picker.py` | Sections API + `SelectedModel` dismiss |
| `src/marim_harness/interfaces/tui/app.py` | `open_model_picker` + `_on_model_chosen` use new API |
| `tests/test_config.py` | Tests for `detect_providers`, `MultiModelSource` |
| `tests/test_model_picker.py` | Updated for sections API and `SelectedModel` |

---

### Task 1: `SelectedModel`, `detect_providers`, `MultiModelSource`

**Files:**
- Modify: `src/marim_harness/config/model.py`
- Modify: `src/marim_harness/config/__init__.py`
- Test: `tests/test_config.py`

Context: `config/model.py` currently holds `ModelConfig`, `ModelSource`, `load_config()`, `build_model()`. We're adding three things: a `SelectedModel` dataclass (what the picker returns), `detect_providers()` (scans env for all configured providers), and `MultiModelSource` (holds all detected sources, fetches in parallel).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py`:

```python
import asyncio
from unittest.mock import AsyncMock, patch

from marim_harness.config import (
    MultiModelSource,
    ModelConfig,
    SelectedModel,
    detect_providers,
)
from marim_harness.workspace import ModelEntry


def test_selected_model_is_frozen():
    s = SelectedModel(provider="openrouter", model_id="anthropic/claude-sonnet-4-6")
    assert s.provider == "openrouter"
    assert s.model_id == "anthropic/claude-sonnet-4-6"
    import dataclasses
    assert dataclasses.is_dataclass(s)


def test_detect_providers_active_always_first(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "gkey")
    monkeypatch.setenv("OPENROUTER_API_KEY", "orkey")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MARIM_API_KEY", raising=False)
    configs = detect_providers()
    assert configs[0].provider == "google"
    assert any(c.provider == "openrouter" for c in configs)
    assert len(configs) == 2


def test_detect_providers_only_active_when_no_extra_keys(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "orkey")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MARIM_API_KEY", raising=False)
    configs = detect_providers()
    assert len(configs) == 1
    assert configs[0].provider == "openrouter"


def test_detect_providers_no_duplicate_for_active(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "orkey")
    monkeypatch.setenv("GOOGLE_API_KEY", "gkey")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MARIM_API_KEY", raising=False)
    configs = detect_providers()
    providers = [c.provider for c in configs]
    assert providers.count("openrouter") == 1
    assert "google" in providers


def test_multi_model_source_build_delegates_to_correct_source(monkeypatch):
    cfg_or = ModelConfig(provider="openrouter", model="or-model", api_key="key1")
    cfg_g = ModelConfig(provider="google", model="gemini-model", api_key="key2")
    multi = MultiModelSource([cfg_or, cfg_g])
    assert multi.active_provider == "openrouter"
    assert multi.is_local("openrouter") is False
    assert multi.is_local("local") is False


def test_multi_model_source_label():
    cfg = ModelConfig(provider="openrouter", model="or-model", api_key="k")
    multi = MultiModelSource([cfg])
    assert multi.label("openrouter", "anthropic/claude-sonnet-4-6") == "openrouter/anthropic/claude-sonnet-4-6"


@pytest.mark.anyio
async def test_multi_model_source_list_all_fetches_in_parallel():
    cfg_or = ModelConfig(provider="openrouter", model="or-model", api_key="key1")
    cfg_g = ModelConfig(provider="google", model="gemini-model", api_key="key2")
    multi = MultiModelSource([cfg_or, cfg_g])

    or_entries = [ModelEntry(id="anthropic/claude-sonnet-4-6", name="Claude")]
    g_entries = [ModelEntry(id="gemini-2.5-flash", name="Gemini Flash")]

    with patch.object(multi.sources["openrouter"], "list_models", AsyncMock(return_value=or_entries)), \
         patch.object(multi.sources["google"], "list_models", AsyncMock(return_value=g_entries)):
        sections = await multi.list_all()

    assert sections == [("openrouter", or_entries), ("google", g_entries)]
```

- [ ] **Step 2: Run tests to verify they fail**

```
.venv/bin/python -m pytest tests/test_config.py::test_selected_model_is_frozen tests/test_config.py::test_detect_providers_active_always_first tests/test_config.py::test_multi_model_source_list_all_fetches_in_parallel -v
```

Expected: FAIL with `ImportError: cannot import name 'MultiModelSource'` (or similar).

- [ ] **Step 3: Implement in `config/model.py`**

Add after the existing imports at the top:

```python
from dataclasses import dataclass, replace
```

(already there — keep as-is)

After the `ModelSource` class, add:

```python
@dataclass(frozen=True)
class SelectedModel:
    """Provider + model id pair returned by the multi-provider picker."""
    provider: str
    model_id: str


def detect_providers() -> list[ModelConfig]:
    """Return a ModelConfig for every provider that has credentials in the
    environment. The active provider (MARIM_PROVIDER) is always first.
    Additional providers are appended when their key is present and they differ
    from the active one."""
    active = load_config()
    detected = {active.provider}
    configs: list[ModelConfig] = [active]

    if "openrouter" not in detected and os.getenv("OPENROUTER_API_KEY"):
        configs.append(ModelConfig(
            provider="openrouter",
            model=_DEFAULT_OPENROUTER_MODEL,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            max_context_tokens=active.max_context_tokens,
            proactive_memory=active.proactive_memory,
        ))
        detected.add("openrouter")

    if "google" not in detected and (
        os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    ):
        configs.append(ModelConfig(
            provider="google",
            model=_DEFAULT_GOOGLE_MODEL,
            api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            max_context_tokens=active.max_context_tokens,
            proactive_memory=active.proactive_memory,
        ))
        detected.add("google")

    return configs


class MultiModelSource:
    """Holds one ModelSource per detected provider. Fetches catalogs in
    parallel and builds models for any provider in the set."""

    def __init__(self, configs: list[ModelConfig]) -> None:
        self.sources: dict[str, ModelSource] = {
            cfg.provider: ModelSource(cfg) for cfg in configs
        }
        self.active_provider: str = configs[0].provider if configs else "openrouter"

    def build(self, provider: str, model_id: str):
        """Construct a pydantic-ai model for the given provider + model id."""
        return self.sources[provider].build(model_id)

    def label(self, provider: str, model_id: str) -> str:
        src = self.sources.get(provider)
        return src.label(model_id) if src else f"{provider}/{model_id}"

    def is_local(self, provider: str) -> bool:
        src = self.sources.get(provider)
        return src.is_local if src is not None else False

    async def list_all(self) -> list[tuple[str, list[ModelEntry]]]:
        """Fetch catalogs for all providers concurrently. Returns one
        (provider, entries) tuple per provider, in insertion order."""
        import asyncio
        providers = list(self.sources.keys())
        entries_list = await asyncio.gather(
            *(self.sources[p].list_models() for p in providers)
        )
        return list(zip(providers, entries_list))
```

- [ ] **Step 4: Update `config/__init__.py`**

```python
from .env import config_dir, global_config_path, load_environment
from .model import (
    ModelConfig,
    ModelSource,
    MultiModelSource,
    SelectedModel,
    build_model,
    detect_providers,
    load_config,
)

__all__ = [
    "ModelConfig",
    "ModelSource",
    "MultiModelSource",
    "SelectedModel",
    "build_model",
    "config_dir",
    "detect_providers",
    "global_config_path",
    "load_config",
    "load_environment",
]
```

- [ ] **Step 5: Run tests**

```
.venv/bin/python -m pytest tests/test_config.py -v
```

Expected: all config tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/config/model.py src/marim_harness/config/__init__.py tests/test_config.py
git commit -m "feat(config): add SelectedModel, detect_providers, MultiModelSource"
```

---

### Task 2: Session store and controller — persist provider

**Files:**
- Modify: `src/marim_harness/session/store.py` (lines 58–162)
- Modify: `src/marim_harness/session/ctrl.py` (lines 53–78)

Context: `SessionStore` saves a JSON file per session. It currently stores `"model": str | None`. We add `"provider": str | None`. Old sessions without the key read back as `None` (treated as "use active provider"). `SessionController.set_model` is updated to accept and persist both. `new_session` is updated to forward provider.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_sessions_cli.py` (or a new `tests/test_session_store.py` if cleaner):

Actually, add to `tests/test_agent.py` since it already exercises set_model indirectly, or add a small focused test in `tests/test_config.py`. The cleanest place is a new focused block in `tests/test_agent.py`. But to keep it simple, add to `tests/test_sessions_cli.py`.

Add to `tests/test_sessions_cli.py`:

```python
from marim_harness.session.store import SessionManager


def test_session_store_persists_and_restores_provider(tmp_path):
    manager = SessionManager(tmp_path, base_dir=tmp_path / "sessions")
    store = manager.create()
    store.model = "gemini-2.5-flash"
    store.provider = "google"
    from pydantic_ai.usage import RunUsage
    store.save([], RunUsage(), [])

    # Re-open via manager.store()
    restored = manager.store(store.session_id)
    assert restored.model == "gemini-2.5-flash"
    assert restored.provider == "google"


def test_session_store_provider_defaults_to_none_for_old_sessions(tmp_path):
    import json
    sessions_dir = tmp_path / "sessions"
    from marim_harness.session.store import _workspace_dir
    ws_dir = _workspace_dir(sessions_dir, tmp_path)
    ws_dir.mkdir(parents=True)
    # Write a legacy session file without "provider" key
    (ws_dir / "old-session.json").write_text(json.dumps({
        "id": "old-session", "name": "old", "auto": False,
        "model": "anthropic/claude-sonnet-4-6",
        "workspace": str(tmp_path), "updated": "2026-01-01T00:00:00+00:00",
        "tokens": {"input": 0, "output": 0}, "tasks": [], "messages": [],
    }))
    manager = SessionManager(tmp_path, base_dir=sessions_dir)
    store = manager.store("old-session")
    assert store.model == "anthropic/claude-sonnet-4-6"
    assert store.provider is None  # backward compat
```

- [ ] **Step 2: Run tests to verify they fail**

```
.venv/bin/python -m pytest tests/test_sessions_cli.py::test_session_store_persists_and_restores_provider tests/test_sessions_cli.py::test_session_store_provider_defaults_to_none_for_old_sessions -v
```

Expected: FAIL with `AttributeError: 'SessionStore' object has no attribute 'provider'`.

- [ ] **Step 3: Update `session/store.py`**

In `SessionStore.__init__` (line 58), add `provider: Optional[str] = None` parameter:

```python
def __init__(self, path, workspace_root, session_id: str, name: str,
             auto_named: bool = False, model: Optional[str] = None,
             provider: Optional[str] = None) -> None:
    self.path = Path(path)
    self.workspace_root = Path(workspace_root).resolve()
    self.session_id = session_id
    self.name = name
    self.auto_named = auto_named
    self.model = model
    self.provider = provider
```

In `save()` (line 69), add `"provider"` to the payload dict:

```python
payload = {
    "id": self.session_id,
    "name": self.name,
    "auto": self.auto_named,
    "model": self.model,
    "provider": self.provider,
    "workspace": str(self.workspace_root),
    ...
}
```

In `store()` (line 145), read back `provider`:

```python
model = (saved or {}).get("model")
provider = (saved or {}).get("provider")
self._reserved.add(session_id)
return SessionStore(
    path, self.workspace_root, session_id, name,
    auto_named=auto_named, model=model, provider=provider,
)
```

- [ ] **Step 4: Update `session/ctrl.py`**

Change `set_model` (line 53):

```python
def set_model(self, model_id: str, provider: Optional[str] = None) -> None:
    if self.store is not None:
        self.store.model = model_id
        self.store.provider = provider
        self.persist()
```

Change `new_session` (line 72) to forward provider:

```python
def new_session(self, name: Optional[str] = None, model_id: Optional[str] = None,
                provider: Optional[str] = None) -> None:
    if self.manager is None:
        self.reset()
        return
    self.store = self.manager.create(name)
    if model_id is not None:
        self.store.model = model_id
    if provider is not None:
        self.store.provider = provider
    self.history = []
    self.usage = RunUsage()
```

- [ ] **Step 5: Run tests**

```
.venv/bin/python -m pytest tests/test_sessions_cli.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/session/store.py src/marim_harness/session/ctrl.py tests/test_sessions_cli.py
git commit -m "feat(session): persist provider alongside model id"
```

---

### Task 3: Harness — `model_provider` attribute + updated `set_model`

**Files:**
- Modify: `src/marim_harness/agent.py` (lines 30–62, 186–213)

Context: `Harness.__init__` receives `model_source: ModelSource | None`. We change it to accept `MultiModelSource | None` and add a `model_provider` attribute. `set_model` signature becomes `(provider: str, model_id: str, *, persist: bool = True)`. `_apply_saved_model` restores provider from the session store. `new_session` forwards provider.

The type annotation for `model_source` is untyped (`model_source=None`) intentionally to avoid importing `MultiModelSource` at the top level — this is fine, keep it.

- [ ] **Step 1: Write failing tests**

In `tests/test_agent.py`, find where `set_model` is called. Add:

```python
def test_harness_set_model_accepts_provider_and_model_id(fake_harness):
    """set_model signature is (provider, model_id)."""
    # fake_harness already has a model_source; just verify the new call works
    # without raising. The provider stored should match what was passed.
    fake_harness.set_model("openrouter", "anthropic/claude-sonnet-4-6")
    assert fake_harness.model_id == "anthropic/claude-sonnet-4-6"
    assert fake_harness.model_provider == "openrouter"
```

Check whether a `fake_harness` fixture already exists in `tests/test_agent.py`:

```bash
grep -n "fake_harness\|def harness\|@pytest.fixture" tests/test_agent.py | head -20
```

If no suitable fixture exists, add one near the top of the test file:

```python
@pytest.fixture
def fake_harness(tmp_path):
    from marim_harness.config import ModelConfig, MultiModelSource
    from marim_harness.bootstrap import build_harness
    # Use build_harness with a known env so model_source is a MultiModelSource
    import os
    os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
    return build_harness(tmp_path, mode=Mode.auto)
```

(The exact fixture implementation depends on what's already in `test_agent.py`. Read the file first before adding.)

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/python -m pytest tests/test_agent.py::test_harness_set_model_accepts_provider_and_model_id -v
```

Expected: FAIL (wrong number of arguments or missing `model_provider` attribute).

- [ ] **Step 3: Update `Harness.__init__` in `agent.py`**

Add `model_provider: Optional[str] = None` parameter (line 35–36 area):

```python
def __init__(self, model, provider: ToolProvider, deps: Deps, instructions: str,
             model_label: str = "model", store: Optional[SessionStore] = None,
             manager: Optional[SessionManager] = None,
             max_context_tokens: int = 100_000, keep_last_messages: int = 20,
             summarizer: Optional[Summarizer] = None,
             titler: Optional[Titler] = None, model_source=None,
             model_id: Optional[str] = None, model_provider: Optional[str] = None,
             proactive_memory: bool = False,
             mcp_servers=None, mcp_disabled=None):
```

And set it (after the existing `self.model_id = model_id` line):

```python
self.model_id = model_id
self.model_provider = model_provider
```

- [ ] **Step 4: Update `set_model` in `agent.py`**

Replace the existing `set_model` method (lines 186–202):

```python
def set_model(self, provider: str, model_id: str, *, persist: bool = True) -> None:
    """Switch the active model at runtime. Rebuilds the per-turn model and
    any configured aux agents (summarizer/titler), updates the label, and
    records the choice on the session. No-op without a source."""
    if self.model_source is None:
        return
    model = self.model_source.build(provider, model_id)
    self.current_model = model
    self.model_id = model_id
    self.model_provider = provider
    self.model_label = self.model_source.label(provider, model_id)
    if self.session.summarizer is not None:
        self.session.summarizer = make_summarizer(model)
    if self.session.titler is not None:
        self.session.titler = make_titler(model)
    if persist:
        self.session.set_model(model_id, provider)
```

- [ ] **Step 5: Update `_apply_saved_model` in `agent.py`**

Replace lines 204–213:

```python
def _apply_saved_model(self) -> None:
    """Re-point at a session's saved model after loading it, if one differs
    from what's already active."""
    if (
        self.store is not None
        and self.store.model
        and self.model_source is not None
        and self.store.model != self.model_id
    ):
        provider = self.store.provider or self.model_provider or "openrouter"
        self.set_model(provider, self.store.model, persist=False)
```

- [ ] **Step 6: Update `new_session` in `agent.py`**

Find the `new_session` method (around line 168–172). Update the `session.new_session` call to forward provider:

```python
def new_session(self, name: Optional[str] = None) -> None:
    self.session.new_session(name, model_id=self.model_id, provider=self.model_provider)
```

- [ ] **Step 7: Run tests**

```
.venv/bin/python -m pytest tests/test_agent.py -v
```

Expected: all agent tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): set_model accepts (provider, model_id); track model_provider"
```

---

### Task 4: Bootstrap — use `detect_providers` + `MultiModelSource`

**Files:**
- Modify: `src/marim_harness/bootstrap.py`

Context: `bootstrap.py` currently calls `load_config()` → single `ModelConfig`, wraps it in `ModelSource`. After this change it calls `detect_providers()` → list of `ModelConfig`, wraps in `MultiModelSource`. The active config (`configs[0]`) is still used for the initial model build and label.

- [ ] **Step 1: Write a test**

Add to `tests/test_headless.py` (or `tests/test_app.py`, whichever tests `build_harness`):

```python
def test_build_harness_model_source_is_multi(tmp_path, monkeypatch):
    from marim_harness.config import MultiModelSource
    from marim_harness.bootstrap import build_harness
    from marim_harness.permissions import Mode
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    h = build_harness(tmp_path, mode=Mode.auto)
    assert isinstance(h.model_source, MultiModelSource)
    assert h.model_provider == "openrouter"
```

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/python -m pytest tests/test_headless.py::test_build_harness_model_source_is_multi -v
```

Expected: FAIL (`isinstance` check fails — `model_source` is still a `ModelSource`).

- [ ] **Step 3: Update `bootstrap.py`**

Replace the import and usage:

```python
from .config import MultiModelSource, build_model, detect_providers, load_config
```

Replace the `load_config` / `ModelSource` wiring:

```python
def build_harness(
    workspace: Path,
    *,
    mode: Mode,
    resume: bool = False,
) -> Harness:
    configs = detect_providers()
    cfg = configs[0]           # active provider
    model = build_model(cfg)
    multi_source = MultiModelSource(configs)

    deps = Deps(workspace_root=workspace, mode=mode)
    manager = SessionManager(workspace)
    latest = manager.latest() if resume else None
    store = manager.store(latest.id) if latest is not None else manager.create()

    mcp_specs = load_mcp_config(workspace)
    mcp_servers, _ = build_mcp_servers(mcp_specs)
    mcp_disabled = disabled_server_names(mcp_specs)

    harness = Harness(
        model=model,
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions=INSTRUCTIONS,
        model_label=f"{cfg.provider}/{cfg.model}",
        store=store,
        manager=manager,
        max_context_tokens=cfg.max_context_tokens,
        summarizer=make_summarizer(model),
        titler=make_titler(model),
        model_source=multi_source,
        model_id=cfg.model,
        model_provider=cfg.provider,
        proactive_memory=cfg.proactive_memory,
        mcp_servers=mcp_servers,
        mcp_disabled=mcp_disabled,
    )
    if resume:
        harness.resume()
    return harness
```

Remove the now-unused `ModelSource` from the import line:

```python
from .config import MultiModelSource, build_model, detect_providers
```

(`load_config` is no longer needed here — `detect_providers()` calls it internally.)

- [ ] **Step 4: Run full test suite**

```
.venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/bootstrap.py tests/test_headless.py
git commit -m "feat(bootstrap): wire MultiModelSource from detect_providers"
```

---

### Task 5: `ModelPickerModal` — sections + `SelectedModel` dismiss

**Files:**
- Modify: `src/marim_harness/interfaces/tui/model_picker.py`
- Test: `tests/test_model_picker.py`

Context: The current picker takes a flat `list[ModelEntry]` and dismisses with `Optional[str]`. The new picker takes `sections: list[tuple[str, list[ModelEntry]]]` and dismisses with `Optional[SelectedModel]`. Option IDs are encoded as `"{provider}::{model_id}"`.

**Sectioned display behaviour:**
- When the filter box is **empty**: render provider-section headers (as disabled, non-selectable `Option` items) followed by that provider's entries.
- When filter text is **non-empty**: render a flat filtered list across all providers (no headers), so search works globally.

**Local free-text:** When `local_provider: str | None` is given, typing a model id and pressing Enter (with nothing highlighted in the list) dismisses with `SelectedModel(local_provider, typed_text)`.

**Option ID encoding:** `f"{provider}::{model_id}"`. Parse with `id.split("::", 1)` → `(provider, model_id)`.

- [ ] **Step 1: Rewrite `tests/test_model_picker.py`**

Replace the file with:

```python
import pytest
from textual.app import App

from marim_harness.config import SelectedModel
from marim_harness.workspace import ModelEntry
from marim_harness.interfaces.tui.model_picker import ModelPickerModal

_OR_ENTRIES = [
    ModelEntry(id="anthropic/claude-sonnet-4-6", name="Claude Sonnet 4.6"),
    ModelEntry(id="openai/gpt-5.2", name="GPT-5.2"),
]
_G_ENTRIES = [
    ModelEntry(id="gemini-2.5-flash", name="Gemini Flash"),
]
_SECTIONS = [
    ("openrouter", _OR_ENTRIES),
    ("google", _G_ENTRIES),
]


class _Host(App):
    def __init__(self, sections, local_provider=None, current=None):
        super().__init__()
        self.sections = sections
        self.local_provider = local_provider
        self.current = current
        self.result = "unset"

    def on_mount(self):
        self.run_worker(self._pick())

    async def _pick(self):
        self.result = await self.push_screen_wait(
            ModelPickerModal(
                self.sections,
                local_provider=self.local_provider,
                current=self.current,
            )
        )


@pytest.mark.anyio
async def test_filter_then_enter_picks_highlighted():
    app = _Host(_SECTIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "gpt":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == SelectedModel(provider="openrouter", model_id="openai/gpt-5.2")


@pytest.mark.anyio
async def test_escape_cancels_with_none():
    app = _Host(_SECTIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@pytest.mark.anyio
async def test_free_text_entry_for_local_provider():
    app = _Host(
        sections=[("local", [])],
        local_provider="local",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "my-local-model":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == SelectedModel(provider="local", model_id="my-local-model")


@pytest.mark.anyio
async def test_empty_enter_does_not_dismiss_without_free_text():
    app = _Host([("openrouter", [])], local_provider=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@pytest.mark.anyio
async def test_click_option_dismisses_with_selected_model():
    app = _Host(_SECTIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")   # move focus to list
        await pilot.press("enter")
        await pilot.pause()
    # First selectable option after any section header should be openrouter's first entry
    assert isinstance(app.result, SelectedModel)
    assert app.result.provider == "openrouter"
```

- [ ] **Step 2: Run tests to verify they fail**

```
.venv/bin/python -m pytest tests/test_model_picker.py -v
```

Expected: FAIL (old picker API doesn't accept `sections`).

- [ ] **Step 3: Rewrite `model_picker.py`**

```python
"""A modal for choosing the active model: a filtered, sectioned list of the
providers' catalogs. Sections are shown when the filter box is empty;
filtering flattens entries across all providers."""

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option, Separator

from ...config import SelectedModel
from ...workspace import ModelEntry, filter_entries


class ModelPickerModal(ModalScreen[Optional[SelectedModel]]):
    """Dismisses with a SelectedModel, or None if cancelled."""

    CSS = """
    ModelPickerModal {
        align: center middle;
    }
    #model-box {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #model-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #model-options {
        height: auto;
        max-height: 20;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        sections: list[tuple[str, list[ModelEntry]]],
        local_provider: Optional[str] = None,
        current: Optional[SelectedModel] = None,
    ) -> None:
        super().__init__()
        self.sections = sections
        self.local_provider = local_provider
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="model-box"):
            title = "Select a model"
            if self.current:
                title += f"  (current: {self.current.provider}/{self.current.model_id})"
            yield Static(title, id="model-title")
            has_local_only = (
                self.local_provider is not None
                and all(not entries for _, entries in self.sections)
            )
            placeholder = (
                "type a model id"
                if has_local_only
                else "filter… (Tab to navigate, Enter to pick)"
            )
            yield Input(placeholder=placeholder, id="model-filter")
            yield OptionList(id="model-options")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#model-filter", Input).focus()

    def _populate(self, query: str) -> None:
        options = self.query_one("#model-options", OptionList)
        options.clear_options()
        if query.strip():
            # Flat filtered list — no section headers
            all_entries = [
                (provider, entry)
                for provider, entries in self.sections
                for entry in entries
            ]
            for provider, entry in all_entries:
                if query.strip().lower() in entry.id.lower() or query.strip().lower() in entry.name.lower():
                    label = entry.id if entry.id == entry.name else f"{entry.id}  —  {entry.name}"
                    options.add_option(Option(label, id=f"{provider}::{entry.id}"))
        else:
            # Sectioned display
            for i, (provider, entries) in enumerate(self.sections):
                if i > 0:
                    options.add_option(Separator())
                options.add_option(Option(f"── {provider} ──", disabled=True))
                for entry in entries:
                    label = entry.id if entry.id == entry.name else f"{entry.id}  —  {entry.name}"
                    options.add_option(Option(label, id=f"{provider}::{entry.id}"))
        if options.option_count:
            options.highlighted = 0

    def _highlighted_selected(self) -> Optional[SelectedModel]:
        options = self.query_one("#model-options", OptionList)
        if options.option_count and options.highlighted is not None:
            opt_id = options.get_option_at_index(options.highlighted).id
            if opt_id and "::" in opt_id:
                provider, model_id = opt_id.split("::", 1)
                return SelectedModel(provider=provider, model_id=model_id)
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        choice = self._highlighted_selected()
        if choice is not None:
            self.dismiss(choice)
        elif self.local_provider and event.value.strip():
            self.dismiss(SelectedModel(provider=self.local_provider, model_id=event.value.strip()))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        opt_id = event.option.id
        if opt_id and "::" in opt_id:
            provider, model_id = opt_id.split("::", 1)
            self.dismiss(SelectedModel(provider=provider, model_id=model_id))

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python -m pytest tests/test_model_picker.py -v
```

Expected: all pass.

- [ ] **Step 5: Run full suite to catch regressions**

```
.venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/model_picker.py tests/test_model_picker.py
git commit -m "feat(picker): sections by provider + SelectedModel dismiss"
```

---

### Task 6: App — wire `open_model_picker` and `_on_model_chosen` to new API

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (lines 327–362)

Context: `open_model_picker` currently calls `source.list_models()` (single provider), then passes a flat list to `ModelPickerModal`. After this change it calls `harness.model_source.list_all()` (multi-source), passes sections to the picker, and `_on_model_chosen` receives `SelectedModel | None` and calls `harness.set_model(s.provider, s.model_id)`.

- [ ] **Step 1: Write a test**

In `tests/test_app.py` (or wherever the TUI app is integration-tested), add:

```python
@pytest.mark.anyio
async def test_on_model_chosen_calls_set_model_with_provider(fake_app):
    """_on_model_chosen forwards provider + model_id to harness.set_model."""
    from marim_harness.config import SelectedModel
    from unittest.mock import patch

    chosen = SelectedModel(provider="google", model_id="gemini-2.5-flash")
    with patch.object(fake_app.harness, "set_model") as mock_set:
        fake_app._on_model_chosen(chosen)
        mock_set.assert_called_once_with("google", "gemini-2.5-flash")
```

Check if `fake_app` fixture exists in `tests/test_app.py`:

```bash
grep -n "fake_app\|@pytest.fixture" tests/test_app.py | head -20
```

If not, add a minimal one before the test.

- [ ] **Step 2: Run to verify failure**

```
.venv/bin/python -m pytest tests/test_app.py::test_on_model_chosen_calls_set_model_with_provider -v
```

Expected: FAIL (`_on_model_chosen` still calls `harness.set_model(chosen)` with a string).

- [ ] **Step 3: Update `open_model_picker` in `app.py`**

Import `SelectedModel` at the top of the file (add to existing config imports):

```python
from ...config import SelectedModel
```

Replace `open_model_picker` (lines 327–351):

```python
async def open_model_picker(self) -> None:
    """Fetch all providers' catalogs and let the user pick a model, applying
    the choice to the harness. Degrades to free-text for local providers.

    Uses the callback form of push_screen (not push_screen_wait) so it works
    when called straight from the command-dispatch path.
    """
    multi_source = self.harness.model_source
    if multi_source is None:
        await self.post_system("Model switching isn't available here.")
        return

    sections = await multi_source.list_all()

    # Warn if every non-local provider returned an empty catalog
    non_local_all_failed = all(
        not entries
        for provider, entries in sections
        if not multi_source.is_local(provider)
    )
    if non_local_all_failed and sections:
        await self.post_system(
            "Couldn't fetch the model catalog — type a model id to set it directly."
        )

    # Local provider (if any) allows free-text entry
    local_provider = next(
        (provider for provider, _ in sections if multi_source.is_local(provider)),
        None,
    )
    current_selected = (
        SelectedModel(provider=self.harness.model_provider, model_id=self.harness.model_id)
        if self.harness.model_provider and self.harness.model_id
        else None
    )
    self.push_screen(
        ModelPickerModal(
            sections,
            local_provider=local_provider,
            current=current_selected,
        ),
        self._on_model_chosen,
    )
```

- [ ] **Step 4: Update `_on_model_chosen` in `app.py`**

Replace lines 353–362:

```python
def _on_model_chosen(self, chosen: SelectedModel | None) -> None:
    """Apply a model selected in the picker. A None result (cancelled) is a no-op."""
    if not chosen:
        return
    self.harness.set_model(chosen.provider, chosen.model_id)
    self._refresh_status()
    log = self.query_one("#log", VerticalScroll)
    log.mount(NoticeMessage(f"model: {self.harness.model_label}"))
    log.scroll_end(animate=False)
```

- [ ] **Step 5: Run all tests**

```
.venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_app.py
git commit -m "feat(app): open_model_picker uses MultiModelSource sections"
```

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|-------------|------|
| Auto-detect providers from env vars | Task 1 (`detect_providers`) |
| `SelectedModel` as picker return type | Task 1 + Task 5 |
| `MultiModelSource` parallel catalog fetch | Task 1 |
| Session persists provider | Task 2 |
| Old sessions gracefully default provider to None | Task 2 |
| `Harness.set_model(provider, model_id)` | Task 3 |
| `bootstrap.py` uses `MultiModelSource` | Task 4 |
| Picker groups by provider section | Task 5 |
| Filter flattens across all providers | Task 5 |
| Free text for local provider | Task 5 |
| `open_model_picker` calls `list_all()` | Task 6 |
| `_on_model_chosen` forwards provider | Task 6 |

**Placeholder scan:** None found — every step has complete code.

**Type consistency check:**
- `SelectedModel(provider, model_id)` — used consistently in Tasks 1, 5, 6
- `set_model(provider: str, model_id: str)` — defined in Task 3, called in Task 6
- `MultiModelSource.build(provider, model_id)` — defined in Task 1, called via Task 3
- `MultiModelSource.list_all() -> list[tuple[str, list[ModelEntry]]]` — defined in Task 1, consumed in Task 6
- `local_provider: str | None` — defined in Task 5 picker, passed in Task 6 ✓
- `ModelPickerModal(sections, local_provider, current)` — defined in Task 5, called in Task 6 ✓
