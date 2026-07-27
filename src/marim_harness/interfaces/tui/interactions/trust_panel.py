"""First-open project trust prompt. Inline panel (never a modal): lists the
project's gated surface so the decision is informed, resolves True/False.
Both answers persist (the caller records them); escape counts as decline —
an unanswered prompt must not linger while turns run untrusted underneath."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from ....trust_surface import ProjectSurface
from .base import InteractionPanel


class TrustPanel(InteractionPanel):
    """Asks whether to trust the project's gated surface (hooks/MCP/skills/
    agents/plugins). Resolves True (trust) / False (don't trust) — never a
    third "undecided" state, so a dismissed dialog still lets the session
    proceed (untrusted) instead of stalling the turn underneath it."""

    DEFAULT_CSS = """
    TrustPanel { border: round $warning; }
    #trust-title { text-style: bold; color: $warning; margin-bottom: 1; }
    #trust-summary { color: $text-muted; margin-bottom: 1; }
    #trust-note { color: $text-muted; margin-bottom: 1; }
    """

    # The panel itself takes focus on mount so t/d/Esc are live immediately
    # (mirrors ApprovalPanel — the modal it replaced got this from the
    # screen's focus scope).
    can_focus = True

    BINDINGS = InteractionPanel.BINDINGS + [
        Binding("t", "trust", "Trust"),
        Binding("d", "decline", "Don't trust"),
        Binding("escape", "decline", "Don't trust", show=False),
    ]

    def __init__(self, surface: ProjectSurface) -> None:
        super().__init__(id="trust-panel")
        self._surface = surface

    def compose(self) -> ComposeResult:
        yield Static(
            "[b]This project ships configuration that loads on startup[/b]",
            id="trust-title",
        )
        # The surface summary is built from project-local names (hook events,
        # MCP server ids, skill/agent dirs) — an untrusted project's own
        # content, so it must render as literal text, never parsed as markup
        # (a bracket in a directory name must not raise MarkupError or style
        # the panel), matching how plan_card.py treats model-authored text.
        yield Static(self._surface.summary(), id="trust-summary", markup=False)
        yield Static(
            "Trust it? Hooks and MCP servers run code with no per-call approval; "
            "skills and agents inject prompt content. docs/guides/trust.md",
            id="trust-note",
        )
        yield Static("[b]\\[t][/b] Trust   [b]\\[d][/b] Don't trust")

    def on_mount(self) -> None:
        self.focus()

    def action_trust(self) -> None:
        self.resolve(True)

    def action_decline(self) -> None:
        self.resolve(False)
