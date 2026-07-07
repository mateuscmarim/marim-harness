from pathlib import Path

import pytest

from marim_harness.forge import gitref


def _patch_git(monkeypatch, out: str | None):
    async def fake_git(args, root):
        return out
    monkeypatch.setattr(gitref, "_git", fake_git)


@pytest.mark.anyio
async def test_current_branch_returns_name(monkeypatch):
    _patch_git(monkeypatch, "feature/x\n")
    assert await gitref.current_branch(Path(".")) == "feature/x"


@pytest.mark.anyio
async def test_current_branch_none_on_detached(monkeypatch):
    _patch_git(monkeypatch, "HEAD\n")
    assert await gitref.current_branch(Path(".")) is None


@pytest.mark.anyio
async def test_current_branch_none_on_failure(monkeypatch):
    _patch_git(monkeypatch, None)
    assert await gitref.current_branch(Path(".")) is None


@pytest.mark.anyio
async def test_branch_pushed_true_when_ref_present(monkeypatch):
    _patch_git(monkeypatch, "abc123 refs/remotes/origin/feature/x\n")
    assert await gitref.branch_pushed(Path("."), "feature/x") is True


@pytest.mark.anyio
async def test_branch_pushed_false_when_absent(monkeypatch):
    _patch_git(monkeypatch, None)
    assert await gitref.branch_pushed(Path("."), "feature/x") is False
