import json
import subprocess
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
    update_plugin,
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


def test_linked_plugin_without_executables_is_not_auto_trusted(tmp_path, monkeypatch):
    """A linked plugin points at a live, mutable source dir whose executable
    surface (hooks/MCP) can be added after install and would then run trusted
    with no prompt. So linked installs must never be auto-trusted on the basis of
    having no executable parts at install time."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "linkbenign")  # no hooks/MCP
    rec = install_plugin(
        str(src), scope="global", workspace_root=ws, trust=False, link=True, now="T"
    )
    assert rec.linked is True
    assert rec.trusted is False  # linked => never auto-trusted


def test_linked_plugin_trusted_only_when_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "linkbenign2")
    rec = install_plugin(
        str(src), scope="global", workspace_root=ws, trust=True, link=True, now="T"
    )
    assert rec.trusted is True


def test_clone_git_handles_commit_sha_ref(tmp_path):
    """A commit SHA is not a valid `git clone --branch` argument; _clone_git must
    fall back so a SHA-pinned source clones instead of erroring."""
    from marim_harness.plugins.install import _clone_git

    repo = _make_git_repo(tmp_path / "repo", "shademo")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    dest = tmp_path / "clone"
    record = _clone_git(str(repo), dest, ref=sha)
    assert record["sha"] == sha
    assert record["ref"] == sha
    assert (dest / ".marim-plugin" / "plugin.json").is_file()


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


# ---------------------------------------------------------------------------
# Helper — build a minimal local git repo with a plugin and return its path.
# ---------------------------------------------------------------------------


def _make_git_repo(root: Path, name: str, version: str = "1.0.0") -> Path:
    """Create a git-tracked plugin directory at *root* and return *root*."""
    _make_source(root, name)
    # Write the desired version into plugin.json (overwrite what _make_source wrote).
    manifest_path = root / ".marim-plugin" / "plugin.json"
    manifest_path.write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def test_update_plugin_happy_path_git(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = _make_git_repo(tmp_path / "repo", "myplugin", version="1.0.0")

    install_plugin(
        str(repo),
        scope="global",
        workspace_root=ws,
        trust=False,
        now="T1",
        _force_git=True,
    )

    # Bump version in the repo and commit.
    manifest_path = repo / ".marim-plugin" / "plugin.json"
    manifest_path.write_text(
        json.dumps({"name": "myplugin", "version": "2.0.0"}), encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bump"],
        cwd=repo,
        check=True,
    )

    rec2 = update_plugin("myplugin", scope="global", workspace_root=ws, now="T2")
    assert rec2.version == "2.0.0"
    assert rec2.source["type"] == "git"
    assert rec2.source.get("sha")
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    installed_manifest = (
        gdir / "myplugin" / ".marim-plugin" / "plugin.json"
    )
    assert json.loads(installed_manifest.read_text())["version"] == "2.0.0"


def test_update_plugin_rejects_local_source(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "localplugin")
    install_plugin(
        str(src), scope="global", workspace_root=ws, trust=False, now="T"
    )
    with pytest.raises(InstallError):
        update_plugin("localplugin", scope="global", workspace_root=ws, now="T")


def test_update_plugin_rejects_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(InstallError):
        update_plugin("nope", scope="global", workspace_root=ws, now="T")


def test_remove_symlinked_plugin_keeps_source(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "linked")
    install_plugin(
        str(src),
        scope="global",
        workspace_root=ws,
        trust=False,
        link=True,
        now="T",
    )
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    dest = gdir / "linked"
    assert dest.is_symlink(), "expected a symlink after linked install"

    remove_plugin("linked", scope="global", workspace_root=ws)

    assert not dest.exists() and not dest.is_symlink(), "symlink should be gone"
    assert (src / ".marim-plugin" / "plugin.json").is_file(), "source must be intact"
    assert "linked" not in load_state(gdir), "state entry should be removed"


def test_git_with_link_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = _make_git_repo(tmp_path / "repo", "gitlink")
    with pytest.raises(InstallError):
        install_plugin(
            str(repo),
            scope="global",
            workspace_root=ws,
            trust=False,
            link=True,
            now="T",
            _force_git=True,
        )


def test_unknown_scope_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    with pytest.raises(InstallError):
        install_plugin(
            str(src), scope="bogus", workspace_root=ws, trust=False, now="T"
        )
