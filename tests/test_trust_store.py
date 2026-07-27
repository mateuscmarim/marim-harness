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
