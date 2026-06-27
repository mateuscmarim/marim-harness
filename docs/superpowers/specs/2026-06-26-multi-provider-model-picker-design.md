# Multi-provider model picker — design

**Date:** 2026-06-26
**Status:** Approved (brainstorm), pending implementation plan

## Problem

Today marim is single-provider. `MARIM_PROVIDER` selects one of `openrouter |
google | local`; `load_config()` builds one `ModelConfig`; `Harness` holds one
`ModelSource`; the picker and `marim models list` only ever show that one
provider's catalog. To use a different provider you change the env var and
restart, and there's no way to see models from more than one provider at once or
to tell which provider a model belongs to.

## Goal

A single unified, filterable model picker that lists models from **all
configured providers at once**, each row tagged with its provider, where picking
routes the build to the right provider — and that choice survives resume.

## Decisions (locked during brainstorm)

1. **Provider activation: auto-detect from env.** A provider is "active" if its
   credentials are present — no new config knob. `MARIM_PROVIDER` becomes the
   *default* provider (startup model + fallback for bare ids), not the only one.
2. **Picker layout: flat list with inline provider tags.** One filterable list;
   each row shows `· {provider}` as a muted suffix; filtering matches id, name,
   AND provider.
3. **Free-text routing: prefix form, else default.** Typing `openrouter:anthropic/
   claude-opus-4-8` or `local:some-model` targets a provider explicitly; a bare id
   (or one whose prefix isn't a known active provider) routes to the default
   provider.
4. **Qualified-id separator: colon.** `provider:model_id`. Slash is ambiguous
   because OpenRouter ids are themselves `vendor/model` (e.g. `google/gemma-2-9b`
   would collide with the `google` provider). Model ids don't contain `:`.

## Architecture

Introduce **`MultiModelSource`**, a composite that implements the *same*
interface `Harness` already depends on (`list_models()`, `build(id)`,
`label(id)`, `is_local`) and holds one per-provider `ModelSource` sub-source.
`Harness.model_source` becomes this composite whenever ≥1 provider is active.
Everything downstream — picker, `_on_model_chosen`, session store — keeps talking
to `model_source` through the unchanged interface.

### Qualified id

`provider:model_id`, e.g. `openrouter:anthropic/claude-sonnet-4-6`,
`google:gemini-2.5-pro`, `local:qwen2.5-coder`.

**Parse rule** (`split(":", 1)`): if the left segment is a known **active**
provider, route there with the right segment as the bare id; otherwise treat the
**whole string** as a bare id on the **default** provider. This single rule
delivers backward compatibility (old sessions store a bare id), `MARIM_MODEL`
support (bare id on `MARIM_PROVIDER`), and forgiving free-text.

## Components / touch-points

### 1. Config — `config/model.py`

- Add `detect_active_providers() -> list[ModelConfig]`: one `ModelConfig` per
  provider whose creds are present:
  - `openrouter` when `OPENROUTER_API_KEY` is set,
  - `google` when `GOOGLE_API_KEY` or `GEMINI_API_KEY` is set,
  - `local` when `MARIM_BASE_URL` is set.
  Each carries its existing env-derived fields (model, base_url, api_key, plus the
  shared `common` knobs).
- The provider named by `MARIM_PROVIDER` (default `openrouter`) is the **default**.
  If `MARIM_PROVIDER`'s own creds are absent but it's the configured default, it is
  still included so startup has a home (it may simply return an empty catalog).
- When exactly one provider is active, the composite wraps a single sub-source and
  behavior is identical to today (modulo qualified-id labels — see Compatibility).

### 2. Catalog — `workspace/catalog.py`

- Add `provider: str | None = None` to `ModelEntry` (kept `None` by the raw
  parsers; stamped by the composite).
- `parse_models` / `parse_google_models` and the three `fetch_*` functions are
  unchanged; the composite is responsible for stamping `provider` on the entries a
  sub-source returns.

### 3. `MultiModelSource` — `config/model.py` (new)

- Constructor takes the list of sub-sources and the name of the default.
- `list_models()`: `asyncio.gather` over sub-sources' `list_models()`; stamp each
  result's entries with that sub-source's provider; concatenate. A sub-source that
  raises or returns `[]` contributes nothing — the others still list. Entries keep
  the existing per-provider alpha sort; providers interleave in the merged list
  (the tag + filter handle grouping).
- `build(qualified)`: parse → delegate to the matching sub-source's
  `build(bare_id)`; unknown/bare prefix → default sub-source.
- `label(qualified)`: return the qualified id as-is when already provider-prefixed;
  otherwise `f"{default}:{id}"`. The status bar shows the qualified id.
- `is_local`: the composite is not "a local provider"; expose this such that the
  picker always allows free-text (so the user can type a `provider:id` prefix).
- Keep the single-provider `ModelSource` as-is; the composite is built around it.

### 4. Picker — `interfaces/tui/model_picker.py`

- Render each row's provider as a muted `· {provider}` suffix when
  `entry.provider` is set.
- `filter_entries` also matches on `entry.provider` (so typing `local` or
  `openrouter` narrows by provider).
- The OptionList **option id is the qualified id** (`provider:model_id`) when the
  entry has a provider, so the value handed back to `_on_model_chosen` (→
  `set_model`) routes correctly. Bare entries (single-provider, no tag) keep using
  the bare id.
- Free-text stays enabled for the composite so a `provider:id` can be typed even
  when catalogs loaded.

### 5. Persistence — `session/`

- **No schema change.** `store.model` simply holds the qualified id string now.
  `set_model` / `model_label` already round-trip through `model_source`, so
  qualified ids flow through unchanged. On resume, a stored bare id (old session)
  resolves on the default provider via the parse rule.

## Error handling

- **No providers detected at all** → fall back to today's single-provider
  construction from `MARIM_PROVIDER`; if that too has nothing, surface a clear "no
  providers configured" rather than crashing.
- **One provider's catalog fails** (missing key, server down) → it contributes
  `[]`; no error surfaced for the common "only one key set" case. The picker shows
  whatever loaded.
- **Typed id with an unknown `provider:` prefix** → treated as a bare id on the
  default provider (forgiving), never an error.

## Compatibility

- Existing single-provider setups keep working: one active provider → composite of
  one. The visible change is the status-bar/picker label gains a `provider:`
  prefix and the picker shows a (single) provider tag.
- Existing sessions: `store.model` holds a bare id → resolves on the default
  provider. No migration needed.
- `marim models list` already calls `source.list_models()`; with a composite it
  now lists every active provider's models, each tagged.

## Testing (TDD)

Unit, no network unless mocked:

- `detect_active_providers`: env-var combinations → expected set of `ModelConfig`s
  and which is default.
- `MultiModelSource.list_models`: two stub sub-sources → merged, each entry
  provider-stamped; one raising → still returns the other's entries.
- `MultiModelSource.build`: `openrouter:x/y` → openrouter sub-source with `x/y`;
  `local:m` → local with `m`; bare id and unknown-prefix id → default sub-source.
- `MultiModelSource.label`: qualified id passes through; bare id gains the default
  prefix.
- Catalog: `ModelEntry.provider` defaults `None`; composite stamping sets it.
- Picker: option ids are qualified for tagged entries; `filter_entries` matches
  provider; the `· provider` suffix renders.
- Backward compat: a session whose `store.model` is a bare id builds on the default
  provider.
- CLI-startup invariant (`tests/test_cli_startup.py`) stays green — composite
  construction must not eagerly import `pydantic_ai` / `httpx`.

## Scope guard (YAGNI)

- Single colon separator; no per-provider config file.
- No support for two endpoints of the same provider type (e.g. two OpenRouter-style
  base URLs) — that's the providers-config-file approach we explicitly deferred.
- No model favoriting / custom sort beyond the existing per-provider alpha sort.
- No new UI grouping/headers — flat list + tag only.
