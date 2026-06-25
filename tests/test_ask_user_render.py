import json

from marim_harness.interfaces.tui.widgets.ask_user_render import (
    ask_user_body,
    ask_user_title_tail,
    overall_state,
    parse_ask_user,
)


def _args(*questions):
    return {"questions": list(questions)}


def _q(question, options, header="", multi=False):
    return {
        "question": question,
        "header": header,
        "options": [{"label": o} for o in options],
        "multi": multi,
    }


def test_overall_state_pending_answered_cancelled():
    assert overall_state("", "pending") == "pending"
    assert overall_state(json.dumps({"h": "A"}), "done") == "answered"
    assert overall_state("User dismissed the prompt without answering.", "done") == "cancelled"
    assert overall_state("not json", "done") == "cancelled"


def test_single_select_answered_title_and_body():
    args = _args(_q("Which approach?", ["Option A", "Option B"], header="approach"))
    result = json.dumps({"approach": "Option B"})
    qas = parse_ask_user(args, result, "done")
    assert ask_user_title_tail(qas, "answered") == "Which approach? → Option B"
    assert ask_user_body(qas) == "Which approach?\n→ Option B"


def test_multi_select_with_typed_other_is_quoted():
    args = _args(_q("Which features?", ["Caching", "Retries"], header="features", multi=True))
    result = json.dumps({"features": ["Caching", "rate-limit by IP"]})
    qas = parse_ask_user(args, result, "done")
    # "rate-limit by IP" is not an offered label → quoted as the user's own words.
    assert ask_user_body(qas) == 'Which features?\n→ Caching, "rate-limit by IP"'


def test_multiple_questions_title_is_count():
    args = _args(
        _q("Q1?", ["A", "B"], header="q1"),
        _q("Q2?", ["C", "D"], header="q2"),
    )
    result = json.dumps({"q1": "A", "q2": "D"})
    qas = parse_ask_user(args, result, "done")
    assert ask_user_title_tail(qas, "answered") == "2 questions answered"
    assert ask_user_body(qas) == "Q1?\n→ A\n\nQ2?\n→ D"


def test_blank_header_pairs_by_position():
    # Both headers blank: pairing must use order, not the (colliding) header key.
    args = _args(_q("First?", ["A", "B"]), _q("Second?", ["C", "D"]))
    result = json.dumps({"First?": "B", "Second?": "C"})  # keys are fallback headers
    qas = parse_ask_user(args, result, "done")
    assert [qa.answers for qa in qas] == [["B"], ["C"]]


def test_pending_state_title_and_body():
    args = _args(_q("Which approach?", ["A", "B"], header="approach"))
    qas = parse_ask_user(args, "", "pending")
    assert ask_user_title_tail(qas, "pending") == "Which approach?  awaiting answer…"
    assert ask_user_body(qas) == "Which approach?\n→ (awaiting answer)"


def test_cancelled_state_title_and_body():
    args = _args(_q("Which approach?", ["A", "B"], header="approach"))
    result = "User dismissed the prompt without answering."
    qas = parse_ask_user(args, result, "done")
    assert ask_user_title_tail(qas, "cancelled") == "cancelled — no answer"
    assert ask_user_body(qas) == "Which approach?\n→ (cancelled)"


def test_malformed_inputs_do_not_raise():
    # Missing questions, non-dict items, non-list options — all degrade.
    assert parse_ask_user({}, "", "pending") == []
    qas = parse_ask_user({"questions": ["x", {"question": "Ok?", "options": "nope"}]},
                         json.dumps({"a": "v"}), "done")
    assert [qa.question for qa in qas] == ["Ok?"]
