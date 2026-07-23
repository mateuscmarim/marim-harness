"""Doc-lint: the reference docs must stay complete as the surface grows.

These tests cross-check the documented surface against the source of truth:
every ``MARIM_*`` environment variable referenced anywhere under ``src/`` must
appear in ``docs/reference/configuration.md``, and every TUI slash command in
the registry must appear in ``docs/guides/tui.md``. A failure here means a
variable or command was added (or renamed) without updating the docs — update
the doc page, don't loosen the test.

``test_relative_links_resolve`` additionally keeps the maintained doc pages
free of dangling relative links (a page rename or move must update everything
that points at it). Only maintained docs are checked — ``docs/internal/`` and
``docs/superpowers/`` are historical records and exempt.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "marim_harness"

_ENV_VAR_RE = re.compile(r"MARIM_[A-Z_]+")
_LINK_RE = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")


def _source_env_vars() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        names.update(_ENV_VAR_RE.findall(path.read_text(encoding="utf-8")))
    return names


def test_every_env_var_is_documented() -> None:
    doc = (ROOT / "docs" / "reference" / "configuration.md").read_text(encoding="utf-8")
    missing = sorted(v for v in _source_env_vars() if v not in doc)
    assert not missing, f"undocumented MARIM_* variables: {missing}"


def test_every_tui_command_is_documented() -> None:
    from marim_harness.interfaces.tui.commands import COMMANDS

    doc = (ROOT / "docs" / "guides" / "tui.md").read_text(encoding="utf-8")
    missing = sorted(c.name for c in COMMANDS if f"/{c.name}" not in doc)
    assert not missing, f"undocumented slash commands: {missing}"


def _maintained_doc_pages() -> list[Path]:
    docs = ROOT / "docs"
    exempt = {docs / "internal", docs / "superpowers"}
    pages = [p for p in docs.rglob("*.md") if not any(e in p.parents for e in exempt)]
    pages += [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
    return pages


def test_relative_links_resolve() -> None:
    dangling: list[str] = []
    for page in _maintained_doc_pages():
        text = _CODE_SPAN_RE.sub("", _FENCE_RE.sub("", page.read_text(encoding="utf-8")))
        for match in _LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (page.parent / target).resolve().exists():
                dangling.append(f"{page.relative_to(ROOT)}: {target}")
    assert not dangling, f"dangling relative links: {dangling}"
