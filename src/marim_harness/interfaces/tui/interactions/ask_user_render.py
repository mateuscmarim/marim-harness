"""Pure formatter for rendering an ``ask_user`` tool call in the chat transcript.

Pairs each question with the answer the user chose and produces the collapsed
title tail and the expanded body as plain strings, across the three states the
interaction passes through (pending / answered / cancelled). No Textual/Rich/app
imports beyond ``_clip``/``humanize_tool`` — glyph + Content styling live in the
widget, so this stays unit-testable without an App. Pairs by POSITION (the answer
dict is built in question order; headers may be blank/fallback) and detects
answered-vs-note by JSON-parse (never raises on malformed input)."""

import json
from dataclasses import dataclass

from ..widgets.tool_summary import _clip

_AWAITING = "awaiting answer…"
_CANCELLED_TAIL = "cancelled — no answer"
# Clip widths: the pending/single-answered lines carry a full question, so they get
# a roomier cap than the 100-char generic tool target.
_PENDING_Q_CAP = 60
_ANSWERED_LINE_CAP = 90


@dataclass
class AskUserQA:
    """One question paired with its answer(s). ``answers`` is empty while pending
    or cancelled; ``typed[i]`` is True when ``answers[i]`` was free-text the user
    typed rather than an offered option label. ``state`` mirrors the overall state."""

    question: str
    answers: list[str]
    typed: list[bool]
    state: str


def _answers_obj(result_text: str) -> dict | None:
    """The ``{header: answer}`` dict the tool returns on success, or None when the
    result is a non-JSON note (cancelled / no-UI / empty) or otherwise unparseable."""
    try:
        obj = json.loads(result_text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def overall_state(result_text: str, status: str) -> str:
    """``pending`` while the call is in flight; ``answered`` when the result parses
    to a JSON object; ``cancelled`` for any other (note-string) result."""
    if status == "pending":
        return "pending"
    return "answered" if _answers_obj(result_text) is not None else "cancelled"


def _question_dicts(args: dict) -> list[dict]:
    qs = args.get("questions")
    return [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else []


def _option_labels(q: dict) -> set[str]:
    opts = q.get("options")
    out: set[str] = set()
    if isinstance(opts, list):
        for o in opts:
            if isinstance(o, dict) and isinstance(o.get("label"), str):
                out.add(o["label"])
    return out


def _normalize_answer(ans) -> list[str]:
    """A single-select answer is a string; multi-select is a list. Normalize to a
    list of strings."""
    if isinstance(ans, list):
        return [str(a) for a in ans]
    return [str(ans)]


def parse_ask_user(args: dict, result_text: str, status: str) -> list[AskUserQA]:
    """Pair each question (in order) with its answer. Answers come from the result
    JSON's values in insertion (question) order, so position pairing is robust to
    blank/duplicate headers. Degrades on malformed input — never raises."""
    state = overall_state(result_text, status)
    questions = _question_dicts(args)
    answers_obj = _answers_obj(result_text) if state == "answered" else None
    values: list = list(answers_obj.values()) if answers_obj is not None else []
    out: list[AskUserQA] = []
    for i, q in enumerate(questions):
        qtext = str(q.get("question", "")).strip()
        if state == "answered" and i < len(values):
            answers = _normalize_answer(values[i])
            labels = _option_labels(q)
            typed = [a not in labels for a in answers]
        else:
            answers, typed = [], []
        out.append(AskUserQA(question=qtext, answers=answers, typed=typed, state=state))
    return out


def _fmt_answers(qa: AskUserQA) -> str:
    """The chosen answer(s) on one line — typed free-text wrapped in quotes so it
    reads as the user's own words, options bare; joined with ', '."""
    parts = [f'"{a}"' if t else a for a, t in zip(qa.answers, qa.typed, strict=False)]
    return ", ".join(parts) or "(no answer)"


def ask_user_title_tail(qas: list[AskUserQA], state: str) -> str:
    """The collapsed-title text after ``{glyph} {label} · `` (the widget adds the
    glyph + 'Ask User'). One question shows ``Q → A``; many show a count; pending
    shows the question + 'awaiting answer…'; cancelled a fixed note."""
    if state == "pending":
        first = qas[0].question if qas else "question"
        return f"{_clip(first, _PENDING_Q_CAP)}  {_AWAITING}"
    if state == "cancelled":
        return _CANCELLED_TAIL
    if len(qas) == 1:
        return _clip(f"{qas[0].question} → {_fmt_answers(qas[0])}", _ANSWERED_LINE_CAP)
    return f"{len(qas)} questions answered"


def ask_user_body(qas: list[AskUserQA]) -> str:
    """The expanded body: one ``question`` / ``→ answer`` block per question,
    separated by a blank line. Pending/cancelled show a placeholder answer."""
    blocks = []
    for qa in qas:
        if qa.state == "answered":
            ans = _fmt_answers(qa)
        elif qa.state == "pending":
            ans = "(awaiting answer)"
        else:
            ans = "(cancelled)"
        blocks.append(f"{qa.question}\n→ {ans}")
    return "\n\n".join(blocks)
