# Multi-provider model picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show models from every auto-detected provider in one tagged, filterable picker, route a pick to the right provider, and survive resume.

**Architecture:** A new `MultiModelSource` composite implements the same interface `Harness` already uses (`list_models`/`build`/`label`/`is_local`) over one per-provider `ModelSource`. Models are addressed by a colon-qualified id `provider:model_id`; a single parse rule (known active provider prefix → that provider, else default) gives backward-compat, `MARIM_MODEL` support, and forgiving free-text for free.

**Tech Stack:** Python 3.10+, Pydantic AI, Textual, pytest (anyio), uv, ruff, pyright.

## Global Constraints

- Python `>=3.10`; no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- Run `uv run …`; never bare `python`/`pytest`/`pip`.
- CI order, must pass locally: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- **CLI-startup invariant:** `config/model.py` and `bootstrap` must not eagerly import `pydantic_ai` or `httpx`. `tests/test_cli_startup.py` must stay green. Keep those imports lazy (inside functions).
- Qualified-id separator is a colon: `provider:model_id`. Known providers: `openrouter`, `google`, `local`.
- Commit message trailer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/marim_harness/workspace/catalog.py` — add `ModelEntry.provider` + `qualified` property; teach `filter_entries` to match provider.
- `src/marim_harness/config/model.py` — add `parse_qualified`, `detect_active_providers` (refactor `load_config` to share helpers), and `MultiModelSource`.
- `src/marim_harness/bootstrap.py` — construct `MultiModelSource` and the qualified startup id.
- `src/marim_harness/interfaces/tui/model_picker.py` — render provider tag, use qualified option ids.
- `src/marim_harness/interfaces/tui/app.py` — key vision-caps by qualified id.
- `.env.example` — document multiple providers active at once.
- Tests: `tests/test_catalog.py`, `tests/test_config.py`, `tests/test_bootstrap.py`, `tests/test_model_picker.py` (new if absent), `tests/test_cli_startup.py` (unchanged, must stay green).

---

## Task 1: `ModelEntry.provider` + `qualified`, provider-aware filter

**Files:**
- Modify: `src/marim_harness/workspace/catalog.py` (ModelEntry ~14-21, filter_entries ~51-58)
- Test: `tests/test_catalog.py`

**Interfaces:**
- Produces: `ModelEntry(id, name, supports_images=None, provider=None)`; property `ModelEntry.qualified -> str` = `f"{provider}:{id}"` when `provider` else `id`. `filter_entries(entries, query)` also matches `entry.provider`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_catalog.py`:

```python
def test_model_entry_qualified_uses_colon_when_provider_set():
    assert ModelEntry(id="qwen2.5-coder", name="Qwen", provider="local").qualified == "local:qwen2.5-coder"


def test_model_entry_qualified_is_bare_without_provider():
    assert ModelEntry(id="anthropic/claude-sonnet-4-6", name="Sonnet").qualified == "anthropic/claude-sonnet-4-6"


def test_filter_entries_matches_provider():
    entries = [
        ModelEntry(id="x", name="X", provider="openrouter"),
        ModelEntry(id="y", name="Y", provider="local"),
    ]
    assert [e.id for e in filter_entries(entries, "local")] == ["y"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_catalog.py -k "qualified or matches_provider" -v`
Expected: FAIL — `ModelEntry` has no `provider`/`qualified`; provider filter returns nothing.

- [ ] **Step 3: Write minimal implementation**

In `catalog.py`, replace the `ModelEntry` dataclass body:

```python
@dataclass(frozen=True)
class ModelEntry:
    """One selectable model: its provider id and a human-readable name.
    ``supports_images`` is True/False when the catalog states it, else None.
    ``provider`` is the source provider (stamped by MultiModelSource); None for a
    raw single-provider catalog. ``qualified`` is the canonical selectable id."""

    id: str
    name: str
    supports_images: bool | None = None
    provider: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.id}" if self.provider else self.id
```

