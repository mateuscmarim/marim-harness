# Image Input via Paste Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a TUI user attach images to a prompt via app-intercepted `Ctrl+V` (OS clipboard) or by pasting an image file path, and send them to vision-capable models.

**Architecture:** A new non-TUI module `images.py` owns the clipboard reader (behind a mockable interface), a content-addressed disk cache, image file-path detection, and persistence externalize/rehydrate helpers. `PromptInput` intercepts `Ctrl+V` to cache an image and insert an `[Image #N]` token; `app.py` keeps a per-draft attachment list and passes `(bytes, media_type)` tuples to `Harness.run_turn`, which builds a list-form `[text, *BinaryContent]` user prompt for pydantic-ai. Model vision capability is parsed from the catalog; submitting an image to a positively text-only model warns first. Session persistence swaps inline base64 for `marim-image-cache://<sha>` references.

**Tech Stack:** Python 3.10+, pydantic-ai 1.107, Textual (TextArea/pilot), pytest + anyio, ruff, pyright. Clipboard via platform CLIs (`wl-paste`, `xclip`, `pngpaste`, PowerShell).

## Global Constraints

- `pydantic-ai>=1.107,<2` — user prompt may be `Sequence[str | BinaryContent]`; `BinaryContent(data: bytes, media_type: str)`.
- A serialized binary user-content item is a dict `{"kind": "binary", "data": <base64>, "media_type": ..., "identifier": ..., "vendor_metadata": ...}` inside `parts[].content[]` of a message with `part_kind == "user-prompt"`.
- Tests live flat in `tests/`, run with `uv run pytest`; async tests use `@pytest.mark.anyio`; TUI tests use `async with app.run_test() as pilot`.
- `ModelEntry` is imported from `marim_harness.workspace` and defined in `src/marim_harness/workspace/catalog.py` (frozen dataclass).
- Lint/type gates must stay green: `uv run ruff check src tests` and `uv run pyright`.
- ruff line-length is 100.
- Scope is the TUI top-level prompt only. Do **not** change `interfaces/cli/headless.py` behavior or subagents; new `run_turn` params must default so those paths are byte-for-byte unchanged.
- Image cache root: `MARIM_IMAGE_CACHE_DIR` env override, else `~/.marim/image-cache`; files are stored per session as `<root>/<session_id>/<sha256>.<ext>`.
- Capability gating must **never** block or delay submit on a missing/slow/failed catalog fetch — only a positive "text-only" (`supports_images is False`) warns.

---

### Task 1: Spike — prove `Ctrl+V` reaches `PromptInput`

De-risk the one keystone that can't be verified without a running TUI: that Textual delivers a `ctrl+v` key event to our `TextArea` subclass before we build anything on top of it. We add a minimal, overridable hook and assert via pilot that pressing `ctrl+v` invokes it, then verify once in a real terminal.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets.py` (`PromptInput._on_key`, ~line 370)
- Test: `tests/test_image_paste.py` (new)

**Interfaces:**
- Produces: `PromptInput._on_paste_image() -> bool` — called when `ctrl+v` is pressed; returns `True` if it consumed the event (an image was attached), `False` to fall through to default handling. Task 6 replaces the body; here it is a no-op returning `False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_image_paste.py
import pytest

from marim_harness.interfaces.tui.app import HarnessApp


def _app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_ctrl_v_invokes_paste_image_hook(tmp_path):
    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        calls = []
        box._on_paste_image = lambda: (calls.append(1), False)[1]
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert calls == [1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_image_paste.py::test_ctrl_v_invokes_paste_image_hook -v`
Expected: FAIL — `AttributeError: 'PromptInput' object has no attribute '_on_paste_image'` (and the hook is never called).

- [ ] **Step 3: Add the hook and the `ctrl+v` branch**

In `widgets.py`, add the method to `PromptInput` and branch in `_on_key` **before** `await super()._on_key(event)`:

```python
    def _on_paste_image(self) -> bool:
        """Hook: try to attach an image from the OS clipboard. Returns True when
        an image was consumed; False to fall through to default paste handling.
        Replaced with real logic in the clipboard-paste task."""
        return False
```

Add to `_on_key`, immediately before the final `await super()._on_key(event)`:

```python
        if event.key == "ctrl+v":
            if self._on_paste_image():
                event.prevent_default()
                event.stop()
                return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_image_paste.py::test_ctrl_v_invokes_paste_image_hook -v`
Expected: PASS

- [ ] **Step 5: Manual real-terminal check (the actual keystone)**

Pilot injects key events directly and does NOT prove a real terminal sends `ctrl+v`. Verify by hand: temporarily make `_on_paste_image` write a marker, run the TUI in a real terminal, focus the prompt, press `Ctrl+V` (NOT Ctrl+Shift+V), and confirm the marker fires.

```bash
uv run python -m marim_harness  # focus the prompt box, press Ctrl+V, watch for the marker
```

