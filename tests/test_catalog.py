from unittest.mock import AsyncMock, patch

import pytest

from marim_harness.workspace import (
    ModelEntry,
    filter_entries,
    model_supports_images,
    parse_models,
)
from marim_harness.workspace.catalog import (
    fetch_local_models,
    parse_google_models,
    parse_lmstudio_models,
)


def _mock_async_client(payload, *, raises: Exception | None = None):
    """Patch catalog.httpx.AsyncClient with a stub whose .get returns ``payload``
    (or raises ``raises``). Returns the patch context manager and the captured
    get mock so the caller can assert the URL that was hit."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: payload
    client = AsyncMock()
    client.get = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=mock_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    # catalog imports httpx lazily inside the fetcher (keeps the CLI import chain
    # light), so there's no catalog.httpx attribute — patch httpx.AsyncClient itself.
    cm = patch("httpx.AsyncClient", return_value=client)
    return cm, client.get


@pytest.mark.anyio
async def test_fetch_local_models_hits_models_endpoint_and_parses():
    """Fetches {base_url}/models and parses the OpenAI-style payload that an
    LM Studio / Ollama server returns."""
    payload = {"data": [{"id": "qwen2.5-coder"}, {"id": "llama-3.1-8b"}]}
    cm, get = _mock_async_client(payload)
    with cm:
        entries = await fetch_local_models("http://localhost:1234/v1", "lmstudio")
    assert [e.id for e in entries] == ["llama-3.1-8b", "qwen2.5-coder"]  # sorted
    assert get.await_args.args[0] == "http://localhost:1234/v1/models"


@pytest.mark.anyio
async def test_fetch_local_models_strips_trailing_slash():
    """A base_url with a trailing slash must not yield a doubled // before models."""
    cm, get = _mock_async_client({"data": []})
    with cm:
        await fetch_local_models("http://localhost:1234/v1/", None)
    assert get.await_args.args[0] == "http://localhost:1234/v1/models"


@pytest.mark.anyio
async def test_fetch_local_models_returns_empty_on_error():
    """A network/HTTP failure degrades to [] so the picker falls back to free
    text rather than crashing."""
    cm, _ = _mock_async_client(None, raises=RuntimeError("connection refused"))
    with cm:
        assert await fetch_local_models("http://localhost:1234/v1", None) == []


def test_parse_google_models_skips_row_with_non_list_methods():
    """A row whose supportedGenerationMethods is present-but-null (or any
    non-list) must be skipped, not raise — otherwise the outer fetch's
    except-Exception discards the entire catalog over one malformed row."""
    payload = {
        "models": [
            {"name": "models/gemini-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/broken", "supportedGenerationMethods": None},
            {"name": "models/also-broken", "supportedGenerationMethods": "generateContent"},
        ]
    }
    entries = parse_google_models(payload)
    assert [e.id for e in entries] == ["gemini-pro"]


def test_parse_google_models_leaves_image_support_unknown():
    """The Gemini catalog doesn't report input modalities, so supports_images
    must be None (unknown), not a hardcoded True — matching the ModelEntry
    contract and the OpenRouter parser's behavior for rows lacking the field."""
    payload = {
        "models": [
            {"name": "models/gemini-pro", "supportedGenerationMethods": ["generateContent"]},
        ]
    }
    entries = parse_google_models(payload)
    assert entries[0].supports_images is None


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


def test_parse_models_reads_image_modality():
    payload = {"data": [
        {"id": "a/vision", "name": "V",
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "b/text", "name": "T",
         "architecture": {"input_modalities": ["text"]}},
        {"id": "c/unknown", "name": "U"},
    ]}
    by_id = {e.id: e for e in parse_models(payload)}
    assert by_id["a/vision"].supports_images is True
    assert by_id["b/text"].supports_images is False
    assert by_id["c/unknown"].supports_images is None


def test_model_supports_images_lookup():
    entries = parse_models({"data": [
        {"id": "a/vision", "architecture": {"input_modalities": ["image"]}},
    ]})
    assert model_supports_images(entries, "a/vision") is True
    assert model_supports_images(entries, "missing/model") is None


def test_model_entry_qualified_uses_colon_when_provider_set():
    entry = ModelEntry(id="qwen2.5-coder", name="Qwen", provider="local")
    assert entry.qualified == "local:qwen2.5-coder"


def test_model_entry_qualified_is_bare_without_provider():
    entry = ModelEntry(id="anthropic/claude-sonnet-4-6", name="Sonnet")
    assert entry.qualified == "anthropic/claude-sonnet-4-6"


def test_filter_entries_matches_provider():
    entries = [
        ModelEntry(id="x", name="X", provider="openrouter"),
        ModelEntry(id="y", name="Y", provider="local"),
    ]
    assert [e.id for e in filter_entries(entries, "local")] == ["y"]


def test_parse_models_keeps_openrouter_context_length():
    payload = {"data": [
        {"id": "anthropic/claude-opus-4-8", "name": "Opus", "context_length": 200000},
        {"id": "some/other", "name": "Other"},                    # field absent
        {"id": "bad/ctx", "name": "Bad", "context_length": "big"},  # non-int ignored
    ]}
    entries = {e.id: e for e in parse_models(payload)}
    assert entries["anthropic/claude-opus-4-8"].context_window == 200000
    assert entries["some/other"].context_window is None
    assert entries["bad/ctx"].context_window is None


def test_parse_google_models_keeps_input_token_limit():
    payload = {"models": [
        {"name": "models/gemini-2.5-pro", "displayName": "Gemini",
         "supportedGenerationMethods": ["generateContent"],
         "inputTokenLimit": 1048576},
    ]}
    (entry,) = parse_google_models(payload)
    assert entry.context_window == 1048576


def test_parse_lmstudio_models_reports_only_the_served_window():
    """The exact shape the enhanced /api/v0/models returns (verified live):
    a loaded model carries loaded_context_length — the true serving window,
    which can be far below max_context_length — while a not-loaded model
    only advertises its weights' max.

    Only the *served* window (loaded_context_length) is trusted. A model's
    max_context_length is what the weights support, NOT what the server will
    accept: advertising it as the window inflated the compaction threshold to
    ~0.8x the weights-max while the server rejected anything past its much
    smaller loaded window — requests overflowed while the gauge read 12%. So a
    row without loaded_context_length is omitted (window unknown → the caller
    falls back to its conservative default threshold)."""
    payload = {"data": [
        {"id": "qwen/qwen3.5-9b", "state": "loaded",
         "max_context_length": 262144, "loaded_context_length": 101039},
        {"id": "ornith-1.0-35b", "state": "not-loaded",
         "max_context_length": 262144},
        {"id": "junk", "max_context_length": "nope"},
    ]}
    windows = parse_lmstudio_models(payload)
    assert windows["qwen/qwen3.5-9b"] == 101039   # the served window
    assert "ornith-1.0-35b" not in windows        # weights-max is not servable
    assert "junk" not in windows


def test_parse_lmstudio_models_tolerates_garbage():
    assert parse_lmstudio_models({}) == {}
    assert parse_lmstudio_models({"data": "nope"}) == {}
