"""Producer collection ceiling and historical absolute-output pointer readers.

Ordinary output reduction belongs to Pydantic AI Harness ToolOutputLimits.
Keep the old envelope readable because persisted sessions and explicit report
budgets still contain absolute pointers in this form.
"""

import re
from pathlib import Path

LEGACY_OFFLOAD_DIR = Path(".marim") / "output"
# Producers bound collection before the upstream output capability runs.
MAX_OUTPUT_CHARS = 5_000_000

# --- offload-handle envelope --------------------------------------------------
# Every producer of a "large output saved to a file" handle embeds the path in
# one shared, machine-recognizable form: the words "saved to" followed by the
# absolute path in backticks. Session load revalidates these (compaction.py's
# revalidate_elided_pointers): the scratchpad lives under /tmp, so a resumed
# session can outlive the files its handles point at. Producers keep their own
# natural copy around the core phrase — tests in test_offload.py and
# test_subagent_tool.py pin the legacy envelope and report pointers, so a wording edit that
# breaks the envelope fails a named test instead of silently disabling
# revalidation. Known false positive, priced into the phrase-matching design:
# an inline tool return that merely *contains* a handle-shaped string (e.g. a
# read of a test file with a literal handle in it) gets the gone-note appended
# at next load once that path fails exists() — benign, since the note is
# append-only and idempotent.
OFFLOAD_HANDLE_RE = re.compile(r"saved to `([^`\n]+)`")

# Appended (never replacing — the inline preview is real information) to a
# handle whose file no longer exists. Lives here, next to the envelope, so
# producer copy and revalidation copy stay coherent in one module. Also the
# idempotency marker: revalidation skips content that already contains it.
OFFLOAD_GONE_NOTE = (
    "\n\n⚠️ The offloaded file referenced above no longer exists (the "
    "scratchpad was cleaned since this session last ran) — re-run the tool "
    "if you need the full output."
)


def find_offload_paths(content: str) -> list[str]:
    """Every offload-file path embedded in *content* (usually 0 or 1).

    Pure. Matches only the shared envelope — an elided-pointer placeholder
    (compaction.py) uses different copy on purpose and never matches."""
    return OFFLOAD_HANDLE_RE.findall(content)
