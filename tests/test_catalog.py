import logging
from unittest.mock import AsyncMock, patch

import pytest

from marim_harness.workspace import (
    ModelEntry,
    catalog,
    filter_entries,
    make_supports_images,
    model_supports_images,
    parse_models,
)
from marim_harness.workspace.catalog import (
    fetch_google_models,
    fetch_local_models,
    fetch_openrouter_models,
    parse_google_models,
    parse_lmstudio_models,
    parse_zen_models,
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


# -- strict=True: verification needs the real failure, not a silent [] -------
#
# These hit a real socket (port 9 is "discard", nothing listens there) rather
# than mocking httpx, so they prove the actual connection-refused exception
# propagates end to end — not just that our code re-raises whatever it's handed.


@pytest.mark.anyio
async def test_fetch_local_models_strict_raises_on_connection_refused():
    with pytest.raises(Exception):  # noqa: B017 - real connection-refused, class varies
        await fetch_local_models("http://127.0.0.1:9", strict=True)


@pytest.mark.anyio
async def test_fetch_local_models_non_strict_still_degrades_to_empty():
    """Pins the picker's degrade-to-[] default even after the strict opt-in."""
    assert await fetch_local_models("http://127.0.0.1:9") == []


@pytest.mark.anyio
async def test_fetch_google_models_strict_raises_on_connection_refused(monkeypatch):
    monkeypatch.setattr(catalog, "_GOOGLE_MODELS_URL", "http://127.0.0.1:9/x")
    with pytest.raises(Exception):  # noqa: B017 - real connection-refused, class varies
        await fetch_google_models(strict=True)


@pytest.mark.anyio
async def test_fetch_google_models_non_strict_returns_empty(monkeypatch):
    monkeypatch.setattr(catalog, "_GOOGLE_MODELS_URL", "http://127.0.0.1:9/x")
    assert await fetch_google_models() == []


@pytest.mark.anyio
async def test_fetch_openrouter_models_strict_raises_on_connection_refused(monkeypatch):
    monkeypatch.setattr(catalog, "_OPENROUTER_MODELS_URL", "http://127.0.0.1:9/x")
    with pytest.raises(Exception):  # noqa: B017 - real connection-refused, class varies
        await fetch_openrouter_models(strict=True)


@pytest.mark.anyio
async def test_fetch_openrouter_models_non_strict_returns_empty(monkeypatch):
    monkeypatch.setattr(catalog, "_OPENROUTER_MODELS_URL", "http://127.0.0.1:9/x")
    assert await fetch_openrouter_models() == []


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


# -- openrouter strict key validation ----------------------------------------
#
# OpenRouter's /models endpoint is PUBLIC — a catalog fetch succeeds with a
# garbage (or absent) key, so it can't validate a credential. In strict mode
# the fetcher first hits /key, which requires auth and 401s on a bad key,
# giving verification a real verdict. The non-strict picker path never pays
# that extra request. These run against a real local HTTP server (a thread on
# an OS-assigned port), not a mocked client.


class _OpenRouterStub:
    """A tiny stand-in for openrouter.ai: public /models, authenticated /key."""

    def __init__(self):
        import http.server
        import threading

        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                stub.paths.append(self.path)
                if self.path == "/models":
                    body = b'{"data": []}'
                    self.send_response(200)
                elif self.headers.get("Authorization") == "Bearer good-key":
                    body = b'{"data": {}}'
                    self.send_response(200)
                else:
                    body = b'{"error": "unauthorized"}'
                    self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # keep pytest output clean

        self.paths: list[str] = []
        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def shutdown(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def openrouter_stub(monkeypatch):
    stub = _OpenRouterStub()
    monkeypatch.setattr(catalog, "_OPENROUTER_MODELS_URL", stub.base + "/models")
    monkeypatch.setattr(catalog, "_OPENROUTER_KEY_URL", stub.base + "/key")
    yield stub
    stub.shutdown()


@pytest.mark.anyio
async def test_openrouter_strict_rejects_bad_key_despite_public_catalog(
    openrouter_stub,
):
    """The whole point: /models alone would 200 here, but strict mode must
    still fail on the 401 from the authenticated /key probe."""
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_openrouter_models("garbage", strict=True)


@pytest.mark.anyio
async def test_openrouter_strict_good_key_passes_probe_and_fetches(openrouter_stub):
    assert await fetch_openrouter_models("good-key", strict=True) == []
    assert "/key" in openrouter_stub.paths
    assert "/models" in openrouter_stub.paths


@pytest.mark.anyio
async def test_openrouter_non_strict_never_probes_key(openrouter_stub):
    """The picker path stays single-request: no /key probe, bad key included."""
    assert await fetch_openrouter_models("garbage") == []
    assert "/key" not in openrouter_stub.paths


@pytest.mark.anyio
async def test_openrouter_strict_without_key_skips_probe(openrouter_stub):
    """No credential to validate: strict still fetches the public catalog."""
    assert await fetch_openrouter_models(None, strict=True) == []
    assert "/key" not in openrouter_stub.paths


def test_parse_models_reads_supported_parameters_reasoning():
    payload = {"data": [
        {"id": "a/thinks", "name": "Thinks",
         "supported_parameters": ["reasoning", "tools"]},
        {"id": "b/plain", "name": "Plain", "supported_parameters": ["tools"]},
        {"id": "c/unknown", "name": "Unknown"},
    ]}
    entries = {e.id: e for e in parse_models(payload)}
    assert entries["a/thinks"].supports_thinking is True
    assert entries["b/plain"].supports_thinking is False
    assert entries["c/unknown"].supports_thinking is None


def test_model_supports_thinking_lookup():
    from marim_harness.workspace.catalog import model_supports_thinking

    payload = {"data": [
        {"id": "a/thinks", "name": "T", "supported_parameters": ["reasoning"]},
    ]}
    entries = parse_models(payload)
    assert model_supports_thinking(entries, "a/thinks") is True
    assert model_supports_thinking(entries, "missing") is None


def test_parse_zen_models_filters_non_openai_families():
    payload = {"data": [
        {"id": "mimo-v2.5-free", "object": "model"},
        {"id": "big-pickle", "object": "model"},
        {"id": "claude-sonnet-5", "object": "model"},
        {"id": "gemini-3.5-flash", "object": "model"},
        {"id": "gpt-5.5", "object": "model"},
    ]}
    entries = parse_zen_models(payload)
    ids = [e.id for e in entries]
    assert ids == ["big-pickle", "gpt-5.5", "mimo-v2.5-free"]  # sorted, filtered


def test_parse_zen_models_tolerates_malformed_payloads():
    assert parse_zen_models({}) == []
    assert parse_zen_models({"data": "nope"}) == []


@pytest.mark.anyio
async def test_fetch_zen_models_strict_raises_on_connection_refused(monkeypatch):
    # Mirror test_fetch_openrouter_models_strict_raises_on_connection_refused
    # (test_catalog.py ~L101): point the module URL constant at a dead port.
    monkeypatch.setattr(catalog, "_ZEN_MODELS_URL", "http://127.0.0.1:9/x")
    with pytest.raises(Exception):  # noqa: B017 - real connection-refused, class varies
        await catalog.fetch_zen_models("sk-x", strict=True)


@pytest.mark.anyio
async def test_fetch_zen_models_non_strict_returns_empty(monkeypatch):
    monkeypatch.setattr(catalog, "_ZEN_MODELS_URL", "http://127.0.0.1:9/x")
    assert await catalog.fetch_zen_models("sk-x") == []


@pytest.mark.anyio
async def test_fetch_zen_models_honors_url_override(monkeypatch):
    """The zen-go provider reuses this fetcher pointed at the Go catalog; the
    url kwarg must reach the GET (and default resolution must stay call-time —
    the connection-refused tests above monkeypatch _ZEN_MODELS_URL)."""
    import httpx

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "glm-5.2", "object": "model"}]}

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    entries = await catalog.fetch_zen_models(
        "sk-x", url="https://opencode.ai/zen/go/v1/models")
    assert captured["url"] == "https://opencode.ai/zen/go/v1/models"
    assert [e.id for e in entries] == ["glm-5.2"]


