"""Image attachments for the TUI prompt: clipboard reading, a content-addressed
disk cache, image file-path detection, and session-history externalization.

The clipboard reader is the only part that shells out to the OS; it is isolated
here behind read_clipboard_image() so every other unit is testable with a mock."""

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
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


def image_cache_root() -> Path:
    """Root directory for the content-addressed image cache.

    Override via MARIM_IMAGE_CACHE_DIR environment variable; defaults to
    ~/.marim/image-cache."""
    override = os.environ.get("MARIM_IMAGE_CACHE_DIR")
    return Path(override) if override else Path.home() / ".marim" / "image-cache"


@dataclass(frozen=True)
class CachedImage:
    """A cached image record: its path, content hash, and media type."""

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


_EXT_TO_MEDIA = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def media_type_for_path(path: Path) -> Optional[str]:
    """Return the media type for a path based on its extension, or None if not
    a known image extension."""
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
