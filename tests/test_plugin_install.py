import json
from pathlib import Path

import pytest

from marim_harness.plugins import (
    InstallError,
    install_plugin,
    is_git_source,
    load_state,
    remove_plugin,
    set_enabled,
    set_trusted,
)


def _make_source(src: Path, name: str, *, with_hooks: bool = False):
    (src / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (src / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    sk = src / "skills" / "demo"
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nx", encoding="utf-8")
    if with_hooks:
        (src / "hooks").mkdir(parents=True, exist_ok=True)
        (src / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": [{"type": "command", "command": "echo"}]}}),
            encoding="utf-8",
        )


def test_is_git_source():
    assert is_git_source("https://github.com/a/b.git")
    assert is_git_source("git@github.com:a/b.git")
    assert not is_git_source("/local/path")
    assert not is_git_source("./rel")


def test_install_local_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    rec = install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")
    assert rec.name == "demo"
    assert rec.version == "1.0.0"
    assert rec.trusted is True  # no executable parts -> auto-trusted
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    assert (gdir / "demo" / ".marim-plugin" / "plugin.json").is_file()
    assert "demo" in load_state(gdir)


def test_install_with_hooks_respects_trust_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "exec", with_hooks=True)
    rec = install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")
    assert rec.trusted is False  # executable, not trusted unless asked
    rec2 = install_plugin(str(src), scope="global", workspace_root=ws, trust=True, now="T")
    assert rec2.trusted is True


def test_install_rejects_bad_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(InstallError):
        install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")


def test_install_link_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    rec = install_plugin(
        str(src), scope="global", workspace_root=ws, trust=False, link=True, now="T"
    )
    assert rec.linked is True
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    assert (gdir / "demo").is_symlink()


def test_enable_disable_trust_and_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    install_plugin(str(src), scope="global", workspace_root=ws, trust=False, now="T")
    gdir = tmp_path / "cfg" / "marim" / "plugins"

    assert set_enabled("demo", scope="global", workspace_root=ws, enabled=False) is True
    assert load_state(gdir)["demo"].enabled is False
    assert set_trusted("demo", scope="global", workspace_root=ws, trusted=True) is True
    assert load_state(gdir)["demo"].trusted is True
    assert remove_plugin("demo", scope="global", workspace_root=ws) is True
    assert "demo" not in load_state(gdir)
    assert not (gdir / "demo").exists()


def test_install_from_local_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    # Build a real local git repo containing a plugin.
    repo = tmp_path / "repo"
    _make_source(repo, "gitdemo")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    url = repo.as_uri() + "/.git" if False else str(repo)  # local path clone
    rec = install_plugin(
        url, scope="global", workspace_root=ws, trust=False, now="T", _force_git=True
    )
    assert rec.name == "gitdemo"
    assert rec.source["type"] == "git"
    assert rec.source.get("sha")
