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

There are two packings. The default packs 2×3 modules into each cell using the
sextants from Unicode 13's Symbols for Legacy Computing — 18 text rows for a
typical pairing payload, which is what lets the whole block fit an 80×24
terminal. `wide=True` falls back to the half-blocks (1×2 per cell), five rows
taller but drawn from a block that every terminal font has had for decades. The
sextant cell is a third wider than it is tall in module terms, which scanners
handle: the finder patterns give them a perspective transform, and a uniform
stretch is exactly what that corrects for.
"""

import math
from dataclasses import dataclass

from . import sixel

QUIET_ZONE = 4

# Truecolor black foreground on a truecolor white background, re-emitted per
# line (see the module docstring).
_SGR = "\033[38;2;0;0;0;48;2;255;255;255m"
_RESET = "\033[0m"

# Dark modules are painted by the black foreground; light ones show the white
# background. Both tables are indexed by the cell's modules read left-to-right,
# top-to-bottom, as a bitmask with the first module in bit 0.
_HALF_BLOCKS = (" ", "▀", "▄", "█")

# The four combinations Unicode did *not* give a sextant of its own, because a
# block-element character already draws them.
_SEXTANT_ALIASES = {0b000000: " ", 0b010101: "▌", 0b101010: "▐", 0b111111: "█"}


def encode(uri: str) -> list[tuple[int, ...]]:
    """``uri`` as a QR matrix of 0 (light) / 1 (dark), quiet zone included.

    Raises ``ImportError`` when segno isn't installed; the caller degrades to
    printing the URI as text.
    """
    import segno

    qr = segno.make(uri, error="m")
    return [tuple(row) for row in qr.matrix_iter(border=QUIET_ZONE)]


def _sextant(bits: int) -> str:
    """The glyph painting a 2×3 cell whose modules are ``bits``.

    The 60 sextants run consecutively from U+1FB00 in bitmask order, with the
    four aliased combinations left out of the block — so the codepoint is the
    bitmask minus however many of those it has passed.
    """
    alias = _SEXTANT_ALIASES.get(bits)
    if alias is not None:
        return alias
    return chr(0x1FB00 + bits - 1 - (bits > 0b010101) - (bits > 0b101010))


def _pack(matrix: list[tuple[int, ...]], *, height: int, width: int) -> str:
    """``matrix`` drawn ``height``×``width`` modules to the character cell.

    Cells that run past the last row or column are padded with light modules.
    Both edges being padded are the far ones, so padding only ever *widens* the
    quiet zone — never encroaches on it.
    """
    glyph = _sextant if (height, width) == (3, 2) else _HALF_BLOCKS.__getitem__
    columns = len(matrix[0])
    lines = []
    for top in range(0, len(matrix), height):
        cells = []
        for left in range(0, columns, width):
            bits = 0
            for offset_y in range(height):
                row = matrix[top + offset_y] if top + offset_y < len(matrix) else ()
                for offset_x in range(width):
                    x = left + offset_x
                    if x < len(row) and row[x]:
                        bits |= 1 << (offset_y * width + offset_x)
            cells.append(glyph(bits))
        lines.append(f"{_SGR}{''.join(cells)}{_RESET}")
    return "\n".join(lines)


def render_matrix(matrix: list[tuple[int, ...]], *, wide: bool = False) -> str:
    """The matrix as block text: 2×3 modules per cell, or 1×2 when ``wide``."""
    if not matrix:
        return ""
    return _pack(matrix, height=2, width=1) if wide else _pack(matrix, height=3, width=2)


def rendered_rows(matrix: list[tuple[int, ...]], *, wide: bool = False) -> int:
    """Text rows `render_matrix` will produce for ``matrix``."""
    return math.ceil(len(matrix) / (2 if wide else 3))


@dataclass(frozen=True)
class Rendering:
    """How to draw the code, once the terminal has been asked what it can do.

    The three ways trade size against what the terminal has to support: a sixel
    image is square and needs no font at all, the sextants are the smallest thing
    a font can draw, and the half-blocks are square but ask nothing newer than
    1993 of it.
    """

    wide: bool = False
    sixel: bool = False
    cell: tuple[int, int] | None = None

    def draw(self, matrix: list[tuple[int, ...]]) -> tuple[str, int]:
        """The drawn code, and the text rows it will occupy."""
        if not self.sixel:
            return render_matrix(matrix, wide=self.wide), rendered_rows(matrix, wide=self.wide)
        scale = sixel.choose_scale(len(matrix), cell=self.cell)
        return (
            sixel.render(matrix, scale=scale),
            sixel.rendered_rows(len(matrix), scale=scale, cell=self.cell),
        )

    @property
    def needs_a_font_escape_hatch(self) -> bool:
        """Whether the drawing depends on glyphs a font may not have — i.e. is
        worth printing `--wide` under."""
        return not self.sixel and not self.wide


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


# The address line, the password warning, the two hint lines, and the blank
# lines between them: what the caller prints around the code itself. The `--wide`
# hint is one of them and only prints in the default packing, so the estimate is
# a line pessimistic under `wide=True` — which costs at most one spurious note on
# a terminal the block fits exactly.
_SURROUNDING_LINES = 7
