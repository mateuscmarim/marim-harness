"""The MARIM wordmark and the predicates that decide when to show it.

Lives at the ``interfaces/`` level rather than inside ``interfaces/tui/``
because the serve CLI needs the same art: reaching it via
``interfaces.tui.app`` would import Textual as a side effect, and the
``[serve]`` extra deliberately doesn't depend on the TUI.

Everything here is pure — the art is constants, and *whether* to print it
(a tty? ``--no-banner``? ``NO_COLOR``?) is two predicates over an injected
environment, so both are unit-testable without a terminal.
"""

import os
from collections.abc import Mapping, Sequence

WORDMARK = (
    " ███╗   ███╗ █████╗ ██████╗ ██╗███╗   ███╗\n"
    " ████╗ ████║██╔══██╗██╔══██╗██║████╗ ████║\n"
    " ██╔████╔██║███████║██████╔╝██║██╔████╔██║\n"
    " ██║╚██╔╝██║██╔══██║██╔══██╗██║██║╚██╔╝██║\n"
    " ██║ ╚═╝ ██║██║  ██║██║  ██║██║██║ ╚═╝ ██║\n"
    " ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝"
)

# The letter-spaced strapline under the wordmark. Each surface fills in its own
# (the TUI says what marim is; serve says which daemon you just started), so the
# family resemblance is the spacing idiom, not one fixed string.
TAGLINE = "   · · ·   a   t e r m i n a l   h a r n e s s"

# What the TUI intro header mounts: wordmark + the product tagline.
BANNER = f"{WORDMARK}\n{TAGLINE}"

# The default theme's accent (#4cb6a8) snapped to the 256-color cube (#5fafaf).
# Plain SGR rather than truecolor: this prints to whatever terminal launched the
# daemon, and 256 colors are universal in a way 24-bit still isn't.
_ACCENT = "\033[38;5;73m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_TRUTHY = {"1", "true", "yes", "on"}


def banner_enabled(
    *, isatty: bool, disabled: bool = False, env: Mapping[str, str] | None = None
) -> bool:
    """Whether to print the wordmark.

    Interactive launches only. ``marim serve`` is a daemon — under systemd,
    Docker, or ``nohup`` its stdout is a log file, and block-ASCII in journald
    is noise, not polish. ``--no-banner`` (``disabled``) and ``MARIM_NO_BANNER``
    are the escape hatches for a tty you'd rather keep quiet.
    """
    if disabled:
        return False
    env = os.environ if env is None else env
    raw = env.get("MARIM_NO_BANNER")
    if raw is not None and raw.strip().lower() in _TRUTHY:
        return False
    return isatty


def color_enabled(*, isatty: bool, env: Mapping[str, str] | None = None) -> bool:
    """Whether to emit SGR escapes. Honors the no-color.org convention (``NO_COLOR``
    set to anything non-empty) and ``TERM=dumb``; otherwise follows the tty."""
    env = os.environ if env is None else env
    if (env.get("NO_COLOR") or "").strip():
        return False
    if (env.get("TERM") or "").strip().lower() == "dumb":
        return False
    return isatty


def wordmark_block(subtitle: str, *, color: bool) -> str:
    """The wordmark plus a letter-spaced ``subtitle``, optionally accented."""
    art = f"{WORDMARK}\n{_spaced(subtitle)}"
    return f"{_ACCENT}{art}{_RESET}" if color else art


def field_block(fields: Sequence[tuple[str, str]], *, color: bool, indent: str = "  ") -> str:
    """``fields`` as a label-aligned block — the startup facts under a wordmark."""
    if not fields:
        return ""
    width = max(len(label) for label, _ in fields) + 2
    lines = []
    for label, value in fields:
        padded = label.ljust(width)
        head = f"{_DIM}{padded}{_RESET}" if color else padded
        lines.append(f"{indent}{head}{value}")
    return "\n".join(lines)


def package_version() -> str:
    """The installed package version, or a placeholder when running from a source
    tree that was never installed (no dist metadata)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("marim-harness")
    except PackageNotFoundError:
        return "unknown"


def _spaced(text: str) -> str:
    """Render ``text`` in the tagline idiom: words letter-spaced, except tokens
    that are already dense with punctuation (a version like ``v0.2.0`` reads as
    noise spaced out, so it's left alone)."""
    parts = []
    for word in text.split():
        parts.append(word if any(c.isdigit() or c == "." for c in word) else " ".join(word))
    return "   · · ·   " + "   ".join(parts)
