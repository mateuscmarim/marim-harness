"""Tests for the settings screen's Providers section: the pure spec/helper
layer and the ProvidersPane widget (compose, commit, verify, remove, default)."""

import os

import pytest
from textual.app import App
from textual.widgets import Button, Input, Static

from marim_harness.interfaces.tui.providers import (
    PROVIDER_SPECS,
    ProvidersPane,
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
        "openrouter", "google", "zen", "zen-go", "local", "claude-cli"]
    assert specs["openrouter"].write_key == "OPENROUTER_API_KEY"
    assert specs["openrouter"].drop_keys == ("OPENROUTER_API_KEY",)
    # google always WRITES GOOGLE_API_KEY but reads/drops both env names.
    assert specs["google"].write_key == "GOOGLE_API_KEY"
    assert specs["google"].key_fallbacks == ("GEMINI_API_KEY",)
    assert set(specs["google"].read_keys) == {"GOOGLE_API_KEY", "GEMINI_API_KEY"}
    assert set(specs["google"].drop_keys) == {"GOOGLE_API_KEY", "GEMINI_API_KEY"}
    # zen: one canonical key env, fixed endpoint (no base URL row).
    assert specs["zen"].write_key == "OPENCODE_API_KEY"
    assert specs["zen"].key_fallbacks == ()
    assert specs["zen"].read_keys == ("OPENCODE_API_KEY",)
    assert specs["zen"].drop_keys == ("OPENCODE_API_KEY",)
    assert specs["zen"].base_url_key is None
    # zen-go: SAME key env as zen (one Zen account covers both plans), so
    # removing the key from either card deconfigures both.
    assert specs["zen-go"].write_key == "OPENCODE_API_KEY"
    assert specs["zen-go"].key_fallbacks == ()
    assert specs["zen-go"].read_keys == ("OPENCODE_API_KEY",)
    assert specs["zen-go"].drop_keys == ("OPENCODE_API_KEY",)
    assert specs["zen-go"].base_url_key is None
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
    """Mounting paints all six cards and must not write .env (mount-time
    widget events are gated, like the settings screen's _ready flag)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        for name in ("openrouter", "google", "zen", "zen-go", "local", "claude-cli"):
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


@pytest.mark.anyio
async def test_verify_against_real_dead_server_shows_x_badge(
    isolated_env, monkeypatch, tmp_path
):
    """No stubbed list_models here: a REAL ModelSource pointed at a dead local
    server (port 9 is "discard" — connection refused, instantly). This proves
    the production strict=True path — catalog.py's re-raise, ModelSource
    threading strict through, and _verify's except clause — actually renders
    a ✗ badge end to end, not just that a mock was told to raise."""
    from marim_harness.config import ModelConfig
    from marim_harness.config import model as _m
    from marim_harness.config.model import ModelSource, MultiModelSource

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "local")
    monkeypatch.setenv("MARIM_BASE_URL", "http://127.0.0.1:9/v1")
    multi = MultiModelSource(
        {
            "local": ModelSource(
                ModelConfig(provider="local", model="x", base_url="http://127.0.0.1:9/v1")
            )
        },
        default="local",
    )
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        badge = str(
            app.query_one(ProvidersPane)
            .query_one("#prov-status-local", Static)
            .render()
        )
    # The badge must lead with the ✗ verdict (mirrors _verify's f"✗ {...}").
    assert badge.lstrip().startswith("✗")


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


# -- follow-ups: verify-result cache, deferred verification, compact button --


@pytest.mark.anyio
async def test_verify_result_survives_repaint(isolated_env, monkeypatch, tmp_path):
    """Repainting a card (what the default-provider radio does to every card)
    must not regress a live '✓ connected' badge back to plain 'configured' —
    the last verify verdict is cached and preferred."""
    from marim_harness.interfaces.tui.providers import _SPECS
    from marim_harness.workspace import ModelEntry

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    multi = _multi_with_fake_openrouter(
        monkeypatch, entries=[ModelEntry(id="a/x", name="X")]
    )
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        badge = pane.query_one("#prov-status-openrouter", Static)
        assert "✓ connected · 1 models" in str(badge.render())
        pane._paint_card(_SPECS["openrouter"])
        assert "✓ connected · 1 models" in str(badge.render())


@pytest.mark.anyio
async def test_remove_clears_cached_verify_result(isolated_env, monkeypatch, tmp_path):
    """After a remove, the card must read 'not configured' — never a stale
    cached '✓ connected' from before the credential was dropped."""
    from marim_harness.workspace import ModelEntry

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    multi = _multi_with_fake_openrouter(
        monkeypatch, entries=[ModelEntry(id="a/x", name="X")]
    )
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        pane._remove("openrouter")
        await pilot.pause()
        badge = str(pane.query_one("#prov-status-openrouter", Static).render())
        assert "not configured" in badge
        assert "connected" not in badge


@pytest.mark.anyio
async def test_verification_deferred_until_pane_shown(
    isolated_env, monkeypatch, tmp_path
):
    """A pane mounted hidden (the settings screen opens on Session) must not
    fetch any catalog; the first time it becomes visible, it verifies."""
    from marim_harness.workspace import ModelEntry

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    multi = _multi_with_fake_openrouter(
        monkeypatch, entries=[ModelEntry(id="a/x", name="X")]
    )
    stub = multi.sources["openrouter"].list_models

    class _HiddenHost(_PaneHost):
        def compose(self):
            pane = ProvidersPane(
                model_source=self._model_source,
                status=self.statuses.append,
                set_badge=self.badges.append,
                cli_detected=self._cli_detected,
            )
            pane.display = False
            yield pane

    app = _HiddenHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert stub.await_count == 0  # hidden: no network
        pane = app.query_one(ProvidersPane)
        pane.display = True
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert stub.await_count == 1
        badge = str(pane.query_one("#prov-status-openrouter", Static).render())
        assert "✓ connected · 1 models" in badge
        # Hiding and re-showing must not re-fetch (verify-once).
        pane.display = False
        await pilot.pause()
        pane.display = True
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert stub.await_count == 1


@pytest.mark.anyio
async def test_remove_button_is_compact(isolated_env, monkeypatch, tmp_path):
    """compact=True is what keeps the label visible at the card's 1-row head:
    a default-style Button draws tall top/bottom borders that squeeze the
    label out entirely at height 1 (it rendered as a bare ▔-strip)."""
    from textual.widgets import Button

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-1234abcd")
    app = _PaneHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        pane = app.query_one(ProvidersPane)
        assert pane.query_one("#prov-remove-openrouter", Button).compact is True


@pytest.mark.anyio
async def test_remove_cancels_inflight_verify(isolated_env, monkeypatch, tmp_path):
    """Removing a provider while its verify is still in flight must not let
    the late verdict repaint '✓ connected' onto the unconfigured card or
    re-populate the verdict cache the removal just dropped."""
    import asyncio

    from marim_harness.workspace import ModelEntry

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    multi = _multi_with_fake_openrouter(monkeypatch, entries=[])
    gate = asyncio.Event()

    async def slow_list_models(*args, **kwargs):
        await gate.wait()
        return [ModelEntry(id="a/x", name="X")]

    monkeypatch.setattr(multi.sources["openrouter"], "list_models", slow_list_models)
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()  # on_show sweep started the verify; it is gated
        pane = app.query_one(ProvidersPane)
        pane._remove("openrouter")
        await pilot.pause()
        gate.set()  # a surviving worker would now land its stale verdict
        await app.workers.wait_for_complete()
        await pilot.pause()
        badge = str(pane.query_one("#prov-status-openrouter", Static).render())
        assert "connected" not in badge
        assert "not configured" in badge
        assert "openrouter" not in pane._verify_results


@pytest.mark.anyio
async def test_repaint_during_reverify_keeps_verifying_badge(
    isolated_env, monkeypatch, tmp_path
):
    """A repaint while a re-verify is in flight must show 'verifying…', not
    resurrect the previous key's cached verdict as if it were current."""
    import asyncio

    from marim_harness.interfaces.tui.providers import _SPECS
    from marim_harness.workspace import ModelEntry

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    multi = _multi_with_fake_openrouter(
        monkeypatch, entries=[ModelEntry(id="a/x", name="X")]
    )
    app = _PaneHost(model_source=multi)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()  # first verdict landed and is cached
        pane = app.query_one(ProvidersPane)
        gate = asyncio.Event()

        async def slow_list_models(*args, **kwargs):
            await gate.wait()
            return []

        monkeypatch.setattr(
            multi.sources["openrouter"], "list_models", slow_list_models
        )
        pane._start_verify("openrouter")  # what a re-save triggers
        await pilot.pause()
        pane._paint_card(_SPECS["openrouter"])  # e.g. the default radio moved
        badge = str(pane.query_one("#prov-status-openrouter", Static).render())
        assert "verifying" in badge
        assert "connected" not in badge
        gate.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        badge = str(pane.query_one("#prov-status-openrouter", Static).render())
        assert "✓ connected · 0 models" in badge
