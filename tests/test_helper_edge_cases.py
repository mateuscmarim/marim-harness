"""Edge paths of small pure helpers: degenerate inputs, fallbacks, and error
branches that no end-to-end test reaches (empty/None payloads, malformed
frontmatter, corrupt session headers, unknown provider names). Each test pins
the documented fallback so a refactor cannot quietly turn a tolerant helper
into one that raises."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from marim_harness.config.cli_input import (
    _latest_request,
    _part_text,
    _render_tool_args,
    latest_user_text,
)
from marim_harness.config.codex_schema import _pointer_target
from marim_harness.config.context_limits import ContextLimits, build_context_limits
from marim_harness.config.model import ModelConfig, build_model, detect_active_providers
from marim_harness.config.quota import format_window
from marim_harness.runtime.permissions import (
    _bash_command,
    _call_args,
    _scratchpad_write_target,
)
from marim_harness.session import SessionManager
from marim_harness.session.ctrl import SessionController
from marim_harness.session.store import _header_fields
from marim_harness.tools.names import READ_TOOLS
from marim_harness.workspace.agents import _parse_agent, _parse_tools
from tests.conftest import _make_deps

# --- config/quota.format_window ---------------------------------------------


@pytest.mark.parametrize(
    ("mins", "expected"),
    [
        (None, ""),
        (0, ""),
        (-30, ""),
        (10080, "1w"),
        (20160, "2w"),
        (1440, "1d"),
        (4320, "3d"),
        (300, "5h"),
        (90, "90m"),
    ],
)
def test_format_window_picks_the_largest_exact_unit(mins, expected):
    assert format_window(mins) == expected


# --- config/cli_input -------------------------------------------------------


def test_part_text_reduces_every_content_shape():
    image = BinaryContent(data=b"\x89PNG", media_type="image/png")
    assert _part_text("plain") == "plain"
    assert _part_text(["a", image, "b"]) == "a\nb"
    assert _part_text(None) == ""
    assert _part_text(42) == "42"


def test_latest_user_text_falls_back_to_empty_without_a_user_request():
    assert latest_user_text([]) == ""
    only_response: list[ModelMessage] = [ModelResponse(parts=[TextPart("hi")])]
    assert latest_user_text(only_response) == ""
    tool_only: list[ModelMessage] = [
        ModelRequest(parts=[ToolReturnPart("t", "out", tool_call_id="c1")])
    ]
    assert latest_user_text(tool_only) == ""


def test_latest_user_text_joins_prompt_and_retry_parts():
    msgs: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("first")]),
        ModelResponse(parts=[TextPart("ok")]),
        ModelRequest(parts=[UserPromptPart("second"), RetryPromptPart("fix it")]),
    ]
    text = latest_user_text(msgs)
    assert text.startswith("second\n")
    assert "fix it" in text
    assert "first" not in text


def test_render_tool_args_handles_str_none_dict_and_unserializable():
    assert _render_tool_args("raw payload") == "raw payload"
    assert _render_tool_args(None) == ""
    assert _render_tool_args({"b": 1, "a": "é"}) == '{"a": "é", "b": 1}'
    weird = {"obj": object()}
    assert _render_tool_args(weird) == str(weird)


def test_latest_request_returns_empty_without_a_user_bearing_request():
    assert _latest_request([]) == []
    tool_only: list[ModelMessage] = [
        ModelRequest(parts=[ToolReturnPart("t", "out", tool_call_id="c1")])
    ]
    assert _latest_request(tool_only) == []
    user = ModelRequest(parts=[UserPromptPart("hello")])
    assert _latest_request([user, *tool_only]) == [user]


# --- runtime/permissions arg coercion ---------------------------------------


def test_call_args_coerces_json_strings_and_rejects_non_dicts():
    assert _call_args(SimpleNamespace(args='{"command": "ls"}')) == {"command": "ls"}
    assert _call_args(SimpleNamespace(args="not json")) == {}
    assert _call_args(SimpleNamespace(args=["a", "b"])) == {}
    assert _call_args(SimpleNamespace(args=None)) == {}
    assert _call_args(object()) == {}
    assert _bash_command(SimpleNamespace(args='{"command": "ls"}')) == "ls"
    assert _bash_command(SimpleNamespace(args="broken")) == ""


def test_scratchpad_write_target_resolution_order(tmp_path):
    root = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    # Not a write tool: never a scratchpad write.
    assert _scratchpad_write_target(SimpleNamespace(tool_name="read_file"), root, scratch) is None
    # A write with no path can't be classified.
    call = SimpleNamespace(tool_name="write_file", args={"path": ""})
    assert _scratchpad_write_target(call, root, scratch) is None
    # A relative path always lands in the workspace → normal gating.
    call = SimpleNamespace(tool_name="edit_file", args={"path": "notes.md"})
    assert _scratchpad_write_target(call, root, scratch) is None
    # An absolute scratchpad path resolves to the real file inside it.
    call = SimpleNamespace(tool_name="write_file", args={"path": str(scratch / "a" / "b.txt")})
    assert _scratchpad_write_target(call, root, scratch) == (scratch / "a" / "b.txt").resolve()
    # Outside both roots: not a scratchpad write either.
    call = SimpleNamespace(tool_name="write_file", args={"path": str(tmp_path / "elsewhere")})
    assert _scratchpad_write_target(call, root, scratch) is None


# --- workspace/agents frontmatter parsing -----------------------------------


def test_parse_tools_accepts_strings_lists_and_falls_back_to_read_set():
    assert _parse_tools(None) == READ_TOOLS
    assert _parse_tools(42) == READ_TOOLS
    assert _parse_tools("nonsense, also-unknown") == READ_TOOLS
    assert _parse_tools("read_file, bash") == frozenset({"read_file", "bash"})
    assert _parse_tools(["grep", "unknown"]) == frozenset({"grep"})


@pytest.mark.parametrize(
    "body",
    [
        "no frontmatter at all\n",
        "---\n: : not: yaml: [\n---\nbody\n",
        "---\n- just\n- a list\n---\nbody\n",
        "---\nname: other-name\ndescription: mismatch\n---\nbody\n",
        "---\ndescription: '   '\n---\nbody\n",
        "---\ntier: med\n---\nno description\n",
    ],
)
def test_parse_agent_rejects_malformed_files(tmp_path, body):
    p = tmp_path / "helper.md"
    p.write_text(body, encoding="utf-8")
    assert _parse_agent("project", p) is None


def test_parse_agent_rejects_bad_names_and_unreadable_files(tmp_path):
    bad = tmp_path / "Not Valid.md"
    bad.write_text("---\ndescription: x\n---\n", encoding="utf-8")
    assert _parse_agent("project", bad) is None
    # A directory with a valid stem trips the OSError branch on read.
    (tmp_path / "helper.md").mkdir()
    assert _parse_agent("project", tmp_path / "helper.md") is None
    # Undecodable bytes are treated like an unreadable file.
    garbled = tmp_path / "garbled.md"
    garbled.write_bytes(b"---\ndescription: \xff\xfe\n---\n")
    assert _parse_agent("project", garbled) is None


# --- session/store header fast path + id allocation -------------------------


def test_header_fields_falls_back_on_missing_or_corrupt_heads(tmp_path):
    assert _header_fields(tmp_path / "missing.json") is None
    no_marker = tmp_path / "old.json"
    no_marker.write_text('{"messages": []}', encoding="utf-8")
    assert _header_fields(no_marker) is None
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text('{"message_count": 1, "x": [1, , "messages": []}', encoding="utf-8")
    assert _header_fields(corrupt) is None
    pre_header = tmp_path / "prehdr.json"
    pre_header.write_text('{"model": "m", "messages": []}', encoding="utf-8")
    assert _header_fields(pre_header) is None


def test_persisted_jobs_is_empty_for_a_corrupt_pre_header_file(tmp_path):
    mgr = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    store = mgr.store("broken")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert mgr.persisted_jobs("broken") == []


def test_unique_id_suffixes_reserved_and_on_disk_collisions(tmp_path):
    mgr = SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")
    mgr.store("taken")
    assert mgr._unique_id("taken") == "taken-2"
    mgr.store("taken-2")
    assert mgr._unique_id("taken") == "taken-3"
    on_disk = mgr.session_path("disk")
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_text("{}", encoding="utf-8")
    assert mgr._unique_id("disk") == "disk-2"
    assert mgr._unique_id("fresh") == "fresh"


# --- session/ctrl versioned history -----------------------------------------


def test_every_in_place_history_mutation_bumps_the_version(tmp_path):
    ctrl = SessionController(None, None, _make_deps(tmp_path), 100_000, 20)
    ctrl.history = ["a", "b", "c"]
    version = ctrl.history_version
    h = ctrl.history

    def bumped() -> bool:
        nonlocal version
        before, version = version, ctrl.history_version
        return version == before + 1

    h.append("d")
    assert bumped()
    h.extend(["e"])
    assert bumped()
    h.insert(0, "z")
    assert bumped()
    assert h.pop() == "e"
    assert bumped()
    h.remove("z")
    assert bumped()
    h.sort()
    assert bumped()
    h.reverse()
    assert bumped()
    h[0] = "x"
    assert bumped()
    del h[0]
    assert bumped()
    h += ["y"]
    assert bumped()
    h *= 2
    assert bumped()
    assert ctrl.history is h  # augmented assignment kept the tracked list
    h.clear()
    assert bumped()
    assert ctrl.history == []


# --- config/model provider fallbacks ----------------------------------------


def test_unknown_provider_falls_back_to_openrouter(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "no-such-provider")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    configs, default = detect_active_providers()
    assert default == "openrouter"
    assert "openrouter" in configs


def test_build_model_google_uses_the_google_provider():
    from pydantic_ai.models.google import GoogleModel

    cfg = ModelConfig(provider="google", model="gemini-2.5-pro", api_key="AIza-test")
    model = build_model(cfg)
    assert isinstance(model, GoogleModel)
    assert model.model_name == "gemini-2.5-pro"


# --- config/context_limits --------------------------------------------------


def test_note_reported_window_ignores_unusable_readings():
    limits = ContextLimits(budget=None)
    limits.note_reported_window(None, 1000)
    limits.note_reported_window("m", True)
    limits.note_reported_window("m", "1000")  # type: ignore[arg-type]
    limits.note_reported_window("m", 0)
    limits.note_reported_window("m", -5)
    assert limits.window_for("m") is None
    limits.note_reported_window("local:m", 4000)
    assert limits.window_for("local:m") == 4000
    assert limits.window_for("m") == 4000


@pytest.mark.anyio
async def test_build_context_limits_wires_the_google_catalog(monkeypatch):
    from marim_harness.workspace import catalog
    from marim_harness.workspace.catalog import ModelEntry

    async def fake_google(api_key=None, timeout=10.0):
        return [ModelEntry(id="gemini-2.5-pro", name="Gemini", context_window=1_000_000)]

    monkeypatch.setattr(catalog, "fetch_google_models", fake_google)
    configs = {"google": SimpleNamespace(base_url=None, api_key="AIza-x")}
    limits = build_context_limits(configs, window_override=None, budget=None)
    assert await limits.resolve("gemini-2.5-pro") == 800_000


# --- config/codex_schema pointer resolution ---------------------------------


@pytest.mark.parametrize("ref", ["#/items/01", "#/items/x", "#/items/9", "#/missing", "#/leaf"])
def test_pointer_target_rejects_bad_array_indexes_and_missing_keys(ref):
    root = {"items": [{"type": "string"}], "leaf": "not-a-schema"}
    with pytest.raises(UserError, match="Cannot normalize local JSON Schema reference"):
        _pointer_target(root, ref)


def test_pointer_target_resolves_escaped_and_indexed_paths():
    root = {"a/b": {"~x": [{"type": "integer"}]}}
    assert _pointer_target(root, "#/a~1b/~0x/0") == {"type": "integer"}
