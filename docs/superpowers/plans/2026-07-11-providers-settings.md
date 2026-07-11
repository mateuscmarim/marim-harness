# Providers Settings Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Providers" section on the TUI settings screen: enter/replace/remove provider credentials, set the local base URL, and pick the default provider — applying live (no restart) with implicit catalog verification on save.

**Architecture:** A new `MultiModelSource.refresh_from_env()` mutates the live source object **in place** (the harness captures the object in closures at `build_collaborators`, so in-place mutation propagates to the picker, `set_model`, and sub-agents with zero rewiring). A new self-contained `ProvidersPane` widget (`interfaces/tui/providers.py`) renders stacked provider cards, persists via the existing `save_env_settings` (global `.env` + `os.environ` mirror), refreshes the source, and verifies with a background `list_models()` fetch. The `SettingsScreen` mounts the pane as one more rail section.

**Tech Stack:** Python ≥3.10, Textual (TUI), pytest + `pytest.mark.anyio` + Textual pilot tests, `uv` for all commands.

**Spec:** `docs/superpowers/specs/2026-07-11-providers-settings-design.md`

## Global Constraints

- Use `uv` for everything: `uv run pytest …`, `uv run ruff …`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- `requires-python >=3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity capped at 10 (`C901`) — extract helpers rather than adding `# noqa: C901`.
- The screen must **never display a stored secret**: key inputs are `password=True`, start empty, and the placeholder proves state (`configured · …7f2a — type to replace` / `not set`). An empty commit is a no-op.
- Credentials are written to the **global** `.env` only (via `save_env_settings`), consistent with `_PROJECT_ENV_BLOCKLIST` in `config/env.py`.
- Preserve the codebase's long "why" comments; write model/user-facing copy exactly as given in this plan.
- CI order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.

## File Structure

- **Modify** `src/marim_harness/config/model.py` — add `MultiModelSource.refresh_from_env()`.
- **Create** `src/marim_harness/interfaces/tui/providers.py` — provider spec table, pure helpers, and the `ProvidersPane` widget. One responsibility: the Providers section.
- **Modify** `src/marim_harness/interfaces/tui/settings.py` — mount the pane as a new rail section; rail badge.
- **Modify** `tests/test_config.py` — `refresh_from_env` tests.
- **Create** `tests/test_providers_section.py` — pure-helper unit tests + pane pilot tests.
- **Modify** `tests/test_settings_screen.py` — section-mount assertions.

---

### Task 1: `MultiModelSource.refresh_from_env()`

**Files:**
- Modify: `src/marim_harness/config/model.py` (class `MultiModelSource`, after `from_env`, ~line 441)
- Test: `tests/test_config.py` (append after `test_multi_source_build_routes_by_prefix`)

