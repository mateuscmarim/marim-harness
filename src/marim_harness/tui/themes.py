"""The harness's custom Textual themes.

Four dark themes sharing one neutral base (background / surface / panel /
foreground), differing only in the accent (``primary``). A shared base keeps the
app feeling like one product with a swappable accent rather than four apps.
``$text-muted`` is provided through each theme's ``variables`` so the stylesheet
can lean on it uniformly.
"""

from textual.theme import Theme

# Shared neutral dark base — identical across every theme.
_BACKGROUND = "#16181d"
_SURFACE = "#1c1f26"
_PANEL = "#232730"
_FOREGROUND = "#d7dae0"
_TEXT_MUTED = "#7c828d"

_BASE = {
    "background": _BACKGROUND,
    "surface": _SURFACE,
    "panel": _PANEL,
    "foreground": _FOREGROUND,
    "dark": True,
    "variables": {"text-muted": _TEXT_MUTED},
}


def _theme(name: str, accent: str) -> Theme:
    """A marim theme: the shared neutral base plus one accent hue."""
    return Theme(
        name=name,
        primary=accent,
        accent=accent,
        error="#d9544f",
        warning="#d9a14f",
        success="#5fae7e",
        **_BASE,
    )


MARIM_THEMES = (
    _theme("marim-teal", "#4cb6a8"),
    _theme("marim-amber", "#d6a45c"),
    _theme("marim-violet", "#9a86d4"),
    _theme("marim-green", "#7fae6b"),
)

THEME_NAMES = tuple(t.name for t in MARIM_THEMES)
DEFAULT_THEME = "marim-teal"
