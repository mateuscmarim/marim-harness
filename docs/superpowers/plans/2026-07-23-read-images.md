# read_file Image Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `read_file` returns image files as model-visible image content on vision-capable models, gated by the provider catalog.

**Architecture:** The impl layer (`tools/impl/fs.py`) detects image extensions and returns raw `(bytes, media_type)`; the tool layer (`tools/fs_tools.py`, now async) applies a per-call vision gate read from a new `services.supports_images` seam (wired in `build_collaborators` from `cfg.model_source`, backed by a one-shot-cached catalog fetch) and wraps passing reads in pydantic-ai `BinaryContent`. Session persistence, the TUI renderers, and compaction each get a small guard so binary tool returns are cached/displayed/masked as images, never as raw byte dumps.

**Tech Stack:** Python 3.10+, pydantic-ai (`BinaryContent`, `ToolReturnPart`), pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-07-23-read-images-design.md`

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity cap 10 — extract helpers rather than `# noqa: C901`.
- Image size cap: **5 MB** (`_MAX_IMAGE_BYTES = 5 * 1024 * 1024`).
- Gate semantics: only an explicit catalog `False` downgrades an image read to a text notice; `True`, unknown (`None`), fetch failure, and no-catalog-composed all send the image (optimistic).
- Recognized image extensions come from the existing `media_type_for_path` in `src/marim_harness/images.py` (png/jpg/jpeg/webp/gif) — do not add a second extension table.
- Preserve existing explanatory comments when editing nearby code.

---

### Task 1: Impl layer — image branch in `fs.read_file`

**Files:**
- Modify: `src/marim_harness/tools/impl/fs.py` (branch goes in `read_file`, currently lines 203–254; helper next to `_looks_binary` ~line 190)
- Test: `tests/test_fs.py`

**Interfaces:**
- Consumes: `media_type_for_path(path: Path) -> str | None` from `marim_harness.images` (exists).
- Produces: `fs.read_file(...) -> str | tuple[bytes, str]` — a `(data, media_type)` tuple for an in-cap image, a `str` for everything else (text window, binary notice, over-cap notice). Task 4's tool layer switches on `isinstance(out, str)`. Also `fs._MAX_IMAGE_BYTES: int` (monkeypatchable in tests).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_fs.py` (it already has `from marim_harness.tools.impl import fs` and `from pathlib import Path`):

```python
def test_read_file_returns_image_bytes_and_media_type(tmp_path: Path):
    raw = b"\x89PNG\r\n\x1a\nfakepixels"
    (tmp_path / "shot.png").write_bytes(raw)
    out = fs.read_file(tmp_path, "shot.png")
    assert out == (raw, "image/png")


def test_read_file_image_ignores_offset_and_limit(tmp_path: Path):
    raw = b"\x89PNG\r\n\x1a\nfakepixels"
    (tmp_path / "shot.png").write_bytes(raw)
    out = fs.read_file(tmp_path, "shot.png", offset=1, limit=5)
    assert out == (raw, "image/png")


def test_read_file_image_over_cap_returns_notice(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fs, "_MAX_IMAGE_BYTES", 10)
    (tmp_path / "big.jpg").write_bytes(b"x" * 11)
    out = fs.read_file(tmp_path, "big.jpg")
    assert isinstance(out, str)
    assert "model-visible images" in out


def test_read_file_image_records_read_ledger(tmp_path: Path):
    class _Ledger:
        def __init__(self):
            self.paths = []

        def record(self, p):
            self.paths.append(p)

    (tmp_path / "shot.png").write_bytes(b"\x89PNGdata")
    ledger = _Ledger()
    fs.read_file(tmp_path, "shot.png", ledger=ledger)
    assert ledger.paths and ledger.paths[0].name == "shot.png"


def test_read_file_over_cap_image_not_recorded_in_ledger(tmp_path: Path, monkeypatch):
    class _Ledger:
        def __init__(self):
            self.paths = []

        def record(self, p):
            self.paths.append(p)

    monkeypatch.setattr(fs, "_MAX_IMAGE_BYTES", 10)
    (tmp_path / "big.png").write_bytes(b"x" * 11)
    ledger = _Ledger()
    fs.read_file(tmp_path, "big.png", ledger=ledger)
    assert ledger.paths == []


def test_read_file_non_image_binary_still_refuses(tmp_path: Path):
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02")
    out = fs.read_file(tmp_path, "blob.dat")
    assert isinstance(out, str)
    assert "binary file" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_fs.py -k image -v`
