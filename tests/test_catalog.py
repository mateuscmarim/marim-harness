from marim_harness.workspace import ModelEntry, filter_entries, parse_models

_SAMPLE = {
    "data": [
        {"id": "anthropic/claude-sonnet-4-6", "name": "Anthropic: Claude Sonnet 4.6"},
        {"id": "openai/gpt-5.2", "name": "OpenAI: GPT-5.2"},
        {"id": "xiaomi/mimo-v2.5", "name": "Xiaomi: MiMo v2.5"},
    ]
}


def test_parse_models_returns_sorted_entries():
    entries = parse_models(_SAMPLE)
    assert [e.id for e in entries] == [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-5.2",
        "xiaomi/mimo-v2.5",
    ]
    assert entries[0].name == "Anthropic: Claude Sonnet 4.6"


def test_parse_models_tolerates_malformed_payloads():
    assert parse_models({}) == []
    assert parse_models({"data": "nonsense"}) == []
    assert parse_models({"data": [{"no_id": 1}, {"id": "ok/model"}]}) == [
        ModelEntry(id="ok/model", name="ok/model")
    ]


def test_parse_models_falls_back_to_id_for_missing_name():
    entries = parse_models({"data": [{"id": "a/b"}]})
    assert entries == [ModelEntry(id="a/b", name="a/b")]


def test_filter_entries_matches_id_and_name_case_insensitively():
    entries = parse_models(_SAMPLE)
    assert [e.id for e in filter_entries(entries, "gpt")] == ["openai/gpt-5.2"]
    # matches against the display name too
    assert [e.id for e in filter_entries(entries, "claude")] == [
        "anthropic/claude-sonnet-4-6"
    ]
    # blank query returns everything
    assert filter_entries(entries, "") == entries
    assert filter_entries(entries, "   ") == entries
    # no match -> empty
    assert filter_entries(entries, "zzz") == []
