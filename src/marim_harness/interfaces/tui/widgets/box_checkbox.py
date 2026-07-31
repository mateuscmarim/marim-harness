"""A terminal-native checkbox. Lives in ``widgets/`` rather than beside the
settings screen because it is a plain reusable widget with no settings
knowledge — the settings screen just happens to be its only caller today."""

from __future__ import annotations

from textual.content import Content
from textual.widgets import Checkbox


class BoxCheckbox(Checkbox):
    """A terminal-native ``[x]`` / ``[ ]`` checkbox. Textual's Checkbox draws a
    ``▐X▌`` block and signals on/off by colour alone; we render literal brackets with
    a blank inner glyph when off so it reads like a TUI checkbox, not an iOS slider.
    Brackets take the muted colour and the check takes the success colour."""

    @property
    def _button(self) -> Content:
        tv = self.app.theme_variables
        bracket = tv.get("text-muted", "#7c828d")
        inner = "x" if self.value else " "
        icol = tv.get("success", "#5fae7e") if self.value else bracket
        return Content.assemble(("[", bracket), (inner, icol), ("]", bracket))