Expected: FAIL — `read_file` returns the `"binary file, cannot display."` string (PNG bytes contain no NUL in these fixtures, so the tuple assertion fails on the numbered-text rendering instead; either way, red).

- [ ] **Step 3: Implement the image branch** in `src/marim_harness/tools/impl/fs.py`.

Add to the module's imports (top of file, keeping ruff import order — `..._safe_read`/`ModelRetry` imports already exist):

```python
from ...images import media_type_for_path
```

Add next to `_looks_binary` (~line 190):

```python
# Provider image-size ceilings sit around 5 MB (Anthropic's documented limit);
# larger files come back as a text notice rather than a doomed upload.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _read_image(p: Path, path: str, media_type: str, ledger: "ReadLedger | None"):
    """Image bytes + media type for a model-visible image read, or a text
    notice when the file exceeds the size cap. The ledger is recorded only on
    a successful read — an over-cap notice means the agent has not observed
    the content, so it must not satisfy the read-before-edit guard."""
    size = p.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        cap_mb = _MAX_IMAGE_BYTES // (1024 * 1024)
        return (
            f"{path}: image is {size / (1024 * 1024):.1f} MB — over the "
            f"{cap_mb} MB limit for model-visible images."
        )
    if ledger is not None:
        ledger.record(p)
    return p.read_bytes(), media_type
```

(Match the `ReadLedger` annotation style to how the existing `read_file` signature spells it — reuse the same imported name, quoted only if the file already quotes it.)

In `read_file`, after the `if not p.is_file(): raise ModelRetry(...)` check and **before** the `_looks_binary` sniff, insert:

```python
    # An image is returned as raw bytes for the tool layer to wrap as
    # model-visible content (spec 2026-07-23-read-images-design). Checked
    # before the binary sniff: a valid image is binary, but not "cannot
    # display". offset/limit don't apply to images.
    media_type = media_type_for_path(p)
    if media_type is not None:
        return _read_image(p, path, media_type, ledger)
```

Update `read_file`'s return annotation to `-> "str | tuple[bytes, str]"` (or unquoted if the file doesn't use deferred annotations) and add one line to its docstring: `An image file (png/jpg/webp/gif) under the 5 MB cap returns ``(bytes, media_type)`` instead of text; the tool layer wraps it as model-visible image content.`

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_fs.py -v`
Expected: ALL PASS (including the pre-existing read_file tests — the image branch must not disturb text reads).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/tools/impl/fs.py tests/test_fs.py
git commit -m "feat(fs): read_file impl returns image bytes for image extensions"
```

---

### Task 2: Catalog vision-gate factory

**Files:**
- Modify: `src/marim_harness/workspace/catalog.py` (append after `model_supports_images`, ~line 312)
- Modify: `src/marim_harness/workspace/__init__.py` (re-export)
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: `model_supports_images(entries, model_id) -> bool | None` (exists, same file); `ModelEntry` (exists).
- Produces: `make_supports_images(list_models: Callable[[], Awaitable[list[ModelEntry]]]) -> Callable[[str], Awaitable[bool | None]]` — Task 3 wires it into services; Task 4 awaits the returned callable.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_catalog.py` (check its existing imports; it already imports `ModelEntry`; add `make_supports_images` to that import and `import pytest` if absent):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_catalog.py -k supports_images -v`
Expected: FAIL with `ImportError: cannot import name 'make_supports_images'`.

- [ ] **Step 3: Implement** in `src/marim_harness/workspace/catalog.py`. Add `from collections.abc import Awaitable, Callable` to the imports, then append:

