# OpenCode Zen provider (`zen`) — design

Date: 2026-07-23. Status: approved.

## Goal

A first-class `zen` provider for [OpenCode Zen](https://opencode.ai/docs/zen/),
opencode's curated model gateway. Motivation: free daily-driver models
(`mimo-v2.5-free`, `big-pickle`, …) so small tasks cost nothing, consistent
with marim's zero-key-quick-start posture.

## Decisions

- **Scope: OpenAI-compatible models only.** Zen serves three API shapes under
  `https://opencode.ai/zen/v1` — OpenAI (`/chat/completions`), Anthropic
  (`/messages` — Claude and Qwen ids), Google (Gemini ids). marim covers only
  the OpenAI shape: one `OpenAIChatModel`, no new model class. Claude/Gemini
  ids are filtered out of the catalog so the picker never offers a model that
  would hit the wrong endpoint shape.
- **Name: `zen`.** `MARIM_PROVIDER=zen`; qualified ids read `zen:<model-id>`
  (e.g. `zen:mimo-v2.5-free`).
- **Default model: `mimo-v2.5-free`** — free, and MiMo-V2.5 is already proven
  with marim's tool-calling (it drove the README demo recording).
- **Credentials: `OPENCODE_API_KEY`**, falling back to `MARIM_API_KEY`.
  `_provider_has_creds("zen")` keys off `OPENCODE_API_KEY` only (same pattern
  as openrouter/google: the provider-specific env is the activation signal).
- **Base URL is a fixed constant** (`https://opencode.ai/zen/v1`), not
  `MARIM_BASE_URL`-overridable — that env belongs to the `local` provider and
  both can be active at once.

## Changes by file

- `src/marim_harness/config/model.py` — `"zen"` in `KNOWN_PROVIDERS`;
  `_DEFAULT_ZEN_MODEL = "mimo-v2.5-free"`; `_provider_config` branch;
  `_provider_has_creds` branch; `build_model` branch mirroring the `local`
  arm (`OpenAIProvider(base_url=…, api_key=…)` + `OpenAIChatModel`);
  `ModelSource.list_models` branch → `fetch_zen_models`.
- `src/marim_harness/workspace/catalog.py` — `fetch_zen_models(api_key,
  strict)`: GET `/zen/v1/models` (public; standard OpenAI list shape, no
  pricing/family metadata), drop ids starting `claude-`/`gemini-`, return
  id-only `ModelEntry` rows. `strict=True` raises on failure (provider
  verification), else degrades to `[]`.
- TUI providers settings — a `zen` row on the existing pattern: key field
  bound to `OPENCODE_API_KEY`, verify probe via `list_models(strict=True)`.
- Docs — `.env.example`, README configuration table, `docs/reference/
  configuration.md` (doc-lint enforces env-var completeness), CLAUDE.md
  provider list, CHANGELOG Unreleased.

## Testing

- Config: `MARIM_PROVIDER=zen` selection, default model, `OPENCODE_API_KEY`
  → `MARIM_API_KEY` fallback, creds detection on/off.
- Catalog: mocked HTTP — happy path, `claude-*`/`gemini-*` filtered, free
  ids present, strict-mode raise vs non-strict `[]` (mirror the existing
  `fetch_openrouter_models` test style).
- Settings row: whatever the existing per-provider row tests cover.
- Live smoke (after implementation, before PR merge): `MARIM_PROVIDER=zen` headless one-shot
  against `mimo-v2.5-free` exercising a real tool call. Zero spend; the
  user's key is temporary and disabled after.

## Out of scope

- Anthropic/Google-shaped zen models (revisit if demand shows up).
- Zen-specific cost tracking — the `/models` response carries no pricing.
- Auto-defaulting to zen when its key is present; the default provider
  stays OpenRouter.

## Notes

- Qualified ids make `zen:<free-model>` immediately valid as a sub-agent
  tier slug — free-tier routing falls out with no extra code.
