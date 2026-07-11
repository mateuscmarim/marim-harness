"""Tests for the shared project-trust security predicate."""

import pytest

from marim_harness.trust import project_trusted


def test_explicit_true_wins_over_env(monkeypatch):
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "false")
    assert project_trusted(True) is True


def test_explicit_false_wins_over_env(monkeypatch):
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "true")
    assert project_trusted(False) is False


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", "TRUE", "On", "YES"])
def test_env_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", value)
    assert project_trusted(None) is True


def test_env_unset_is_untrusted(monkeypatch):
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    assert project_trusted(None) is False


def test_env_unset_default_arg_is_untrusted(monkeypatch):
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    assert project_trusted() is False


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "junk", ""])
def test_env_junk_value_is_untrusted(monkeypatch, value):
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", value)
    assert project_trusted(None) is False