If `Ctrl+V` does not arrive as a key event in your terminal, STOP and revisit the trigger (e.g. a different binding) before continuing — every later task assumes this works. Revert the temporary marker after verifying.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets.py tests/test_image_paste.py
git commit -m "feat(images): intercept Ctrl+V in PromptInput with a paste-image hook"
```

---

### Task 2: Clipboard reader (Wayland) behind a mockable interface

**Files:**
- Create: `src/marim_harness/images.py`
- Test: `tests/test_images.py` (new)

**Interfaces:**
- Produces: `read_clipboard_image() -> Optional[tuple[bytes, str]]` — returns `(data, media_type)` for an image on the clipboard, or `None` when there is none or no platform helper is available. Wayland backend only in this task; other platforms return `None` here and are added in Task 10.
- Produces: `media_ext(media_type: str) -> str` — maps `"image/png"`→`"png"`, `"image/jpeg"`→`"jpg"`, else the subtype after `/` (or `"bin"`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_images.py
import subprocess

from marim_harness import images


def test_media_ext_maps_common_types():
    assert images.media_ext("image/png") == "png"
    assert images.media_ext("image/jpeg") == "jpg"
    assert images.media_ext("image/webp") == "webp"
    assert images.media_ext("application/octet-stream") == "bin"


def test_read_clipboard_image_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(images.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["wl-paste", "--list-types"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"text/plain\nimage/png\n")
        if cmd == ["wl-paste", "--type", "image/png"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"\x89PNGdata")
        raise AssertionError(cmd)

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.read_clipboard_image() == (b"\x89PNGdata", "image/png")


def test_read_clipboard_image_none_when_no_image(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(images.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        images.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"text/plain\n"),
    )
    assert images.read_clipboard_image() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_images.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.images'`

- [ ] **Step 3: Create `images.py` with the Wayland reader**

```python
# src/marim_harness/images.py
"""Image attachments for the TUI prompt: clipboard reading, a content-addressed
disk cache, image file-path detection, and session-history externalization.

The clipboard reader is the only part that shells out to the OS; it is isolated
here behind read_clipboard_image() so every other unit is testable with a mock."""

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/webp": "webp", "image/gif": "gif"}


def media_ext(media_type: str) -> str:
    """File extension for a media type. Falls back to the subtype, then 'bin'."""
    if media_type in _EXT:
        return _EXT[media_type]
    subtype = media_type.rsplit("/", 1)[-1] if "/" in media_type else ""
    return subtype or "bin"


def _run(cmd: list[str]) -> Optional[bytes]:
    """Run a clipboard helper, returning stdout bytes or None on any failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("clipboard helper %s failed: %s", cmd, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _read_wayland() -> Optional[tuple[bytes, str]]:
    if not shutil.which("wl-paste"):
        return None
    types = _run(["wl-paste", "--list-types"])
    if not types:
        return None
    available = types.decode("utf-8", "replace").split()
    target = "image/png" if "image/png" in available else next(
        (t for t in available if t.startswith("image/")), None)
    if target is None:
        return None
    data = _run(["wl-paste", "--type", target])
    if not data:
        return None
    return data, target


def read_clipboard_image() -> Optional[tuple[bytes, str]]:
    """The image currently on the OS clipboard as (bytes, media_type), or None.

    Only Wayland is wired here; other platforms are added later and return None
    for now so callers degrade to 'no image / install a helper'."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return _read_wayland()
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_images.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/images.py tests/test_images.py
git commit -m "feat(images): clipboard reader (Wayland) behind a mockable interface"
```

---

### Task 3: Content-addressed disk cache

**Files:**
- Modify: `src/marim_harness/images.py`
- Test: `tests/test_images.py`

**Interfaces:**
- Produces: `image_cache_root() -> Path` — `MARIM_IMAGE_CACHE_DIR` env override, else `~/.marim/image-cache`.
- Produces: `CachedImage` (frozen dataclass) with `path: Path`, `sha: str`, `media_type: str`.
- Produces: `store_image(session_id: str, data: bytes, media_type: str) -> CachedImage` — writes `<root>/<session_id>/<sha256>.<ext>` (idempotent; reuses an existing file), returns the record.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_images.py


