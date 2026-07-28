"""Terminal QR rendering: segno is used only as an encoder, so the half-block
packing and the forced black-on-white are ours to test."""

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
    line = render_matrix(matrix).splitlines()[0]
    assert line == f"{BLACK_ON_WHITE}▀▄{RESET}"


def test_render_covers_every_module_pair():
    matrix = [(0, 1, 0, 1), (0, 0, 1, 1)]
    assert render_matrix(matrix).splitlines()[0] == f"{BLACK_ON_WHITE} ▀▄█{RESET}"


def test_render_pads_an_odd_final_row_with_light_modules():
    """The quiet zone is light, so a dangling row must not invent dark modules."""
    matrix = [(1, 1), (1, 1), (1, 1)]
    lines = render_matrix(matrix).splitlines()
    assert len(lines) == 2
    assert lines[1] == f"{BLACK_ON_WHITE}▀▀{RESET}"


def test_every_rendered_line_forces_its_own_colors():
    """One SGR pair per line, so the code survives being scrolled through or
    copied out of a transcript with other output interleaved."""
    matrix = [(1, 0), (0, 1), (1, 1), (0, 0)]
    for line in render_matrix(matrix).splitlines():
        assert line.startswith(BLACK_ON_WHITE)
        assert line.endswith(RESET)


def test_rendered_rows_halves_the_matrix_rounding_up():
    assert rendered_rows([(0,)] * 53) == 27
    assert rendered_rows([(0,)] * 52) == 26


def test_height_note_only_fires_on_a_short_terminal():
    assert height_note(rendered=27, terminal_lines=60) is None
    note = height_note(rendered=27, terminal_lines=24)
    assert note and "24" in note
    # An unknown terminal size (0) is not a short terminal.
    assert height_note(rendered=27, terminal_lines=0) is None


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
    rendered = render_matrix(encode("marim://pair?v=1&url=x&token=y&name=z"))
    assert rendered.count("\n") + 1 == rendered_rows(encode("marim://pair?v=1&url=x&token=y&name=z"))