```python
def make_supports_images(
    list_models: Callable[[], Awaitable[list[ModelEntry]]],
) -> Callable[[str], Awaitable[bool | None]]:
    """A per-call vision gate over a lazily-fetched, one-shot-cached catalog.

    The first call fetches the catalog once; success and failure are both
    cached for the life of the closure (a transient startup failure degrades
    the whole session to "unknown" — acceptable, because unknown is treated
    optimistically by the reader and never blocks). Never raises. A rare
    concurrent first call may skip the fetch and return None once — also
    just an optimistic send, not worth a lock.
    """
    entries: list[ModelEntry] | None = None
    attempted = False

    async def supports(model_id: str) -> bool | None:
        nonlocal entries, attempted
        if not attempted:
            attempted = True
            try:
                entries = await list_models()
            except Exception:  # noqa: BLE001 — any catalog failure means "unknown"
                logger.debug("vision-gate catalog fetch failed", exc_info=True)
                entries = None
        if entries is None:
            return None
        return model_supports_images(entries, model_id)

    return supports
```

(If ruff complains about the `noqa` code not being in the lint set, drop the noqa comment — `BLE001` is not enabled in this repo; keep the plain `except Exception`.)

Add `make_supports_images` to the catalog re-exports in `src/marim_harness/workspace/__init__.py` (both the import list next to `model_supports_images` at line 20 and the `__all__` entry at line 65).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_catalog.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/catalog.py src/marim_harness/workspace/__init__.py tests/test_catalog.py
git commit -m "feat(catalog): make_supports_images one-shot cached vision gate"
```

---

### Task 3: `services.supports_images` seam + construction wiring

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (`HarnessServices`, lines 118–162)
- Modify: `src/marim_harness/runtime/harness.py` (`build_services` ~line 249; the `build_services(...)` call inside `build_collaborators` ~line 472)
- Test: `tests/test_deps.py`

**Interfaces:**
- Consumes: `make_supports_images` from Task 2.
- Produces: `HarnessServices.supports_images: Callable[[str], Awaitable[bool | None]] | None` (default `None`); `build_services(..., supports_images=...)` pass-through. Task 4 reads `ctx.deps.services.supports_images`.

- [ ] **Step 1: Write the failing test** — in `tests/test_deps.py`, extend `test_build_services_populates_and_assigns` (line 47): inside the test add a fake gate and pass it through, keeping every existing assertion:

```python
    async def fake_gate(model_id: str):
        return True

    services = build_services(
        deps, lsp=lsp, turn_hooks=turn_hooks, subagents=subs, supports_images=fake_gate
    )
```

(replace the existing `services = build_services(...)` line), and append at the end of the test:

```python
    assert services.supports_images is fake_gate
```

Also add a default check as a new test right after it:

```python
def test_harness_services_supports_images_defaults_none():
    from marim_harness.runtime.deps import HarnessServices

    assert HarnessServices().supports_images is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_deps.py -k "build_services or supports_images" -v`
Expected: FAIL with `TypeError: build_services() got an unexpected keyword argument 'supports_images'`.

- [ ] **Step 3: Implement.**

In `src/marim_harness/runtime/deps.py`, add a field to `HarnessServices` after `advise` (line 162), matching the file's comment style (`Awaitable` may need adding to the `collections.abc` import):

```python
    # Whether a model accepts image input, per the provider catalog. Async: the
    # first call may fetch the catalog (one-shot cached — see
    # workspace/catalog.make_supports_images). Keyed by the model's unqualified
    # id (``ctx.model.model_name``), so the same gate serves the main loop and
    # sub-agents on tiered models. None ⇒ no catalog source composed; readers
    # treat capability as unknown and stay optimistic.
    supports_images: Callable[[str], Awaitable[bool | None]] | None = None
