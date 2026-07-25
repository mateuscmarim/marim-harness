# zen-go Provider (OpenCode Go Subscription) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `zen-go` as a sixth provider: OpenCode's flat-rate Go subscription plan, driven through its OpenAI-compatible endpoint `https://opencode.ai/zen/go/v1` with the same `OPENCODE_API_KEY` as the existing `zen` provider.

**Architecture:** `zen-go` is a sibling of `zen` in every seam that enumerates providers: `KNOWN_PROVIDERS` + `_provider_config` + `_provider_has_creds` + `build_model` + `ModelSource.list_models` (config/model.py), `_PROVIDER_PREFIXES` (config/context_limits.py), and `PROVIDER_SPECS` (interfaces/tui/providers.py). The only new I/O surface is a `url=` parameter on the existing `fetch_zen_models` catalog fetcher — the Go `/models` payload is shape-identical to Zen's, so `parse_zen_models` is reused unchanged. Everything downstream (qualified `zen-go:<model>` ids, sub-agent tiers, advisor, sessions) routes through `parse_qualified`/`MultiModelSource` and needs no changes.

**Tech Stack:** Python 3.10+, httpx (lazy import), pydantic-ai `OpenAIChatModel`/`OpenAIProvider`, pytest (+anyio), Textual TUI.

**Spec:** `docs/superpowers/specs/2026-07-25-zen-go-provider-design.md`

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- CI order to match locally before claiming done: ruff → pyright → pytest.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity ≤ 10 per function.
- Base URL (chat): `https://opencode.ai/zen/go/v1` — fixed, NOT `MARIM_BASE_URL`.
- Catalog URL: `https://opencode.ai/zen/go/v1/models` (public, unauthenticated).
- Default model: `glm-5.2`.
- Credential: `OPENCODE_API_KEY` (fallback `MARIM_API_KEY`) — the SAME env var as `zen`; no new env var.
- Provider name everywhere (env value, qualified-id prefix, spec name, card id): `zen-go`.

---

### Task 1: `fetch_zen_models` gains a `url=` parameter

**Files:**
- Modify: `src/marim_harness/workspace/catalog.py:223-245` (`fetch_zen_models`)
- Test: `tests/test_catalog.py` (append after `test_fetch_zen_models_non_strict_returns_empty`, ~line 427)

**Interfaces:**
- Consumes: existing `parse_zen_models(payload) -> list[ModelEntry]`, module constant `_ZEN_MODELS_URL`.
- Produces: `async def fetch_zen_models(api_key: str | None = None, timeout: float = 10.0, *, strict: bool = False, url: str | None = None) -> list[ModelEntry]` — `url=None` means "the Zen catalog URL, resolved at call time". Task 2 calls it with `url=_ZEN_GO_BASE_URL + "/models"`.

