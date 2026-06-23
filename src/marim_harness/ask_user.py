"""Structured user prompts: the data model and serialization behind the
``ask_user`` tool. A prompt is a list of :class:`Question`s, each offering
:class:`Choice`s plus an always-available free-text field; the user's answers
come back as a ``{header: answer}`` mapping that this module renders to JSON for
the agent. Pure data + normalization — no I/O, no UI. The TUI modal and the tool
both import these types.
"""

import json
from dataclasses import dataclass, field

# Most questions the modal will present in one prompt — a bad call can't open an
# unbounded modal, and it mirrors AskUserQuestion's 1–4 range.
_MAX_QUESTIONS = 4
# How much of the question text to use as a fallback header (the result's key)
# when the model omits one.
_HEADER_FALLBACK_CHARS = 40


@dataclass
class Choice:
    """One selectable option. ``description`` is optional secondary text shown
    dim beneath the label in the picker."""

    label: str
    description: str | None = None


@dataclass
class Question:
    """One question in a prompt. ``header`` is the short key the answer is
    returned under (falls back to the question text if blank); ``multi`` makes it
    multi-select."""

    question: str
    header: str = ""
    options: list[Choice] = field(default_factory=list)
    multi: bool = False


def _clean_choice(choice: Choice) -> Choice | None:
    """Drop a choice with a blank label; trim a blank description to None."""
    label = (choice.label or "").strip()
    if not label:
        return None
    desc = (choice.description or "").strip() or None
    return Choice(label=label, description=desc)


def _fallback_header(question: str) -> str:
    """A stable key from the question text when no header was given."""
    return " ".join(question.split())[:_HEADER_FALLBACK_CHARS]


def _clean_question(q: Question) -> Question | None:
    """Normalize one question, or None to drop it. Drops blank-label choices and
    then the whole question if it has no text or no surviving options; fills a
    blank header from the question text."""
    question = (q.question or "").strip()
    if not question:
        return None
    options = [c for c in (_clean_choice(o) for o in q.options) if c is not None]
    if not options:
        return None
    header = (q.header or "").strip() or _fallback_header(question)
    return Question(question=question, header=header, options=options, multi=bool(q.multi))


def coerce_questions(questions: list[Question]) -> list[Question]:
    """Normalize a validated question list defensively: drop malformed entries,
    fill blank headers, and cap the count. pydantic-ai has already validated the
    shapes; this guards the values."""
    cleaned = [q for q in (_clean_question(q) for q in questions) if q is not None]
    return cleaned[:_MAX_QUESTIONS]


def answers_to_json(answers: dict) -> str:
    """Render the ``{header: answer}`` mapping the modal returns to a compact
    JSON object string for the agent — single-select answers are strings,
    multi-select answers are lists."""
    return json.dumps(answers, ensure_ascii=False)
