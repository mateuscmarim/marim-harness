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

_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def media_ext(media_type: str) -> str:
    """File extension for a media type. Falls back to the subtype, then 'bin'."""
    if media_type in _EXT:
        return _EXT[media_type]
    if media_type.startswith("image/"):
        subtype = media_type.rsplit("/", 1)[-1]
        return subtype if subtype else "bin"
    return "bin"


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
    available = types.decode("utf-8", "replace").splitlines()
    target = "image/png" if "image/png" in available else next(
        (t for t in available if t.startswith("image/")), None
    )
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
