"""LSP providers: one language's server contribution, and the per-session
registry assembled from them.

A provider is parsed from a plugin manifest's ``lsp`` block. Third-party
plugins may use only the declarative ``command``/``args`` form; the ``backend``
and named-``diagnostics`` keys are a bundled-only seam into in-tree tuned code
(BasedPyrightServer, lsp/checks.py). This module is pure stdlib — no multilspy
import — so importing it (from the tools/bootstrap layer) never drags in the
heavy dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import registry as _registry

logger = logging.getLogger(__name__)

# Recognized bundled-only backend keys. `multilspy:<lang>` is validated by prefix.
_BUNDLED_BACKENDS = frozenset({"basedpyright"})
_MULTILSPY_PREFIX = "multilspy:"
_DIAGNOSTICS_STRATEGIES = frozenset({"lsp", "python-checks"})


class LspProviderError(Exception):
    """A manifest ``lsp`` block is malformed or uses a bundled-only key."""


@dataclass(frozen=True)
class LspProvider:
    language: str
    extensions: tuple[str, ...]
    probe: tuple[str, ...]
    install_hint: str
    command: str | None
    args: tuple[str, ...]
    root_markers: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    backend: str | None
    diagnostics: str
    source: str  # "bundled" | "global" | "project"
    plugin_root: Path | None


def _norm_ext(ext: str) -> str:
    e = str(ext).strip().lower()
    return e if e.startswith(".") else f".{e}"


def _valid_backend(backend: str) -> bool:
    return backend in _BUNDLED_BACKENDS or backend.startswith(_MULTILSPY_PREFIX)


def _validate_launch_config(
    backend: str | None, command: str | None, bundled: bool, language: str, fail
) -> tuple[str | None, str | None] | None:
    """Validate backend/command exclusivity and bundled restrictions.
    Returns (backend, command) tuple or None if validation failed."""
    if backend is not None:
        if not bundled:
            fail(f"'backend' is bundled-only (provider {language!r})")
            return None
        if command is not None:
            fail(f"provider {language!r}: 'backend' and 'command' are exclusive")
            return None
        if not isinstance(backend, str) or not _valid_backend(backend):
            fail(f"provider {language!r}: unknown backend {backend!r}")
            return None
    elif command is not None:
        if not isinstance(command, str) or not command.strip():
            fail(f"provider {language!r}: 'command' must be a non-empty string")
            return None
    else:
        fail(f"provider {language!r}: needs 'command' or 'backend'")
        return None
    return backend, command


def _validate_diagnostics(diagnostics: str, bundled: bool, language: str, fail) -> str:
    """Validate diagnostics strategy and bundled restrictions."""
    if diagnostics not in _DIAGNOSTICS_STRATEGIES:
        return fail(f"provider {language!r}: unknown diagnostics {diagnostics!r}")
    if diagnostics != "lsp" and not bundled:
        return fail(f"named diagnostics {diagnostics!r} is bundled-only ({language!r})")
    return diagnostics


def _parse_optional_fields(raw: dict, command: str | None) -> tuple:
    """Parse optional environment, markers, args, and probe fields."""
    env_raw = raw.get("env")
    env = (
        tuple((str(k), str(v)) for k, v in env_raw.items())
        if isinstance(env_raw, dict)
        else ()
    )
    markers_raw = raw.get("rootMarkers")
    root_markers = (
        tuple(str(m) for m in markers_raw) if isinstance(markers_raw, list) else ()
    )
    args_raw = raw.get("args")
    args = tuple(str(a) for a in args_raw) if isinstance(args_raw, list) else ()

    probe_raw = raw.get("probe")
    if isinstance(probe_raw, list):
        probe = tuple(str(b) for b in probe_raw)
    elif command is not None:
        probe = (command.split()[0],)  # default to the command's binary
    else:
        probe = ()  # backend providers carry their own probe or auto-provide

    return env, root_markers, args, probe


def _parse_one(
    raw: dict, *, bundled: bool, source: str, plugin_root: Path | None, strict: bool
) -> LspProvider | None:
    def fail(msg: str) -> LspProvider | None:
        if strict:
            raise LspProviderError(msg)
        logger.warning("skipping lsp provider: %s", msg)
        return None

    if not isinstance(raw, dict):
        return fail(f"lsp provider must be an object, got {type(raw).__name__}")
    language = raw.get("language")
    if not isinstance(language, str) or not language.strip():
        return fail("lsp provider missing 'language'")
    exts = raw.get("extensions")
    if not isinstance(exts, list) or not exts:
        return fail(f"lsp provider {language!r} missing non-empty 'extensions'")

    backend = raw.get("backend")
    command = raw.get("command")
    result = _validate_launch_config(
        backend, command, bundled, language, fail
    )
    if result is None:
        return None
    backend, command = result

    diagnostics = raw.get("diagnostics", "lsp")
    diagnostics = _validate_diagnostics(diagnostics, bundled, language, fail)
    if diagnostics is None:
        return None

    env, root_markers, args, probe = _parse_optional_fields(raw, command)

    return LspProvider(
        language=language,
        extensions=tuple(_norm_ext(e) for e in exts),
        probe=probe,
        install_hint=str(raw.get("installHint", "") or ""),
        command=command,
        args=args,
        root_markers=root_markers,
        env=env,
        backend=backend,
        diagnostics=diagnostics,
        source=source,
        plugin_root=plugin_root,
    )


def parse_lsp_providers(
    block, *, bundled: bool, source: str, plugin_root: Path | None, strict: bool
) -> list[LspProvider]:
    """Parse an ``lsp`` manifest value (object or list of objects) into providers.
    ``strict`` raises on any problem (install/validate time); non-strict logs and
    drops the bad entry (discovery time)."""
    entries = block if isinstance(block, list) else [block]
    out: list[LspProvider] = []
    for entry in entries:
        p = _parse_one(
            entry, bundled=bundled, source=source, plugin_root=plugin_root, strict=strict
        )
        if p is not None:
            out.append(p)
    return out


class LspRegistry:
    """The per-session merged view of all LSP providers. Later providers win on
    an extension or language collision (project plugins shadow global shadow
    bundled — callers order the list accordingly)."""

    def __init__(self, providers: list[LspProvider]) -> None:
        self._providers = list(providers)
        self._ext_to_lang: dict[str, str] = {}
        self._by_language: dict[str, LspProvider] = {}
        self._probes: dict[str, tuple[tuple[str, ...], str]] = {}
        for p in self._providers:
            self._by_language[p.language] = p
            self._probes[p.language] = (p.probe, p.install_hint)
            for ext in p.extensions:
                self._ext_to_lang[ext] = p.language

    def language_for(self, path: str) -> str | None:
        return _registry.language_for(path, self._ext_to_lang)

    def availability(self, language: str) -> _registry.Availability:
        return _registry.availability(language, self._probes)

    def workspace_languages(self, root, *, max_entries: int = 50_000) -> set[str]:
        return _registry.workspace_languages(root, self._ext_to_lang, max_entries=max_entries)

    def locally_installed_languages(self) -> set[str]:
        return _registry.locally_installed_languages(self._probes)

    def provider_for(self, language: str) -> LspProvider | None:
        return self._by_language.get(language)