Replace `filter_entries` return:

```python
    return [
        e for e in entries
        if q in e.id.lower()
        or q in e.name.lower()
        or (e.provider is not None and q in e.provider.lower())
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_catalog.py -v`
Expected: PASS (all catalog tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/catalog.py tests/test_catalog.py
git commit -m "feat(catalog): ModelEntry.provider + qualified id; filter on provider

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `parse_qualified` helper

**Files:**
- Modify: `src/marim_harness/config/model.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `parse_qualified(qualified: str, active: set[str] | frozenset[str], default: str) -> tuple[str, str]` returning `(provider, bare_id)`. If the part before the first `:` is in `active`, that's the provider and the remainder is the bare id; otherwise `(default, qualified)` (the whole string is the bare id).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_parse_qualified_known_prefix_routes_to_provider():
    from marim_harness.config.model import parse_qualified
    active = {"openrouter", "local", "google"}
    assert parse_qualified("openrouter:anthropic/claude-sonnet-4-6", active, "openrouter") == (
        "openrouter", "anthropic/claude-sonnet-4-6")
    assert parse_qualified("local:qwen2.5-coder", active, "openrouter") == ("local", "qwen2.5-coder")


def test_parse_qualified_bare_id_uses_default():
    from marim_harness.config.model import parse_qualified
    active = {"openrouter", "local"}
    assert parse_qualified("anthropic/claude-sonnet-4-6", active, "openrouter") == (
        "openrouter", "anthropic/claude-sonnet-4-6")


def test_parse_qualified_unknown_prefix_is_treated_as_bare_id():
    from marim_harness.config.model import parse_qualified
    # 'google' is NOT active here, so 'google/gemma' is a bare OpenRouter id, not a provider.
    active = {"openrouter", "local"}
    assert parse_qualified("google/gemma-2-9b", active, "openrouter") == (
        "openrouter", "google/gemma-2-9b")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_config.py -k parse_qualified -v`
Expected: FAIL — `cannot import name 'parse_qualified'`.

- [ ] **Step 3: Write minimal implementation**

In `config/model.py`, add after `_KNOWN_PROVIDERS`:

```python
def parse_qualified(qualified, active, default):
    """Split a ``provider:model_id`` into ``(provider, bare_id)``.

    If the segment before the first ':' is an active provider, route there with
    the remainder as the bare id. Otherwise the whole string is a bare id on the
    ``default`` provider — which makes bare ids (old sessions, MARIM_MODEL) and
    unknown prefixes (e.g. an OpenRouter ``vendor/model`` id) Just Work."""
    head, sep, rest = qualified.partition(":")
    if sep and head in active:
        return head, rest
    return default, qualified
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_config.py -k parse_qualified -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/model.py tests/test_config.py
git commit -m "feat(config): parse_qualified for provider:model_id ids

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `detect_active_providers` (refactor `load_config` to share builders)

**Files:**
- Modify: `src/marim_harness/config/model.py` (`load_config` body ~79-159)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ModelConfig`, the existing `common` kwargs.
- Produces:
  - `_common_kwargs() -> dict[str, Any]` — the provider-independent knobs (verbatim move of the current `common` dict).
  - `_provider_config(provider: str, common: dict) -> ModelConfig` — the per-provider config (verbatim move of the three branches).
  - `detect_active_providers() -> tuple[dict[str, ModelConfig], str]` — `({provider: ModelConfig} for each active provider}, default_provider)`. Active = creds present: `openrouter`⇐`OPENROUTER_API_KEY`, `google`⇐`GOOGLE_API_KEY|GEMINI_API_KEY`, `local`⇐`MARIM_BASE_URL`. The default provider (`MARIM_PROVIDER`, default `openrouter`) is ALWAYS included even if its creds are absent. `load_config()` keeps returning the single default `ModelConfig` (unchanged external contract).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_detect_active_providers_includes_each_with_creds(monkeypatch):
    from marim_harness.config.model import detect_active_providers
    for k in ("MARIM_PROVIDER", "OPENROUTER_API_KEY", "GOOGLE_API_KEY",
              "GEMINI_API_KEY", "MARIM_BASE_URL", "MARIM_API_KEY", "MARIM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("MARIM_BASE_URL", "http://localhost:1234/v1")
    configs, default = detect_active_providers()
    assert set(configs) == {"openrouter", "local"}
    assert default == "openrouter"
    assert configs["local"].base_url == "http://localhost:1234/v1"


def test_detect_active_providers_always_includes_default(monkeypatch):
    from marim_harness.config.model import detect_active_providers
    for k in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
              "MARIM_BASE_URL", "MARIM_API_KEY", "MARIM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MARIM_PROVIDER", "google")  # default, but no key set
    configs, default = detect_active_providers()
    assert default == "google"
    assert "google" in configs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_config.py -k detect_active -v`
Expected: FAIL — `cannot import name 'detect_active_providers'`.

- [ ] **Step 3: Write minimal implementation**

In `config/model.py`, refactor: extract the `common = dict(...)` block (currently inside `load_config`) into `_common_kwargs()`, and the three provider branches into `_provider_config`. Then have `load_config` call them, and add `detect_active_providers`.

```python
def _common_kwargs() -> dict[str, Any]:
    """Provider-independent knobs shared by every ModelConfig (verbatim of the
    former inline ``common`` dict in load_config)."""
    return dict(
        max_context_tokens=_int_env("MARIM_MAX_CONTEXT_TOKENS", 100_000),
        proactive_memory=_bool_env("MARIM_PROACTIVE_MEMORY", False),
        trust_project_hooks=_bool_env("MARIM_TRUST_PROJECT_HOOKS", False),
        lsp_enabled=_bool_env("MARIM_LSP", True),
        lsp_tools_enabled=_bool_env("MARIM_LSP_TOOLS", True),
        job_tool_combined=_bool_env("MARIM_JOB_TOOL_COMBINED", False),
        autonomous_wake=_bool_env("MARIM_AUTONOMOUS_WAKE", True),
        wake_depth_cap=_int_env("MARIM_WAKE_DEPTH_CAP", 8),
        subagent_concurrency=(_int_env("MARIM_SUBAGENT_CONCURRENCY", 0) or None),
        subagent_transcript_cap=_int_env("MARIM_SUBAGENT_TRANSCRIPT_CAP", 2000),
        detach_fanout=_bool_env("MARIM_DETACH_FANOUT", True),
        command_denylist=split_patterns(os.getenv("MARIM_COMMAND_DENYLIST", "")),
        command_allowlist=split_patterns(os.getenv("MARIM_COMMAND_ALLOWLIST", "")),
        notifications_enabled=_bool_env("MARIM_NOTIFICATIONS", True),
        notification_events=parse_events(os.getenv("MARIM_NOTIFICATION_EVENTS", "")),
    )


def _provider_config(provider: str, common: dict) -> ModelConfig:
    """Build the per-provider ModelConfig (model id, base_url, api_key) sharing
    ``common``. Unknown provider falls back to openrouter (historical default)."""
    if provider == "local":
        return ModelConfig(
            provider="local",
            model=os.getenv("MARIM_MODEL", _DEFAULT_LOCAL_MODEL),
            base_url=os.getenv("MARIM_BASE_URL", "http://localhost:11434/v1"),
            api_key=os.getenv("MARIM_API_KEY", "local"),
            **common,
        )
    if provider == "google":
        return ModelConfig(
            provider="google",
            model=os.getenv("MARIM_MODEL", _DEFAULT_GOOGLE_MODEL),
            base_url=None,
            api_key=(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
                     or os.getenv("MARIM_API_KEY")),
            **common,
        )
    return ModelConfig(
        provider="openrouter",
        model=os.getenv("MARIM_MODEL", _DEFAULT_OPENROUTER_MODEL),
        base_url=None,
        api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("MARIM_API_KEY"),
        **common,
    )


def _provider_has_creds(provider: str) -> bool:
    if provider == "openrouter":
        return bool(os.getenv("OPENROUTER_API_KEY"))
    if provider == "google":
        return bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    if provider == "local":
        return bool(os.getenv("MARIM_BASE_URL"))
    return False


def detect_active_providers() -> tuple[dict[str, "ModelConfig"], str]:
    """Every provider whose creds are present, keyed by name, plus the default
    provider (MARIM_PROVIDER). The default is always included so startup has a
    home even if its creds are absent."""
    default = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    if default not in _KNOWN_PROVIDERS:
        default = "openrouter"
    common = _common_kwargs()
    active = {p for p in _KNOWN_PROVIDERS if _provider_has_creds(p)}
    active.add(default)
    return {p: _provider_config(p, common) for p in active}, default
```

Then rewrite `load_config` to use the helpers (keep the unknown-provider warning):

```python
def load_config() -> ModelConfig:
    """Build the default-provider ModelConfig from environment variables."""
    provider = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    if provider not in _KNOWN_PROVIDERS:
        logger.warning(
            "Unknown MARIM_PROVIDER=%r; falling back to 'openrouter' "
            "(known providers: %s).",
            provider, ", ".join(sorted(_KNOWN_PROVIDERS)),
        )
        provider = "openrouter"
    return _provider_config(provider, _common_kwargs())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_config.py -v`
Expected: PASS — both new tests and every pre-existing `load_config` test (behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/model.py tests/test_config.py
git commit -m "refactor(config): extract provider-config builders; add detect_active_providers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `MultiModelSource` composite

**Files:**
- Modify: `src/marim_harness/config/model.py` (after `ModelSource`)
- Modify: `src/marim_harness/config/__init__.py` (export `MultiModelSource`, `detect_active_providers`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ModelSource`, `parse_qualified`, `detect_active_providers`.
- Produces: `MultiModelSource(sources: dict[str, ModelSource], default: str)` with:
  - `is_local: bool` — always `False` (composite is not a single local provider; the picker uses this only to keep free-text on).
  - `label(qualified: str) -> str` — returns a qualified `provider:id`; bare/unknown-prefix ids get the default prefix.
  - `build(qualified: str)` — parse → delegate to the matching sub-source's `build(bare)`.
  - `async list_models() -> list[ModelEntry]` — gather all sub-sources concurrently, stamp each entry's `provider`, concatenate; a sub-source that raises contributes nothing.
  - classmethod `from_env() -> "MultiModelSource"` — build from `detect_active_providers()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
@pytest.mark.anyio
async def test_multi_source_list_models_merges_and_tags(monkeypatch):
    from unittest.mock import AsyncMock
    from marim_harness.config.model import MultiModelSource, ModelSource, ModelConfig
    from marim_harness.workspace import ModelEntry

    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    loc = ModelSource(ModelConfig(provider="local", model="y", base_url="http://h/v1"))
    monkeypatch.setattr(orc, "list_models",
                        AsyncMock(return_value=[ModelEntry(id="anthropic/c", name="C")]))
    monkeypatch.setattr(loc, "list_models",
                        AsyncMock(return_value=[ModelEntry(id="qwen", name="Qwen")]))
    multi = MultiModelSource({"openrouter": orc, "local": loc}, "openrouter")
    entries = await multi.list_models()
    tagged = {e.qualified for e in entries}
    assert tagged == {"openrouter:anthropic/c", "local:qwen"}


@pytest.mark.anyio
async def test_multi_source_list_models_survives_a_failing_provider(monkeypatch):
    from unittest.mock import AsyncMock
    from marim_harness.config.model import MultiModelSource, ModelSource, ModelConfig
    from marim_harness.workspace import ModelEntry

    ok = ModelSource(ModelConfig(provider="local", model="y", base_url="http://h/v1"))
    bad = ModelSource(ModelConfig(provider="openrouter", model="x"))
    monkeypatch.setattr(ok, "list_models", AsyncMock(return_value=[ModelEntry(id="qwen", name="Q")]))
    monkeypatch.setattr(bad, "list_models", AsyncMock(side_effect=RuntimeError("down")))
    multi = MultiModelSource({"local": ok, "openrouter": bad}, "openrouter")
    entries = await multi.list_models()
    assert [e.qualified for e in entries] == ["local:qwen"]


def test_multi_source_build_routes_by_prefix(monkeypatch):
    from marim_harness.config.model import MultiModelSource, ModelSource, ModelConfig
    calls = {}
    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    loc = ModelSource(ModelConfig(provider="local", model="y", base_url="http://h/v1"))
    monkeypatch.setattr(orc, "build", lambda mid: calls.setdefault("or", mid))
    monkeypatch.setattr(loc, "build", lambda mid: calls.setdefault("loc", mid))
    multi = MultiModelSource({"openrouter": orc, "local": loc}, "openrouter")
    multi.build("local:qwen2.5-coder")
    multi.build("anthropic/claude-sonnet-4-6")  # bare -> default (openrouter)
    assert calls == {"loc": "qwen2.5-coder", "or": "anthropic/claude-sonnet-4-6"}


def test_multi_source_label_qualifies():
    from marim_harness.config.model import MultiModelSource, ModelSource, ModelConfig
    orc = ModelSource(ModelConfig(provider="openrouter", model="x"))
    multi = MultiModelSource({"openrouter": orc}, "openrouter")
    assert multi.label("openrouter:anthropic/c") == "openrouter:anthropic/c"
    assert multi.label("anthropic/c") == "openrouter:anthropic/c"  # bare gains default prefix
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_config.py -k multi_source -v`
Expected: FAIL — `cannot import name 'MultiModelSource'`.

- [ ] **Step 3: Write minimal implementation**

First, add `import asyncio` to the top-of-file imports (alongside `import logging`, `import os`). `replace` and `logger` are already imported at the top — do not re-import them.

Then, after the `ModelSource` class, add:

```python
class MultiModelSource:
    """A ModelSource over several providers at once. Implements the same
    interface the Harness/picker use (``list_models``/``build``/``label``/
    ``is_local``); models are addressed by a colon-qualified ``provider:model_id``.
    A bare or unknown-prefix id resolves on ``default``."""

    def __init__(self, sources: dict[str, ModelSource], default: str) -> None:
        self.sources = sources
        self.default = default

    @classmethod
    def from_env(cls) -> "MultiModelSource":
        configs, default = detect_active_providers()
        return cls({p: ModelSource(c) for p, c in configs.items()}, default)

    @property
    def is_local(self) -> bool:
        # The composite is not a single local provider; the picker reads this only
        # to keep free-text entry available, which we always want here.
        return False

    def _route(self, qualified: str) -> tuple[ModelSource, str]:
        provider, bare = parse_qualified(qualified, set(self.sources), self.default)
        return self.sources.get(provider, self.sources[self.default]), bare

    def label(self, model_id: str) -> str:
        provider, bare = parse_qualified(model_id, set(self.sources), self.default)
        return f"{provider}:{bare}"

    def build(self, model_id: str):
        source, bare = self._route(model_id)
        return source.build(bare)

    async def list_models(self) -> list[ModelEntry]:
        async def _one(provider: str, source: ModelSource) -> list[ModelEntry]:
            try:
                entries = await source.list_models()
            except Exception as exc:  # noqa: BLE001 - one provider's failure must not sink the rest
                logger.warning("model catalog for %s failed: %s", provider, exc)
                return []
            return [replace(e, provider=provider) for e in entries]

        results = await asyncio.gather(
            *[_one(p, s) for p, s in self.sources.items()]
        )
        return [e for group in results for e in group]
```

In `config/__init__.py`, add `MultiModelSource` and `detect_active_providers` to the imports/`__all__` next to `ModelSource`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_config.py -k multi_source -v`
Expected: PASS.

- [ ] **Step 5: Verify the CLI-startup invariant still holds**

Run: `uv run pytest --no-cov tests/test_cli_startup.py -v`
Expected: PASS (no eager pydantic_ai/httpx import; `asyncio` is stdlib and fine).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/config/model.py src/marim_harness/config/__init__.py tests/test_config.py
git commit -m "feat(config): MultiModelSource composite over multiple providers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Wire the composite into bootstrap

**Files:**
- Modify: `src/marim_harness/bootstrap.py` (lines ~35-37 and the resume block ~63-65)
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `MultiModelSource.from_env()`, `detect_active_providers`, `load_config` (still used for the harness-level knobs).
- Produces: `Harness.model_source` is a `MultiModelSource`; startup `model_id` is the qualified default `f"{default}:{default_model}"`; `model = model_source.build(model_id)`. Resume still honors `store.model` verbatim.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bootstrap.py` (follow the file's existing harness-construction fixtures; this asserts the wiring shape):

```python
def test_build_harness_uses_multi_model_source(monkeypatch, tmp_path):
    import marim_harness.bootstrap as b
    from marim_harness.config.model import MultiModelSource
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("MARIM_BASE_URL", "http://localhost:1234/v1")
    # Avoid constructing real provider models in the test:
    monkeypatch.setattr(MultiModelSource, "build", lambda self, mid: ("model", mid))
    h = b.build_harness(tmp_path)
    assert isinstance(h.model_source, MultiModelSource)
    assert set(h.model_source.sources) >= {"openrouter", "local"}
    assert h.model_id.startswith("openrouter:")  # qualified default
```

(If `test_bootstrap.py` already stubs `ModelSource.build` via a fixture, extend that fixture to also stub `MultiModelSource.build` so no network/model construction happens.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_bootstrap.py -k multi_model_source -v`
Expected: FAIL — `model_source` is a `ModelSource`, not `MultiModelSource`; `model_id` is bare.

- [ ] **Step 3: Write minimal implementation**

In `bootstrap.py`, replace the model-construction lines (currently `model_source = ModelSource(cfg)` / `model = build_model(cfg)` / `model_id = cfg.model`):

```python
    configs, default_provider = detect_active_providers()
    model_source = MultiModelSource(
        {p: ModelSource(c) for p, c in configs.items()}, default_provider
    )
    model_id = f"{default_provider}:{configs[default_provider].model}"
    model = model_source.build(model_id)
```

Keep `cfg = load_config()` for the harness-level knobs already read from `cfg` below (command lists, trust_project_hooks, notifications, detach_fanout, etc. — unchanged). Update the imports at the top of `bootstrap.py`:

```python
from .config import (
    ModelSource,
    MultiModelSource,
    detect_active_providers,
    load_config,
)
```

(`build_model` import can stay if used elsewhere; if it becomes unused, remove it to satisfy ruff F401.)

The resume block stays as-is — `model_source.build(store.model)` already routes correctly because `MultiModelSource.build` parses qualified-or-bare:

```python
    if not resume and store.model and store.model != model_id:
        model_id = store.model
        model = model_source.build(model_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_bootstrap.py -v`
Expected: PASS (new test + existing bootstrap tests; adjust the stub fixture if an existing test built a real model via `ModelSource`).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/bootstrap.py tests/test_bootstrap.py
git commit -m "feat(bootstrap): build MultiModelSource; qualified startup model id

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Picker renders provider tags, uses qualified ids; vision-caps keyed by qualified id

**Files:**
- Modify: `src/marim_harness/interfaces/tui/model_picker.py` (`_populate` ~116-123)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`_refresh_vision_caps` ~826-831)
- Test: `tests/test_model_picker.py` (create if absent)

**Interfaces:**
- Consumes: `ModelEntry.qualified`, `filter_entries` (provider-aware from Task 1).
- Produces: picker OptionList option id == `entry.qualified`; row label shows `{id}  —  {name}  · {provider}` when provider set; `app._refresh_vision_caps` builds `{entry.qualified: supports_images}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_model_picker.py` (mirror the Textual `run_test` pattern used in `tests/test_subagents_screen.py`):

```python
import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from marim_harness.interfaces.tui.model_picker import ModelPickerModal
from marim_harness.workspace import ModelEntry


class _Host(App):
    def compose(self) -> ComposeResult:
        yield from ()


@pytest.mark.anyio
async def test_picker_option_id_is_qualified_and_label_tags_provider():
    entries = [ModelEntry(id="anthropic/c", name="Claude", provider="openrouter")]
    app = _Host()
    async with app.run_test() as pilot:
        await app.push_screen(ModelPickerModal(entries=entries, allow_free_text=True))
        await pilot.pause()
        opts = app.query_one("#model-options", OptionList)
        opt = opts.get_option_at_index(0)
        assert opt.id == "openrouter:anthropic/c"
        assert "· openrouter" in str(opt.prompt)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_model_picker.py -v`
Expected: FAIL — option id is the bare `anthropic/c` and the label has no provider tag.

- [ ] **Step 3: Write minimal implementation**

In `model_picker.py`, replace `_populate`:

```python
    def _populate(self, entries: list[ModelEntry]) -> None:
        options = self.query_one("#model-options", OptionList)
        options.clear_options()
        for entry in entries:
            label = entry.id if entry.id == entry.name else f"{entry.id}  —  {entry.name}"
            if entry.provider:
                label = f"{label}  · {entry.provider}"
            options.add_option(Option(label, id=entry.qualified))
        if entries:
            options.highlighted = 0
```

In `app.py`, change `_refresh_vision_caps` to key by qualified id:

```python
        self._vision_caps = {e.qualified: e.supports_images for e in entries}
```

(The lookup at `self._vision_caps.get(model_id)` already uses the active `model_id`, which is now qualified — keys and lookup stay consistent.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_model_picker.py tests/test_subagents_screen.py -v`
Expected: PASS (new picker test + existing TUI tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/model_picker.py src/marim_harness/interfaces/tui/app.py tests/test_model_picker.py
git commit -m "feat(tui): tag picker rows with provider; qualified option ids + vision caps

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Docs + full verification gate

**Files:**
- Modify: `.env.example`
- (no new tests)

- [ ] **Step 1: Document multi-provider in `.env.example`**

Add below the existing local-provider block:

```bash
# --- Multiple providers at once ---
# Any provider whose credentials are present is auto-detected and its models are
# merged into one tagged picker list (filter by id, name, or provider). Set
# several at once; MARIM_PROVIDER selects the default (startup model + the target
# for a typed model id with no provider prefix). Pick another provider's model in
# the picker, or type a qualified id like:  local:qwen2.5-coder
```

- [ ] **Step 2: Full CI gate**

Run: `uv run ruff check src tests`
Expected: `All checks passed!`

Run: `uv run pyright`
Expected: `0 errors`.

Run: `uv run pytest --no-cov -p no:warnings`
Expected: all pass (exit 0).

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(env): document multi-provider auto-detection and qualified ids

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** activation→Task 3; flat-list+tags→Tasks 1,6; free-text/colon parse→Task 2; composite/routing→Task 4; persistence (no schema change)→Task 5 (resume block); error handling (failing provider, unknown prefix)→Tasks 2,4; CLI-startup invariant→Tasks 4,7; `.env`→Task 7. No gaps.
- **Placeholder scan:** none — every code/test step carries real content.
- **Type consistency:** `ModelEntry.qualified`, `parse_qualified(qualified, active, default)->(provider,bare)`, `detect_active_providers()->(dict,str)`, `MultiModelSource(sources: dict[str,ModelSource], default)` with `list_models/build/label/is_local` — names/signatures consistent across Tasks 1-6.