**Gotcha this task must respect:** existing tests (`test_fetch_zen_models_strict_raises_on_connection_refused`, `test_fetch_zen_models_non_strict_returns_empty`) monkeypatch the module attribute `catalog._ZEN_MODELS_URL`. A `url: str = _ZEN_MODELS_URL` default would bind at `def` time and silently break them — the default MUST be `None` with call-time resolution (`url or _ZEN_MODELS_URL`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_catalog.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_catalog.py::test_fetch_zen_models_honors_url_override -v`
Expected: FAIL with `TypeError: fetch_zen_models() got an unexpected keyword argument 'url'`

- [ ] **Step 3: Implement the parameter**

In `src/marim_harness/workspace/catalog.py`, replace `fetch_zen_models` with:

```python
async def fetch_zen_models(
    api_key: str | None = None,
    timeout: float = 10.0,
    *,
    strict: bool = False,
    url: str | None = None,
) -> list[ModelEntry]:
    """Fetch the OpenCode Zen catalog. Returns ``[]`` on any failure so the
    picker degrades to free-text entry, unless ``strict=True`` (verification
    needs the real error), in which case the exception is re-raised. ``url``
    selects the endpoint — the zen-go provider passes its Go-plan catalog URL
    (same payload shape); ``None`` resolves to ``_ZEN_MODELS_URL`` at CALL
    time, not def time, so tests that monkeypatch the module constant keep
    working. Note: ``/models`` is public, so strict mode verifies
    *connectivity*, not the key — Zen has no known key-validation endpoint
    (unlike OpenRouter's ``/key``); a bad key surfaces at first chat request
    instead. httpx is imported lazily to keep the import chain light."""
    import httpx

    target = url or _ZEN_MODELS_URL
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(target, headers=headers)
            response.raise_for_status()
            return parse_zen_models(response.json())
    except Exception as exc:
        if strict:
            raise
        logger.warning("failed to fetch OpenCode Zen model catalog: %s", exc)
        return []
```

- [ ] **Step 4: Run the zen catalog tests to verify all pass (new + the two monkeypatching ones)**

Run: `uv run pytest --no-cov tests/test_catalog.py -k zen -v`
Expected: PASS (all, including `test_fetch_zen_models_strict_raises_on_connection_refused` and `test_fetch_zen_models_non_strict_returns_empty`)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/catalog.py tests/test_catalog.py
git commit -m "feat(catalog): url parameter on fetch_zen_models for the Go-plan catalog"
```

---

### Task 2: `zen-go` provider in config/model.py

**Files:**
- Modify: `src/marim_harness/config/model.py` (constants ~line 25-33, `ModelConfig` comment ~line 129, `_provider_config` ~line 339, `_provider_has_creds` ~line 388, `build_model` ~line 481, `ModelSource.list_models` ~line 542)
- Test: `tests/test_config.py` (append after `test_model_source_list_models_routes_zen`, ~line 1088)

**Interfaces:**
- Consumes: Task 1's `fetch_zen_models(..., url=...)`.
- Produces: `KNOWN_PROVIDERS` containing `"zen-go"`; `_provider_config("zen-go", common) -> ModelConfig(provider="zen-go", model=<MARIM_MODEL or "glm-5.2">, base_url="https://opencode.ai/zen/go/v1", api_key=<OPENCODE_API_KEY or MARIM_API_KEY>)`; `_provider_has_creds("zen-go") -> bool(OPENCODE_API_KEY)`; `build_model` and `ModelSource.list_models` handling `provider == "zen-go"`. `detect_active_providers` picks all of this up automatically (it iterates `KNOWN_PROVIDERS`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (module top already has `import os`, `import pytest`; `load_config` is imported in sibling tests from `marim_harness.config.model` — mirror that style):

```python
def test_load_config_zen_go(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "zen-go")
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen-test")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    monkeypatch.delenv("MARIM_API_KEY", raising=False)
    cfg = load_config()
    assert cfg.provider == "zen-go"
    assert cfg.model == "glm-5.2"
    assert cfg.base_url == "https://opencode.ai/zen/go/v1"
    assert cfg.api_key == "sk-zen-test"


def test_zen_go_shares_the_zen_credential(monkeypatch):
    """One OPENCODE_API_KEY activates BOTH plans (design decision: shared key,
    always active — an unsubscribed key fails clearly at first request)."""
    from marim_harness.config.model import _provider_has_creds, detect_active_providers

    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen-test")
    assert _provider_has_creds("zen-go")
    configs, _default = detect_active_providers()
    assert "zen" in configs and "zen-go" in configs

    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("MARIM_API_KEY", "sk-generic")
    # MARIM_API_KEY alone does NOT activate zen-go (fallback credential only).
    assert not _provider_has_creds("zen-go")


def test_parse_qualified_routes_zen_go():
    from marim_harness.config.model import parse_qualified

    active = {"zen", "zen-go", "openrouter"}
    assert parse_qualified("zen-go:glm-5.2", active, "openrouter") == (
        "zen-go", "glm-5.2")


@pytest.mark.anyio
async def test_model_source_list_models_routes_zen_go(monkeypatch):
    from marim_harness.config import model as model_module
    from marim_harness.config.model import ModelConfig, ModelSource
    from marim_harness.workspace import catalog

    async def fake_fetch(api_key=None, timeout=10.0, *, strict=False, url=None):
        assert api_key == "sk-zen-test"
        assert url == "https://opencode.ai/zen/go/v1/models"
        return [catalog.ModelEntry(id="glm-5.2", name="glm-5.2")]

    # model.py imports the *name* fetch_zen_models, so patch it there.
    monkeypatch.setattr(model_module, "fetch_zen_models", fake_fetch)
    src = ModelSource(ModelConfig(provider="zen-go", model="glm-5.2",
                                  api_key="sk-zen-test"))
    entries = await src.list_models()
    assert [e.id for e in entries] == ["glm-5.2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py -k zen_go -v`
Expected: FAIL — `test_load_config_zen_go` falls through to the openrouter branch (provider == "openrouter" after the unknown-provider warning), the others fail on creds/routing.

- [ ] **Step 3: Implement the provider**

In `src/marim_harness/config/model.py`, five edits:

(a) Constants block (after `_ZEN_BASE_URL`, line 28):

```python
_DEFAULT_ZEN_GO_MODEL = "glm-5.2"
# OpenCode Go: Zen's flat-rate subscription plan. Same account and the same
# OPENCODE_API_KEY, but a separate OpenAI-compatible endpoint whose catalog is
# open coding models only — the subscription changes billing (flat monthly with
# usage windows), not the protocol.
_ZEN_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
```

(b) `KNOWN_PROVIDERS` (line 33):

```python
KNOWN_PROVIDERS = frozenset({"openrouter", "local", "google", "claude-cli", "zen", "zen-go"})
```

(c) `_provider_config` — insert after the `zen` branch (line 346):

```python
    if provider == "zen-go":
        return ModelConfig(
            provider="zen-go",
            model=os.getenv("MARIM_MODEL", _DEFAULT_ZEN_GO_MODEL),
            base_url=_ZEN_GO_BASE_URL,
            api_key=os.getenv("OPENCODE_API_KEY") or os.getenv("MARIM_API_KEY"),
            **common,
        )
```

(d) `_provider_has_creds` — replace the `zen` line (line 388-389):

```python
    if provider in ("zen", "zen-go"):
        # One Zen-account key covers both plans; a key without a Go
        # subscription shows zen-go as active and fails clearly (402/403,
        # surfaced by _actionable_error_note) at first request.
        return bool(os.getenv("OPENCODE_API_KEY"))
```

(e) `build_model` (line 481-482) and `ModelSource.list_models` (after the `zen` branch, line 543):

```python
    if cfg.provider in ("local", "zen", "zen-go"):
        assert cfg.model is not None  # these providers always have a model id
```

```python
        if self.cfg.provider == "zen-go":
            return await fetch_zen_models(
                self.cfg.api_key, strict=strict, url=_ZEN_GO_BASE_URL + "/models")
```

Also update the `ModelConfig.provider` field comment (line 129) to `# "openrouter" | "local" | "google" | "claude-cli" | "zen" | "zen-go"`.

- [ ] **Step 4: Run tests to verify they pass (plus the untouched zen ones)**

Run: `uv run pytest --no-cov tests/test_config.py -k "zen or qualified" -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/model.py tests/test_config.py
git commit -m "feat(config): zen-go provider — OpenCode Go subscription endpoint"
```

---

### Task 3: `zen-go` as a context-limits provider prefix

**Files:**
- Modify: `src/marim_harness/config/context_limits.py:85` (`_PROVIDER_PREFIXES`)
- Test: `tests/test_context_limits.py` (append after `test_bare_id_survives_ollama_style_tags`, ~line 55)

**Interfaces:**
- Consumes: nothing from other tasks (the module deliberately does NOT import `KNOWN_PROVIDERS` — it must stay light; the set is a mirrored literal, per the comment above it).
- Produces: `_bare_id("zen-go:<model>") -> "<model>"`, so per-model window/threshold overrides match both qualified and bare ids.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_context_limits.py`:

```python
def test_bare_id_strips_zen_go_qualifier():
    assert _bare_id("zen-go:glm-5.2") == "glm-5.2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_context_limits.py::test_bare_id_strips_zen_go_qualifier -v`
Expected: FAIL — `_bare_id` returns `"zen-go:glm-5.2"` unchanged (unknown prefix).

- [ ] **Step 3: Add the prefix**

In `src/marim_harness/config/context_limits.py` line 85:

```python
_PROVIDER_PREFIXES = frozenset({"openrouter", "local", "google", "claude-cli", "zen", "zen-go"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_context_limits.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/context_limits.py tests/test_context_limits.py
git commit -m "feat(context-limits): recognize the zen-go qualifier prefix"
```

---

### Task 4: `zen-go` card in the TUI providers section

**Files:**
- Modify: `src/marim_harness/interfaces/tui/providers.py` (module docstring lines 1-2, `PROVIDER_SPECS` ~line 64-71)
- Test: `tests/test_providers_section.py` (`test_provider_specs_env_keys` ~line 46, the card-mount loop ~line 120)

**Interfaces:**
- Consumes: the `ProviderSpec` dataclass already in the module.
- Produces: `PROVIDER_SPECS` order `("openrouter", "google", "zen", "zen-go", "local", "claude-cli")` — the card stack and default-provider radio derive from this tuple; other tests/UI code index it by name via `_SPECS`, so only order-asserting tests need touching.

- [ ] **Step 1: Update the tests (failing first)**

In `tests/test_providers_section.py`, `test_provider_specs_env_keys`: replace the order assertion and add zen-go assertions after the zen block:

```python
    assert [s.name for s in PROVIDER_SPECS] == [
        "openrouter", "google", "zen", "zen-go", "local", "claude-cli"]
```

```python
    # zen-go: SAME key env as zen (one Zen account covers both plans), so
    # removing the key from either card deconfigures both.
    assert specs["zen-go"].write_key == "OPENCODE_API_KEY"
    assert specs["zen-go"].key_fallbacks == ()
    assert specs["zen-go"].read_keys == ("OPENCODE_API_KEY",)
    assert specs["zen-go"].drop_keys == ("OPENCODE_API_KEY",)
    assert specs["zen-go"].base_url_key is None
```

And in the mount test (~line 120), extend the loop tuple:

```python
        for name in ("openrouter", "google", "zen", "zen-go", "local", "claude-cli"):
```

Also update that test's docstring ("all five cards" → "all six cards").

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: FAIL — `test_provider_specs_env_keys` (KeyError/`zen-go` missing) and the mount test (no `#prov-card-zen-go`).

- [ ] **Step 3: Add the spec**

In `src/marim_harness/interfaces/tui/providers.py`, insert after the zen `ProviderSpec` (line 71):

```python
    # zen-go (OpenCode Go, Zen's flat-rate subscription plan): the SAME key
    # env as zen — one Zen-account key covers both plans, so removing the key
    # from either card deconfigures both providers.
    ProviderSpec(
        "zen-go",
        write_key="OPENCODE_API_KEY",
        key_fallbacks=(),
        read_keys=("OPENCODE_API_KEY",),
        drop_keys=("OPENCODE_API_KEY",),
    ),
```

Update the module docstring: "stacked cards for the five built-in providers (openrouter / google / zen / local / claude-cli)" → "stacked cards for the six built-in providers (openrouter / google / zen / zen-go / local / claude-cli)".

- [ ] **Step 4: Run the full providers-section suite**

Run: `uv run pytest --no-cov tests/test_providers_section.py -v`
Expected: PASS (all — the remove/verify tests are name-indexed and unaffected)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py
git commit -m "feat(tui): zen-go provider card (shared OPENCODE_API_KEY)"
```

---

### Task 5: Documentation and changelog

**Files:**
- Modify: `.env.example` (after the OpenCode Zen block, ~line 67), `docs/reference/configuration.md` (`OPENCODE_API_KEY` row ~line 69 + zen paragraph ~line 88-96), `README.md` (feature bullet ~line 50-53 + env table row ~line 184), `CHANGELOG.md` (Unreleased, top)

**Interfaces:** none — prose only; copy the exact snippets below.

- [ ] **Step 1: `.env.example`** — insert after the zen block (after the `MARIM_MODEL=mimo-v2.5-free` line):

```
# --- OpenCode Go (Zen's flat-rate subscription plan; open coding models) ---
# Same OPENCODE_API_KEY as zen — the subscription changes billing, not the key.
# MARIM_PROVIDER=zen-go
# MARIM_MODEL=glm-5.2          # default; catalog: kimi-k3, minimax-m3, deepseek-v4, qwen, ...
```

- [ ] **Step 2: `docs/reference/configuration.md`** — two edits:

(a) `OPENCODE_API_KEY` table row becomes:

```
| `OPENCODE_API_KEY` | unset | OpenCode Zen API key, shared by the `zen` and `zen-go` providers (preferred over `MARIM_API_KEY`). Get one at <https://opencode.ai/auth>. |
```

(b) Append after the zen paragraph (after "including sub-agent tier slugs."):

```
The `zen-go` provider is the same Zen account on OpenCode's flat-rate
[Go subscription plan](https://opencode.ai/docs/go/): a separate
OpenAI-compatible endpoint at `https://opencode.ai/zen/go/v1` whose catalog
is open coding models only (`glm-5.2` default, `kimi-k3`, `minimax-m3`,
`deepseek-v4`, …). It authenticates with the same `OPENCODE_API_KEY`; a key
without an active Go subscription lists the provider but fails clearly at the
first chat request. Billing is flat monthly with usage windows, so marim shows
no per-token cost for it.
```

Also update the generic `MARIM_API_KEY` row's fallback list on line 67: `openrouter`, `google`, and `zen` → `openrouter`, `google`, `zen`, and `zen-go`.

- [ ] **Step 3: `README.md`** — two edits:

(a) Feature bullet: change `OpenRouter, Google, OpenCode Zen (free
  models available), any local OpenAI-compatible server (Ollama, LM Studio)` to `OpenRouter, Google, OpenCode Zen (free
  models available) and its flat-rate Go plan, any local OpenAI-compatible server (Ollama, LM Studio)`.

(b) Env table row: `API key for OpenCode Zen` → `API key for OpenCode Zen / Go`.

- [ ] **Step 4: `CHANGELOG.md`** — add at the top of `## [Unreleased]`:

```
- New `zen-go` provider: OpenCode Go, Zen's flat-rate subscription plan, via
  its OpenAI-compatible endpoint — `MARIM_PROVIDER=zen-go` with the same
  `OPENCODE_API_KEY` as `zen`, default model `glm-5.2` (open coding models
  only). Catalog, settings card, and qualified `zen-go:<model>` ids included.
```

- [ ] **Step 5: Doc-lint check and commit**

Run: `uv run pytest --no-cov tests/test_docs_reference.py -v`
Expected: PASS (this suite link-checks/lints the reference docs; a broken anchor or table row fails here)

```bash
git add .env.example docs/reference/configuration.md README.md CHANGELOG.md
git commit -m "docs: zen-go provider (OpenCode Go subscription)"
```

---

### Task 6: Full gates + live smoke test

**Files:** none created — verification only.

**Interfaces:** consumes everything above; the live steps need `OPENCODE_API_KEY` in the environment (the machine key lives in `~/.config/marim/.env`; export it into the shell for the one-shot below if not already present).

- [ ] **Step 1: Full local CI, in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all three clean (pytest with coverage on, as configured).

- [ ] **Step 2: Live catalog probe (no key needed)**

```bash
MARIM_PROVIDER=zen-go uv run marim models list
```

(`marim models list` lists models for the active providers; entries are provider-stamped.)
Expected: `zen-go`-stamped entries showing the Go catalog (glm-5.2, kimi-k3, minimax-m3, …) — NOT `claude-*` ids (those belong to the plain `zen` catalog).

- [ ] **Step 3: Live headless one-shot on the cheapest Go model**

Run from a scratch directory — a session in this repo would inherit the last session's model, which overrides `MARIM_MODEL` (known gotcha):

```bash
export OPENCODE_API_KEY=$(grep -oP '(?<=^OPENCODE_API_KEY=).*' ~/.config/marim/.env)
d=$(mktemp -d) && cd "$d" && \
MARIM_PROVIDER=zen-go MARIM_MODEL=deepseek-v4-flash \
  uv run --project /home/mateuscmarim/Projects/marim.dev/marim-harness/.claude/worktrees/oss-hygiene \
  marim -p "Reply with exactly: ok"
```

Expected: a completed turn printing a reply containing "ok"; no auth/404 errors. If it 402/403s, confirm the key's Go subscription is active before debugging code.

- [ ] **Step 4: Report results**

Paste the three gate outputs and the smoke reply into the final summary. Do not claim done on a red gate.
