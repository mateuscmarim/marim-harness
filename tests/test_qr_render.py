"""Terminal QR rendering: segno is used only as an encoder, so the module
packing and the forced black-on-white are ours to test."""

import unicodedata

from marim_harness.interfaces.qr import (
    QUIET_ZONE,
    encode,
    height_note,
    render_matrix,
    rendered_rows,
)

BLACK_ON_WHITE = "\033[38;2;0;0;0;48;2;255;255;255m"
RESET = "\033[0m"


def test_render_packs_two_module_rows_into_one_text_row():
    #  dark  light
    #  light dark
    matrix = [(1, 0), (0, 1)]
    line = render_matrix(matrix, wide=True).splitlines()[0]
    assert line == f"{BLACK_ON_WHITE}▀▄{RESET}"


def test_render_covers_every_module_pair():
    matrix = [(0, 1, 0, 1), (0, 0, 1, 1)]
    assert render_matrix(matrix, wide=True).splitlines()[0] == f"{BLACK_ON_WHITE} ▀▄█{RESET}"


def test_render_pads_an_odd_final_row_with_light_modules():
    """The quiet zone is light, so a dangling row must not invent dark modules."""
    matrix = [(1, 1), (1, 1), (1, 1)]
    lines = render_matrix(matrix, wide=True).splitlines()
    assert len(lines) == 2
    assert lines[1] == f"{BLACK_ON_WHITE}▀▀{RESET}"


def test_every_rendered_line_forces_its_own_colors():
    """One SGR pair per line, so the code survives being scrolled through or
    copied out of a transcript with other output interleaved."""
    matrix = [(1, 0), (0, 1), (1, 1), (0, 0)]
    for line in render_matrix(matrix, wide=True).splitlines():
        assert line.startswith(BLACK_ON_WHITE)
        assert line.endswith(RESET)


def test_default_packing_is_three_module_rows_by_two_columns():
    """Six modules per cell — the whole reason the default block fits an 80×24
    terminal. Sextant names spell out which of the six are dark, numbered in
    reading order, so the name is an independent check on the codepoint math."""
    #  dark  light
    #  light dark
    #  dark  dark
    matrix = [(1, 0), (0, 1), (1, 1)]
    line = render_matrix(matrix).splitlines()[0]
    cells = line[len(BLACK_ON_WHITE):-len(RESET)]
    assert len(cells) == 1
    assert unicodedata.name(cells) == "BLOCK SEXTANT-1456"


def test_default_packing_uses_the_block_elements_unicode_left_out():
    """Four of the 64 combinations have no sextant of their own; drawing them as
    U+1FB00-block arithmetic would land on the wrong glyph."""
    columns = [(0, 0), (1, 0), (0, 1), (1, 1)]
    matrix = [tuple(bit for column in columns for bit in column)] * 3
    cells = render_matrix(matrix).splitlines()[0][len(BLACK_ON_WHITE):-len(RESET)]
    assert cells == " ▌▐█"


def test_default_packing_pads_partial_cells_with_light_modules():
    """An odd column count and a row count off a multiple of three both pad with
    light — which widens the quiet zone rather than eating into it."""
    matrix = [(1, 1, 1), (1, 1, 1)]
    cells = render_matrix(matrix).splitlines()[0][len(BLACK_ON_WHITE):-len(RESET)]
    assert unicodedata.name(cells[0]) == "BLOCK SEXTANT-1234"  # bottom row light
    assert unicodedata.name(cells[1]) == "BLOCK SEXTANT-13"    # right column light too


def test_every_default_line_forces_its_own_colors():
    matrix = [(1, 0), (0, 1), (1, 1), (0, 0)]
    for line in render_matrix(matrix).splitlines():
        assert line.startswith(BLACK_ON_WHITE)
        assert line.endswith(RESET)


def test_rendered_rows_divides_the_matrix_rounding_up():
    assert rendered_rows([(0,)] * 53) == 18
    assert rendered_rows([(0,)] * 54) == 18
    assert rendered_rows([(0,)] * 55) == 19
    assert rendered_rows([(0,)] * 53, wide=True) == 27
    assert rendered_rows([(0,)] * 52, wide=True) == 26


def test_height_note_only_fires_on_a_short_terminal():
    assert height_note(rendered=27, terminal_lines=60) is None
    note = height_note(rendered=27, terminal_lines=24)
    assert note and "24" in note
    # An unknown terminal size (0) is not a short terminal.
    assert height_note(rendered=27, terminal_lines=0) is None


def test_the_default_packing_fits_a_pairing_payload_on_a_short_terminal():
    """The point of the sextants. A realistic payload — LAN address, a 43-char
    token, a hostname — is 18 rows of code and 24 lines of block, so it clears a
    25-line terminal where the half-block form needs 33 and doesn't."""
    uri = ("marim://pair?v=1&url=http%3A%2F%2F192.168.0.3%3A8642"
           "&token=" + "a" * 43 + "&name=workstation")
    matrix = encode(uri)
    assert rendered_rows(matrix) == 18
    assert height_note(rendered=rendered_rows(matrix), terminal_lines=25) is None
    assert height_note(rendered=rendered_rows(matrix, wide=True), terminal_lines=25)


def test_encode_returns_a_square_matrix_with_the_quiet_zone():
    matrix = encode("marim://pair?v=1&url=http%3A%2F%2F192.168.0.3%3A8642&token=abc&name=box")
    assert len(matrix) == len(matrix[0])
    # The quiet zone is light on every side; segno's `matrix` property omits it,
    # `matrix_iter(border=…)` includes it — this asserts we used the latter.
    assert set(matrix[0]) == {0}
    assert set(row[0] for row in matrix) == {0}
    assert all(matrix[QUIET_ZONE - 1][i] == 0 for i in range(len(matrix)))


def test_encode_round_trips_through_the_renderer():
    """Guards the seam: whatever segno hands back must be renderable as-is."""
    matrix = encode("marim://pair?v=1&url=x&token=y&name=z")
    for wide in (False, True):
        rendered = render_matrix(matrix, wide=wide)
        assert rendered.count("\n") + 1 == rendered_rows(matrix, wide=wide)
