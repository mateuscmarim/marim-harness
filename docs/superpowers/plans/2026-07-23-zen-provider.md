# OpenCode Zen Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A first-class `zen` provider (OpenCode Zen, opencode's model gateway) so `MARIM_PROVIDER=zen` + `OPENCODE_API_KEY` gives free daily-driver models.

**Architecture:** Zen is OpenAI-compatible at a fixed base URL, so the provider reuses `OpenAIChatModel` + `OpenAIProvider` (same as `local`) — no new model class. The catalog comes from Zen's public `/models` endpoint, filtered to drop ids that Zen routes to non-OpenAI endpoint shapes (`claude-*`, `gemini-*`). The TUI settings card is one declarative `ProviderSpec` entry.

**Tech Stack:** Python 3.10+, pydantic-ai (`OpenAIChatModel`/`OpenAIProvider`), httpx (lazy import), Textual (spec-driven providers pane), pytest.

**Spec:** `docs/superpowers/specs/2026-07-23-zen-provider-design.md`

## Global Constraints

- Run everything through `uv` (`uv run pytest`, `uv run ruff check src tests`, `uv run pyright`). Never bare `python`/`pip`.
- `requires-python = ">=3.10"` — no 3.11+-only syntax.
- Ruff line length 100; cyclomatic complexity ≤ 10 (C901) — no blanket `noqa`.
- Provider name is exactly `zen`; default model `mimo-v2.5-free`; base URL constant `https://opencode.ai/zen/v1`; key env `OPENCODE_API_KEY` with `MARIM_API_KEY` fallback.
- Catalog excludes ids starting with `claude-` or `gemini-` (they route to Anthropic/Google endpoint shapes marim doesn't cover for zen).
- Work on branch `feat/zen-provider`. Commit only the files each task names.
- CWD is the worktree root `/home/mateuscmarim/Projects/marim.dev/marim-harness/.claude/worktrees/oss-hygiene`; all file paths below are relative to it. File tools need absolute paths under this root.

---

### Task 1: Config layer — `zen` in provider selection and model building

**Files:**
- Modify: `src/marim_harness/config/model.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `"zen" in KNOWN_PROVIDERS`; `load_config()` returns `ModelConfig(provider="zen", model="mimo-v2.5-free", base_url="https://opencode.ai/zen/v1", api_key=<OPENCODE_API_KEY or MARIM_API_KEY>)` when `MARIM_PROVIDER=zen`; `_provider_has_creds("zen")` is True iff `OPENCODE_API_KEY` is set; `build_model` handles `provider == "zen"` via the OpenAI chat path. Task 3 relies on `ModelConfig.provider == "zen"` reaching `ModelSource.list_models`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py` (match the file's existing style; it uses `monkeypatch` and imports `load_config` etc. at top — check the imports and reuse them):

```python
def test_load_config_zen(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "zen")
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen-test")
    monkeypatch.delenv("MARIM_MODEL", raising=False)
    monkeypatch.delenv("MARIM_API_KEY", raising=False)
    cfg = load_config()
    assert cfg.provider == "zen"
    assert cfg.model == "mimo-v2.5-free"
    assert cfg.base_url == "https://opencode.ai/zen/v1"
    assert cfg.api_key == "sk-zen-test"


def test_load_config_zen_key_falls_back_to_marim_api_key(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "zen")
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("MARIM_API_KEY", "sk-generic")
    cfg = load_config()
    assert cfg.api_key == "sk-generic"


def test_load_config_zen_model_override(monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "zen")
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen-test")
    monkeypatch.setenv("MARIM_MODEL", "big-pickle")
    cfg = load_config()
    assert cfg.model == "big-pickle"


def test_zen_creds_detection(monkeypatch):
    from marim_harness.config.model import _provider_has_creds

    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen-test")
    assert _provider_has_creds("zen")
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    # MARIM_API_KEY alone does NOT activate zen (fallback credential only,
    # same pattern as openrouter's MARIM_API_KEY fallback).
    monkeypatch.setenv("MARIM_API_KEY", "sk-generic")
    assert not _provider_has_creds("zen")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py -q -k zen`
Expected: FAIL — `load_config` falls back to openrouter for unknown provider `zen` (asserts on `provider == "zen"` fail).

- [ ] **Step 3: Implement in `src/marim_harness/config/model.py`** — five edits:

(a) Near the other `_DEFAULT_*` constants (top of file):

```python
_DEFAULT_ZEN_MODEL = "mimo-v2.5-free"
# OpenCode Zen's OpenAI-compatible endpoint root. Fixed, not MARIM_BASE_URL —
# that env belongs to the `local` provider and both can be active at once.
_ZEN_BASE_URL = "https://opencode.ai/zen/v1"
```

(b) `KNOWN_PROVIDERS = frozenset({"openrouter", "local", "google", "claude-cli", "zen"})`

(c) In `_provider_config`, add a branch (place it after the `"local"` branch):

```python
    if provider == "zen":
        return ModelConfig(
            provider="zen",
            model=os.getenv("MARIM_MODEL", _DEFAULT_ZEN_MODEL),
            base_url=_ZEN_BASE_URL,
            api_key=os.getenv("OPENCODE_API_KEY") or os.getenv("MARIM_API_KEY"),
            **common,
        )
```

(d) In `_provider_has_creds`, add (before the final `return False`):

```python
    if provider == "zen":
        return bool(os.getenv("OPENCODE_API_KEY"))
```

(e) In `build_model`, widen the existing `local` arm — zen is the same OpenAI-compatible construction:

```python
    if cfg.provider in ("local", "zen"):
        assert cfg.model is not None  # local/zen always have a model id
        provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key)
        return OpenAIChatModel(cfg.model, provider=provider)
```

Also update the `ModelConfig.provider` field comment to `# "openrouter" | "local" | "google" | "claude-cli" | "zen"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_config.py -q`
Expected: all PASS (the new 4 plus every pre-existing test — the file's other tests must not regress).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/model.py tests/test_config.py
git commit -m "feat(config): zen provider — OpenCode Zen via the OpenAI-compatible path"
```

---

### Task 2: Trust boundary — `OPENCODE_API_KEY` in the project-env blocklist

**Files:**
- Modify: `src/marim_harness/config/env.py`
- Test: `tests/test_env_blocklist.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: a project-local `.env` cannot set `OPENCODE_API_KEY` (same supply-chain guard as `OPENROUTER_API_KEY`/`GOOGLE_API_KEY` — a cloned repo must not be able to swap the credential a model request carries).

- [ ] **Step 1: Write the failing test** — in `tests/test_env_blocklist.py`, the test `test_blocklist_contains_all_provider_keys` (~line 125) iterates a literal tuple of key names. Add `"OPENCODE_API_KEY",` to that tuple, right after `"GEMINI_API_KEY",`. Also extend the project-.env attack-simulation test near the top of the file (~lines 64–90): it writes a `.env` containing `OPENROUTER_API_KEY=attacker` / `GEMINI_API_KEY=attacker` and asserts each key never lands in `os.environ` — add an `OPENCODE_API_KEY=attacker` line and the matching key in both assertion lists.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_env_blocklist.py -q`
Expected: FAIL — `OPENCODE_API_KEY` not in `_PROJECT_ENV_BLOCKLIST`.

- [ ] **Step 3: Implement** — in `src/marim_harness/config/env.py`, add `"OPENCODE_API_KEY",` to `_PROJECT_ENV_BLOCKLIST`, right after `"GEMINI_API_KEY",` (it belongs to the provider/credential group whose rationale comment already covers it).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_env_blocklist.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/env.py tests/test_env_blocklist.py
git commit -m "feat(config): OPENCODE_API_KEY joins the project-env credential blocklist"
```

---

### Task 3: Catalog — `fetch_zen_models` with family filtering

**Files:**
- Modify: `src/marim_harness/workspace/catalog.py`
- Modify: `src/marim_harness/config/model.py` (the `ModelSource.list_models` branch + import)
- Test: `tests/test_catalog.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `ModelEntry`, `parse_models` (both already in `catalog.py`); Task 1's `ModelConfig(provider="zen")`.
- Produces: `parse_zen_models(payload: dict) -> list[ModelEntry]` (pure) and `async fetch_zen_models(api_key: str | None = None, timeout: float = 10.0, *, strict: bool = False) -> list[ModelEntry]`. `ModelSource.list_models` returns zen entries when `cfg.provider == "zen"`. Task 4's verify badge calls this via `list_models(strict=True)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_catalog.py` (mirror the existing openrouter strict/non-strict tests around lines 101–111 for the fetch pair; check what they monkeypatch and copy the mechanism):

```python
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
    with pytest.raises(Exception):
        await catalog.fetch_zen_models("sk-x", strict=True)


@pytest.mark.anyio
async def test_fetch_zen_models_non_strict_returns_empty(monkeypatch):
    monkeypatch.setattr(catalog, "_ZEN_MODELS_URL", "http://127.0.0.1:9/x")
    assert await catalog.fetch_zen_models("sk-x") == []
```

The file already uses `@pytest.mark.anyio` for async tests (the `anyio_backend` fixture lives in `tests/conftest.py`) and imports the `catalog` module plus names from `marim_harness.workspace.catalog` at top — extend that import block with `parse_zen_models`.

Also append to `tests/test_config.py`:

```python
@pytest.mark.anyio
async def test_model_source_list_models_routes_zen(monkeypatch):
    from marim_harness.config.model import ModelConfig, ModelSource
    from marim_harness.workspace import catalog

    async def fake_fetch(api_key=None, timeout=10.0, *, strict=False):
        assert api_key == "sk-zen-test"
        return [catalog.ModelEntry(id="mimo-v2.5-free", name="mimo-v2.5-free")]

    monkeypatch.setattr(catalog, "fetch_zen_models", fake_fetch)
    src = ModelSource(ModelConfig(provider="zen", model="mimo-v2.5-free",
                                  api_key="sk-zen-test"))
    entries = await src.list_models()
    assert [e.id for e in entries] == ["mimo-v2.5-free"]
```

(`@pytest.mark.anyio` works in `tests/test_config.py` too — the `anyio_backend` fixture is in `tests/conftest.py`. Patch `fetch_zen_models` where `ModelSource.list_models` looks it up: if `model.py` imports the *name* (`from ..workspace.catalog import fetch_zen_models`), monkeypatch it on `marim_harness.config.model`, not on `catalog`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_catalog.py tests/test_config.py -q -k zen`
Expected: FAIL — `parse_zen_models` / `fetch_zen_models` don't exist.

- [ ] **Step 3: Implement in `src/marim_harness/workspace/catalog.py`** — add near the other URL constants:

```python
_ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
# Zen serves three API shapes under one roof; marim's zen provider speaks only
# the OpenAI-compatible one, so ids Zen routes to the Anthropic/Google shapes
# are filtered out of the catalog — the picker must not offer a model the
# provider's chat/completions path can't actually drive.
_ZEN_EXCLUDED_PREFIXES = ("claude-", "gemini-")
```

and, after `fetch_local_models`:

```python
def parse_zen_models(payload: dict) -> list[ModelEntry]:
    """Turn Zen's ``/models`` response (standard OpenAI list shape, id-only —
    no pricing/context metadata) into sorted entries, dropping ids that route
    to non-OpenAI endpoint shapes (see _ZEN_EXCLUDED_PREFIXES)."""
    return [
        e for e in parse_models(payload)
        if not e.id.startswith(_ZEN_EXCLUDED_PREFIXES)
    ]


async def fetch_zen_models(
    api_key: str | None = None, timeout: float = 10.0, *, strict: bool = False
) -> list[ModelEntry]:
    """Fetch the OpenCode Zen catalog. Returns ``[]`` on any failure so the
    picker degrades to free-text entry, unless ``strict=True`` (verification
    needs the real error), in which case the exception is re-raised. Note:
    ``/models`` is public, so strict mode verifies *connectivity*, not the
    key — Zen has no known key-validation endpoint (unlike OpenRouter's
    ``/key``); a bad key surfaces at first chat request instead. httpx is
    imported lazily to keep the import chain light."""
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_ZEN_MODELS_URL, headers=headers)
            response.raise_for_status()
            return parse_zen_models(response.json())
    except Exception as exc:
        if strict:
            raise
        logger.warning("failed to fetch OpenCode Zen model catalog: %s", exc)
        return []
```

Update the module docstring's first line to mention Zen alongside OpenRouter and Google.

In `src/marim_harness/config/model.py`: extend the `from ..workspace.catalog import (...)` block with `fetch_zen_models`, and add to `ModelSource.list_models` (before the final `return []`):

```python
        if self.cfg.provider == "zen":
            return await fetch_zen_models(self.cfg.api_key, strict=strict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_catalog.py tests/test_config.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/catalog.py src/marim_harness/config/model.py tests/test_catalog.py tests/test_config.py
git commit -m "feat(catalog): OpenCode Zen model catalog with non-OpenAI-family filtering"
```

---

### Task 4: TUI settings — a `zen` provider card

**Files:**
- Modify: `src/marim_harness/interfaces/tui/providers.py`
- Test: `tests/test_providers_section.py`

**Interfaces:**
- Consumes: `_provider_has_creds("zen")` semantics from Task 1 (card "configured" = `OPENCODE_API_KEY` set); `fetch_zen_models` via `ModelSource.list_models(strict=True)` from Task 3 (the verify badge).
- Produces: a `ProviderSpec("zen", ...)` entry in `PROVIDER_SPECS` — the pane renders the card, key commit, remove button, and default-radio entry entirely from the spec.

- [ ] **Step 1: Write the failing test** — `tests/test_providers_section.py` has `test_provider_specs_env_keys` (~line 45). It asserts the EXACT spec-name order:

```python
    assert [s.name for s in PROVIDER_SPECS] == [
        "openrouter", "google", "local", "claude-cli"]
```

Update that literal to `["openrouter", "google", "zen", "local", "claude-cli"]` (zen goes after google — see Step 3), and append to the same test, following its per-provider comment style:

```python
    # zen: one canonical key env, fixed endpoint (no base URL row).
    assert specs["zen"].write_key == "OPENCODE_API_KEY"
    assert specs["zen"].key_fallbacks == ()
    assert specs["zen"].read_keys == ("OPENCODE_API_KEY",)
    assert specs["zen"].drop_keys == ("OPENCODE_API_KEY",)
    assert specs["zen"].base_url_key is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_providers_section.py -q`
Expected: FAIL — the name-order assertion (no `zen` in `PROVIDER_SPECS` yet).

- [ ] **Step 3: Implement** — in `PROVIDER_SPECS`, insert after the `google` entry:

```python
    # zen (OpenCode Zen): one canonical key env, no base URL (fixed endpoint).
    ProviderSpec(
        "zen",
        write_key="OPENCODE_API_KEY",
        key_fallbacks=(),
        read_keys=("OPENCODE_API_KEY",),
        drop_keys=("OPENCODE_API_KEY",),
    ),
```

Update the module docstring's first sentence: "stacked cards for the five built-in providers (openrouter / google / zen / local / claude-cli)".

- [ ] **Step 4: Run the section's tests**

Run: `uv run pytest --no-cov tests/test_providers_section.py -q`
Expected: all PASS — the pane tests iterate `PROVIDER_SPECS`, so the mount/paint tests exercise the new card for free. If `test_pane_mounts_all_cards_without_writing_env` asserts a card *count*, update the expectation to include zen.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/providers.py tests/test_providers_section.py
git commit -m "feat(tui): zen provider card in the settings Providers section"
```

---

### Task 5: Documentation

**Files:**
- Modify: `.env.example`, `docs/reference/configuration.md`, `README.md`, `CLAUDE.md`, `CHANGELOG.md`
- Test: `tests/test_docs_reference.py` (existing doc-lint; no new test code)

**Interfaces:**
- Consumes: the exact names/values from Tasks 1–3 (`zen`, `OPENCODE_API_KEY`, `mimo-v2.5-free`, `https://opencode.ai/zen/v1`).

- [ ] **Step 1: `.env.example`** — add a zen block following the file's per-provider comment style (see the local/google blocks around lines 14–60):

```bash
# --- OpenCode Zen (opencode's model gateway; free models available) ---
# MARIM_PROVIDER=zen
# OPENCODE_API_KEY=
# MARIM_MODEL=mimo-v2.5-free   # default; other free ids: big-pickle, deepseek-v4-flash-free, ...
```

- [ ] **Step 2: `docs/reference/configuration.md`** — three edits: add `OPENCODE_API_KEY` to the credentials list at line ~30; change the `MARIM_PROVIDER` table row to `` `openrouter`, `local`, `google`, `zen`, or `claude-cli` ``; add a table row after `OPENROUTER_API_KEY`:

```markdown
| `OPENCODE_API_KEY` | unset | OpenCode Zen API key (preferred over `MARIM_API_KEY`). Get one at <https://opencode.ai/auth>. |
```

If the file has a per-provider prose section (read it), add a short zen paragraph there: OpenAI-compatible gateway at `https://opencode.ai/zen/v1`, default model `mimo-v2.5-free`, catalog filtered to OpenAI-compatible ids (`claude-*`/`gemini-*` route to other endpoint shapes and are hidden), free ids carry a `-free` suffix.

- [ ] **Step 3: `README.md` + `CLAUDE.md`** — README: extend the `MARIM_PROVIDER` row of the configuration table (~line 180) with `zen`, and if the Providers feature bullet enumerates providers, add "OpenCode Zen (free models available)". CLAUDE.md line ~32: `(`openrouter`|`local`|`google`|`claude-cli`|`zen`)`.

- [ ] **Step 4: `CHANGELOG.md`** — under `## [Unreleased]`:

```markdown
- New `zen` provider: OpenCode Zen (opencode's model gateway) via its
  OpenAI-compatible endpoint — `MARIM_PROVIDER=zen` + `OPENCODE_API_KEY`,
  default model `mimo-v2.5-free` (free tier). Catalog, settings card, and
  qualified `zen:<model>` ids included.
```

- [ ] **Step 5: Run doc-lint + full checks**

Run: `uv run pytest --no-cov tests/test_docs_reference.py -q`
Expected: PASS (`test_every_env_var_is_documented` now sees `OPENCODE_API_KEY` documented; the relative-link checker stays green).

- [ ] **Step 6: Commit**

```bash
git add .env.example docs/reference/configuration.md README.md CLAUDE.md CHANGELOG.md
git commit -m "docs: zen provider — env reference, README, CLAUDE.md, changelog"
```

---

### Task 6: Full verification + live smoke

**Files:** none new — verification only.

- [ ] **Step 1: CI-order checks**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest -q`
Expected: ruff clean; pyright 0 errors; full suite green with coverage ≥ 90%.

- [ ] **Step 2: Live catalog sanity (no key needed)**

```bash
uv run python -c "
import asyncio
from marim_harness.workspace.catalog import fetch_zen_models
entries = asyncio.run(fetch_zen_models())
ids = [e.id for e in entries]
print(len(ids), 'models;', 'mimo-v2.5-free' in ids, not any(i.startswith(('claude-','gemini-')) for i in ids))
"
```

Expected: a positive count, `True True` (default model present, no filtered families).

- [ ] **Step 3: Live smoke (needs the user's key in the environment)** — the key is expected in the machine env file (`~/.config/marim/.env`, `OPENCODE_API_KEY=...`). Then:

```bash
cd /tmp/claude-1000 && mkdir -p zen-smoke && cd zen-smoke
MARIM_PROVIDER=zen MARIM_MODEL=mimo-v2.5-free uv run --project /home/mateuscmarim/Projects/marim.dev/marim-harness/.claude/worktrees/oss-hygiene marim -p "Create a file named hello.txt containing exactly 'zen works', then read it back and tell me its contents." --mode auto
```

Expected: the run completes, `hello.txt` exists with `zen works` — proving a real tool-calling turn through Zen. If the session-model-override gotcha bites (a stale session model shadowing the env — known behavior), use a fresh empty workspace dir as shown. Zero spend: `mimo-v2.5-free` is a free model.

- [ ] **Step 4: Report** — summarize results; the controller (main session) pushes the branch and opens the PR.
