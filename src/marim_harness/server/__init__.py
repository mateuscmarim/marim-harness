"""marim serve: the HTTP server daemon's transport-neutral core.

Like ``runtime/``, the package root deliberately re-exports nothing — import
submodules directly. Only ``http.py`` may import starlette (an optional
extra); everything else stays importable on a bare install."""
