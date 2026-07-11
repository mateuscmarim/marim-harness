# Providers section in the settings screen — design

**Date:** 2026-07-11
**Status:** approved

## Goal

Configure model providers from the TUI settings screen instead of hand-editing
`.env`: enter/replace/remove credentials, set the local base URL, pick the
default provider — with changes applying **live** (no restart) and each save
implicitly verified against the provider's model catalog.

Personal-use scope: the four built-in providers (`openrouter`, `local`,
`google`, `claude-cli`), no arbitrary/custom providers.

## Current state

- Providers are env-only: `MARIM_PROVIDER`, `OPENROUTER_API_KEY`,
  `GOOGLE_API_KEY`/`GEMINI_API_KEY`, `MARIM_BASE_URL`, `MARIM_API_KEY`
  (`config/model.py`).
- `detect_active_providers()` returns every provider whose creds are present
  (plus the default); `MultiModelSource` routes `provider:model_id` across
  them for the model picker and `Harness.set_model`.
- The settings screen (`interfaces/tui/settings.py`) is a full-bleed rail of
  topic sections with an auto-save-to-global-`.env` pattern
  (`save_env_settings`), which already mirrors saved values into `os.environ`
  and supports atomic key removal via `drop=`.
- `build_collaborators` captures the model-source *object* in a closure
  (`runtime/harness.py` — `lambda mid, _src=cfg.model_source: _src.build(mid)`),
  so **in-place mutation of the `MultiModelSource` propagates everywhere**
  (harness, picker, sub-agent model building) with zero rewiring.

## Approach (chosen)

**In-place refresh of `MultiModelSource`.** Add
`MultiModelSource.refresh_from_env()` that re-runs `detect_active_providers()`
and mutates `self.sources` / `self.default` in place. The settings screen
commits a credential via `save_env_settings`, then calls `refresh_from_env()`,
then fires a background catalog fetch for that provider to flip its status.

Rejected alternatives:

- *Structured provider registry (providers.toml + store):* more flexible
  (multiple local endpoints), but env vars are what the whole codebase
  consumes — a bridge layer and migration for no real gain at this scope.
- *Next-launch only:* simplest, but explicitly not wanted.

## UI

New **Providers** rail section, placed right after **Session**. Stacked cards
— all four providers always visible; status dot + name + badge on the header
line, fields beneath; a **Default provider** radio set at the bottom.

```
settings  ›  Providers

 ● openrouter                 connected · default        [remove]
   API key   [ configured · …7f2a — type to replace ]

 ● google                     connected                  [remove]
   API key   [ configured · …x9Qd — type to replace ]

 ○ local                      not configured
   Base URL  [ http://localhost:11434/v1            ]
   API key   [ local                                ]

 ● claude-cli                 detected on PATH
   (auth handled by the claude CLI itself)

 Default provider   (•) openrouter ( ) google
                    ( ) local      ( ) claude-cli
```

- Status dot: `●` configured/active, `○` not configured. Badge text:
  `connected · N models` / `verifying…` / `✗ <short error>` /
  `not configured`; claude-cli shows `detected on PATH` / `not found`.
- Rail badge for the section: the default provider's name.

## Fields per card

| Provider     | Fields → env keys                                             |
| ------------ | ------------------------------------------------------------- |
| `openrouter` | API key → `OPENROUTER_API_KEY`                                 |
| `google`     | API key → `GOOGLE_API_KEY` (reads `GEMINI_API_KEY` as configured too; always **writes** `GOOGLE_API_KEY`) |
| `local`      | Base URL → `MARIM_BASE_URL`, API key → `MARIM_API_KEY`         |
| `claude-cli` | none — status from `resolve_cli_binary()`; the CLI owns auth   |

**Default provider** radio → `MARIM_PROVIDER`. New sessions start there; the
running session keeps its current model.

## Secret handling

- Key inputs are `password=True` and start **empty**; the placeholder proves
  state without painting the secret: `configured · …7f2a — type to replace`
  or `not set`.
- Enter (or blur with non-empty text) commits. An **empty input commits
  nothing** — casual focus/blur can never clobber or rewrite a key.
- The screen never displays a stored secret.
- Keys land in the **global** `.env` (0600, atomic write) — consistent with
  `_PROJECT_ENV_BLOCKLIST`, which forbids a project `.env` from setting these
  keys at all.

## Live apply + implicit verification

On commit: `save_env_settings(...)` → `model_source.refresh_from_env()`
(guarded by `isinstance(harness.model_source, MultiModelSource)`; a plain
`ModelSource` or `None` skips refresh) → a background `list_models()` for just
that provider. The card badge flips `verifying…` → `✓ connected · N models`
or `✗ <short error>`; the footer status line shows the save confirmation like
every other auto-saved field. The model picker immediately sees the new
provider's catalog.

## Key removal

Each card with a stored credential shows a `remove` button on its header line
(only rendered when configured). Pressing it:

1. `save_env_settings({}, drop=<credential keys>)` — removed from the global
   `.env` atomically and popped from `os.environ` in the same call:
   - `openrouter` → drop `OPENROUTER_API_KEY`
   - `google` → drop **both** `GOOGLE_API_KEY` and `GEMINI_API_KEY`
     (either makes it configured, so removal must clear both)
   - `local` → drop `MARIM_BASE_URL` and `MARIM_API_KEY` together (base URL
     marks it configured; a leftover key alone is meaningless)
   - `claude-cli` → no remove button (nothing stored)
2. `model_source.refresh_from_env()` — the provider drops out of the active
   set (unless it is the default provider, which `detect_active_providers`
   always keeps so startup has a home).
3. The card flips to `○ not configured`, the key input placeholder resets to
   `not set`, the footer confirms `✓ removed`.

If the running session's model is on the removed provider, the session keeps
working (the harness holds the already-built model instance); the next model
switch or new session routes to the default provider. No confirmation modal —
a deliberate button click in a personal tool, confirmed on the footer.

## Out of scope

- Per-provider default models.
- claude-cli binary path override (`MARIM_CLAUDE_CLI_BIN`).
- Arbitrary/custom providers, multiple local endpoints.

## Testing

- **`MultiModelSource.refresh_from_env` (unit):** provider appears after the
  env gains a key; default switches; a de-credentialed provider drops out
  while the default is kept.
- **Settings screen (pilot tests, `tests/test_settings_screen.py` pattern —
  fake harness + tmp `.env`):**
  - committing a key writes the right env key to the tmp `.env`;
  - empty commit is a no-op;
  - google reads `GEMINI_API_KEY` for configured-status but writes
    `GOOGLE_API_KEY`;
  - default-provider radio persists `MARIM_PROVIDER`;
  - the verification callback flips the status badge;
  - remove drops the right key(s) from the tmp `.env` and `os.environ`;
    google removal clears both env names; the button only renders when
    configured.
