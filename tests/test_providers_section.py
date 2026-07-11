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
