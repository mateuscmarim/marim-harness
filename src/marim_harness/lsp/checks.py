"""Stateless, on-demand diagnostics for languages whose resident language server
gives weak results.

For Python, multilspy starts ``jedi-language-server``, whose diagnostics are
*syntax-errors-only* (it just runs ``compile(..., PyCF_ONLY_AST)``) — it misses
exactly the mistakes an agent makes: undefined names, bad imports, type errors.
So instead of the jedi push path we shell out to real checkers: ``ruff`` (always —
it's a hard dependency) for fast lint/dataflow diagnostics, and ``pyright`` (only
when on PATH, and only for a *deep* check) for type errors.

Each run is a bounded subprocess that emits structured JSON and exits — there is
no resident server and no shared state, so concurrent fan-out across many
sub-agents is trivially safe (separate processes share nothing). This is the
deliberate counterpoint to the LSP path: navigation wants a warm semantic server,
but a diagnostics pass is a stateless request/response, so we make it one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Generous per-checker ceilings: ruff is near-instant; pyright pays a node + import
# analysis cost on cold start, so it gets more room. Both are best-effort — a
# timeout yields no diagnostics from that checker, never an error into the tool.
_RUFF_TIMEOUT = 10.0
_PYRIGHT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Diag:
    """One diagnostic, in agent-facing 1-based coordinates."""

    line: int
    col: int
    severity: str  # error | warning | info | hint
    message: str
    source: str  # ruff | pyright


async def _run(cmd: list[str], cwd: Path, timeout: float) -> str | None:
    """Run ``cmd`` under ``timeout`` and return its stdout, or None if the binary
    is missing, the run times out, or anything else goes wrong. Best-effort: a
    None tells the caller "no diagnostics from this checker", never raising into a
    tool. stderr is discarded — checkers write progress/errors there that would
    otherwise be noise."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.debug("checker %s not launchable: %s", cmd[0], exc)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.debug("checker %s timed out after %ss", cmd[0], timeout)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()  # reap so we don't leak a zombie
        return None
    return out.decode("utf-8", "replace")


def _parse_ruff(stdout: str) -> list[Diag]:
    """Parse ``ruff check --output-format=json`` output. ruff reports 1-based
    row/column already; a null ``code`` is a syntax error (no rule), which we
    surface as an error rather than a lint warning."""
    try:
        items = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    out: list[Diag] = []
    for it in items:
        loc = it.get("location") or {}
        row = loc.get("row") or 1
        col = loc.get("column") or 1
        code = it.get("code")
        msg = (it.get("message") or "").splitlines()
        head = msg[0] if msg else ""
        if code:
            head = f"{head} ({code})"
        out.append(Diag(row, col, "warning" if code else "error", head, "ruff"))
    return out


# pyright DiagnosticSeverity string -> our label.
_PYRIGHT_SEV = {"error": "error", "warning": "warning", "information": "info"}


def _parse_pyright(stdout: str) -> list[Diag]:
    """Parse ``pyright --outputjson`` output (its ``generalDiagnostics`` array).
    pyright ranges are 0-based; translate to 1-based."""
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    out: list[Diag] = []
    for d in data.get("generalDiagnostics", []):
        start = (d.get("range") or {}).get("start") or {}
        line = (start.get("line") or 0) + 1
        col = (start.get("character") or 0) + 1
        sev = _PYRIGHT_SEV.get(d.get("severity", ""), "error")
        msg = (d.get("message") or "").splitlines()
        head = msg[0] if msg else ""
        rule = d.get("rule")
        if rule:
            head = f"{head} ({rule})"
        out.append(Diag(line, col, sev, head, "pyright"))
    return out


def _pyright_bin() -> str | None:
    """The pyright CLI to use, preferring the actively-maintained basedpyright
    fork when present. None when neither is on PATH."""
    for b in ("basedpyright", "pyright"):
        if shutil.which(b):
            return b
    return None


async def python_diagnostics(root: Path, path: str, *, deep: bool) -> list[Diag]:
    """Real diagnostics for ``path`` (relative to ``root``): ``ruff`` always, plus
    ``pyright`` when ``deep`` and a pyright binary is available. Both run as
    independent subprocesses, so this is safe to call concurrently from many
    sub-agents. Results are sorted by position for stable output."""
    diags: list[Diag] = []
    if shutil.which("ruff"):
        out = await _run(
            ["ruff", "check", "--output-format=json", "--", path], root, _RUFF_TIMEOUT
        )
        if out is not None:
            diags.extend(_parse_ruff(out))
    if deep:
        pyright = _pyright_bin()
        if pyright is not None:
            out = await _run([pyright, "--outputjson", path], root, _PYRIGHT_TIMEOUT)
            if out is not None:
                diags.extend(_parse_pyright(out))
    diags.sort(key=lambda d: (d.line, d.col, d.source))
    return diags


def format_checks(path: str, diags: list[Diag], *, max_results: int = 50) -> str:
    """Render diagnostics as ``path:line:col: severity: message (code) [source]``
    lines — the same leading shape the LSP path uses (see
    ``lsp.diagnostics.format_diagnostics``), so the provider's diagnostic-line
    detector treats both identically. Empty list → a clear 'no diagnostics' note."""
    if not diags:
        return f"{path}: no diagnostics"
    lines = [
        f"{path}:{d.line}:{d.col}: {d.severity}: {d.message} [{d.source}]"
        for d in diags[:max_results]
    ]
    extra = len(diags) - max_results
    if extra > 0:
        lines.append(f"… and {extra} more")
    return "\n".join(lines)