def test_store_image_is_content_addressed(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    a = images.store_image("sess1", b"\x89PNGbytes", "image/png")
    assert a.path.exists()
    assert a.path.read_bytes() == b"\x89PNGbytes"
    assert a.path.name == f"{a.sha}.png"
    assert a.path.parent.name == "sess1"
    # identical bytes reuse the same file
    b = images.store_image("sess1", b"\x89PNGbytes", "image/png")
    assert b.sha == a.sha and b.path == a.path
    # different bytes -> different file
    c = images.store_image("sess1", b"other", "image/png")
    assert c.sha != a.sha
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_images.py::test_store_image_is_content_addressed -v`
Expected: FAIL — `AttributeError: module 'marim_harness.images' has no attribute 'store_image'`

- [ ] **Step 3: Add cache code to `images.py`**

Add imports at the top (merge with existing): `import hashlib`, `from dataclasses import dataclass`, `from pathlib import Path`.

```python
def image_cache_root() -> Path:
    override = os.environ.get("MARIM_IMAGE_CACHE_DIR")
    return Path(override) if override else Path.home() / ".marim" / "image-cache"


@dataclass(frozen=True)
class CachedImage:
    path: Path
    sha: str
    media_type: str


def store_image(session_id: str, data: bytes, media_type: str) -> CachedImage:
    """Cache image bytes under <root>/<session_id>/<sha256>.<ext>. Idempotent:
    identical bytes map to the same path and are not rewritten."""
    sha = hashlib.sha256(data).hexdigest()
    out = image_cache_root() / session_id / f"{sha}.{media_ext(media_type)}"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
    return CachedImage(path=out, sha=sha, media_type=media_type)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_images.py::test_store_image_is_content_addressed -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/images.py tests/test_images.py
git commit -m "feat(images): content-addressed per-session disk cache"
```

---

### Task 4: Image file-path detection

**Files:**
- Modify: `src/marim_harness/images.py`
- Test: `tests/test_images.py`

**Interfaces:**
- Produces: `detect_image_path(text: str) -> Optional[Path]` — returns a `Path` only when the **entire** stripped `text` is a single bare token resolving to an existing file with an image extension; otherwise `None` (so a path mentioned mid-sentence stays plain text).
- Produces: `media_type_for_path(path: Path) -> Optional[str]` — media type from the file extension, or `None` if not a known image extension.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_images.py


def test_detect_image_path_only_for_bare_existing_image(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG")
    assert images.detect_image_path(f"  {img}  ") == img
    # quoted (drag-and-drop often quotes) still works
    assert images.detect_image_path(f'"{img}"') == img
    # path inside prose -> not an attachment
    assert images.detect_image_path(f"see {img} please") is None
    # non-image extension -> None
    other = tmp_path / "notes.txt"
    other.write_text("hi")
    assert images.detect_image_path(str(other)) is None
    # nonexistent -> None
    assert images.detect_image_path(str(tmp_path / "nope.png")) is None


def test_media_type_for_path():
    assert images.media_type_for_path(images.Path("a.PNG")) == "image/png"
    assert images.media_type_for_path(images.Path("a.jpg")) == "image/jpeg"
    assert images.media_type_for_path(images.Path("a.txt")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_images.py::test_detect_image_path_only_for_bare_existing_image tests/test_images.py::test_media_type_for_path -v`
Expected: FAIL — `AttributeError: ... 'detect_image_path'`

- [ ] **Step 3: Add detection to `images.py`**

```python
_EXT_TO_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "webp": "image/webp", "gif": "image/gif"}


def media_type_for_path(path: Path) -> Optional[str]:
    return _EXT_TO_MEDIA.get(path.suffix.lower().lstrip("."))


def detect_image_path(text: str) -> Optional[Path]:
    """A bare path to an existing image file, or None. The whole text (minus
    surrounding whitespace/quotes) must be the path — a path embedded in a
    sentence is deliberately ignored to avoid false positives."""
    token = text.strip().strip('"').strip("'")
    if not token or "\n" in token:
        return None
    path = Path(token).expanduser()
    if media_type_for_path(path) is None:
        return None
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    return path
```

Note: the test references `images.Path` — `Path` is imported into `images` in Task 3, so this resolves.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_images.py::test_detect_image_path_only_for_bare_existing_image tests/test_images.py::test_media_type_for_path -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/images.py tests/test_images.py
git commit -m "feat(images): bare image file-path detection"
```

---

### Task 5: List-form user prompt in `run_turn`

**Files:**
- Modify: `src/marim_harness/agent.py` (`run_turn`, ~line 412; `agent.run` call, ~line 466)
- Test: `tests/test_image_attachments.py` (new)

**Interfaces:**
- Consumes: `_assemble_prompt(prompt) -> str` (unchanged).
- Produces: `Harness.run_turn(self, prompt: str, event_stream_handler=None, attachments: Optional[list[tuple[bytes, str]]] = None) -> str` — when `attachments` is non-empty, the value passed to `agent.run` is `[assembled_text, *[BinaryContent(data, media_type) for (data, media_type) in attachments]]`; otherwise it is the plain `assembled_text` string exactly as today.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_image_attachments.py
import pytest
from pydantic_ai.messages import BinaryContent, UserPromptPart

from marim_harness.agent import Harness
from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider


def _harness(tmp_path):
    from pydantic_ai.models.test import TestModel

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    return Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps,
                   instructions="test")


def _last_user_content(harness):
    for msg in reversed(harness.session.history):
        for part in getattr(msg, "parts", []):
            if isinstance(part, UserPromptPart):
                return part.content
    raise AssertionError("no user prompt recorded")


@pytest.mark.anyio
async def test_run_turn_attaches_binary_content(tmp_path):
    harness = _harness(tmp_path)
    await harness.run_turn("describe this", attachments=[(b"\x89PNGx", "image/png")])
    content = _last_user_content(harness)
    assert isinstance(content, list)
    assert any(isinstance(c, BinaryContent) and c.media_type == "image/png"
               for c in content)


@pytest.mark.anyio
async def test_run_turn_without_attachments_uses_plain_string(tmp_path):
    harness = _harness(tmp_path)
    await harness.run_turn("just text")
    content = _last_user_content(harness)
    assert isinstance(content, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_image_attachments.py -v`
Expected: FAIL — `TypeError: run_turn() got an unexpected keyword argument 'attachments'`

- [ ] **Step 3: Thread `attachments` through `run_turn`**

Add the import near the other pydantic-ai message imports at the top of `agent.py`:

```python
from pydantic_ai.messages import BinaryContent
```

Change the `run_turn` signature (line ~412):

```python
    async def run_turn(self, prompt: str, event_stream_handler=None,
                       attachments: Optional[list[tuple[bytes, str]]] = None) -> str:
```

After `user_prompt: Optional[str] = await self._assemble_prompt(prompt)` (line ~416), widen it to list form when there are attachments. Keep `_assemble_prompt` untouched (text-only); images never enter it:

```python
        user_prompt: Optional[str] = await self._assemble_prompt(prompt)
        if attachments and user_prompt is not None:
            user_prompt = [user_prompt, *(BinaryContent(data=d, media_type=m)
                                          for d, m in attachments)]
```

The existing `agent.run(user_prompt, ...)` call needs no change — pydantic-ai accepts the list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_image_attachments.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py tests/test_image_attachments.py
git commit -m "feat(images): list-form user prompt with image attachments in run_turn"
```

---

### Task 6: Wire the paste trigger and attachment tray into the app

Connect the keystroke (Task 1) and file-path path to the reader/cache (Tasks 2–4), maintain a per-draft attachment list, insert `[Image #N]`, and pass attachments to `run_turn` (Task 5).

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets.py` (`PromptInput`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`on_prompt_input_submitted` ~625, `_run_turn` ~640, `compose`/init for the attachment list)
- Test: `tests/test_image_paste.py`

**Interfaces:**
- Consumes: `images.read_clipboard_image`, `images.store_image`, `images.detect_image_path`, `images.media_type_for_path`, `Harness.run_turn(..., attachments=...)`.
- Produces: `PromptInput.attachments: list[tuple[Path, str]]` — `(cache_path, media_type)` per attached image, indexed 1:1 with the `[Image #N]` tokens; cleared on submit.
- Produces: `PromptInput.Submitted.attachments: list[tuple[bytes, str]]` — the message carries the read bytes so the app can forward them to `run_turn` without touching the filesystem again.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_image_paste.py


@pytest.mark.anyio
async def test_ctrl_v_caches_image_and_inserts_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert "[Image #1]" in box.text
        assert len(box.attachments) == 1
        path, media_type = box.attachments[0]
        assert path.exists() and media_type == "image/png"


@pytest.mark.anyio
async def test_submit_forwards_attachments_to_run_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    seen = {}

    async def fake_run_turn(prompt, event_stream_handler=None, attachments=None):
        seen["attachments"] = attachments
        return "ok"

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        box.text = "[Image #1] what is this?"
        await pilot.press("enter")
        await pilot.pause()
        assert seen["attachments"] == [(b"\x89PNGbytes", "image/png")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_image_paste.py -v`
Expected: FAIL — `AttributeError: 'PromptInput' object has no attribute 'attachments'`

- [ ] **Step 3: Implement the trigger and tray in `PromptInput`**

In `widgets.py`, add `from pathlib import Path` at the module top if not already present. (`images` is imported lazily inside the methods below — `from ... import images`, three dots from `interfaces/tui/` up to the `marim_harness` package root — which sidesteps any import-order concerns.) Initialise the list in `PromptInput.__init__` (after `super().__init__(...)`):

```python
        self.attachments: list[tuple[Path, str]] = []
```

Replace the placeholder `_on_paste_image` body from Task 1 with:

```python
    def _on_paste_image(self) -> bool:
        from ... import images

        got = images.read_clipboard_image()
        if got is None:
            return False
        data, media_type = got
        return self._attach(data, media_type)

    def _attach(self, data: bytes, media_type: str) -> bool:
        from ... import images

        cached = images.store_image(self._session_id(), data, media_type)
        self.attachments.append((cached.path, media_type))
        self.insert(f"[Image #{len(self.attachments)}]")
        return True

    def _session_id(self) -> str:
        # Resolve lazily from the running app's harness; fall back to a constant
        # bucket if unavailable (e.g. isolated widget tests). Persistence (the
        # externalize task) re-stores under the real session id regardless, so a
        # fallback bucket here only affects the transient paste-time cache path.
        try:
            return self.app.harness.session.store.session_id  # type: ignore[attr-defined]
        except Exception:
            return "default"
```

Extend `Submitted` to carry attachment bytes and clear the tray on submit. Update the `Submitted` message class:

```python
    class Submitted(Message):
        """Posted when the user presses Enter; carries the box's full text and
        any attached images as (bytes, media_type) tuples."""

        def __init__(self, value: str,
                     attachments: list[tuple[bytes, str]] | None = None) -> None:
            self.value = value
            self.attachments = attachments or []
            super().__init__()
```

In `_on_key`, change the `enter` branch to read the attachment bytes and reset the tray:

```python
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Submitted(self.text, atts))
            self.attachments = []
            self._reset_nav()
            return
```

Add file-path attach: when a bracketed paste arrives, if it is a bare image path, attach it instead of inserting the path text. Add an `on_paste` handler to `PromptInput`:

```python
    def on_paste(self, event) -> None:
        from ... import images

        path = images.detect_image_path(event.text)
        if path is None:
            return  # let TextArea insert the pasted text normally
        media_type = images.media_type_for_path(path)
        if media_type is None:
            return
        event.prevent_default()
        event.stop()
        self._attach(path.read_bytes(), media_type)
```

- [ ] **Step 4: Forward attachments from the app to `run_turn`**

In `app.py`, update `on_prompt_input_submitted` (line ~625) to thread attachments, and `_run_turn` (line ~640) to accept and pass them:

```python
    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self._history.add(text)
        self.query_one(PromptInput).text = ""
        if text.startswith("/"):
            await dispatch(self, text)
            return
        log = self.query_one("#log", VerticalScroll)
        await log.mount(UserMessage(text))
        self._current_assistant = None
        self._auto_turn_depth = 0
        self._turn_worker = self.run_worker(
            self._run_turn(text, event.attachments), exclusive=True
        )
```

```python
    async def _run_turn(self, text: str, attachments=None) -> None:
        self._set_busy(True)
        log = self.query_one("#log", VerticalScroll)
        try:
            await self.harness.run_turn(
                text, event_stream_handler=self._on_events, attachments=attachments
            )
        except CancelledError:
            log.mount(ErrorMessage("turn cancelled"))
            raise
        except Exception as exc:
            await log.mount(ErrorMessage(f"{type(exc).__name__}: {exc}"))
        finally:
            self._turn_worker = None
            self._set_busy(False)
            self._maybe_wake()
```

The other `_run_turn("")` caller (line ~463, the autonomous-wake path) still works since `attachments` defaults to `None`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_image_paste.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets.py src/marim_harness/interfaces/tui/app.py tests/test_image_paste.py
git commit -m "feat(images): wire Ctrl+V and file-path paste into the prompt tray"
```

---

### Task 7: Parse vision capability from the catalog

**Files:**
- Modify: `src/marim_harness/workspace/catalog.py` (`ModelEntry` ~line 15, `parse_models` ~line 23, `parse_google_models` ~line 52)
- Modify: `src/marim_harness/workspace/__init__.py` (export the new helper)
- Test: `tests/test_catalog.py`

**Interfaces:**
- Produces: `ModelEntry.supports_images: Optional[bool] = None` — `True`/`False` from OpenRouter modalities, `True` for Gemini, `None` when unknown.
- Produces: `model_supports_images(entries: list[ModelEntry], model_id: str) -> Optional[bool]` — capability of the entry whose `id == model_id`, or `None` if absent.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_catalog.py
from marim_harness.workspace import model_supports_images


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_catalog.py::test_parse_models_reads_image_modality tests/test_catalog.py::test_model_supports_images_lookup -v`
Expected: FAIL — `ImportError: cannot import name 'model_supports_images'`

- [ ] **Step 3: Add the field, parsing, and lookup**

In `catalog.py`, add the field to `ModelEntry`:

```python
@dataclass(frozen=True)
class ModelEntry:
    """One selectable model: its provider id and a human-readable name.
    ``supports_images`` is True/False when the catalog states it, else None."""

    id: str
    name: str
    supports_images: Optional[bool] = None
```

In `parse_models`, compute the capability per row and pass it:

```python
        name = row.get("name")
        display = name if isinstance(name, str) and name else model_id
        arch = row.get("architecture")
        supports_images: Optional[bool] = None
        if isinstance(arch, dict):
            mods = arch.get("input_modalities")
            if isinstance(mods, list):
                supports_images = "image" in mods
        entries.append(ModelEntry(id=model_id, name=display,
                                  supports_images=supports_images))
```

In `parse_google_models`, set it `True` on the appended entry:

```python
        entries.append(ModelEntry(id=model_id, name=display, supports_images=True))
```

Add the lookup helper at the end of `catalog.py`:

```python
def model_supports_images(entries: list[ModelEntry], model_id: str) -> Optional[bool]:
    """Whether ``model_id`` accepts image input per the catalog; None if the id
    is not present (capability unknown)."""
    for entry in entries:
        if entry.id == model_id:
            return entry.supports_images
    return None
```

In `src/marim_harness/workspace/__init__.py`, add `model_supports_images` to the imports/`__all__` alongside `ModelEntry`/`parse_models`/`filter_entries`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: PASS (existing + 2 new)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/catalog.py src/marim_harness/workspace/__init__.py tests/test_catalog.py
git commit -m "feat(images): parse model vision capability from the catalog"
```

---

### Task 8: Detect-and-warn gating in the app

Warn before sending an image to a model the catalog says is text-only. Never block on an unknown/slow/failed fetch.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Test: `tests/test_image_paste.py`

**Interfaces:**
- Consumes: `model_supports_images`, `Harness.model_id`, `Harness.model_source.list_models`.
- Produces: `HarnessApp._vision_caps: dict[str, Optional[bool]]` — cached capability per model id (populated opportunistically; empty means "not yet known").
- Produces: `HarnessApp._image_block_reason(attachments) -> Optional[str]` — a warning string when there are attachments **and** `self._vision_caps.get(self.harness.model_id) is False`; otherwise `None`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_image_paste.py


@pytest.mark.anyio
async def test_text_only_model_blocks_image_submit_with_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import NoticeMessage, PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    called = {"run": False}

    async def fake_run_turn(*a, **k):
        called["run"] = True
        return "ok"

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn
        app.harness.model_id = "b/text"
        app._vision_caps = {"b/text": False}
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        box.text = "[Image #1] look"
        await pilot.press("enter")
        await pilot.pause()
        assert called["run"] is False
        log = app.query_one("#log")
        assert any(isinstance(w, NoticeMessage) for w in log.walk_children())


@pytest.mark.anyio
async def test_unknown_capability_allows_image_submit(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    called = {"run": False}

    async def fake_run_turn(*a, **k):
        called["run"] = True
        return "ok"

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn
        app._vision_caps = {}  # unknown
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        box.text = "[Image #1] look"
        await pilot.press("enter")
        await pilot.pause()
        assert called["run"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_image_paste.py::test_text_only_model_blocks_image_submit_with_warning tests/test_image_paste.py::test_unknown_capability_allows_image_submit -v`
Expected: FAIL — `AttributeError: 'HarnessApp' object has no attribute '_vision_caps'`

- [ ] **Step 3: Add the cap cache and the gate**

In `app.py`, import the widget if not already (`NoticeMessage` is already imported per existing tests). Initialise the cache in `HarnessApp.__init__` (add the attribute near the other instance state):

```python
        self._vision_caps: dict[str, "Optional[bool]"] = {}
```

Add the gate method:

```python
    def _image_block_reason(self, attachments) -> "Optional[str]":
        """A warning to show instead of submitting, or None to proceed. Only a
        positive text-only capability blocks; unknown always proceeds."""
        if not attachments:
            return None
        if self._vision_caps.get(self.harness.model_id) is False:
            return (f"{self.harness.model_id} can't read images — "
                    "switch to a vision model (Ctrl+P) or remove the image.")
        return None
```

In `on_prompt_input_submitted`, check the gate before starting the worker (place it after the `/`-command branch, before mounting the user message):

```python
        reason = self._image_block_reason(event.attachments)
        if reason is not None:
            log = self.query_one("#log", VerticalScroll)
            await log.mount(NoticeMessage(reason))
            return
```

Add opportunistic cap population: after a successful clipboard/file attach the app can't easily hook in, so populate on submit's happy path is too late. Instead, kick a one-shot background fetch when the picker is opened (caps are then ready for the next submit). In `open_model_picker` (line ~557), after obtaining `source`, schedule a fetch that fills the cache (non-blocking, best-effort):

```python
        self.run_worker(self._refresh_vision_caps(source.list_models),
                        exclusive=False)
```

and add:

```python
    async def _refresh_vision_caps(self, fetch) -> None:
        from ...workspace import model_supports_images  # noqa: F401

        try:
            entries = await fetch()
        except Exception:
            return  # unknown stays unknown; never blocks submit
        self._vision_caps = {e.id: e.supports_images for e in entries}
```

(The import of `model_supports_images` is not strictly needed here since we read `e.supports_images` directly; drop it if pyright flags it unused.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_image_paste.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_image_paste.py
git commit -m "feat(images): warn before sending images to text-only models"
```

---

### Task 9: Persist images as cache references (externalize / rehydrate)

Keep session JSON small and resumable: replace inline base64 with `marim-image-cache://<sha>` on save, restore on load, and degrade gracefully if a cache file is gone.

**Files:**
- Modify: `src/marim_harness/images.py`
- Modify: `src/marim_harness/session/store.py` (`save` ~line 72, `load` ~line 101)
- Test: `tests/test_images.py`, `tests/test_session.py`

**Interfaces:**
- Produces: `externalize_images(messages: list[dict], session_id: str) -> list[dict]` — for every user-prompt binary content item, writes its bytes to the cache and replaces `data` with `marim-image-cache://<sha>`; mutates and returns the same list.
- Produces: `rehydrate_images(messages: list[dict], session_id: str) -> list[dict]` — replaces each `marim-image-cache://<sha>` reference with base64 read from the cache; a missing file degrades the item to the string `"[image unavailable]"`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_images.py
import base64


def _binary_message(data_b64, media_type="image/png"):
    return [{"parts": [{"part_kind": "user-prompt", "content": [
        "hi", {"kind": "binary", "data": data_b64, "media_type": media_type,
               "identifier": "x", "vendor_metadata": None}]}]}]


def test_externalize_then_rehydrate_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    raw_bytes = b"\x89PNGpayload"
    b64 = base64.b64encode(raw_bytes).decode()
    msgs = _binary_message(b64)
    out = images.externalize_images(msgs, "sess")
    item = out[0]["parts"][0]["content"][1]
    assert item["data"].startswith("marim-image-cache://")
    assert b64 not in str(out)  # base64 no longer inline
    back = images.rehydrate_images(out, "sess")
    assert back[0]["parts"][0]["content"][1]["data"] == b64


def test_rehydrate_degrades_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    msgs = [{"parts": [{"part_kind": "user-prompt", "content": [
        "hi", {"kind": "binary", "data": "marim-image-cache://deadbeef",
               "media_type": "image/png", "identifier": "x",
               "vendor_metadata": None}]}]}]
    back = images.rehydrate_images(msgs, "sess")
    assert back[0]["parts"][0]["content"][1] == "[image unavailable]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_images.py::test_externalize_then_rehydrate_round_trips tests/test_images.py::test_rehydrate_degrades_when_cache_missing -v`
Expected: FAIL — `AttributeError: ... 'externalize_images'`

- [ ] **Step 3: Add externalize/rehydrate to `images.py`**

Add `import base64` to the imports, then:

```python
_REF_PREFIX = "marim-image-cache://"


def _iter_user_content(messages: list[dict]):
    """Yield each user-prompt content list so callers can edit binary items
    in place."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []) or []:
            if not isinstance(part, dict) or part.get("part_kind") != "user-prompt":
                continue
            content = part.get("content")
            if isinstance(content, list):
                yield content


def externalize_images(messages: list[dict], session_id: str) -> list[dict]:
    """Replace inline base64 in binary user-content with cache references."""
    for content in _iter_user_content(messages):
        for item in content:
            if not (isinstance(item, dict) and item.get("kind") == "binary"):
                continue
            data = item.get("data")
            if not isinstance(data, str) or data.startswith(_REF_PREFIX):
                continue
            try:
                raw = base64.b64decode(data)
            except (ValueError, TypeError):
                continue
            cached = store_image(session_id, raw, item.get("media_type", "image/png"))
            item["data"] = f"{_REF_PREFIX}{cached.sha}"
    return messages


def rehydrate_images(messages: list[dict], session_id: str) -> list[dict]:
    """Restore base64 from cache references; missing files degrade to a text
    placeholder so the session still loads."""
    for content in _iter_user_content(messages):
        for i, item in enumerate(content):
            if not (isinstance(item, dict) and item.get("kind") == "binary"):
                continue
            data = item.get("data")
            if not (isinstance(data, str) and data.startswith(_REF_PREFIX)):
                continue
            sha = data[len(_REF_PREFIX):]
            ext = media_ext(item.get("media_type", "image/png"))
            path = image_cache_root() / session_id / f"{sha}.{ext}"
            try:
                content[i] = item  # keep ref unless we can restore
                raw = path.read_bytes()
            except OSError:
                content[i] = "[image unavailable]"
                continue
            item["data"] = base64.b64encode(raw).decode()
    return messages
```

- [ ] **Step 4: Run the new images tests**

Run: `uv run pytest tests/test_images.py -v`
Expected: PASS

- [ ] **Step 5: Wire into `SessionStore.save`/`load`**

In `store.py`, add the import at the top:

```python
from ..images import externalize_images, rehydrate_images
```

In `save` (line ~95), replace the `messages` line:

```python
        messages_json = json.loads(ModelMessagesTypeAdapter.dump_json(history))
        messages_json = externalize_images(messages_json, self.session_id)
```

and use `messages_json` in the payload:

```python
            "messages": messages_json,
```

In `load` (line ~107), rehydrate before validating:

```python
        raw_messages = rehydrate_images(data.get("messages", []), self.session_id)
        messages = ModelMessagesTypeAdapter.validate_python(raw_messages)
```

- [ ] **Step 6: Write a session round-trip test**

```python
# add to tests/test_session.py
import pytest
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart
from pydantic_ai.usage import RunUsage

from marim_harness.session.store import SessionManager


def test_session_save_load_round_trips_image(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "imgs"))
    mgr = SessionManager(tmp_path / "ws", base_dir=tmp_path / "sessions")
    store = mgr.create("with-image")
    history = [ModelRequest(parts=[UserPromptPart(
        content=["see this", BinaryContent(data=b"\x89PNGz", media_type="image/png")]
    )])]
    store.save(history, RunUsage())
    # session JSON must not carry the base64 payload inline
    assert "marim-image-cache://" in store.path.read_text()
    loaded, _usage, _tasks = store.load()
    parts = loaded[0].parts
    binaries = [c for c in parts[0].content if isinstance(c, BinaryContent)]
    assert binaries and binaries[0].data == b"\x89PNGz"
```

- [ ] **Step 7: Run the session and full suite**

Run: `uv run pytest tests/test_session.py::test_session_save_load_round_trips_image -v`
Expected: PASS

Run: `uv run pytest`
Expected: PASS (whole suite)

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/images.py src/marim_harness/session/store.py tests/test_images.py tests/test_session.py
git commit -m "feat(images): persist attachments as cache references with graceful fallback"
```

---

### Task 10: Remaining platform clipboard backends (X11, macOS, Windows)

**Files:**
- Modify: `src/marim_harness/images.py` (`read_clipboard_image` dispatch + backends)
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes/extends: `read_clipboard_image()` — dispatch order: Wayland (if `WAYLAND_DISPLAY`), else X11 (if `DISPLAY`), else macOS (if `sys.platform == "darwin"`), else Windows (if `sys.platform == "win32"`), else `None`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_images.py
import sys


def test_read_clipboard_image_x11(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(images.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake_run(cmd, **kw):
        import subprocess
        if cmd[-1] == "TARGETS":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"image/png\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"\x89PNGx11")

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.read_clipboard_image() == (b"\x89PNGx11", "image/png")


def test_read_clipboard_image_macos(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(images.shutil, "which", lambda n: "/usr/bin/pngpaste")

    def fake_run(cmd, **kw):
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, stdout=b"\x89PNGmac")

    monkeypatch.setattr(images.subprocess, "run", fake_run)
    assert images.read_clipboard_image() == (b"\x89PNGmac", "image/png")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_images.py::test_read_clipboard_image_x11 tests/test_images.py::test_read_clipboard_image_macos -v`
Expected: FAIL (X11/macOS return `None` today)

- [ ] **Step 3: Add backends and dispatch**

Add `import sys` to the imports. Add backends and update dispatch:

```python
def _read_x11() -> Optional[tuple[bytes, str]]:
    if not shutil.which("xclip"):
        return None
    targets = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    if not targets:
        return None
    available = targets.decode("utf-8", "replace").split()
    target = "image/png" if "image/png" in available else next(
        (t for t in available if t.startswith("image/")), None)
    if target is None:
        return None
    data = _run(["xclip", "-selection", "clipboard", "-t", target, "-o"])
    if not data:
        return None
    return data, target


def _read_macos() -> Optional[tuple[bytes, str]]:
    if not shutil.which("pngpaste"):
        return None
    data = _run(["pngpaste", "-"])
    if not data:
        return None
    return data, "image/png"


def _read_windows() -> Optional[tuple[bytes, str]]:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$i=[System.Windows.Forms.Clipboard]::GetImage();"
        "if($i -ne $null){$m=New-Object System.IO.MemoryStream;"
        "$i.Save($m,[System.Drawing.Imaging.ImageFormat]::Png);"
        "[Console]::OpenStandardOutput().Write($m.ToArray(),0,$m.Length)}"
    )
    data = _run(["powershell", "-NoProfile", "-Command", script])
    if not data:
        return None
    return data, "image/png"
```

Replace the body of `read_clipboard_image`:

```python
def read_clipboard_image() -> Optional[tuple[bytes, str]]:
    """The image currently on the OS clipboard as (bytes, media_type), or None
    when there is none or no platform helper is available."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return _read_wayland()
    if os.environ.get("DISPLAY"):
        return _read_x11()
    if sys.platform == "darwin":
        return _read_macos()
    if sys.platform == "win32":
        return _read_windows()
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_images.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/images.py tests/test_images.py
git commit -m "feat(images): X11, macOS, and Windows clipboard backends"
```

---

### Task 11: Document the native-paste caveat and helpers

**Files:**
- Modify: `README.md`

**Interfaces:** none.

- [ ] **Step 1: Add a section to README.md**

Add under the usage/TUI section:

```markdown
### Image input

Vision-capable models can read pasted images.

- **Paste a screenshot:** copy an image, then press **`Ctrl+V`** in the prompt
  (the harness reads the OS clipboard and inserts an `[Image #N]` marker).
  Note: your terminal's *native* paste (often `Ctrl+Shift+V` / `Cmd+V`) only
  pastes **text** — image bytes arrive only through the app-intercepted `Ctrl+V`.
- **Paste a file path:** paste or drag an image file; a bare path to an existing
  image is attached automatically.

Clipboard image reading needs a helper per platform: `wl-clipboard` (Wayland),
`xclip` (X11), `pngpaste` (macOS); Windows uses built-in PowerShell. Without one,
the file-path method still works. If the active model is known to be text-only,
the harness warns instead of sending. Cached images live under
`~/.marim/image-cache/` (override with `MARIM_IMAGE_CACHE_DIR`).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(images): document Ctrl+V image paste and the native-paste caveat"
```

---

## Final verification

- [ ] **Run the full suite, lint, and types**

```bash
uv run pytest
uv run ruff check src tests
uv run pyright
```
Expected: all green.

- [ ] **Manual end-to-end** (vision model): copy a screenshot, `Ctrl+V` in the prompt, send, confirm the model describes the image; resume the session and confirm the image rehydrates from cache.
