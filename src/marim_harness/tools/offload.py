# src/marim_harness/tools/offload.py
"""Offload large tool output to a gitignored file instead of flooding context.

A tool builds its full result (bounded by ``MAX_OUTPUT_CHARS``) and passes it
through :func:`offload_if_large`: small results return inline unchanged; large
ones are written under ``.marim/output/`` and replaced by a handle + preview the
agent can page with ``read_file``/``grep``. Mirrors ``fetch``'s offload pattern."""

import hashlib
from pathlib import Path

from ..atomic_io import atomic_write_text

_INLINE_CHAR_LIMIT = 25_000      # at/below this, return inline (~6k tokens)
# Measured in characters (~bytes for ASCII); producers stop collecting here and callers may offload.
MAX_OUTPUT_CHARS = 5_000_000
_PREVIEW_LINES = 40
# The preview glimpses the head of an offloaded result. Bounding it by line count
# alone isn't enough: one minified-JSON / single-line blob is a single "line" of
# megabytes, so a line-only preview would re-flood exactly what offloading exists
# to prevent. Cap the preview's total width too — the full content is in the file
# regardless, so the preview only has to orient the reader.
_PREVIEW_CHARS = 2_000
_OUTPUT_DIR = (".marim", "output")


def _make_preview(lines: list[str]) -> str:
    """First few lines of an offloaded result, bounded in both count and width so
    the preview itself can't flood context (the full body lives in the file)."""
    preview = "\n".join(lines[:_PREVIEW_LINES])
    if len(preview) > _PREVIEW_CHARS:
        preview = preview[:_PREVIEW_CHARS] + "…"
    return preview


def _write_handle(content: str, *, kind: str, key: str,
                  workspace_root: Path, capped: bool) -> str:
    digest = hashlib.sha256(f"{kind}\0{key}".encode()).hexdigest()[:16]
    rel = Path(*_OUTPUT_DIR, f"{kind}-{digest}.txt")
    dest = workspace_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Route through atomic_write_text: the dest is sha-derived, so two concurrent
    # offloads of the same (kind,key) target the *same* filename — a direct
    # write_text would let them clobber each other's partial bytes. The atomic
    # swap (unique temp → os.replace) is exactly what that layer exists to prevent.
    atomic_write_text(dest, content)
    lines = content.splitlines()
    preview = _make_preview(lines)
    cap_note = (
        f"⚠️ Output hit the {MAX_OUTPUT_CHARS:,}-char ceiling; the file holds what "
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


def write_preview_file(content: str, *, rel: Path, workspace_root: Path) -> tuple[str, str, int]:
    """Write *content* to ``workspace_root/rel`` and return (rel_posix, preview,
    line_count) for the caller to format into a handle."""
    dest = workspace_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Atomic swap so concurrent writers to the same sha-derived path can't race on
    # the shared filename (see _write_handle for the same reasoning).
    atomic_write_text(dest, content)
    lines = content.splitlines()
    preview = _make_preview(lines)
    return rel.as_posix(), preview, len(lines)


def offload_if_large(content: str, *, kind: str, key: str,
                     workspace_root: Path | None, capped: bool = False) -> str:
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
