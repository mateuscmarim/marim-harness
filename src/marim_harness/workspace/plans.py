"""The plan artifact written by the ``present_plan`` tool in plan mode.

A plan is a summary plus ordered steps, persisted as a markdown file under
``.marim/plans/<slug>.md`` so it survives the session and can be handed to the
superpowers execution skills. Pure formatting plus a thin atomic write — no
model or UI concerns here."""

import re
import unicodedata
from pathlib import Path

from ..atomic_io import atomic_write_text


def _slugify(text: str) -> str:
    """Reduce text to a filesystem-safe ASCII slug."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")


def plan_slug(session_id: str, summary: str) -> str:
    """One stable slug per plan per session: a short session prefix plus a short
    prefix of the summary's first line. Re-presenting overwrites the same file."""
    first_line = summary.splitlines()[0] if summary.strip() else ""
    prefix = _slugify(first_line)[:40].strip("-") or "plan"
    sid = _slugify(session_id)[:12].strip("-") or "session"
    return f"{sid}-{prefix}"


def format_plan(
    summary: str, steps: list[str], *, created: str, session_id: str
) -> str:
    """Render the plan markdown: frontmatter, summary, then a step checklist."""
    lines = [
        "---",
        f"session: {session_id}",
        f"created: {created}",
        "status: proposed",
        "---",
        "",
        "# Plan",
        "",
        summary.strip(),
        "",
        "## Steps",
        "",
    ]
    lines += [f"- [ ] {step.strip()}" for step in steps if step.strip()]
    return "\n".join(lines) + "\n"


def plans_dir(workspace_root: Path) -> Path:
    return workspace_root / ".marim" / "plans"


def write_plan(
    workspace_root: Path,
    *,
    session_id: str,
    summary: str,
    steps: list[str],
    created: str,
) -> Path:
    """Write ``.marim/plans/<slug>.md`` and return its path."""
    directory = plans_dir(workspace_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan_slug(session_id, summary)}.md"
    atomic_write_text(
        path, format_plan(summary, steps, created=created, session_id=session_id)
    )
    return path
