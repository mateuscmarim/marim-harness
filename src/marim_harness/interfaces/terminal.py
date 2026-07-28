"""Terminal capability probes — the effectful half of `sixel.py`.

Both functions answer "don't know" rather than raising. A capability probe that
can fail loudly is worse than one that can only be wrong, because everything
downstream of it already has a working fallback: not knowing costs a bigger QR,
while an exception escaping here would take down `marim serve --qr`'s never-fatal
contract with it.
"""

import os
import select
import struct
import sys
from contextlib import suppress

# The DA1 query. The reply is `ESC [ ? <params> c`, and `4` among the params is
# sixel — see `sixel.supports_sixel`, which does the parsing.
_DEVICE_ATTRIBUTES_QUERY = b"\033[c"
_REPLY_TERMINATOR = b"c"

# Long enough for a terminal on the far end of an ssh hop to answer, short enough
# that a terminal which never will doesn't make `marim serve qr` feel broken.
DEFAULT_TIMEOUT = 0.35


def device_attributes(stream, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Ask ``stream``'s terminal what it can do, or "" if we couldn't ask.

    Requires a real terminal on both ends: the query goes out on ``stream`` and
    the reply arrives on stdin, so a StringIO, a pipe, or a daemon with no
    controlling terminal all answer "" and take the character rendering.
    """
    try:
        import termios
        import tty
    except ImportError:  # not a POSIX terminal (Windows)
        return ""
    read_fd = _tty_fileno(sys.stdin)
    write_fd = _tty_fileno(stream)
    if read_fd is None or write_fd is None:
        return ""
    try:
        saved = termios.tcgetattr(read_fd)
    except termios.error:
        return ""
    try:
        # cbreak, not raw: it leaves signal handling alone, so a Ctrl-C during
        # the probe still interrupts rather than being read as a reply byte.
        tty.setcbreak(read_fd, termios.TCSANOW)
        os.write(write_fd, _DEVICE_ATTRIBUTES_QUERY)
        return _read_reply(read_fd, timeout=timeout)
    # termios.error is not an OSError, and both the mode switch and the restore
    # below raise it — a probe that leaves the terminal in cbreak, or takes the
    # daemon down with it, is worse than one that doesn't know the answer.
    except (OSError, termios.error):
        return ""
    finally:
        with suppress(termios.error):
            termios.tcsetattr(read_fd, termios.TCSADRAIN, saved)


def _read_reply(fd: int, *, timeout: float) -> str:
    """Bytes until the reply's terminator or ``timeout``, whichever comes first."""
    reply = b""
    while len(reply) < 64 and select.select([fd], [], [], timeout)[0]:
        chunk = os.read(fd, 64)
        if not chunk:
            break
        reply += chunk
        if _REPLY_TERMINATOR in chunk:
            break
    return reply.decode("ascii", "replace")


def cell_pixels(stream) -> tuple[int, int] | None:
    """The size of one character cell in pixels, or None if unreported.

    `TIOCGWINSZ` carries the window's pixel size alongside its character size,
    but plenty of terminals — tmux among them, depending on version — leave the
    pixel fields zero. Zero means unknown, not zero-sized.
    """
    fd = _tty_fileno(stream)
    if fd is None:
        return None
    try:
        import fcntl
        import termios

        rows, columns, width, height = struct.unpack(
            "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        )
    except (ImportError, OSError, struct.error):
        return None
    if not (rows and columns and width and height):
        return None
    return width // columns, height // rows


def _tty_fileno(stream) -> int | None:
    """``stream``'s descriptor when it is a real terminal, else None."""
    try:
        if not stream.isatty():
            return None
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
