from textual.theme import Theme

from marim_harness.interfaces.tui.themes import (
    DEFAULT_THEME,
    MARIM_THEMES,
    THEME_NAMES,
)


def test_four_themes_defined():
    assert len(MARIM_THEMES) == 4
    assert all(isinstance(t, Theme) for t in MARIM_THEMES)


def test_names_match_themes():
    assert THEME_NAMES == tuple(t.name for t in MARIM_THEMES)
    assert set(THEME_NAMES) == {
        "marim-teal",
        "marim-amber",
        "marim-violet",
        "marim-green",
    }


def test_default_is_teal_and_registered():
    assert DEFAULT_THEME == "marim-teal"
    assert DEFAULT_THEME in THEME_NAMES


def test_all_themes_are_dark_and_share_base():
    backgrounds = {t.background for t in MARIM_THEMES}
    surfaces = {t.surface for t in MARIM_THEMES}
    assert all(t.dark for t in MARIM_THEMES)
    assert len(backgrounds) == 1
    assert len(surfaces) == 1


def test_accents_are_distinct():
    primaries = {t.primary for t in MARIM_THEMES}
    assert len(primaries) == 4


def test_text_muted_present_on_all_themes():
    for t in MARIM_THEMES:
        assert "text-muted" in (t.variables or {}), f"{t.name} missing text-muted"
