# src/marim_harness/tools/offload.py
"""Offload large tool output to a gitignored file instead of flooding context.

A tool builds its full result (bounded by ``MAX_OUTPUT_CHARS``) and passes it
through :func:`offload_if_large`: small results return inline unchanged; large
ones are written to a caller-provided directory and replaced by a handle +
preview the agent can page with ``read_file``/``grep``."""

import hashlib
import re
from pathlib import Path

from ...atomic_io import atomic_write_text

# Legacy offload directory under the workspace root, used as a fallback when no
# scratchpad is available. Single source of truth — fs.py and shell.py import it.
LEGACY_OFFLOAD_DIR = Path(".marim") / "output"


def get_offload_dir(
    workspace_root: Path | None, scratchpad: Path | None
) -> Path | None:
    """Return the best directory for offloading large tool output.

    Prefer the session scratchpad (session-scoped, auto-cleaned) over the
    workspace-rooted `.marim/output/` (persists across sessions). Returns
    None when neither is available, which degrades offloading to clipping.
    """
    if scratchpad is not None:
        return scratchpad
    return workspace_root

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

# --- offload-handle envelope --------------------------------------------------
# Every producer of a "large output saved to a file" handle embeds the path in
# one shared, machine-recognizable form: the words "saved to" followed by the
# absolute path in backticks. Session load revalidates these (compaction.py's
# revalidate_elided_pointers): the scratchpad lives under /tmp, so a resumed
# session can outlive the files its handles point at. Producers keep their own
# natural copy around the core phrase — tripwire tests in test_offload.py /
# test_subagent_tool.py pin each one to the regex, so a wording edit that
# breaks the envelope fails a named test instead of silently disabling
# revalidation.
OFFLOAD_HANDLE_RE = re.compile(r"saved to `([^`\n]+)`")

# Appended (never replacing — the inline preview is real information) to a
# handle whose file no longer exists. Lives here, next to the envelope, so
# producer copy and revalidation copy stay coherent in one module. Also the
# idempotency marker: revalidation skips content that already contains it.
OFFLOAD_GONE_NOTE = (
    "\n\n⚠️ The offloaded file referenced above no longer exists (the "
    "scratchpad was cleaned since this session last ran) — re-run the tool "
    "if you need the full output."
)


def find_offload_paths(content: str) -> list[str]:
    """Every offload-file path embedded in *content* (usually 0 or 1).

    Pure. Matches only the shared envelope — an elided-pointer placeholder
    (compaction.py) uses different copy on purpose and never matches."""
    return OFFLOAD_HANDLE_RE.findall(content)


def _make_preview(lines: list[str]) -> str:
    """First few lines of an offloaded result, bounded in both count and width so
    the preview itself can't flood context (the full body lives in the file)."""
    preview = "\n".join(lines[:_PREVIEW_LINES])
    if len(preview) > _PREVIEW_CHARS:
        preview = preview[:_PREVIEW_CHARS] + "…"
    return preview


def _write_handle(content: str, *, kind: str, key: str,
                  offload_dir: Path, capped: bool) -> str:
    digest = hashlib.sha256(f"{kind}\0{key}".encode()).hexdigest()[:16]
    dest = offload_dir / f"{kind}-{digest}.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, content, durable=False)
    lines = content.splitlines()
    preview = _make_preview(lines)
    cap_note = (
        f"⚠️ Output hit the {MAX_OUTPUT_CHARS:,}-char ceiling; the file holds what "
        "was collected.\n" if capped else ""
    )
    return (
        f"⚠️ Large {kind} result ({len(content):,} chars, {len(lines):,} lines) — "
        f"full output saved to `{dest.as_posix()}`. Read more with read_file "
        f"(it paginates) or grep that path.\n"
        f"{cap_note}"
        f"--- preview (first {min(_PREVIEW_LINES, len(lines))} lines) ---\n"
        f"{preview}"
    )


def write_preview_file(content: str, *, filename: str,
                       offload_dir: Path) -> tuple[str, str, int]:
    """Write *content* to ``offload_dir/filename`` and return (absolute_path,
    preview, line_count) for the caller to format into a handle."""
    dest = offload_dir / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, content, durable=False)
    lines = content.splitlines()
    preview = _make_preview(lines)
    return dest.as_posix(), preview, len(lines)


def offload_if_large(content: str, *, kind: str, key: str,
                     offload_dir: Path | None, capped: bool = False) -> str:
    """Return ``content`` inline when small; otherwise offload to a file and
    return a handle + preview. With no offload directory (or on write failure),
    clip to the inline limit instead, so a large result can never flood
    context."""
    if len(content) <= _INLINE_CHAR_LIMIT:
        return content
    if offload_dir is not None:
        try:
            return _write_handle(content, kind=kind, key=key,
                                 offload_dir=offload_dir, capped=capped)
        except OSError:
            pass
    clipped = content[:_INLINE_CHAR_LIMIT]
    return (
        f"{clipped}\n"
        f"…(output clipped to {_INLINE_CHAR_LIMIT:,} chars; offload unavailable)"
    )
