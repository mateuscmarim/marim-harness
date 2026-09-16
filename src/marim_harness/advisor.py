"""Marim selection sentinel and guidance for the upstream Advisor capability."""

ADVISOR_OFF = "off"

ADVISOR_GUIDANCE = (
    "Use advisor(prompt=...) for difficult decisions, when stuck, or before completing "
    "complex work. Include a self-contained question and the current evidence in prompt: "
    "the advisor sees completed conversation history, but not the current response or live "
    "workspace. Weigh its advice against evidence from files and commands. "
    "Skip consultation for trivial questions and mechanical edits."
)


def selection_notice(model_id: str | None, executor_id: str | None = None) -> str:
    """Describe persisted selection without implying a mid-turn switch."""
    notice = f"Advisor: {model_id or 'off'} — applies to the next turn."
    if executor_id and executor_id.split(":", 1)[0] in {"claude-cli", "codex-cli"}:
        notice += " Runtime advisor is unavailable with a CLI executor."
    return notice
