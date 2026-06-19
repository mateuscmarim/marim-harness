# Image input via paste — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation plan to follow.

## Goal

Let a TUI user attach images to a prompt the way Claude Code does: by pasting a
screenshot from the OS clipboard (app-intercepted `Ctrl+V`) or by pasting/dragging
an image file path. Images ride along with the typed text to vision-capable
models. Text-only models are detected and the user is warned before sending.

## Scope

- **In scope:** the TUI top-level prompt (`interfaces/tui`), message assembly in
  `agent.py`, model-capability parsing, and session persistence of attached
  images.
- **Out of scope:** the headless CLI (`interfaces/cli/headless.py`) and subagents
  — both stay text-only and unchanged. No slash-command attach (rejected during
  brainstorming in favor of paste + file-path, matching Claude Code).

## UX truth to surface

The terminal's *native* paste shortcut (Ctrl+Shift+V on Linux, Cmd+V on macOS)
travels through **bracketed paste, which carries text only — image bytes never
arrive that way.** Only the app-intercepted `Ctrl+V` reads the OS clipboard for
images. This is exactly how Claude Code behaves; the help text / docs must state
it so it does not read as a bug. The file-path attach path works through bracketed
paste because a file path *is* text.

## Verified foundations (empirical, before design)

- `pydantic-ai 1.107` is installed; `BinaryContent(data=bytes, media_type=...)`
  imports and a user prompt can be a list mixing `str` and `BinaryContent`.
- A `BinaryContent` user part serializes via `ModelMessagesTypeAdapter.dump_json`
  as a dict `{"kind": "binary", "data": <base64>, "media_type": ..., "identifier":
  ..., "vendor_metadata": ...}` nested in `parts[].content[]`.
- On this Wayland machine the clipboard round-trip works: `wl-paste --list-types`
  reports `image/png` and `wl-paste --type image/png` returns byte-identical PNG
  data. (`xclip`, `pngpaste`, `PIL` are absent here — Linux/X11, macOS, Windows
  helpers are coded but unverified on this box.)

## The one unproven keystone

Whether Textual delivers a `ctrl+v` (0x16) **key event** to the `PromptInput`
widget — TextArea may consume or remap it. This requires a running TUI to prove,
so it is **Milestone 0** of the implementation plan: a spike that confirms the
keystroke reaches `_on_key` before any of the five layers below are built on top
of it. If it does not arrive as expected, the attach trigger is reworked before
proceeding.

## Components

### 1. Clipboard reader (new module, isolated and mockable)

A small module exposing one function:

```
read_clipboard_image() -> Optional[tuple[bytes, str]]   # (data, media_type)
```

Returns `None` when there is no image on the clipboard or the platform helper is
missing. The platform shell-outs live behind this single interface so they are the
only untestable part of the feature; every other unit consumes the reader and can
be tested with a mock.

Platform implementations:

- **Linux / Wayland:** `wl-paste --list-types`, and if an image type is present,
  `wl-paste --type image/png` (prefer `image/png`, else first `image/*`).
  *(proven on this machine)*
- **Linux / X11:** `xclip -selection clipboard -t TARGETS -o`, then
  `xclip -selection clipboard -t image/png -o`.
- **macOS:** `pngpaste -`; fallback to `osascript` clipboard read.
- **Windows:** PowerShell (`System.Windows.Forms.Clipboard` / `Get-Clipboard`).

