"""Sixel rendering for the pairing QR — pure functions, no terminal I/O.

Block characters can only draw a *square* QR by packing the character cell 1×2
(half-blocks), because a cell is about twice as tall as it is wide. Denser
packings buy their compactness by stretching the modules: 2×3 sextants make each
module a third taller than it is wide, and 2×4 octants — the packing that would
be both square and small — cannot be drawn at all, since Unicode 16 leaves 10 of
the 256 two-by-four patterns unencoded (there are no quarter-block characters)
and a real pairing payload needs one of them in 29 of its 378 cells.

Sixel sidesteps the grid entirely: the terminal draws pixels, so the code is
square and as small as the display can resolve, with no font involved. Where it
isn't available the block renderings in `qr.py` still stand.

The format, briefly: a DCS-introduced payload where each character carries six
vertical pixels of one color, `$` returns to the start of the band for another
color pass, and `-` starts the next band.
"""

import math

# `#<register>;2;<r>;<g>;<b>` — type 2 is RGB, and the components are percent,
# not 0-255. Forced black-on-white for the same reason the block renderer forces
# it: a code drawn in the terminal's palette renders inverted on a dark theme.
_WHITE, _BLACK = 0, 1
_PALETTE = f"#{_WHITE};2;100;100;100#{_BLACK};2;0;0;0"

_BAND = 6  # pixel rows per sixel character

# Run-length encoding is `!<count><char>`, so it only pays from four repeats up.
_RLE_FLOOR = 4

# Pixels per module when the terminal won't say how big a character cell is.
# Six lands a typical 45-module payload near 320px — comfortably scannable off a
# screen, and roughly 19 rows tall on a common 8×17 cell.
DEFAULT_SCALE = 6
_MIN_SCALE, _MAX_SCALE = 3, 12

# What the code should leave room for: the surrounding text plus a couple of
# lines of breathing room above and below.
_ROW_BUDGET = 20


def supports_sixel(device_attributes: str) -> bool:
    """Whether a DA1 reply (``ESC [ ? 62 ; 4 ; 6 c``) advertises sixel.

    Sixel is parameter ``4``. Anything unparseable answers False: a terminal that
    didn't reply, replied late, or replied in a shape we don't know is a terminal
    we draw with characters.
    """
    _, marker, rest = device_attributes.partition("\033[?")
    if not marker:
        return False
    params, terminator, _ = rest.partition("c")
    return bool(terminator) and "4" in params.split(";")


def choose_scale(modules: int, *, cell: tuple[int, int] | None) -> int:
    """Pixels per module, given the terminal's character-cell size in pixels.

    Without a cell size (tmux and plenty of terminals report none) there is no
    way to turn pixels into rows, so this falls back to a fixed scale rather than
    guessing at the geometry.
    """
    if cell is None:
        return DEFAULT_SCALE
    _, cell_height = cell
    if cell_height <= 0 or modules <= 0:
        return DEFAULT_SCALE
    return max(_MIN_SCALE, min(_MAX_SCALE, _ROW_BUDGET * cell_height // modules))


def rendered_rows(modules: int, *, scale: int, cell: tuple[int, int] | None) -> int:
    """Text rows the image will occupy, for the height note.

    The estimate is only as good as the reported cell size; with none, assume a
    17-pixel cell, which is the common case for a 1080p terminal at a readable
    point size.
    """
    cell_height = cell[1] if cell and cell[1] > 0 else 17
    return math.ceil(modules * scale / cell_height)


def render(matrix: list[tuple[int, ...]], *, scale: int) -> str:
    """``matrix`` as a sixel image at ``scale`` pixels per module.

    Every pixel is painted explicitly — the light ones too. Leaving them to the
    terminal's background would paint the quiet zone in the theme's color, and a
    quiet zone that isn't light is not a quiet zone.
    """
    if not matrix:
        return ""
    height, width = len(matrix) * scale, len(matrix[0]) * scale
    bands = [_band(matrix, top, width=width, height=height, scale=scale)
             for top in range(0, height, _BAND)]
    return f'\033Pq"1;1;{width};{height}{_PALETTE}' + "-".join(bands) + "\033\\"


def _band(matrix: list[tuple[int, ...]], top: int, *,
          width: int, height: int, scale: int) -> str:
    """One band of six pixel rows, as a color pass per palette entry."""
    dark = [0] * width
    for offset in range(_BAND):
        y = top + offset
        if y >= height:
            break
        row = matrix[y // scale]
        for x in range(width):
            if row[x // scale]:
                dark[x] |= 1 << offset
    full = (1 << min(_BAND, height - top)) - 1
    return "$".join([
        f"#{_WHITE}" + _run_length(full ^ value for value in dark),
        f"#{_BLACK}" + _run_length(iter(dark)),
    ])


def _run_length(values) -> str:
    """Sixel characters for ``values``, collapsing runs of four or more."""
    out: list[str] = []
    previous, count = None, 0
    for value in values:
        if value == previous:
            count += 1
            continue
        if previous is not None:
            out.append(_repeat(previous, count))
        previous, count = value, 1
    if previous is not None:
        out.append(_repeat(previous, count))
    return "".join(out)


def _repeat(value: int, count: int) -> str:
    char = chr(63 + value)
    return f"!{count}{char}" if count >= _RLE_FLOOR else char * count
