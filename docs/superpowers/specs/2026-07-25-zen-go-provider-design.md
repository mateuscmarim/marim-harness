# zen-go provider (OpenCode Go subscription) — design

**Date:** 2026-07-25
**Status:** approved (brainstorm 2026-07-25)

## Problem

OpenCode Go is OpenCode's flat-rate subscription plan ($5 first month, then
$10/month) for open coding models. The user wants to drive marim's main loop
on it. Although it is subscription-billed, Go is *not* a CLI-locked
subscription like Claude Pro/Max (the reason `claude-cli` is a launcher
provider): the same Zen account API key works against a plain
OpenAI-compatible endpoint, so it fits marim's existing OpenAI-compatible
provider path directly.

Verified facts (probed 2026-07-25):

- Base URL: `https://opencode.ai/zen/go/v1` (chat completions; Zen's is
  `https://opencode.ai/zen/v1`).
- `GET https://opencode.ai/zen/go/v1/models` is live, unauthenticated, and
  returns the same payload shape Zen's `/models` does — `parse_zen_models`
  works unchanged.
- Catalog is a distinct ~16-model subset of open models: `glm-5.2`, `glm-5.1`,
  `glm-5`, `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.6`, `kimi-k2.5`,
  `minimax-m3`, `minimax-m2.7`, `minimax-m2.5`, deepseek-v4 variants, qwen,
  etc. No Claude/frontier closed models (those stay on `zen`).
- Auth: the *same* `OPENCODE_API_KEY` as Zen; the subscription only changes
  billing (flat rate with 5-hour/weekly/monthly usage windows instead of
  pay-per-token; optional fallback to Zen balance is an account-side setting).

## Decisions (from brainstorm)

1. **Separate provider `zen-go`**, not a knob on `zen` and not a merged
   catalog. It gets its own row in the provider picker and its own
   `MARIM_PROVIDER` value, matching the user's mental model ("I subscribed to
   Go") and letting sessions flip between Zen pay-as-you-go and Go models.
2. **Shared key, always active.** `_provider_has_creds("zen-go")` is the same
   `OPENCODE_API_KEY` check as `zen`. A Zen-only (unsubscribed) key shows the
   provider as active; the first request fails with a clear 402/403, which
   `_actionable_error_note` already surfaces (4xx client errors are
   model-visible). No new env var, no startup subscription probe.
3. **Live verification is in scope** — the user has an active Go subscription
   key on this machine.

## Changes

### `config/model.py`

- `KNOWN_PROVIDERS` += `"zen-go"`.
- `_ZEN_GO_BASE_URL = "https://opencode.ai/zen/go/v1"`;
  `_DEFAULT_ZEN_GO_MODEL = "glm-5.2"`.
- `_provider_config`: a `zen-go` branch mirroring `zen` (same
  `OPENCODE_API_KEY` / `MARIM_API_KEY` fallback, Go base URL, Go default
  model).
- `_provider_has_creds`: `zen-go` → `bool(os.getenv("OPENCODE_API_KEY"))`.
- `build_model`: treat `zen-go` like `local`/`zen` (OpenAI-compatible model
  over the configured base URL).
- `ModelSource.list_models`: `zen-go` → `fetch_zen_models` pointed at the Go
  models URL.

### `workspace/catalog.py`

- Parametrize the zen fetcher with a `url=` keyword (default: existing
  `_ZEN_MODELS_URL`) instead of duplicating it; add
  `_ZEN_GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"`. Returned
  `ModelEntry.provider` is `"zen-go"` for Go results.

### `config/context_limits.py`

- `_PROVIDER_PREFIXES` += `"zen-go"` (kept as a mirrored literal per the
  module's no-import note). Inherits Zen's documented caveat: the endpoint
  carries no context-window metadata, so thresholds ride on defaults.

### `interfaces/tui/providers.py`

- One `ProviderSpec("zen-go", write_key="OPENCODE_API_KEY",
  read_keys=("OPENCODE_API_KEY",), drop_keys=("OPENCODE_API_KEY",))`.
- Shared-key consequence (accepted): the remove button on either the `zen` or
  `zen-go` card drops `OPENCODE_API_KEY`, deconfiguring both. Note it in the
  card comment/copy.

### Untouched by design

Sub-agent tiers, advisor, `/think`, session persistence — all route by
qualified `zen-go:<model>` ids through `parse_qualified` /
`MultiModelSource`, which handle any known prefix. `openrouter_cost` stays
openrouter-only: Go is flat-rate, there is no per-token cost to display.

### Docs

- `.env.example`: `MARIM_PROVIDER=zen-go` mention next to `zen`.
- Providers doc page: a Go section (what it is, same key as Zen, usage
  windows, catalog is open-models-only).
- `CHANGELOG.md` entry.

## Testing

Unit (offline):

- `_provider_config("zen-go", …)` branch: base URL, default model, key
  fallback chain.
- `_provider_has_creds` / `detect_active_providers`: key present ⇒ both `zen`
  and `zen-go` active.
- `parse_qualified("zen-go:glm-5.2", …)` routes correctly.
- Catalog fetch against a recorded Go `/models` payload → entries with
  `provider="zen-go"`.
- Providers-screen spec table includes `zen-go` with the shared-key
  read/write/drop sets.

Live smoke (Go key present):

- `MARIM_PROVIDER=zen-go` headless one-shot on `deepseek-v4-flash` (cheapest
  usage-window footprint).
- Model listing shows the Go catalog; `/key` strict probe passes.
