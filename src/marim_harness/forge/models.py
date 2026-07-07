"""Forge-neutral value types shared by every ForgeBackend implementation.

Nothing tea- or gh-specific lives here: a backend maps its CLI's output into
these types, and the tool layer consumes only these. That invariance is what
lets a future gh backend drop in without touching the tools or their tests.
"""

from __future__ import annotations

from dataclasses import dataclass


class ForgeError(Exception):
    """A forge CLI call failed, timed out, or returned unparseable output.

    Carries a model-actionable message (typically the CLI's stderr) so the tool
    layer can surface it verbatim rather than a traceback."""


# tea's PR `ci` field / a commit-status state -> our normalized vocabulary.
_CI_MAP = {
    "success": "success",
    "failure": "failure",
    "error": "failure",
    "pending": "pending",
    "": "unknown",
}


def normalize_ci(raw: str | None) -> str:
    """Normalize a backend CI/commit-status string to
    ``success|failure|pending|unknown``. Unrecognized/empty/None -> ``unknown``."""
    return _CI_MAP.get((raw or "").strip().lower(), "unknown")


@dataclass(frozen=True)
class CiRun:
    """One CI/workflow run. ``conclusion`` and ``url`` are optional because the
    tea backend cannot expose per-run pass/fail (tea's ``actions runs`` reports
    only a run ``status`` like ``completed``); a future gh backend fills them."""

    workflow: str
    status: str
    event: str
    branch: str
    started: str
    conclusion: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class CiStatus:
    """Overall CI conclusion for a branch (normalized) plus recent run rows."""

    overall: str
    runs: tuple[CiRun, ...] = ()


@dataclass(frozen=True)
class PullRequest:
    """A pull request, forge-neutral. ``number`` is tea's ``index`` / gh's
    ``number``; ``ci`` is the normalized overall commit-status conclusion."""

    number: int
    title: str
    state: str
    head: str
    base: str
    mergeable: bool
    url: str
    ci: str
    author: str = ""
    updated: str = ""
