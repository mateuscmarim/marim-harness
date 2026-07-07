from pathlib import Path

from marim_harness.forge import select as sel
from marim_harness.forge.tea_backend import TeaBackend


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(sel, "tea_available", lambda: True)
    assert sel.select_backend(False, Path(".")) is None


def test_enabled_but_tea_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(sel, "tea_available", lambda: False)
    assert sel.select_backend(True, Path(".")) is None


def test_enabled_and_available_returns_tea_backend(monkeypatch):
    monkeypatch.setattr(sel, "tea_available", lambda: True)
    backend = sel.select_backend(True, Path("/repo"))
    assert isinstance(backend, TeaBackend)