```

In `src/marim_harness/runtime/harness.py`:
1. `build_services` gains a keyword param `supports_images: Callable[[str], Awaitable[bool | None]] | None = None` and passes it into the `HarnessServices(...)` construction.
2. Inside `build_collaborators`, just above the `build_services(...)` call (~line 472), wire it from the model source (add `from ..workspace.catalog import make_supports_images` to harness.py's imports):

```python
    # Vision gate for read_file image returns: catalog-backed when a model
    # source is composed (CLI path), None for explicit-model embedders
    # (HarnessBuilder) — where unknown capability sends images optimistically.
    supports_images = (
        make_supports_images(cfg.model_source.list_models)
        if cfg.model_source is not None
        else None
    )
```

and pass `supports_images=supports_images` in the `build_services(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_deps.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/deps.py src/marim_harness/runtime/harness.py tests/test_deps.py
git commit -m "feat(runtime): services.supports_images vision-gate seam, wired from model_source"
```

---

### Task 4: Tool layer — async `read_file` returning `BinaryContent`

**Files:**
- Modify: `src/marim_harness/tools/fs_tools.py` (`read_file`, lines 23–45)
- Modify: `tests/test_lsp_tools.py:109` and `tests/test_lsp_tools.py:132` (add `await`)
- Modify: `tests/test_scratchpad.py:106` (add `await`) and `tests/test_scratchpad.py:115-122` (`test_read_tool_reaches_scratchpad` becomes async)
- Create: `tests/test_read_images.py`

**Interfaces:**
- Consumes: `fs.read_file(...) -> str | tuple[bytes, str]` (Task 1); `ctx.deps.services.supports_images` (Task 3); `BinaryContent` from `pydantic_ai.messages`.
- Produces: `async def read_file(ctx, path, offset=1, limit=None) -> str | BinaryContent` — pydantic-ai registers async tools identically (`provider.py` needs no change); sub-agents get the same function with their own `ctx.model`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_read_images.py`:

```python
"""Tool-layer tests for read_file image support: the vision gate and the
BinaryContent return (spec 2026-07-23-read-images-design)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import BinaryContent

from marim_harness.runtime.deps import Deps, HarnessServices, UIHooks, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools import fs_tools

pytestmark = pytest.mark.anyio

PNG = b"\x89PNG\r\n\x1a\nfakepixels"


def _ctx(root: Path, *, gate=None, model_name="test-model"):
    deps = Deps(workspace=WorkspaceConfig(root=root, mode=Mode.auto), ui=UIHooks())
    deps.services = HarnessServices(supports_images=gate)
    return SimpleNamespace(deps=deps, model=SimpleNamespace(model_name=model_name))


async def test_image_returned_as_binary_content_without_gate(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    out = await fs_tools.read_file(_ctx(tmp_path), "shot.png")
    assert isinstance(out, BinaryContent)
    assert out.data == PNG
    assert out.media_type == "image/png"


async def test_image_blocked_when_catalog_says_no_vision(tmp_path):
    async def gate(model_id):
        return False

    (tmp_path / "shot.png").write_bytes(PNG)
    out = await fs_tools.read_file(_ctx(tmp_path, gate=gate), "shot.png")
    assert isinstance(out, str)
    assert "does not accept image input" in out


async def test_image_sent_when_capability_unknown(tmp_path):
    async def gate(model_id):
        return None

    (tmp_path / "shot.png").write_bytes(PNG)
    out = await fs_tools.read_file(_ctx(tmp_path, gate=gate), "shot.png")
    assert isinstance(out, BinaryContent)


async def test_gate_receives_current_model_name(tmp_path):
    seen = []

    async def gate(model_id):
        seen.append(model_id)
        return True

    (tmp_path / "shot.png").write_bytes(PNG)
    await fs_tools.read_file(_ctx(tmp_path, gate=gate, model_name="acme/vlm-1"), "shot.png")
    assert seen == ["acme/vlm-1"]


async def test_text_read_still_returns_numbered_lines(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    out = await fs_tools.read_file(_ctx(tmp_path), "a.txt")
    assert isinstance(out, str)
    assert "1\thello" in out


async def test_ctx_without_model_attr_stays_optimistic(tmp_path):
    async def gate(model_id):  # pragma: no cover — must not be called
        raise AssertionError("gate must not be called without a model name")

    (tmp_path / "shot.png").write_bytes(PNG)
    ctx = SimpleNamespace(
        deps=_ctx(tmp_path).deps, model=SimpleNamespace(model_name=None)
    )
    ctx.deps.services = HarnessServices(supports_images=gate)
    out = await fs_tools.read_file(ctx, "shot.png")
    assert isinstance(out, BinaryContent)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_read_images.py -v`
Expected: FAIL — `TypeError: object str can't be used in 'await' expression` (read_file is still sync) or tuple-vs-BinaryContent assertion errors.

- [ ] **Step 3: Implement** in `src/marim_harness/tools/fs_tools.py`. Add `from pydantic_ai.messages import BinaryContent` to the imports. Replace `read_file` with:

```python
async def read_file(
    ctx: RunContext[Deps], path: str, offset: int = 1, limit: int | None = None
) -> str | BinaryContent:
    """Read a file. `path` is relative to the workspace root.

    For large text files, read a window instead of the whole thing: `offset` is
    the 1-based line to start at and `limit` caps the line count. Prefer locating
    what you need first (with `grep`/`tree`) and reading a targeted range — a
    read with no `limit` is capped and will tell you how to page on.

    Image files (png/jpg/webp/gif, up to 5 MB) are returned as viewable images
    on models that accept image input; `offset`/`limit` don't apply to them.
    Other binary files cannot be displayed.

    Skill directories (which may live outside the workspace) are also readable by
    their absolute path, so a skill's bundled files can be read this way too.
    Files in the session scratchpad directory are likewise readable by absolute
    path."""
    # Whitelist every discovered skill's directory for reading, so an agent that
    # reaches for a skill's bundled file by absolute path succeeds even when the
    # skill lives outside the workspace (discover_skills is cached per workspace).
    skills = discover_skills(ctx.deps.workspace.root, dirs=ctx.deps.workspace.skill_dirs)
    skill_roots = tuple(s.root for s in skills)
    out = fs.read_file(
        ctx.deps.workspace.root, path, offset=offset, limit=limit,
        extra_read_roots=skill_roots + scratch_roots(ctx), ledger=ctx.deps.reads,
    )
    if isinstance(out, str):
        return out
    data, media_type = out
    if await _model_accepts_images(ctx) is False:
        return f"{path}: image file — the current model does not accept image input."
    return BinaryContent(data=data, media_type=media_type)


async def _model_accepts_images(ctx: RunContext[Deps]) -> bool | None:
    """Catalog verdict for the *current* agent's model (main loop or sub-agent —
    each RunContext carries its own model). Only an explicit False downgrades an
    image read; None (no catalog composed, fetch failed, model unlisted, or no
    model name on the context) stays optimistic, mirroring the thinking-level
    rule that best-effort detection never blocks."""
    gate = ctx.deps.services.supports_images
    if gate is None:
        return None
    model_name = getattr(getattr(ctx, "model", None), "model_name", None)
    if not model_name:
        return None
    return await gate(model_name)
```

- [ ] **Step 4: Update the four existing sync call sites**

- `tests/test_lsp_tools.py:109` and `:132`: `fs_tools.read_file(ctx, "m.py")` → `await fs_tools.read_file(ctx, "m.py")` (both enclosing tests are already `async`).
- `tests/test_scratchpad.py:106`: `fs_tools.read_file(ctx, str(scratch / "note.txt"))` → `await fs_tools.read_file(ctx, str(scratch / "note.txt"))` (enclosing test already async).
- `tests/test_scratchpad.py` `test_read_tool_reaches_scratchpad` (~line 115): make it async and await:

```python
@pytest.mark.anyio
async def test_read_tool_reaches_scratchpad(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    ws.mkdir()
    scratch.mkdir()
    (scratch / "data.txt").write_text("payload")
    out = await fs_tools.read_file(_ctx(ws, scratch), str(scratch / "data.txt"))
    assert "payload" in out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_read_images.py tests/test_lsp_tools.py tests/test_scratchpad.py tests/test_provider.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tools/fs_tools.py tests/test_read_images.py tests/test_lsp_tools.py tests/test_scratchpad.py
git commit -m "feat(tools): read_file returns images as BinaryContent behind the vision gate"
```

---

### Task 5: Session persistence — externalize/rehydrate binary tool returns

**Files:**
- Modify: `src/marim_harness/images.py` (`_iter_user_content` lines 214–225, `externalize_images` lines 232–247, `rehydrate_images` lines 250–270)
- Test: `tests/test_images.py`

**Interfaces:**
- Consumes: serialized message dicts — `ToolReturnPart` persists as `{"part_kind": "tool-return", "content": <scalar-or-list>}` where a binary item is `{"kind": "binary", "data": ..., "media_type": ...}`.
- Produces: `externalize_images`/`rehydrate_images` (same signatures) now also cover `tool-return` parts, scalar and list content. `store.py` call sites (lines 228, 284) are unchanged.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_images.py` (uses its existing `import base64`, `images` imports):

```python
def _tool_return_message(content):
    return [{"parts": [{"part_kind": "tool-return", "tool_name": "read_file",
                        "tool_call_id": "t1", "content": content}]}]


def _binary_item(data):
    return {"kind": "binary", "data": data, "media_type": "image/png",
            "identifier": "x", "vendor_metadata": None}


def test_externalize_tool_return_scalar_binary_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    b64 = base64.urlsafe_b64encode(b"\x89PNGtoolreturn").decode()
    msgs = _tool_return_message(_binary_item(b64))
    out = images.externalize_images(msgs, "sess")
    item = out[0]["parts"][0]["content"]
    assert item["data"].startswith("marim-image-cache://")
    back = images.rehydrate_images(out, "sess")
    assert back[0]["parts"][0]["content"]["data"] == b64


def test_externalize_tool_return_list_binary_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    b64 = base64.urlsafe_b64encode(b"\x89PNGinlist").decode()
    msgs = _tool_return_message(["note", _binary_item(b64)])
    out = images.externalize_images(msgs, "sess")
    assert out[0]["parts"][0]["content"][1]["data"].startswith("marim-image-cache://")
    back = images.rehydrate_images(out, "sess")
    assert back[0]["parts"][0]["content"][1]["data"] == b64


def test_rehydrate_tool_return_missing_cache_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path))
    msgs = _tool_return_message(_binary_item("marim-image-cache://deadbeef"))
    back = images.rehydrate_images(msgs, "sess")
    assert back[0]["parts"][0]["content"] == "[image unavailable]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_images.py -k tool_return -v`
