from marim_harness.plugins.state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    save_state,
    state_path,
)


def test_roundtrip(tmp_path):
    pdir = tmp_path / "plugins"
    rec = InstalledPlugin(
        name="p",
        version="1.0.0",
        source={"type": "local", "path": "/src/p"},
        enabled=True,
        trusted=False,
        linked=False,
        installed_at="2026-06-22T00:00:00Z",
    )
    save_state(pdir, {"p": rec})
    loaded = load_state(pdir)
    assert loaded["p"] == rec


def test_missing_state_is_empty(tmp_path):
    assert load_state(tmp_path / "nope") == {}


def test_malformed_state_is_empty(tmp_path):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    state_path(pdir).write_text("{ not json", encoding="utf-8")
    assert load_state(pdir) == {}


def test_from_dict_defaults():
    rec = InstalledPlugin.from_dict("x", {"source": {"type": "local"}})
    assert rec.name == "x"
    assert rec.enabled is True
    assert rec.trusted is False
    assert rec.version is None


def test_scope_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert global_plugins_dir() == tmp_path / "cfg" / "marim" / "plugins"
    assert project_plugins_dir(tmp_path) == tmp_path / ".marim" / "plugins"
