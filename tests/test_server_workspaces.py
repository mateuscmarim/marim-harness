import json
import subprocess

import pytest

from marim_harness.server.workspaces import WorkspaceRegistry


def _registry(tmp_path):
    return WorkspaceRegistry(tmp_path / "state" / "workspaces.json", tmp_path / "managed")


def test_register_existing_directory(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    record = reg.register("My Proj", project)
    assert record.kind == "registered"
    assert record.path == str(project.resolve())
    assert record.id == "my-proj"
    assert reg.get("my-proj") == record
    assert reg.list() == [record]


def test_register_missing_directory_raises(tmp_path):
    with pytest.raises(ValueError):
        _registry(tmp_path).register("nope", tmp_path / "does-not-exist")


def test_registry_persists_across_instances(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    reg.register("proj", project)
    again = _registry(tmp_path)
    assert [r.id for r in again.list()] == ["proj"]


def test_create_managed_empty(tmp_path):
    reg = _registry(tmp_path)
    record = reg.create_managed("fresh")
    assert record.kind == "managed"
    assert (tmp_path / "managed" / "fresh").is_dir()


def test_create_managed_git_clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
    (origin / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=origin, check=True,
    )
    reg = _registry(tmp_path)
    record = reg.create_managed("cloned", git_url=str(origin))
    assert (tmp_path / "managed" / "cloned" / "README.md").read_text() == "hello"
    assert record.kind == "managed"


def test_create_managed_bad_clone_cleans_up(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(ValueError):
        reg.create_managed("bad", git_url=str(tmp_path / "no-such-repo"))
    assert not (tmp_path / "managed" / "bad").exists()
    assert reg.get("bad") is None


def test_delete_and_purge(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    reg.register("proj", project)
    managed = reg.create_managed("m")

    with pytest.raises(ValueError):
        reg.delete("proj", purge=True)  # purge is managed-only
    reg.delete("proj")
    assert project.exists()  # registered dirs are never removed

    reg.delete(managed.id, purge=True)
    assert not (tmp_path / "managed" / "m").exists()
    with pytest.raises(KeyError):
        reg.delete("m")


def test_slug_collision_gets_suffix(tmp_path):
    reg = _registry(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    first = reg.register("Same Name", a)
    second = reg.register("Same Name", b)
    assert first.id == "same-name"
    assert second.id == "same-name-2"


def test_state_file_is_json(tmp_path):
    reg = _registry(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    reg.register("proj", project)
    data = json.loads((tmp_path / "state" / "workspaces.json").read_text())
    assert data["workspaces"][0]["id"] == "proj"