**Interfaces:**
- Consumes: existing `detect_active_providers()`, `ModelSource`.
- Produces: `MultiModelSource.refresh_from_env() -> None` — re-detects providers from the current environment, mutating `self.sources` and `self.default` in place. Later tasks call it after every credential save/remove.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def _clear_provider_env(monkeypatch):
    for k in ("MARIM_PROVIDER", "OPENROUTER_API_KEY", "GOOGLE_API_KEY",
              "GEMINI_API_KEY", "MARIM_BASE_URL", "MARIM_API_KEY", "MARIM_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_multi_source_refresh_picks_up_and_drops_providers(monkeypatch):
    """refresh_from_env mutates sources IN PLACE: a provider appears once its
    creds land in the env, and drops out once they're removed — while the
    default provider is always kept (startup must have a home)."""
    from marim_harness.config import model as _m
    from marim_harness.config.model import MultiModelSource

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    multi = MultiModelSource.from_env()
    held_sources = multi.sources  # the dict object closures/tests may hold
    assert set(multi.sources) == {"openrouter"}

    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    multi.refresh_from_env()
    assert set(multi.sources) == {"openrouter", "google"}
    assert multi.sources is held_sources  # same dict object — mutated, not replaced

    monkeypatch.delenv("GOOGLE_API_KEY")
    multi.refresh_from_env()
    assert set(multi.sources) == {"openrouter"}  # dropped; default kept


def test_multi_source_refresh_switches_default(monkeypatch):
    from marim_harness.config import model as _m
    from marim_harness.config.model import MultiModelSource

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    multi = MultiModelSource.from_env()
    assert multi.default == "openrouter"

    monkeypatch.setenv("MARIM_PROVIDER", "google")
    multi.refresh_from_env()
    assert multi.default == "google"
    assert "google" in multi.sources
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py -k refresh -v`
Expected: FAIL with `AttributeError: 'MultiModelSource' object has no attribute 'refresh_from_env'`

- [ ] **Step 3: Implement**

In `src/marim_harness/config/model.py`, inside `MultiModelSource`, directly after the `from_env` classmethod:

```python
    def refresh_from_env(self) -> None:
        """Re-detect providers from the current environment, IN PLACE.

        ``build_collaborators`` captures this object in closures at Harness
        construction (``lambda mid, _src=cfg.model_source: _src.build(mid)``),
        so mutating — never replacing — ``sources``/``default`` is what makes
        a settings-screen credential change visible to the model picker,
        ``set_model``, and sub-agent model building without any rewiring.
        ``save_env_settings`` mirrors saves into ``os.environ`` first, so
        ``detect_active_providers`` here sees the new credentials."""
        configs, default = detect_active_providers()
        self.sources.clear()
        self.sources.update({p: ModelSource(c) for p, c in configs.items()})
        self.default = default
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_config.py -k "refresh or multi_source" -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/model.py tests/test_config.py
git commit -m "feat(config): MultiModelSource.refresh_from_env for live provider changes"
```

---

### Task 2: Provider spec table + pure helpers

**Files:**
- Create: `src/marim_harness/interfaces/tui/providers.py`
- Test: `tests/test_providers_section.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks (pure module slice).
- Produces (later tasks rely on these exact names):
  - `ProviderSpec` frozen dataclass: `name: str`, `write_key: str | None`, `key_fallbacks: tuple[str, ...]`, `read_keys: tuple[str, ...]`, `drop_keys: tuple[str, ...]`, `base_url_key: str | None = None`
  - `PROVIDER_SPECS: tuple[ProviderSpec, ...]` (order: openrouter, google, local, claude-cli) and `_SPECS: dict[str, ProviderSpec]`
  - `key_hint(value: str | None) -> str`
  - `short_error(exc: Exception) -> str`
  - `spec_configured(spec: ProviderSpec) -> bool` (env-based; claude-cli is special-cased by the pane, not here)
  - `current_default_provider() -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_providers_section.py`:

```python
"""Tests for the settings screen's Providers section: the pure spec/helper
layer and the ProvidersPane widget (compose, commit, verify, remove, default)."""

import os

import pytest

from marim_harness.interfaces.tui.providers import (
    PROVIDER_SPECS,
    current_default_provider,
    key_hint,
    short_error,
    spec_configured,
)


@pytest.fixture
def isolated_env():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_key_hint_states():
    assert key_hint(None) == "not set"
    assert key_hint("") == "not set"
    # Long enough to safely reveal the last 4 chars.
    assert key_hint("sk-or-abcdef7f2a") == "configured · …7f2a — type to replace"
    # Short keys never leak a suffix (it would reveal most of the secret).
    assert key_hint("short") == "configured — type to replace"


def test_short_error_first_line_truncated():
    assert short_error(RuntimeError("boom")) == "boom"
    assert short_error(RuntimeError("line one\nline two")) == "line one"
    long = RuntimeError("x" * 80)
    assert len(short_error(long)) == 48 and short_error(long).endswith("…")
    assert short_error(RuntimeError("")) == "RuntimeError"


def test_provider_specs_env_keys():
    specs = {s.name: s for s in PROVIDER_SPECS}
    assert [s.name for s in PROVIDER_SPECS] == [
        "openrouter", "google", "local", "claude-cli"]
    assert specs["openrouter"].write_key == "OPENROUTER_API_KEY"
    assert specs["openrouter"].drop_keys == ("OPENROUTER_API_KEY",)
    # google always WRITES GOOGLE_API_KEY but reads/drops both env names.
    assert specs["google"].write_key == "GOOGLE_API_KEY"
    assert specs["google"].key_fallbacks == ("GEMINI_API_KEY",)
    assert set(specs["google"].read_keys) == {"GOOGLE_API_KEY", "GEMINI_API_KEY"}
    assert set(specs["google"].drop_keys) == {"GOOGLE_API_KEY", "GEMINI_API_KEY"}
    # local is configured by its base URL; removal clears URL + key together.
    assert specs["local"].base_url_key == "MARIM_BASE_URL"
    assert specs["local"].read_keys == ("MARIM_BASE_URL",)
    assert set(specs["local"].drop_keys) == {"MARIM_BASE_URL", "MARIM_API_KEY"}
    # claude-cli stores nothing.
    assert specs["claude-cli"].write_key is None
    assert specs["claude-cli"].drop_keys == ()


def test_spec_configured_reads_any_key(isolated_env, monkeypatch):
    specs = {s.name: s for s in PROVIDER_SPECS}
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert spec_configured(specs["google"]) is False
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert spec_configured(specs["google"]) is True


def test_current_default_provider(isolated_env, monkeypatch):
    monkeypatch.delenv("MARIM_PROVIDER", raising=False)
    assert current_default_provider() == "openrouter"
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    assert current_default_provider() == "google"
    monkeypatch.setenv("MARIM_PROVIDER", "azure")  # unknown -> fallback
    assert current_default_provider() == "openrouter"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: FAIL at import (`ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.providers'`)

- [ ] **Step 3: Implement the module's pure slice**

Create `src/marim_harness/interfaces/tui/providers.py`:

```python
"""The settings screen's Providers section: stacked cards for the four built-in
providers (openrouter / google / local / claude-cli), a default-provider radio,
live apply, implicit verification, and key removal.

Credentials save to the GLOBAL .env only (a project .env may not set these keys
at all — see _PROJECT_ENV_BLOCKLIST in config/env.py), and ``save_env_settings``
mirrors them into ``os.environ``, so an in-place
``MultiModelSource.refresh_from_env()`` right after a save makes the provider
active for the model picker without a restart. Key inputs are password fields
that start EMPTY — the placeholder proves the configured state without ever
painting the secret, and an empty commit is a no-op so focus/blur can never
clobber a stored key."""

from __future__ import annotations

import os
from dataclasses import dataclass

_KNOWN = ("openrouter", "google", "local", "claude-cli")


@dataclass(frozen=True)
class ProviderSpec:
    """Which env keys one provider reads/writes, driving its settings card."""

    name: str
    write_key: str | None  # env var an API-key commit writes (None: no key field)
    key_fallbacks: tuple[str, ...]  # alt env names probed for the placeholder hint
    read_keys: tuple[str, ...]  # any of these set ⇒ configured
    drop_keys: tuple[str, ...]  # removed together by the remove button
    base_url_key: str | None = None  # local only


PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "openrouter",
        write_key="OPENROUTER_API_KEY",
        key_fallbacks=(),
        read_keys=("OPENROUTER_API_KEY",),
        drop_keys=("OPENROUTER_API_KEY",),
    ),
    # google is configured by EITHER env name, but a save always writes
    # GOOGLE_API_KEY and a remove must drop BOTH (either one alone would
    # keep the provider configured).
    ProviderSpec(
        "google",
        write_key="GOOGLE_API_KEY",
        key_fallbacks=("GEMINI_API_KEY",),
        read_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        drop_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ),
    # local is marked configured by its base URL (matching _provider_has_creds);
    # removal clears URL + key together — a leftover key alone is meaningless.
    ProviderSpec(
        "local",
        write_key="MARIM_API_KEY",
        key_fallbacks=(),
        read_keys=("MARIM_BASE_URL",),
        drop_keys=("MARIM_BASE_URL", "MARIM_API_KEY"),
        base_url_key="MARIM_BASE_URL",
    ),
    # claude-cli stores nothing: the CLI owns auth; status is binary detection.
    ProviderSpec(
        "claude-cli", write_key=None, key_fallbacks=(), read_keys=(), drop_keys=()
    ),
)
_SPECS = {s.name: s for s in PROVIDER_SPECS}

_DEFAULT_LOCAL_URL = "http://localhost:11434/v1"


def key_hint(value: str | None) -> str:
    """Placeholder for a password input: proves whether a key is stored — and
    shows its last 4 chars when the key is long enough that this reveals
    nothing useful — without ever painting the secret itself."""
    if not value:
        return "not set"
    if len(value) >= 8:
        return f"configured · …{value[-4:]} — type to replace"
    return "configured — type to replace"


def short_error(exc: Exception) -> str:
    """First line of an exception, truncated to fit the one-line card badge."""
    text = (str(exc) or type(exc).__name__).splitlines()[0]
    return text if len(text) <= 48 else text[:47] + "…"


def spec_configured(spec: ProviderSpec) -> bool:
    """Env-based configured check (any read key set). claude-cli has no read
    keys — the pane special-cases it via CLI-binary detection instead."""
    return any(os.getenv(k) for k in spec.read_keys)


def current_default_provider() -> str:
    """MARIM_PROVIDER from the env, normalized like load_config: lowercased,
    unknown values falling back to openrouter (the historical default)."""
    default = os.getenv("MARIM_PROVIDER", "openrouter").lower()
    return default if default in _KNOWN else "openrouter"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py
git commit -m "feat(tui): provider spec table + pure helpers for the Providers section"
```

---

### Task 3: `ProvidersPane` — cards, key/base-URL commit, live refresh

**Files:**
- Modify: `src/marim_harness/interfaces/tui/providers.py` (append the widget)
- Test: `tests/test_providers_section.py` (append)

**Interfaces:**
- Consumes: Task 1's `MultiModelSource.refresh_from_env()`; Task 2's specs/helpers; existing `save_env_settings` from `marim_harness.config`.
- Produces: `ProvidersPane(Vertical)` with constructor
  `ProvidersPane(*, model_source: object | None, status: Callable[[str], None], set_badge: Callable[[str], None], cli_detected: bool, id: str | None = None)`
  and internal methods later tasks extend: `_commit(widget_id)`, `_after_change(spec)`, `_paint_card(spec)`, `_refresh_sources()`, `_provider_source(name)`.
  Widget ids: `prov-card-<name>`, `prov-dot-<name>`, `prov-status-<name>`, `prov-remove-<name>`, `prov-key-<name>`, `prov-url-local`, `prov-default-set`, `prov-default-<name>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_providers_section.py`:

```python
from textual.app import App
from textual.widgets import Button, Input, Static

from marim_harness.interfaces.tui.providers import ProvidersPane


class _PaneHost(App):
    """Minimal host mirroring what SettingsScreen passes the pane."""

    def __init__(self, *, model_source=None, cli_detected=False):
        super().__init__()
        self._model_source = model_source
        self._cli_detected = cli_detected
        self.statuses: list[str] = []
        self.badges: list[str] = []

    def compose(self):
        yield ProvidersPane(
            model_source=self._model_source,
            status=self.statuses.append,
            set_badge=self.badges.append,
            cli_detected=self._cli_detected,
        )


@pytest.mark.anyio
async def test_pane_mounts_all_cards_without_writing_env(
    isolated_env, monkeypatch, tmp_path
):
    """Mounting paints all four cards and must not write .env (mount-time
    widget events are gated, like the settings screen's _ready flag)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        for name in ("openrouter", "google", "local", "claude-cli"):
            assert pane.query_one(f"#prov-card-{name}") is not None
        # Key inputs are password fields that start empty.
        key = pane.query_one("#prov-key-openrouter", Input)
        assert key.password is True and key.value == ""
    assert not (tmp_path / "marim" / ".env").exists()


@pytest.mark.anyio
async def test_key_commit_saves_clears_and_repaints(
    isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        inp = pane.query_one("#prov-key-openrouter", Input)
        inp.value = "sk-or-test-1234abcd"
        pane._commit("prov-key-openrouter")  # what Enter/blur trigger
        await pilot.pause()
        assert os.environ.get("OPENROUTER_API_KEY") == "sk-or-test-1234abcd"
        env_text = (tmp_path / "marim" / ".env").read_text()
        assert "OPENROUTER_API_KEY=sk-or-test-1234abcd" in env_text
        # The secret never lingers in the widget; the placeholder proves state.
        assert inp.value == ""
        assert inp.placeholder == "configured · …abcd — type to replace"
        assert any("OPENROUTER_API_KEY" in s for s in app.statuses)


@pytest.mark.anyio
async def test_empty_commit_is_a_noop(isolated_env, monkeypatch, tmp_path):
    """Blur with an empty input (the normal focus-pass-through case) writes
    nothing — a stored key can never be clobbered by navigation."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        pane._commit("prov-key-openrouter")
        await pilot.pause()
    assert not (tmp_path / "marim" / ".env").exists()


@pytest.mark.anyio
async def test_google_configured_via_gemini_key_but_writes_google_key(
    isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key-12345678")
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        # Configured state (and hint) come from the fallback env name...
        inp = pane.query_one("#prov-key-google", Input)
        assert inp.placeholder == "configured · …5678 — type to replace"
        assert "configured" in str(
            pane.query_one("#prov-status-google", Static).render()
        )
        # ...but a save always writes GOOGLE_API_KEY.
        inp.value = "AIza-new-key-0000"
        pane._commit("prov-key-google")
        await pilot.pause()
    assert os.environ.get("GOOGLE_API_KEY") == "AIza-new-key-0000"
    assert "GOOGLE_API_KEY=AIza-new-key-0000" in (
        tmp_path / "marim" / ".env"
    ).read_text()


@pytest.mark.anyio
async def test_local_base_url_commit_marks_configured(
    isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MARIM_BASE_URL", raising=False)
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        status = pane.query_one("#prov-status-local", Static)
        assert "not configured" in str(status.render())
        url = pane.query_one("#prov-url-local", Input)
        url.value = "http://localhost:1234/v1"
        pane._commit("prov-url-local")
        await pilot.pause()
        assert os.environ.get("MARIM_BASE_URL") == "http://localhost:1234/v1"
        assert "not configured" not in str(status.render())
        # Base URL is not a secret: the value stays visible in the input.
        assert url.value == "http://localhost:1234/v1"


@pytest.mark.anyio
async def test_commit_refreshes_live_sources(isolated_env, monkeypatch, tmp_path):
    """A key commit makes the provider active on the live MultiModelSource
    (the model picker sees it immediately — the whole point of 'live')."""
    from marim_harness.config import model as _m
    from marim_harness.config.model import MultiModelSource

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for k in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
              "MARIM_BASE_URL", "MARIM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    multi = MultiModelSource.from_env()
    assert "google" not in multi.sources
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        inp = pane.query_one("#prov-key-google", Input)
        inp.value = "AIza-live-key-0001"
        pane._commit("prov-key-google")
        await pilot.pause()
    assert "google" in multi.sources


@pytest.mark.anyio
async def test_claude_cli_card_reflects_detection(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _PaneHost(cli_detected=True)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        assert "detected on PATH" in str(
            pane.query_one("#prov-status-claude-cli", Static).render()
        )
        # Nothing stored -> no key field, no remove button.
        assert not pane.query("#prov-key-claude-cli")
        assert not pane.query("#prov-remove-claude-cli")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: new tests FAIL (`ImportError: cannot import name 'ProvidersPane'`); Task 2's tests still PASS.

- [ ] **Step 3: Implement the widget**

In `src/marim_harness/interfaces/tui/providers.py` — replace the import block at the top with:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Button, Input, RadioButton, RadioSet, Static

from ...config import MultiModelSource, save_env_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...config.model import ModelSource
```

Then append the widget after the pure helpers:

```python
class ProvidersPane(Vertical):
    """Stacked provider cards + default-provider radio (the Providers section).

    All persistence goes through ``save_env_settings`` (global .env, mirrored
    into os.environ); ``_refresh_sources`` then mutates the live
    ``MultiModelSource`` in place so the model picker sees the change
    immediately — see the module docstring. ``model_source`` may be a plain
    ModelSource or None (embedding/tests): saving still works, only the live
    refresh + verification are skipped."""

    DEFAULT_CSS = """
    ProvidersPane { height: auto; }
    .prov-card { height: auto; margin-bottom: 1; }
    .prov-head { height: 1; }
    .prov-dot { width: 2; }
    .prov-name { width: 14; text-style: bold; }
    .prov-status { width: 1fr; color: $text-muted; }
    .prov-head Button { width: auto; height: 1; border: none; padding: 0 1; }
    .prov-field { height: 3; padding-left: 2; }
    .prov-field Static { width: 10; height: 3; content-align: left middle; color: $text-muted; }
    .prov-field Input { width: 48; }
    .prov-note { height: 1; padding-left: 2; color: $text-muted; }
    #prov-default-label { margin-top: 1; }
    """

    def __init__(
        self,
        *,
        model_source: object | None,
        status: Callable[[str], None],
        set_badge: Callable[[str], None],
        cli_detected: bool,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._model_source = model_source
        self._status = status
        self._set_badge = set_badge
        self._cli_detected = cli_detected
        # Gate commits until mounted: widget events fired while the initial
        # tree mounts (e.g. the RadioSet preselect) must not persist anything.
        self._ready = False

    def compose(self) -> ComposeResult:
        yield Static(
            "Credentials save to the global .env and apply immediately.",
            classes="muted",
        )
        for spec in PROVIDER_SPECS:
            yield from self._card(spec)
        yield Static("Default provider (new sessions)", id="prov-default-label")
        current = current_default_provider()
        with RadioSet(id="prov-default-set"):
            for spec in PROVIDER_SPECS:
                yield RadioButton(
                    spec.name,
                    value=(spec.name == current),
                    id=f"prov-default-{spec.name}",
                )

    def _card(self, spec: ProviderSpec) -> ComposeResult:
        name = spec.name
        with Vertical(id=f"prov-card-{name}", classes="prov-card"):
            with Horizontal(classes="prov-head"):
                yield Static("", id=f"prov-dot-{name}", classes="prov-dot")
                yield Static(name, classes="prov-name")
                yield Static("", id=f"prov-status-{name}", classes="prov-status")
                if spec.drop_keys:
                    yield Button("remove", id=f"prov-remove-{name}")
            if spec.base_url_key is not None:
                with Horizontal(classes="prov-field"):
                    yield Static("Base URL")
                    yield Input(
                        value=os.getenv(spec.base_url_key, ""),
                        placeholder=_DEFAULT_LOCAL_URL,
                        id=f"prov-url-{name}",
                    )
            if spec.write_key is not None:
                with Horizontal(classes="prov-field"):
                    yield Static("API key")
                    yield Input(password=True, id=f"prov-key-{name}")
            if name == "claude-cli":
                yield Static(
                    "(auth handled by the claude CLI itself)", classes="prov-note"
                )

    def on_mount(self) -> None:
        for spec in PROVIDER_SPECS:
            self._paint_card(spec)
        # call_after_refresh (not a bare assignment): the RadioSet's initial
        # Changed message may still be queued when on_mount runs; arming
        # commits only after the first refresh guarantees mount noise is over.
        self.call_after_refresh(self._arm)

    def _arm(self) -> None:
        self._ready = True

    # -- painting ----------------------------------------------------------

    def _configured(self, spec: ProviderSpec) -> bool:
        if spec.name == "claude-cli":
            return self._cli_detected
        return spec_configured(spec)

    def _paint_card(self, spec: ProviderSpec) -> None:
        name = spec.name
        configured = self._configured(spec)
        tv = self.app.theme_variables
        color = (
            tv.get("success", "#5fae7e")
            if configured
            else tv.get("text-muted", "#7c828d")
        )
        self.query_one(f"#prov-dot-{name}", Static).update(
            Content.assemble(("●" if configured else "○", color))
        )
        self.query_one(f"#prov-status-{name}", Static).update(
            self._status_text(spec, configured)
        )
        if spec.drop_keys:
            self.query_one(f"#prov-remove-{name}", Button).display = configured
        if spec.write_key is not None:
            self.query_one(f"#prov-key-{name}", Input).placeholder = key_hint(
                self._stored_key(spec)
            )

    def _status_text(self, spec: ProviderSpec, configured: bool) -> str:
        if spec.name == "claude-cli":
            base = "detected on PATH" if configured else "not found"
        else:
            base = "configured" if configured else "not configured"
        if spec.name == current_default_provider():
            base += " · default"
        return base

    def _stored_key(self, spec: ProviderSpec) -> str | None:
        """The stored credential the placeholder hints at: the canonical write
        key first, then fallbacks (google's GEMINI_API_KEY). Never read_keys —
        local's read key is its base URL, not a secret."""
        for key in (spec.write_key, *spec.key_fallbacks):
            value = os.getenv(key) if key else None
            if value:
                return value
        return None

    # -- persistence -------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._commit(event.input.id or "")

    def on_input_blurred(self, event: Input.Blurred) -> None:
        event.stop()
        self._commit(event.input.id or "")

    def _spec_for_input(self, widget_id: str) -> ProviderSpec | None:
        for prefix in ("prov-key-", "prov-url-"):
            if widget_id.startswith(prefix):
                return _SPECS.get(widget_id.removeprefix(prefix))
        return None

    def _commit(self, widget_id: str) -> None:
        if not self._ready:
            return
        spec = self._spec_for_input(widget_id)
        if spec is None:
            return
        env_key = (
            spec.base_url_key if widget_id.startswith("prov-url-") else spec.write_key
        )
        if env_key is None:
            return
        inp = self.query_one(f"#{widget_id}", Input)
        value = inp.value.strip()
        if not value:
            return  # empty commit is a no-op: focus/blur can't clobber a key
        if not self._save({env_key: value}):
            return
        if inp.password:
            inp.value = ""  # never leave the secret sitting in the widget
        self._status(f"✓ saved {env_key}")
        self._after_change(spec)

    def _save(self, values: dict[str, str], *, drop: tuple[str, ...] = ()) -> bool:
        try:
            save_env_settings(values, drop=drop)
        except Exception as exc:  # surface any write failure on the status line
            self._status(f"Save failed: {exc}")
            return False
        return True

    def _after_change(self, spec: ProviderSpec) -> None:
        self._refresh_sources()
        self._paint_card(spec)

    def _refresh_sources(self) -> None:
        if isinstance(self._model_source, MultiModelSource):
            self._model_source.refresh_from_env()

    def _provider_source(self, name: str) -> ModelSource | None:
        if isinstance(self._model_source, MultiModelSource):
            return self._model_source.sources.get(name)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: all PASS

- [ ] **Step 5: Lint/type-check the new module**

Run: `uv run ruff check src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py && uv run pyright`
Expected: clean (fix any C901/typing findings by extracting helpers, not suppressing)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py
git commit -m "feat(tui): ProvidersPane cards with live credential commit"
```

---

### Task 4: Implicit verification on save (and on mount)

**Files:**
- Modify: `src/marim_harness/interfaces/tui/providers.py`
- Test: `tests/test_providers_section.py` (append)

**Interfaces:**
- Consumes: Task 3's `_after_change`, `_provider_source`, `on_mount`; Task 2's `short_error`.
- Produces: `_start_verify(name: str)`, `async _verify(name: str)`; `_after_change(spec, *, verify: bool = False)` gains the keyword. Badge copy (exact): `verifying…`, `✓ connected · {n} models`, `✗ {short_error(exc)}` (each keeping the ` · default` suffix when applicable).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_providers_section.py`:

```python
def _multi_with_fake_openrouter(monkeypatch, *, entries=None, error=None):
    """A real MultiModelSource whose openrouter source has a stubbed
    list_models — real enough for isinstance checks, no network."""
    from unittest.mock import AsyncMock

    from marim_harness.config import model as _m
    from marim_harness.config.model import MultiModelSource

    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-verify-1234")
    multi = MultiModelSource.from_env()
    stub = AsyncMock(return_value=entries) if error is None else AsyncMock(
        side_effect=error
    )
    monkeypatch.setattr(multi.sources["openrouter"], "list_models", stub)
    return multi


@pytest.mark.anyio
async def test_mount_verifies_configured_provider(isolated_env, monkeypatch, tmp_path):
    """A configured provider is verified on mount: badge ends at
    '✓ connected · N models' (keeping the default marker)."""
    from marim_harness.workspace import ModelEntry

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    multi = _multi_with_fake_openrouter(
        monkeypatch,
        entries=[ModelEntry(id="a/x", name="X"), ModelEntry(id="a/y", name="Y")],
    )
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        badge = str(
            app.query_one(ProvidersPane)
            .query_one("#prov-status-openrouter", Static)
            .render()
        )
    assert "✓ connected · 2 models" in badge
    assert "default" in badge


@pytest.mark.anyio
async def test_failed_verification_shows_short_error(
    isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    multi = _multi_with_fake_openrouter(monkeypatch, error=RuntimeError("401 bad key"))
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        badge = str(
            app.query_one(ProvidersPane)
            .query_one("#prov-status-openrouter", Static)
            .render()
        )
    assert "✗ 401 bad key" in badge
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_providers_section.py -k verif -v`
Expected: FAIL — badge still reads `configured · default` (no verification happens yet)

- [ ] **Step 3: Implement verification**

In `providers.py`:

1. Change `on_mount` to kick a verify for each already-configured catalog provider:

```python
    def on_mount(self) -> None:
        for spec in PROVIDER_SPECS:
            self._paint_card(spec)
            # Verify already-configured providers up front so the cards open
            # showing live truth ('✓ connected · N models'), matching what a
            # save would show — skipped when there's no MultiModelSource
            # (embedding/tests) and for claude-cli (nothing to fetch).
            if spec.write_key is not None and self._configured(spec):
                self._start_verify(spec.name)
        # call_after_refresh (not a bare assignment): the RadioSet's initial
        # Changed message may still be queued when on_mount runs; arming
        # commits only after the first refresh guarantees mount noise is over.
        self.call_after_refresh(self._arm)
```

2. Change `_after_change` to accept and forward a `verify` flag, and update the `_commit` call site:

```python
    def _after_change(self, spec: ProviderSpec, *, verify: bool = False) -> None:
        self._refresh_sources()
        self._paint_card(spec)
        if verify:
            self._start_verify(spec.name)
```

In `_commit`, change `self._after_change(spec)` to `self._after_change(spec, verify=True)`.

3. Append the worker methods to the class:

```python
    # -- verification ------------------------------------------------------

    def _start_verify(self, name: str) -> None:
        """Fire-and-forget catalog fetch for one provider. exclusive per-group:
        a re-save while a fetch is in flight cancels the stale one so badges
        can't arrive out of order."""
        if self._provider_source(name) is None:
            return
        self.run_worker(self._verify(name), group=f"verify-{name}", exclusive=True)

    async def _verify(self, name: str) -> None:
        source = self._provider_source(name)
        if source is None:
            return
        badge = self.query_one(f"#prov-status-{name}", Static)
        default = " · default" if name == current_default_provider() else ""
        badge.update(f"verifying…{default}")
        try:
            models = await source.list_models()
        except Exception as exc:  # noqa: BLE001 - any fetch failure is a verdict
            badge.update(f"✗ {short_error(exc)}{default}")
            return
        badge.update(f"✓ connected · {len(models)} models{default}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: all PASS (earlier tasks' tests must still pass — commit-flow tests use `model_source=None`, so no verification interferes)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py
git commit -m "feat(tui): implicit provider verification via background catalog fetch"
```

---

### Task 5: Key removal

**Files:**
- Modify: `src/marim_harness/interfaces/tui/providers.py`
- Test: `tests/test_providers_section.py` (append)

**Interfaces:**
- Consumes: Task 3's `_save` (its `drop=` passthrough to `save_env_settings`), `_after_change`, `_paint_card`; the spec table's `drop_keys`.
- Produces: `on_button_pressed` handling `prov-remove-<name>`; `_remove(name: str)`. Footer copy (exact): `✓ removed {name} credentials`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_providers_section.py`:

```python
@pytest.mark.anyio
async def test_remove_button_hidden_until_configured(
    isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        assert pane.query_one("#prov-remove-openrouter", Button).display is False
        inp = pane.query_one("#prov-key-openrouter", Input)
        inp.value = "sk-or-test-1234abcd"
        pane._commit("prov-key-openrouter")
        await pilot.pause()
        assert pane.query_one("#prov-remove-openrouter", Button).display is True


@pytest.mark.anyio
async def test_remove_google_drops_both_env_names(isolated_env, monkeypatch, tmp_path):
    """Either env name keeps google configured, so removal must drop BOTH —
    from the .env file and os.environ in the same call."""
    from marim_harness.config import save_env_settings

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_env_settings(
        {"GOOGLE_API_KEY": "g-1", "GEMINI_API_KEY": "g-2"}
    )  # both stored, like a hand-edited .env
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        await pilot.pause()
        pane._remove("google")
        await pilot.pause()
        assert os.environ.get("GOOGLE_API_KEY") is None
        assert os.environ.get("GEMINI_API_KEY") is None
        env_text = (tmp_path / "marim" / ".env").read_text()
        assert "GOOGLE_API_KEY" not in env_text
        assert "GEMINI_API_KEY" not in env_text
        # Card flipped back to unconfigured.
        assert "not configured" in str(
            pane.query_one("#prov-status-google", Static).render()
        )
        assert pane.query_one("#prov-remove-google", Button).display is False
        assert pane.query_one("#prov-key-google", Input).placeholder == "not set"
        assert any("removed google" in s for s in app.statuses)


@pytest.mark.anyio
async def test_remove_local_drops_url_and_key_and_clears_input(
    isolated_env, monkeypatch, tmp_path
):
    from marim_harness.config import save_env_settings

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_env_settings(
        {"MARIM_BASE_URL": "http://localhost:1234/v1", "MARIM_API_KEY": "local"}
    )
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        assert pane.query_one("#prov-url-local", Input).value != ""
        pane._remove("local")
        await pilot.pause()
        assert os.environ.get("MARIM_BASE_URL") is None
        assert os.environ.get("MARIM_API_KEY") is None
        assert pane.query_one("#prov-url-local", Input).value == ""


@pytest.mark.anyio
async def test_remove_via_real_button_click(isolated_env, monkeypatch, tmp_path):
    """End-to-end through the real Textual event: clicking the remove button
    fires Button.Pressed -> on_button_pressed, not a direct _remove call."""
    from marim_harness.config import save_env_settings

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_env_settings({"OPENROUTER_API_KEY": "sk-or-test-1234abcd"})
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.click("#prov-remove-openrouter")
        await pilot.pause()
    assert os.environ.get("OPENROUTER_API_KEY") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_providers_section.py -k remove -v`
Expected: FAIL with `AttributeError: 'ProvidersPane' object has no attribute '_remove'` (and the click test fails because nothing handles the press)

- [ ] **Step 3: Implement removal**

Append to `ProvidersPane` in `providers.py`:

```python
    # -- removal -----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid.startswith("prov-remove-"):
            self._remove(bid.removeprefix("prov-remove-"))

    def _remove(self, name: str) -> None:
        """Drop a provider's stored credentials — .env line(s) and os.environ
        in one save_env_settings call. No confirmation modal: a deliberate
        button click in a personal tool, confirmed on the footer. The running
        session's model keeps working (the harness holds the built instance);
        the next switch or session routes to the default provider."""
        spec = _SPECS[name]
        if not spec.drop_keys or not self._save({}, drop=spec.drop_keys):
            return
        if spec.base_url_key is not None:
            self.query_one(f"#prov-url-{name}", Input).value = ""
        self._status(f"✓ removed {name} credentials")
        self._after_change(spec)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py
git commit -m "feat(tui): provider credential removal from the settings card"
```

---

### Task 6: Default-provider radio

**Files:**
- Modify: `src/marim_harness/interfaces/tui/providers.py`
- Test: `tests/test_providers_section.py` (append)

**Interfaces:**
- Consumes: Task 3's `_save`, `_refresh_sources`, `_paint_card`; the `set_badge` callback; `current_default_provider()`.
- Produces: `on_radio_set_changed` persisting `MARIM_PROVIDER`. Footer copy (exact): `✓ saved MARIM_PROVIDER · new sessions start on {name}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_providers_section.py`:

```python
@pytest.mark.anyio
async def test_default_radio_reflects_env(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import RadioButton

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        assert pane.query_one("#prov-default-google", RadioButton).value is True
    # Reflecting the preset must not have written anything.
    assert not (tmp_path / "marim" / ".env").exists()


@pytest.mark.anyio
async def test_default_radio_persists_and_updates_badge(
    isolated_env, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        await pilot.click("#prov-default-local")
        await pilot.pause()
        assert os.environ.get("MARIM_PROVIDER") == "local"
        assert "MARIM_PROVIDER=local" in (tmp_path / "marim" / ".env").read_text()
        assert app.badges and app.badges[-1] == "local"
        # The '· default' marker moved between the cards.
        assert "default" in str(
            pane.query_one("#prov-status-local", Static).render()
        )
        assert "default" not in str(
            pane.query_one("#prov-status-openrouter", Static).render()
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_providers_section.py -k default_radio -v`
Expected: `test_default_radio_reflects_env` PASSES already (compose preselects it); `test_default_radio_persists_and_updates_badge` FAILS (no handler — nothing persisted)

- [ ] **Step 3: Implement the handler**

Append to `ProvidersPane`:

```python
    # -- default provider --------------------------------------------------

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        event.stop()
        if not self._ready or event.pressed is None:
            return
        name = (event.pressed.id or "").removeprefix("prov-default-")
        if name not in _SPECS or not self._save({"MARIM_PROVIDER": name}):
            return
        self._refresh_sources()
        self._set_badge(name)
        for spec in PROVIDER_SPECS:
            self._paint_card(spec)  # move the '· default' marker between cards
        self._status(f"✓ saved MARIM_PROVIDER · new sessions start on {name}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: all PASS. If `test_pane_mounts_all_cards_without_writing_env` regresses (a mount-time RadioSet.Changed slipping past `_ready`), the `call_after_refresh(self._arm)` gate in `on_mount` is broken — fix the gating, do not weaken the test.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py
git commit -m "feat(tui): default-provider selection persisted from the Providers pane"
```

---

### Task 7: Mount the section in `SettingsScreen` + full gate

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py`
- Test: `tests/test_settings_screen.py` (extend `test_every_page_mounts_its_fields`)

**Interfaces:**
- Consumes: `ProvidersPane`, `current_default_provider` from `.providers`; `resolve_cli_binary` from `marim_harness.subagents.cli_backend`; `harness.model_source`.
- Produces: rail section key `providers` (row id `rail-providers`, badge id `badge-providers`, content id `section-providers`).

- [ ] **Step 1: Write the failing test**

In `tests/test_settings_screen.py`, extend `test_every_page_mounts_its_fields` — add after the Session assertions:

```python
        # Providers: the pane mounts as its own section with all four cards.
        assert s.query_one("#section-providers #prov-card-openrouter") is not None
        assert s.query_one("#section-providers #prov-default-set") is not None
```

And append a new test after `test_opens_on_session_section`:

```python
@pytest.mark.anyio
async def test_providers_rail_badge_shows_default(isolated_env, monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        badge = str(app.screen.query_one("#badge-providers").render())
    assert badge == "google"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_settings_screen.py -k "every_page or providers" -v`
Expected: FAIL with `NoMatches` on `#section-providers …` / `#badge-providers`

- [ ] **Step 3: Wire the section into the screen**

In `src/marim_harness/interfaces/tui/settings.py`:

1. Module docstring, first sentence — update the section list:

```
"""The full-bleed settings screen: topic pages on a left rail (Session,
Providers, Theme, MCP servers, Context & Memory, Tools, Notifications,
Advanced). Live settings (mode, model, theme, MCP, provider credentials)
apply immediately; env-backed settings auto-save per field.
```

(keep the rest of the docstring unchanged)

2. Imports — after `from .model_picker import ModelPickerModal` add:

```python
from ...subagents.cli_backend import resolve_cli_binary
from .providers import ProvidersPane, current_default_provider
```

3. `_SECTIONS` — insert the providers entry right after session:

```python
_SECTIONS = (
    ("session", "Session"),
    ("providers", "Providers"),
    ("theme", "Theme"),
    ("mcp", "MCP servers"),
    ("context", "Context & Memory"),
    ("tools", "Tools"),
    ("notifications", "Notifications"),
    ("advanced", "Advanced"),
)
```

4. `compose` — inside the `VerticalScroll(id="settings-content")` block, right after the `section-session` Vertical, add (the pane IS the section container — `_apply_section` toggles `display` on `#section-providers` like any other):

```python
                yield ProvidersPane(
                    model_source=self.harness.model_source,
                    status=self._status,
                    set_badge=self._set_providers_badge,
                    cli_detected=resolve_cli_binary() is not None,
                    id="section-providers",
                )
```

5. `_rail_badge` — add a branch after the `session` case:

```python
        if key == "providers":
            return current_default_provider()
```

6. Add the badge callback method next to `_status`:

```python
    def _set_providers_badge(self, provider: str) -> None:
        self.query_one("#badge-providers", Static).update(provider)
```

- [ ] **Step 4: Run the settings + providers suites**

Run: `uv run pytest --no-cov tests/test_settings_screen.py tests/test_providers_section.py -v`
Expected: all PASS — including the pre-existing `test_open_does_not_write_env`, which now also proves the pane writes nothing on mount inside the real screen. The fake harness's `model_source=None` exercises the pane's no-source path.

- [ ] **Step 5: Full gate, CI order**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: ruff clean, pyright clean, full suite green with coverage.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat(tui): Providers section on the settings screen"
```

---

### Task 8: Live smoke check in the real TUI

**Files:** none (verification only)

- [ ] **Step 1: Drive the settings screen in a real terminal**

Launch marim in tmux against the free local provider (never a paid model without explicit approval):

```bash
tmux new-session -d -s provcheck -x 140 -y 40 \
  'cd /home/mateuscmarim/Projects/marim.dev/marim-harness && \
   MARIM_PROVIDER=local MARIM_BASE_URL=http://localhost:1234/v1 uv run marim'
```

Then open settings, arrow down to Providers, and verify by capture (`tmux capture-pane -p -t provcheck`):
- all four cards render with dots/status; the default provider shows `· default`;
- typing a garbage OpenRouter key and pressing Enter flips its badge to `verifying…` then `✗ …`;
- the remove button appears and pressing it flips the card to `not configured`;
- the rail badge shows the default provider.

Clean up: `tmux kill-session -t provcheck`, and remove any garbage test key it wrote from `~/.config/marim/.env`.

- [ ] **Step 2: Report**

Report the observed behavior (with capture excerpts) before claiming the feature done.
