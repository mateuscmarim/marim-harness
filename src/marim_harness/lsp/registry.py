"""Map workspace files to multilspy language ids and report server availability.

Pure stdlib + small helpers, with no ``multilspy`` import, so importing the
registry (e.g. from the tools module) never drags in the heavy dependency or
spawns a language server.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

# File extension (lowercase, including dot) -> multilspy ``code_language``.
_EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
}

# language -> (PATH probe binaries, install hint). A language with a non-empty
# probe tuple is "available" only when one of its binaries is on PATH. A language
# with an empty probe tuple is auto-provided by multilspy (it downloads the
# server on first use) and is always reported available.
_PROBES: dict[str, tuple[tuple[str, ...], str]] = {
    "python": (("pyright-langserver", "pyright"), "install pyright (npm i -g pyright)"),
    "typescript": (
        ("typescript-language-server",),
        "install typescript-language-server (npm i -g typescript-language-server typescript)",
    ),
    "javascript": (
        ("typescript-language-server",),
        "install typescript-language-server (npm i -g typescript-language-server typescript)",
    ),
    "cpp": (("clangd",), "install clangd (e.g. pacman -S clang)"),
    "java": ((), "auto-downloaded by multilspy on first use"),
}


def language_for(path: str) -> str | None:
    """Return the multilspy ``code_language`` for ``path``, or None if the file
    extension isn't one we support."""
    dot = path.rfind(".")
    if dot == -1:
        return None
    return _EXT_TO_LANG.get(path[dot:].lower())


@dataclass(frozen=True)
class Availability:
    available: bool
    hint: str


def availability(language: str) -> Availability:
    """Whether a server for ``language`` can be started, with an install hint."""
    entry = _PROBES.get(language)
    if entry is None:
        return Availability(False, "unsupported language")
    probes, hint = entry
    if not probes:  # auto-provided by multilspy
        return Availability(True, hint)
    found = any(shutil.which(b) for b in probes)
    return Availability(found, hint)


def locally_installed_languages() -> set[str]:
    """Languages whose server binary is on PATH right now. Excludes
    auto-download-only languages (e.g. java) so callers can cheaply start
    every locally-present server without triggering a multi-hundred-MB download."""
    out: set[str] = set()
    for language, (probes, _hint) in _PROBES.items():
        if probes and any(shutil.which(b) for b in probes):
            out.add(language)
    return out