Expected: FAIL — tool-return content passes through untouched (`data` still inline base64).

- [ ] **Step 3: Implement.** In `src/marim_harness/images.py`, replace `_iter_user_content` with a slot iterator covering both part kinds (keep the module docstring's framing; update the first docstring line to mention tool returns):

```python
def _iter_binary_slots(messages: list[dict]):
    """Yield ``(container, key)`` pairs where ``container[key]`` is a binary
    content item — inside user-prompt content lists and tool-return parts
    (whose content may be a scalar binary or a list containing one). Yielding
    the slot rather than the item lets rehydrate replace an unavailable image
    with a text placeholder in place."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("part_kind")
            if kind not in ("user-prompt", "tool-return"):
                continue
            content = part.get("content")
            if isinstance(content, list):
                for i, item in enumerate(content):
                    if isinstance(item, dict) and item.get("kind") == "binary":
                        yield content, i
            elif (
                kind == "tool-return"
                and isinstance(content, dict)
                and content.get("kind") == "binary"
            ):
                yield part, "content"
```

Rewrite the two walkers over slots (bodies otherwise unchanged — same ref prefix, same urlsafe alphabet comment kept above `externalize_images`):

```python
def externalize_images(messages: list[dict], session_id: str) -> list[dict]:
    """Replace inline base64 in binary user/tool-return content with cache refs."""
    for container, key in _iter_binary_slots(messages):
        item = container[key]
        data = item.get("data")
        if not isinstance(data, str) or data.startswith(_REF_PREFIX):
            continue
        try:
            raw = base64.urlsafe_b64decode(data)
        except (ValueError, TypeError):
            continue
        cached = store_image(session_id, raw, item.get("media_type", "image/png"))
        item["data"] = f"{_REF_PREFIX}{cached.sha}"
    return messages


def rehydrate_images(messages: list[dict], session_id: str) -> list[dict]:
    """Restore base64 from cache references; missing files degrade to a text
    placeholder so the session still loads."""
    for container, key in _iter_binary_slots(messages):
        item = container[key]
        data = item.get("data")
        if not (isinstance(data, str) and data.startswith(_REF_PREFIX)):
            continue
        sha = data[len(_REF_PREFIX):]
        ext = media_ext(item.get("media_type", "image/png"))
        path = image_cache_root() / _safe_session_segment(session_id) / f"{sha}.{ext}"
        try:
            # mutate item in place on success; replace with placeholder on OSError
            raw = path.read_bytes()
        except OSError:
            container[key] = "[image unavailable]"
            continue
        item["data"] = base64.urlsafe_b64encode(raw).decode()
    return messages
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_images.py tests/test_session.py -v`
Expected: ALL PASS (session round-trip tests confirm the store call sites still work).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/images.py tests/test_images.py
git commit -m "feat(session): externalize/rehydrate binary tool-return content"
```

---

### Task 6: Rendering + compaction guards for binary tool returns

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (new helper near `status_from_part` ~line 62; use at line 1045)
- Modify: `src/marim_harness/interfaces/tui/session_view.py` (`_replay_tool_return_part`, `content = str(part.content)` ~line 111)
- Modify: `src/marim_harness/compaction.py` (`estimate_tokens` lines 54–76, `_mask_part` line 384, `render_transcript` ToolReturnPart arm ~line 510)
- Test: `tests/test_compaction.py`, `tests/test_read_images.py`

**Interfaces:**
- Consumes: `BinaryContent` (`pydantic_ai.messages`); `MASKED_OBSERVATION` (exists in compaction.py).
- Produces: `tool_result_text(content: object) -> str` in `stream_render.py` — used by both renderers; safe on any content value.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/test_read_images.py`:

```python
def test_tool_result_text_renders_image_placeholder():
    from marim_harness.interfaces.tui.stream_render import tool_result_text

    img = BinaryContent(data=b"x" * 2048, media_type="image/png")
    assert tool_result_text(img) == "[image image/png, 2 KB]"
    assert tool_result_text([img, "note"]) == "[image image/png, 2 KB] note"
    assert tool_result_text("plain") == "plain"
    assert tool_result_text(None) == "None"
```

Append to `tests/test_compaction.py` (reuse its existing imports of `ModelRequest`, `ToolReturnPart`, `UserPromptPart`, `BinaryContent` where present — add any missing ones from `pydantic_ai.messages`, and import `MASKED_OBSERVATION`, `estimate_tokens`, `mask_stale_observations`, `render_transcript` from `marim_harness.compaction` if not already imported):

```python
def test_estimate_tokens_counts_scalar_image_tool_return_flat():
    img = BinaryContent(data=b"x" * 100_000, media_type="image/png")
    msg = ModelRequest(parts=[
        ToolReturnPart(tool_name="read_file", content=img, tool_call_id="t1"),
    ])
    tokens = estimate_tokens([msg])
    assert tokens < 100_000 // 4  # flat image cost, not the bytes-repr length
    assert tokens >= 1500


def test_mask_replaces_image_tool_return_regardless_of_min_chars():
    img = BinaryContent(data=b"\x89PNG" + b"p" * 10, media_type="image/png")
    history = [
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content=img, tool_call_id="t1"),
        ]),
        ModelRequest(parts=[UserPromptPart(content="next turn")]),
    ]
    masked, count = mask_stale_observations(history, keep_recent=0, min_chars=10_000)
    assert count == 1
    assert masked[0].parts[0].content == MASKED_OBSERVATION


def test_render_transcript_image_tool_return_is_placeholder():
    img = BinaryContent(data=b"\x89PNGbytes", media_type="image/png")
    history = [ModelRequest(parts=[
        ToolReturnPart(tool_name="read_file", content=img, tool_call_id="t1"),
    ])]
    out = render_transcript(history)
    assert "[image image/png]" in out
    assert "PNGbytes" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_compaction.py -k image -v && uv run pytest --no-cov tests/test_read_images.py -k tool_result_text -v`
Expected: FAIL — import error on `tool_result_text`; token estimate hugely overcounted; mask count 0.

- [ ] **Step 3: Implement.**

`stream_render.py` — add `from pydantic_ai.messages import BinaryContent` to the imports and this helper next to `status_from_part`:

```python
def tool_result_text(content: object) -> str:
    """Display text for a tool-return content value. A binary (image) return
    renders as a compact placeholder — str(BinaryContent) would dump the raw
    bytes repr into the transcript."""
    if isinstance(content, BinaryContent):
        kb = max(1, len(content.data) // 1024)
        return f"[image {content.media_type}, {kb} KB]"
    if isinstance(content, (list, tuple)):
        return " ".join(tool_result_text(item) for item in content)
    return str(content)
```

At line 1045 replace `content = str(getattr(event.part, "content", ""))` with `content = tool_result_text(getattr(event.part, "content", ""))`.

`session_view.py` — in `_replay_tool_return_part`, replace `content = str(part.content)` with `content = tool_result_text(part.content)`, importing `tool_result_text` alongside the module's existing `from .stream_render import ...` imports (top-level import; `status_from_part` is already imported from there — extend that line).

`compaction.py`:

1. `estimate_tokens` — in the per-part loop, add a scalar arm before the final `elif content is not None:` branch (this also avoids materializing a multi-MB `str(BinaryContent)`):

```python
            elif isinstance(content, BinaryContent):
                images += 1
```

2. `_mask_part` — immediately after the `if _is_masked(part.content): return None` guard, add:

```python
    content = part.content
    if isinstance(content, BinaryContent) or (
        isinstance(content, list) and any(isinstance(c, BinaryContent) for c in content)
    ):
        # An image observation is always large in effective tokens and has no
        # faithful text rendering to persist — mask it outright with the plain
        # placeholder. The original file is still on disk; a fresh read_file
        # brings it back if the model needs it again.
        return MASKED_OBSERVATION
```

3. `render_transcript` — in the `ToolReturnPart` arm, guard before `_clip`:

```python
            elif isinstance(part, ToolReturnPart):
                content: object = part.content
                if isinstance(content, BinaryContent):
                    content = f"[image {content.media_type}]"
                lines.append(
                    f"Tool {part.tool_name} returned: {_clip(content, max_part_chars)}"
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_compaction.py tests/test_read_images.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/stream_render.py src/marim_harness/interfaces/tui/session_view.py src/marim_harness/compaction.py tests/test_compaction.py tests/test_read_images.py
git commit -m "feat(tui,compaction): render, estimate, and mask binary tool returns as images"
```

---

### Task 7: Changelog + full verification gates

**Files:**
- Modify: `CHANGELOG.md` (the `## [Unreleased]` section, line 9)

**Interfaces:**
- Consumes: everything above.
- Produces: release notes; a green CI-order run.

- [ ] **Step 1: Add the changelog entry** — first bullet under `## [Unreleased]`:

```markdown
- `read_file` now returns image files (png/jpg/webp/gif, up to 5 MB) as
  model-visible images on vision-capable models — screenshots and diagrams can
  be inspected directly, including by sub-agents (gated per spawn's own model).
  Catalog-gated: a model the catalog marks text-only gets a text notice
  instead; unknown capability sends the image optimistically. Image tool
  results are cached content-addressed on disk (not inlined into session
  files) and masked like any other stale observation.
```

- [ ] **Step 2: Run the full gate sequence in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: ruff clean; pyright clean (watch the widened `str | tuple[bytes, str]` / `str | BinaryContent` returns); full suite green with coverage on. Fix anything that surfaces before committing.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for read_file image support"
```
