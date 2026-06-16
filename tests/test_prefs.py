import pytest

from marim_harness import prefs


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point config_dir() at a temp dir so tests never touch the real prefs."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_load_theme_defaults_when_missing(cfg_home):
    assert prefs.load_theme() == "marim-teal"


def test_save_then_load_round_trips(cfg_home):
    assert prefs.save_theme("marim-amber") is True
    assert prefs.load_theme() == "marim-amber"


def test_load_theme_rejects_unknown_name(cfg_home):
    prefs.prefs_path().parent.mkdir(parents=True, exist_ok=True)
    prefs.prefs_path().write_text('{"theme": "bogus"}', encoding="utf-8")
    assert prefs.load_theme() == "marim-teal"


def test_load_theme_survives_malformed_file(cfg_home):
    prefs.prefs_path().parent.mkdir(parents=True, exist_ok=True)
    prefs.prefs_path().write_text("not json {", encoding="utf-8")
    assert prefs.load_theme() == "marim-teal"


def test_save_rejects_unknown_name(cfg_home):
    assert prefs.save_theme("bogus") is False
    assert prefs.load_theme() == "marim-teal"
