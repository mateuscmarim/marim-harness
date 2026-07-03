"""Adversarial tests for the two plugin supply-chain vectors:

1. Git argument injection — a hostile *project* registry
   (``.marim/plugins/plugins.json`` travels with the repo) records a git ``url``
   or ``ref`` that looks like a git option, so ``marim plugin update`` on a clone
   would make git execute an attacker's command.
2. Path traversal — a registry/CLI plugin *name* is used verbatim as a path
   component, so a traversal name turns a remove into an out-of-tree rmtree and
   lets discovery read manifests out of tree.
"""

import json
import subprocess
from pathlib import Path

import pytest

from marim_harness.plugins import (
    InstallError,
    install_plugin,
    load_state,
    remove_plugin,
    save_state,
    update_plugin,
)
from marim_harness.plugins.install import _clone_git
from marim_harness.plugins.state import InstalledPlugin


def _make_source(src: Path, name: str) -> None:
    (src / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (src / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    sk = src / "skills" / "demo"
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nx", encoding="utf-8")


def _make_git_repo(root: Path, name: str) -> Path:
    _make_source(root, name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


# --- Finding 1: git argument injection --------------------------------------


def test_clone_git_rejects_option_like_source(tmp_path):
    """A ``--upload-pack=<cmd>`` url must be refused before it reaches git, or git
    would run the attacker's command instead of cloning."""
    canary = tmp_path / "pwned"
    hostile_url = f"--upload-pack=touch {canary}"
    with pytest.raises(InstallError, match="git source"):
        _clone_git(hostile_url, tmp_path / "clone", ref=None)
    assert not canary.exists(), "attacker command must never have executed"


def test_clone_git_rejects_option_like_ref(tmp_path):
    repo = _make_git_repo(tmp_path / "repo", "demo")
    with pytest.raises(InstallError, match="git ref"):
        _clone_git(str(repo), tmp_path / "clone", ref="--upload-pack=touch x")


def test_update_plugin_rejects_hostile_registry_url(tmp_path, monkeypatch):
    """The realistic path: a committed project registry pins a hostile git url,
    and ``update_plugin`` (reached by ``marim plugin update``) reads it back."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = _make_git_repo(tmp_path / "repo", "victim")
    install_plugin(
        str(repo), scope="global", workspace_root=ws, trust=False, now="T", _force_git=True
    )
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    # Attacker rewrites the recorded url to a git option.
    canary = tmp_path / "pwned"
    state = load_state(gdir)
    state["victim"].source = {"type": "git", "url": f"--upload-pack=touch {canary}"}
    save_state(gdir, state)

    with pytest.raises(InstallError, match="git source"):
        update_plugin("victim", scope="global", workspace_root=ws, now="T2")
    assert not canary.exists()


# --- Finding 2: path traversal via plugin name ------------------------------


def test_remove_plugin_refuses_traversal_name(tmp_path, monkeypatch):
    """A hostile registry entry named ``../../<outside>`` must not let a
    user-invoked remove rmtree an out-of-tree directory."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"

    # A real directory that must survive, placed outside the scope dir.
    victim = tmp_path / "outside"
    victim.mkdir()
    (victim / "keepme.txt").write_text("precious", encoding="utf-8")

    traversal = "../../../outside"
    # Write the malicious entry straight into the registry JSON (bypassing the
    # save-side path, mirroring a committed hostile plugins.json).
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "plugins.json").write_text(
        json.dumps(
            {"plugins": {traversal: InstalledPlugin(traversal, "1.0.0", {}).to_dict()}}
        ),
        encoding="utf-8",
    )

    assert remove_plugin(traversal, scope="global", workspace_root=ws) is False
    assert victim.exists() and (victim / "keepme.txt").exists(), "out-of-tree dir untouched"


def test_load_state_drops_traversal_names(tmp_path):
    """The load boundary itself sanitizes the registry: a traversal name never
    surfaces as an entry, so no downstream path join can use it."""
    gdir = tmp_path / "plugins"
    gdir.mkdir()
    (gdir / "plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "../../etc": InstalledPlugin("../../etc", "1.0.0", {}).to_dict(),
                    "good-plugin": InstalledPlugin("good-plugin", "1.0.0", {}).to_dict(),
                }
            }
        ),
        encoding="utf-8",
    )
    state = load_state(gdir)
    assert "../../etc" not in state
    assert "good-plugin" in state


def test_install_rejects_traversal_name_override(tmp_path, monkeypatch):
    """The CLI ``--name`` override bypasses the manifest name check, so a
    traversal override must be refused before it becomes a path component."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    src = tmp_path / "src"
    _make_source(src, "demo")
    with pytest.raises(InstallError, match="invalid plugin name"):
        install_plugin(
            str(src),
            scope="global",
            workspace_root=ws,
            trust=False,
            now="T",
            name_override="../../../evil",
        )


def test_update_refuses_traversal_name(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(InstallError, match="invalid plugin name"):
        update_plugin("../../etc", scope="global", workspace_root=ws, now="T")