@pytest.mark.anyio
async def test_make_supports_images_gates_and_caches():
    calls = []

    async def list_models():
        calls.append(1)
        return [
            ModelEntry(id="m1", name="M1", supports_images=True),
            ModelEntry(id="m2", name="M2", supports_images=False),
        ]

    gate = make_supports_images(list_models)
    assert await gate("m1") is True
    assert await gate("m2") is False
    assert await gate("unknown") is None
    assert len(calls) == 1  # one-shot cache: a single catalog fetch


@pytest.mark.anyio
async def test_make_supports_images_fetch_failure_is_unknown():
    calls = []

    async def list_models():
        calls.append(1)
        raise RuntimeError("network down")

    gate = make_supports_images(list_models)
    assert await gate("m1") is None
    assert await gate("m1") is None
    assert len(calls) == 1  # failure is cached too — no per-read refetch storm


# -- quiet failure logging ---------------------------------------------------
#
# A model server being unreachable is routine (LM Studio not running, machine
# offline). These pin that it costs one log line, not an httpx traceback per
# probe — `marim serve` re-fetches catalogs on every session build.


@pytest.fixture
def fresh_unreachable():
    """catalog._unreachable is process-global dedup state; isolate each test."""
    catalog._unreachable.clear()
    yield catalog._unreachable
    catalog._unreachable.clear()


