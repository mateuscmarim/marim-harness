"""Doc-lint: the reference docs must stay complete as the surface grows.

These tests cross-check the documented surface against the source of truth:
every ``MARIM_*`` environment variable referenced anywhere under ``src/`` must
appear in ``docs/reference/configuration.md``, and every TUI slash command in
the registry must appear in ``docs/guides/tui.md``. A failure here means a
variable or command was added (or renamed) without updating the docs — update
the doc page, don't loosen the test.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "marim_harness"

_ENV_VAR_RE = re.compile(r"MARIM_[A-Z_]+")


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
