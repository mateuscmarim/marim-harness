"""The terminal capability probes.

The interesting cases need a real terminal, so these drive a pty rather than a
mock: the point of `device_attributes` is that it puts bytes on a tty and reads
what comes back, and nothing short of a tty exercises that.
"""

import io
import os
import struct
import sys

import pytest

from marim_harness.interfaces import terminal

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal probes")


class _Tty(io.StringIO):
    """A stream that claims to be a terminal but has no descriptor."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def pty_pair():
    """A (master_fd, stream) pair where the stream is the terminal side."""
    import pty

    master, slave = pty.openpty()
    yield master, _Terminal(slave)
    os.close(master)
    os.close(slave)


class _Terminal:
    """The slave end of a pty, shaped like a stream for the probes."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


def test_a_reply_on_the_terminal_comes_back(pty_pair, monkeypatch):
    master, stream = pty_pair
    monkeypatch.setattr(terminal.sys, "stdin", stream)
    # Queued before the probe asks: the reply is waiting by the time it reads,
    # which is the same thing a fast terminal does.
    os.write(master, b"\033[?62;4;22c")
    assert terminal.device_attributes(stream, timeout=2.0) == "\033[?62;4;22c"


def test_a_terminal_that_never_answers_gives_up(pty_pair, monkeypatch):
    """A silent terminal must cost a timeout, not a hang."""
    _, stream = pty_pair
    monkeypatch.setattr(terminal.sys, "stdin", stream)
    assert terminal.device_attributes(stream, timeout=0.05) == ""


def test_the_terminal_is_left_as_it_was_found(pty_pair, monkeypatch):
    """The probe borrows cbreak mode; a shell whose echo never came back would
    be a far worse bug than a QR that came out big."""
    import termios

    master, stream = pty_pair
    monkeypatch.setattr(terminal.sys, "stdin", stream)
    before = termios.tcgetattr(stream.fileno())
    os.write(master, b"\033[?62;4c")
    terminal.device_attributes(stream, timeout=2.0)
    assert termios.tcgetattr(stream.fileno()) == before


@pytest.mark.parametrize("stream", [io.StringIO(), _Tty(), object()])
def test_probing_something_that_is_not_a_terminal_answers_dont_know(stream):
    """A pipe, a StringIO, and a daemon with no controlling terminal all take
    the character rendering rather than raising."""
    assert terminal.device_attributes(stream, timeout=0.05) == ""
    assert terminal.cell_pixels(stream) is None


def test_cell_pixels_reports_the_size_the_terminal_was_told(pty_pair):
    import fcntl
    import termios

    _, stream = pty_pair
    fcntl.ioctl(stream.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 640, 408))
    assert terminal.cell_pixels(stream) == (8, 17)


def test_an_unreported_pixel_size_is_unknown_rather_than_zero(pty_pair):
    """tmux and plenty of terminals leave the pixel fields zero; dividing by
    that, or believing it, would be worse than falling back to a fixed scale."""
    import fcntl
    import termios

    _, stream = pty_pair
    fcntl.ioctl(stream.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    assert terminal.cell_pixels(stream) is None