def _catalog_records(caplog):
    return [r for r in caplog.records if r.name == catalog.__name__]


@pytest.mark.anyio
async def test_transport_failure_logs_one_line_then_quiets(caplog, fresh_unreachable):
    """First outage: one WARNING, no traceback. While it stays down: DEBUG."""
    import httpx

    cm, _ = _mock_async_client(None, raises=httpx.ConnectTimeout(""))
    with caplog.at_level(logging.DEBUG, logger=catalog.__name__), cm:
        assert await fetch_local_models("http://localhost:1234/v1") == []
        assert await fetch_local_models("http://localhost:1234/v1") == []

    first, second = _catalog_records(caplog)
    assert first.levelno == logging.WARNING
    assert first.exc_info is None
    # ConnectTimeout("") stringifies to "", so the type name must carry the message.
    assert "ConnectTimeout" in first.getMessage()
    assert "unreachable" in first.getMessage()
    assert second.levelno == logging.DEBUG


@pytest.mark.anyio
async def test_recovery_rearms_the_warning(caplog, fresh_unreachable):
    """Once the endpoint answers again, the next outage warns rather than
    hiding at DEBUG — otherwise a flapping server would go silent forever."""
    import httpx

    url = "http://localhost:1234/v1"
    down, _ = _mock_async_client(None, raises=httpx.ConnectError("refused"))
    up, _ = _mock_async_client({"data": [{"id": "m"}]})
    with caplog.at_level(logging.DEBUG, logger=catalog.__name__):
        with down:
            await fetch_local_models(url)
        with up:
            assert [e.id for e in await fetch_local_models(url)] == ["m"]
        with down:
            await fetch_local_models(url)

    assert [r.levelno for r in _catalog_records(caplog)] == [logging.WARNING, logging.WARNING]


@pytest.mark.anyio
async def test_http_status_error_logs_without_traceback(caplog, fresh_unreachable):
    """A 401 is actionable, but its stack frames are not — one line every time
    (no dedup: the message is the whole signal and it may change)."""
    import httpx

    request = httpx.Request("GET", "http://localhost:1234/v1/models")
    response = httpx.Response(401, request=request)
    exc = httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
    cm, _ = _mock_async_client(None, raises=exc)
    with caplog.at_level(logging.DEBUG, logger=catalog.__name__), cm:
        await fetch_local_models("http://localhost:1234/v1")
        await fetch_local_models("http://localhost:1234/v1")

    records = _catalog_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert all(r.exc_info is None for r in records)
    assert "401" in records[0].getMessage()


@pytest.mark.anyio
async def test_unexpected_error_keeps_the_traceback(caplog, fresh_unreachable):
    """Not a network condition — a parser bug or the like still needs frames."""
    cm, _ = _mock_async_client(None, raises=RuntimeError("boom"))
    with caplog.at_level(logging.DEBUG, logger=catalog.__name__), cm:
        assert await fetch_local_models("http://localhost:1234/v1") == []

    (record,) = _catalog_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


@pytest.mark.anyio
async def test_zen_and_lmstudio_windows_also_stay_quiet(caplog, fresh_unreachable):
    """The dedup key is the endpoint, so the other fetchers get their own
    first-warning rather than being silenced by an unrelated outage."""
    import httpx

    cm, _ = _mock_async_client(None, raises=httpx.ConnectTimeout(""))
    with caplog.at_level(logging.DEBUG, logger=catalog.__name__), cm:
        assert await catalog.fetch_zen_models("sk-x") == []
        assert await catalog.fetch_lmstudio_windows("http://localhost:1234/v1") == {}

    records = _catalog_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert all(r.exc_info is None for r in records)


def test_describe_names_the_type_for_a_blank_message():
    import httpx

    assert catalog._describe(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert catalog._describe(RuntimeError("boom")) == "RuntimeError: boom"
