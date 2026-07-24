# read_file image support (multimodal reads) — design

**Date:** 2026-07-23
**Status:** Approved

## Problem

`read_file` refuses every binary file with `"binary file, cannot display."` — including
images. Vision-capable models (most modern ones) could act on screenshots, diagrams, and
design assets directly if the tool returned the image bytes as model-visible content.
marim already has image plumbing for the *prompt* side (clipboard paste, disk cache,
session externalization in `images.py`) and catalog-based vision detection
(`workspace/catalog.py: model_supports_images`), but the tool layer uses none of it.

## Decision summary

Overload `read_file` (no separate `read_image` tool): when the target path has a known
image extension, return the bytes as pydantic-ai `BinaryContent` so the framework
threads them to the model as image content. Gate on the catalog: `supports_images is
False` → keep a text notice; `True` or unknown (`None`, including catalog fetch
failure) → send the image optimistically. The gate is evaluated per call against
`ctx.model`, so sub-agents on tiered models and `/model` switches work without rebuilds.

Rejected alternatives:
- **Always send, never gate** (thinking-level philosophy): a non-vision model
  hard-errors the whole request — worse than one degraded tool result.
- **Separate `read_image` tool**: doubles tool surface, model must discover it,
  sub-agent grant lists grow; Claude Code precedent is one Read tool doing both.

## Design

### 1. Tool behavior (`tools/impl/fs.py`, `tools/fs_tools.py`)

In `impl/fs.py: read_file`, *before* the `_looks_binary` sniff: if
`media_type_for_path(p)` (from `images.py`) matches, branch to a new impl helper that
validates size and reads bytes, returning `(data, media_type)`.

- **Size cap:** 5 MB. Over-cap returns a text notice including the actual size.
- `offset`/`limit` are ignored for images.
- The read still records into the `ReadLedger` (consistent with text reads; harmless).
- A non-image binary file keeps today's `"binary file, cannot display."` notice.

The tool layer (`fs_tools.read_file`) applies the vision gate: gate passes → wrap in
`BinaryContent(data, media_type=...)`; gate fails (`False`) → return a notice like
`"<path>: image file — the current model does not support image input."`. Return type
widens to `str | BinaryContent`. The docstring (model-facing) documents that images
are returned visually on vision models.

### 2. Vision gate seam (`runtime/deps.py` services, `bootstrap.py`, `builder.py`)

New optional services callback:

```python
supports_images: Callable[[str], Awaitable[bool | None]] | None
```

- `bootstrap` wires it from `model_source.list_models()` with an internal one-shot
  cache; any fetch failure → `None` (unknown → optimistic send). Never raises.
- `HarnessBuilder` leaves it `None` (embedders: unknown → images sent). No new
  builder knob for v1.
- `fs_tools.read_file` becomes `async def` to await the gate (pydantic-ai registers
  sync/async tools identically; sub-agents get the same function, and their
  `ctx.model` is the spawn's own tiered model).
- Model id passed to the gate is the qualified id from `ctx.model` (matching
  `ModelEntry.qualified` as `_vision_caps` in the TUI already does).

### 3. Session persistence (`images.py`)

`externalize_images` / `rehydrate_images` currently walk only `user-prompt` parts.
Extend the part iterator to also yield binary items inside `tool-return` parts so
image tool results land in the existing content-addressed cache
(`marim-image-cache://<sha>` refs) instead of inlining base64 into every session
file. Missing cache file on rehydrate degrades to the existing `[image unavailable]`
placeholder.

### 4. Rendering & compaction

- **TUI:** tool-result renderers (`stream_render`, `session_view`) show a compact
  `[image: <path>, <n> KB]` line when a tool return is `BinaryContent`, never raw
  bytes/base64.
- **Compaction/masking:** `compaction.py` already special-cases `BinaryContent` for
  token estimation in user parts; verify (with a test) the same handling covers
  tool-return parts once persistence emits them — image tool-returns must be
  maskable like any large observation.

### 5. Testing

- Unit: image-extension branch, size cap, gate matrix (`False` / `True` / `None` /
  catalog fetch error), externalize/rehydrate round-trip for tool-return binary
  parts, TUI render placeholder.
- Live smoke deferred (mimo-v2.5 per standing approval); not part of CI.

## Non-goals

- PDFs, audio, or other non-image binary types.
- Downscaling / re-encoding (no new dependencies).
- `glob` / `grep` awareness of image files.
- A builder-level configuration knob for the gate.

## Notes

- Under the `claude-cli` main-loop provider marim's tools don't apply, so this
  feature is inert there (Claude Code's own Read already handles images).
- Prompt-side image flow (clipboard paste, `detect_image_path`) is untouched.
