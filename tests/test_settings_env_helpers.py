"""Pure helpers for the Tools settings help line and dependency dimming."""

from marim_harness.interfaces.tui.settings_env import (
    CHECK_DEPENDENTS,
    FIELD_HELP,
    SECTION_HELP,
    VALUE_DEPENDENTS,
    dependents_enabled,
    help_for,
)


def test_help_for_direct_id():
    assert help_for(["sw-lsp"]) == FIELD_HELP["sw-lsp"]


def test_help_for_first_matching_ancestor_wins():
    # Focused RadioButton id first, then parent RadioSet — set wins.
    text = help_for(["toolsearch-auto", "toolsearch-set", "section-tools"])
    assert text == FIELD_HELP["toolsearch-set"]


def test_help_for_unknown_returns_none():
    assert help_for(["nope", "also-nope"]) is None
    assert help_for([]) is None


def test_section_help_tools_present():
    assert "tools" in SECTION_HELP
    assert "save automatically" in SECTION_HELP["tools"].lower()


def test_field_help_covers_every_tools_control():
    required = {
        "sw-lsp",
        "sw-lsp-tools",
        "toolsearch-set",
        "toolsearch-threshold",
        "sw-job",
        "sw-workflows",
        "subagent-req-limit",
        "wake-depth-cap",
        "sw-tiering",
        "tier-change-cheap",
        "tier-change-med",
        "tier-change-high",
        "advisor-change",
        "advisor-max-tokens",
        "advisor-max-uses",
        "thinking-change",
    }
    assert required <= set(FIELD_HELP)


def test_dependents_enabled_checkbox_masters():
    enabled = dependents_enabled(
        {"sw-lsp": False, "sw-tiering": True},
        {"toolsearch": "auto", "advisor": "off"},
    )
    assert enabled["row-lsp-tools"] is False
    assert enabled["row-tier-cheap"] is True
    assert enabled["row-tier-med"] is True
    assert enabled["row-tier-high"] is True
    assert enabled["row-toolsearch-threshold"] is True
    assert enabled["row-advisor-tokens"] is False
    assert enabled["row-advisor-uses"] is False


def test_dependents_enabled_toolsearch_off_disables_threshold():
    enabled = dependents_enabled(
        {"sw-lsp": True, "sw-tiering": True},
        {"toolsearch": "off", "advisor": "openrouter/x"},
    )
    assert enabled["row-toolsearch-threshold"] is False
    assert enabled["row-advisor-tokens"] is True


def test_check_and_value_registries_list_expected_rows():
    check_rows = {r for rows in CHECK_DEPENDENTS.values() for r in rows}
    value_rows = {r for rows, _ in VALUE_DEPENDENTS.values() for r in rows}
    assert check_rows == {
        "row-lsp-tools",
        "row-tier-cheap",
        "row-tier-med",
        "row-tier-high",
    }
    assert value_rows == {
        "row-toolsearch-threshold",
        "row-advisor-tokens",
        "row-advisor-uses",
    }
