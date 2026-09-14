"""Bounded file reads anchored to a registered workspace, with no symlink following.

Checking ``resolve()`` then opening the path leaves a replacement race. Walk each
directory through its descriptor instead, including the registered root's parents,
and keep the final descriptor open until the response finishes. This POSIX-only
policy deliberately refuses even symlinks whose current target is in the workspace.
"""

import logging
import mimetypes
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


class InvalidFileRequest(ValueError):
    """Malformed path or file exceeding the download limit (HTTP 400)."""


class WorkspaceFileNotFound(Exception):
    """Missing, forbidden or nonregular file (indistinguishable HTTP 404)."""


def _relative_parts(root: Path, raw_path: str | None) -> tuple[str, ...]:
    if not raw_path or not raw_path.strip() or any(ord(c) < 32 for c in raw_path):
        raise InvalidFileRequest("path must be a nonempty filesystem path")
    path = Path(raw_path)
    if ".." in path.parts:
        raise WorkspaceFileNotFound
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise WorkspaceFileNotFound from None
    if not path.parts:
        raise WorkspaceFileNotFound
    return path.parts


def _open_descriptor(root: Path, parts: tuple[str, ...]) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(root.anchor, directory_flags)
    try:
        for component in (*root.parts[1:], *parts[:-1]):
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        # NONBLOCK prevents a malicious FIFO from hanging the request before
        # fstat can reject it; it has no effect on regular-file reads.
        return os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory,
        )
    finally:
        os.close(directory)


@dataclass
class WorkspaceFile:
    stream: BinaryIO
    filename: str
    size: int

    @property
    def headers(self) -> dict[str, str]:
        return {
            "content-type": mimetypes.guess_type(self.filename)[0] or "application/octet-stream",
            "content-length": str(self.size),
            "content-disposition": f"attachment; filename*=UTF-8''{quote(self.filename, safe='')}",
            "cache-control": "private, no-store",
            "x-content-type-options": "nosniff",
        }

    def chunks(self) -> Iterator[bytes]:
        remaining = self.size
        while remaining:
            chunk = self.stream.read(min(remaining, _CHUNK_BYTES))
            if not chunk:
                raise OSError("workspace file changed during download")
            remaining -= len(chunk)
            yield chunk

    def close(self) -> None:
        self.stream.close()


def open_workspace_file(root: Path, raw_path: str | None) -> WorkspaceFile:
    """Return an owned descriptor; callers must close it on every response exit."""
    parts = _relative_parts(root, raw_path)
    try:
        descriptor = _open_descriptor(root, parts)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceFileNotFound
            if info.st_size > MAX_FILE_BYTES:
                raise InvalidFileRequest("file exceeds the 50 MiB download limit")
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
    except OSError as exc:
        # OSError's normal string contains the raw host path. Keep only errno.
        logger.debug("workspace file open refused errno=%s", exc.errno)
        raise WorkspaceFileNotFound from None
    return WorkspaceFile(stream, parts[-1], info.st_size)
