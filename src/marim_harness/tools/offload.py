# src/marim_harness/tools/offload.py
"""Offload large tool output to a gitignored file instead of flooding context.

A tool builds its full result (bounded by ``MAX_OUTPUT_BYTES``) and passes it
through :func:`offload_if_large`: small results return inline unchanged; large
ones are written under ``.marim/output/`` and replaced by a handle + preview the
agent can page with ``read_file``/``grep``. Mirrors ``fetch``'s offload pattern."""

import hashlib
from pathlib import Path
from typing import Optional

_INLINE_CHAR_LIMIT = 50_000      # at/below this, return inline (~12k tokens)
MAX_OUTPUT_BYTES = 5_000_000     # hard ceiling producers stop collecting at
_PREVIEW_LINES = 40
_OUTPUT_DIR = (".marim", "output")


def _write_handle(content: str, *, kind: str, key: str,
                  workspace_root: Path, capped: bool) -> str:
    digest = hashlib.sha256(f"{kind}\0{key}".encode("utf-8")).hexdigest()[:16]
    rel = Path(*_OUTPUT_DIR, f"{kind}-{digest}.txt")
    dest = workspace_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    lines = content.splitlines()
    preview = "\n".join(lines[:_PREVIEW_LINES])
    cap_note = (
        f"⚠️ Output hit the {MAX_OUTPUT_BYTES:,}-byte ceiling; the file holds what "
        "was collected.\n" if capped else ""
    )
    return (
        f"⚠️ Large {kind} result ({len(content):,} chars, {len(lines):,} lines) — "
        f"full output saved to `{rel.as_posix()}`. Read more with read_file "
        f"(it paginates) or grep that path.\n"
        f"{cap_note}"
        f"--- preview (first {min(_PREVIEW_LINES, len(lines))} lines) ---\n"
        f"{preview}"
    )


def offload_if_large(content: str, *, kind: str, key: str,
                     workspace_root: Optional[Path], capped: bool = False) -> str:
    """Return ``content`` inline when small; otherwise offload to a file and
    return a handle + preview. With no workspace (or on write failure), clip to
    the inline limit instead, so a large result can never flood context."""
    if len(content) <= _INLINE_CHAR_LIMIT:
        return content
    if workspace_root is not None:
        try:
            return _write_handle(content, kind=kind, key=key,
                                 workspace_root=workspace_root, capped=capped)
        except OSError:
            pass
    clipped = content[:_INLINE_CHAR_LIMIT]
    return (
        f"{clipped}\n"
        f"…(output clipped to {_INLINE_CHAR_LIMIT:,} chars; offload unavailable)"
    )
