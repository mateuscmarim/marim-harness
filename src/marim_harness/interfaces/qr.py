"""Terminal QR rendering for `marim serve qr`.

segno is an encoder here and nothing more — the drawing is ours, because two
details decide whether a phone can actually read the result:

* **Forced colors.** Half-blocks drawn in the terminal's own palette render
  *inverted* on a dark theme, which older ZXing-based scanners reject. We emit
  truecolor black-on-white rather than the 4-bit `30`/`47` pair, since 4-bit
  "black" and "white" are palette entries a theme is free to redefine and
  scannability depends on real contrast, not on a color's name.
* **The quiet zone.** Four light modules on every side are required by the QR
  spec, and the terminal's own background does not count once we paint the code
  white.
"""

import math

QUIET_ZONE = 4

# Truecolor black foreground on a truecolor white background, re-emitted per
# line (see the module docstring).
_SGR = "\033[38;2;0;0;0;48;2;255;255;255m"
_RESET = "\033[0m"

# (top module, bottom module) -> the glyph that paints them. Dark modules are
# painted by the black foreground; light ones show the white background.
_BLOCKS = {(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}


def encode(uri: str) -> list[tuple[int, ...]]:
    """``uri`` as a QR matrix of 0 (light) / 1 (dark), quiet zone included.

    Raises ``ImportError`` when segno isn't installed; the caller degrades to
    printing the URI as text.
    """
    import segno

    qr = segno.make(uri, error="m")
    return [tuple(row) for row in qr.matrix_iter(border=QUIET_ZONE)]


def render_matrix(matrix: list[tuple[int, ...]]) -> str:
    """The matrix as half-block text, two module rows per text row."""
    if not matrix:
        return ""
    light = (0,) * len(matrix[0])
    lines = []
    for index in range(0, len(matrix), 2):
        top = matrix[index]
        bottom = matrix[index + 1] if index + 1 < len(matrix) else light
        cells = "".join(_BLOCKS[(t, b)] for t, b in zip(top, bottom, strict=True))
        lines.append(f"{_SGR}{cells}{_RESET}")
    return "\n".join(lines)


def rendered_rows(matrix: list[tuple[int, ...]]) -> int:
    """Text rows `render_matrix` will produce for ``matrix``."""
    return math.ceil(len(matrix) / 2)


def height_note(*, rendered: int, terminal_lines: int) -> str | None:
    """A heads-up when the code won't fit on screen. Not a refusal — a scrolled
    QR is still scannable, a surprising one isn't. ``terminal_lines`` of 0 means
    the size is unknown, which is not evidence of a short terminal."""
    needed = rendered + _SURROUNDING_LINES
    if not terminal_lines or terminal_lines >= needed:
        return None
    return (
        f"note: this code needs {needed} rows and the terminal has {terminal_lines} — "
        "scroll up if the top is cut off"
    )


# The address line, the password warning, the --advertise hint, and the blank
# lines between them: what the caller prints around the code itself.
_SURROUNDING_LINES = 6
