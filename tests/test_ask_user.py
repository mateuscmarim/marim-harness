from marim_harness.ask_user import (
    Choice,
    Question,
    answers_to_json,
    coerce_questions,
)


def test_blank_label_choices_dropped():
    q = Question("Pick", "Pick", [Choice("ok"), Choice("  "), Choice("")])
    [out] = coerce_questions([q])
    assert [c.label for c in out.options] == ["ok"]


def test_question_with_no_usable_options_dropped():
    good = Question("Pick", "Pick", [Choice("ok")])
    empty = Question("Empty", "Empty", [Choice("  ")])
    assert [q.header for q in coerce_questions([empty, good])] == ["Pick"]


def test_blank_question_text_dropped():
    assert coerce_questions([Question("   ", "H", [Choice("ok")])]) == []


def test_missing_header_falls_back_to_question_text():
    long_q = "Which database engine should we use for this service?"
    [out] = coerce_questions([Question(long_q, "", [Choice("Postgres")])])
    assert out.header
    assert long_q.startswith(out.header)
    assert len(out.header) <= 40


def test_questions_capped_at_four():
    qs = [Question(f"q{i}", f"h{i}", [Choice("x")]) for i in range(7)]
    assert len(coerce_questions(qs)) == 4


def test_answers_to_json_preserves_single_and_multi_shapes():
    out = answers_to_json({"DB": "Postgres", "Features": ["auth", "cache"]})
    import json
    assert json.loads(out) == {"DB": "Postgres", "Features": ["auth", "cache"]}