When the needed helper is absent, the reader returns `None` and the caller shows a
one-line hint naming the package to install (e.g. "install wl-clipboard to paste
images"). File-path attach still works everywhere regardless.

### 2. TUI: PromptInput trigger + attachment tray

- `PromptInput._on_key` adds a branch for `ctrl+v` **before** delegating to
  `super()._on_key`. On `ctrl+v`: call `read_clipboard_image()`; if it returns
  bytes, cache them (component 3), append to the draft's attachment list, and
  insert an `[Image #N]` token at the cursor (N = 1-based index into the list).
  If it returns `None`, fall through to default paste handling.
- **File-path attach:** on a Textual `Paste` event (or on submit-time scan of the
  typed text), treat content as an attachment **only** when a paste/drag yields a
  bare token that resolves to an existing image file (by extension + `os.path`
  existence). A path-shaped substring sitting inside a sentence of prose is **not**
  treated as an attachment — this avoids false positives.
- The app (`app.py`) holds a per-draft list `attachments: list[(cache_path,
  media_type)]`, cleared on submit and on input-clear. The `[Image #N]` indices map
  into this list. On submit, the list is handed to the harness alongside the text.

### 3. Disk cache

- Pasted/loaded bytes are written to
  `~/.marim/image-cache/<session_id>/<sha256>.<ext>`, keyed by **content hash**
  (sha256 of the bytes — collision-safe and controlled by us, not pydantic's short
  `identifier`).
- Re-pasting identical bytes reuses the existing file (idempotent).
- The cache backs three things: the `[Image #N]` reference shown to the user, the
  bytes attached to the live turn, and the externalized references in persisted
  session history (component 6).

### 4. Message assembly (`agent.py`)

- `_assemble_prompt` is **unchanged** and still returns a `str`. All of its
  injection, turn-context wrap/slice, and `UserPromptSubmit` hook logic stays
  text-only; image bytes never enter it.
- `run_turn` gains an `attachments` parameter (default `None`). It builds:

  ```
  user_prompt = assembled_text                       # when no attachments
  user_prompt = [assembled_text, *binary_contents]   # when attachments present
  ```

  where each `binary_content` is `BinaryContent(data, media_type)` read from the
  cached file. The string-only path is byte-for-byte the current behavior.

### 5. Vision gating — detect & warn

- `ModelEntry` (in `workspace/catalog.py`) gains `supports_images: Optional[bool]`.
  - **OpenRouter:** parse `row["architecture"]["input_modalities"]`; `True` when it
    contains `"image"`, else `False`.
  - **Google/Gemini:** `True` (all current Gemini chat models accept images).
  - **Local / unknown:** `None`.
- On submit-with-attachments the TUI checks the active model's capability:
  - **Positively text-only (`False`):** warn in the TUI and let the user switch
    models; do not silently send an image that will be rejected.
  - **`True`:** proceed.
  - **Unknown (`None`, offline, catalog miss, slow/failed fetch):** allow silently.
    The capability check must **never** block or delay submit on a missing or slow
    catalog fetch — only a positive "this model is text-only" produces a warning.
- Capability lookup reuses the catalog the model picker already fetches; it is
  cached so the common path needs no extra network call.

### 6. Persistence round-trip (`session/store.py`)

To keep session JSON from bloating with inline base64 while still surviving resume:

- **`save()`:** after `ModelMessagesTypeAdapter.dump_json(history)`, walk the parts;
  for each content entry with `kind == "binary"`, write its decoded bytes to the
  disk cache (by sha256) and replace the inline `data` field with a reference
  string `marim-image-cache://<sha256>`. The resulting JSON carries references, not
  megabytes of base64.
- **`load()`:** before `ModelMessagesTypeAdapter.validate_python(...)`, walk the
  raw JSON and rehydrate every `marim-image-cache://<sha>` reference back to inline
  base64 by reading the cached file, so the type adapter never sees a malformed
  part.
- **Graceful degradation:** if a referenced cache file is missing on load (cache
  cleaned, moved machine), the session must **never** crash. Drop the image part or
  substitute a short text placeholder (e.g. `[image unavailable]`) and continue
  loading — consistent with the codebase's existing resumability care (atomic
  writes, `_repair_unanswered_tool_calls`, tolerance of old files).

## Data flow (paste → model → resume)

1. User presses `Ctrl+V` → reader returns `(bytes, "image/png")`.
2. Bytes hashed → written to `~/.marim/image-cache/<session>/<sha>.png`; attachment
   appended; `[Image #1]` inserted at cursor.
3. User presses Enter → app passes `(text, attachments)` to `harness.run_turn`.
4. `run_turn` builds `[assembled_text, BinaryContent(...)]` and calls `agent.run`.
5. After the run, history holds a `UserPromptPart` with the binary content.
6. `save()` externalizes the binary to a `marim-image-cache://<sha>` reference.
7. On a later launch, `load()` rehydrates the reference from the cache (or degrades
   gracefully if the file is gone).

## Error handling

- Missing platform clipboard helper → reader returns `None`, one-line install hint,
  no crash.
- No image on clipboard → `Ctrl+V` falls through to normal behavior.
- Path token that is not an existing image file → treated as plain text.
- Text-only active model with an attachment → warning, user can switch.
- Catalog fetch slow/failed/absent → capability is unknown → submit proceeds.
- Cached image missing on session load → image dropped / placeholder, session still
  loads.

## Testing

- **Unit (mocked clipboard reader):**
  - file-path attach heuristic: bare image path → attachment; image path mid-prose
    → plain text; non-image path → plain text.
  - list-form prompt assembly: no attachments → `str`; with attachments →
    `[text, BinaryContent...]`.
  - persistence externalize → rehydrate round-trip is identity; missing cache file
    on load degrades instead of raising.
  - `ModelEntry` capability parsing for OpenRouter modalities and Gemini.
- **Spike / manual:**
  - Milestone 0: `ctrl+v` reaches `PromptInput._on_key` on a running TUI.
  - end-to-end: paste a real screenshot, send to a vision model, confirm the model
    sees it; resume the session and confirm the image rehydrates.

## Build order

0. **Spike:** confirm `ctrl+v` reaches `PromptInput`. (Gate — rework trigger if not.)
1. Clipboard reader module (Wayland first) behind the mockable interface.
2. Disk cache + attachment list + `[Image #N]` insertion in the TUI.
3. `run_turn` list-form assembly; end-to-end paste → vision model.
4. Vision-capability parsing + detect-and-warn gating.
5. Persistence externalize/rehydrate with graceful degradation.
6. Remaining platform clipboard helpers (X11, macOS, Windows).
