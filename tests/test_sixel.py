"""The sixel encoder and the capability parse.

The encoder is checked by decoding: a QR whose image doesn't reproduce the
matrix is a code nobody can scan, and only a round trip catches that.
"""

import math
import re

import pytest

from marim_harness.interfaces.sixel import (
    DEFAULT_SCALE,
    choose_scale,
    render,
    rendered_rows,
    supports_sixel,
)


def decode(payload: str) -> list[list[int]]:
    """A sixel payload back into a pixel grid of 0 (white) / 1 (black).

    Deliberately a separate implementation from the encoder's — a shared helper
    would let the same misunderstanding pass both ways.
    """
    body = payload.split("q", 1)[1].removesuffix("\033\\")
    raster = re.match(r'"\d+;\d+;(\d+);(\d+)', body)
    assert raster, "the payload must declare its raster size"
    width, height = int(raster.group(1)), int(raster.group(2))
    # Palette definitions out of the way, `#<n>` can only mean "select color n".
    body = re.sub(r"#\d+;\d+;\d+;\d+;\d+", "", body[raster.end():])
    pixels = [[0] * width for _ in range(height)]
    color, top, x, index = 0, 0, 0, 0
    while index < len(body):
        char = body[index]
        index += 1
        if char == "#":
            digits = ""
            while index < len(body) and body[index].isdigit():
                digits += body[index]
                index += 1
            color = int(digits)
            x = 0
        elif char == "$":
            x = 0
        elif char == "-":
            top += 6
            x = 0
        elif char == "!":
            digits = ""
            while body[index].isdigit():
                digits += body[index]
                index += 1
            x = _paint(pixels, body[index], x, top, color, int(digits), width, height)
            index += 1
        else:
            x = _paint(pixels, char, x, top, color, 1, width, height)
    return pixels


def _paint(pixels, char, x, top, color, count, width, height):
    bits = ord(char) - 63
    for _ in range(count):
        for offset in range(6):
            if bits >> offset & 1 and top + offset < height and x < width:
                pixels[top + offset][x] = color
        x += 1
    return x


@pytest.mark.parametrize("scale", [1, 3, 6])
def test_the_image_decodes_back_to_the_matrix(scale):
    matrix = [
        (1, 1, 1, 0, 1),
        (1, 0, 1, 0, 0),
        (1, 1, 1, 1, 0),
        (0, 0, 0, 1, 1),
        (1, 0, 1, 0, 1),
    ]
    pixels = decode(render(matrix, scale=scale))
    assert len(pixels) == len(matrix) * scale
    assert len(pixels[0]) == len(matrix[0]) * scale
    for y, row in enumerate(pixels):
        for x, value in enumerate(row):
            assert value == matrix[y // scale][x // scale], f"pixel {x},{y}"


def test_a_real_pairing_payload_survives_the_round_trip():
    from marim_harness.interfaces.qr import encode

    matrix = encode("marim://pair?v=1&url=http%3A%2F%2F192.168.0.3%3A8642"
                    "&token=" + "a" * 43 + "&name=workstation")
    pixels = decode(render(matrix, scale=4))
    assert [[pixels[y * 4][x * 4] for x in range(len(matrix[0]))] for y in range(len(matrix))] \
        == [list(row) for row in matrix]


def test_the_image_is_square_and_declares_its_own_size():
    matrix = [(0,) * 53] * 53
    payload = render(matrix, scale=6)
    assert payload.startswith('\033Pq"1;1;318;318')
    assert payload.endswith("\033\\")


def test_light_modules_are_painted_rather_than_left_to_the_background():
    """A quiet zone showing through to a dark terminal theme is not a quiet zone."""
    pixels = decode(render([(0, 0), (0, 0)], scale=2))
    assert pixels == [[0] * 4] * 4
    assert "#0" in render([(0, 0), (0, 0)], scale=2)


def test_a_partial_final_band_stops_at_the_image_edge():
    """Eight pixel rows is a full band plus two, and the stragglers must not
    bleed into rows the image doesn't have."""
    matrix = [(1,)] * 8
    pixels = decode(render(matrix, scale=1))
    assert len(pixels) == 8
    assert all(row == [1] for row in pixels)


def test_runs_collapse_but_short_stretches_stay_literal():
    wide = render([(1,) * 40], scale=1)
    assert "!40" in wide or "!" in wide
    assert "!" not in render([(1,)], scale=1)


def test_empty_matrix_renders_nothing():
    assert render([], scale=6) == ""


@pytest.mark.parametrize("reply,expected", [
    ("\033[?62;4;6;9;22c", True),
    ("\033[?62;4c", True),
    ("\033[?64;22c", False),          # no sixel among the parameters
    ("\033[?62;44c", False),          # 44 is not 4
    ("", False),                      # nothing answered
    ("\033[?62;4", False),            # truncated: no terminator
    ("garbage", False),
])
def test_sixel_is_read_from_the_device_attributes_reply(reply, expected):
    assert supports_sixel(reply) is expected


def test_scale_falls_back_when_the_cell_size_is_unknown():
    assert choose_scale(53, cell=None) == DEFAULT_SCALE
    assert choose_scale(53, cell=(8, 0)) == DEFAULT_SCALE
    assert choose_scale(0, cell=(8, 17)) == DEFAULT_SCALE


def test_scale_targets_a_row_budget_and_stays_in_bounds():
    # 20 rows of a 17px cell is 340px across 53 modules — six pixels each.
    assert choose_scale(53, cell=(8, 17)) == 6
    # A huge cell (hidpi) can't push the modules past the ceiling...
    assert choose_scale(21, cell=(20, 42)) == 12
    # ...and a tiny one can't shrink them below the floor.
    assert choose_scale(177, cell=(4, 8)) == 3


def test_rendered_rows_uses_the_cell_height_when_there_is_one():
    assert rendered_rows(53, scale=6, cell=(8, 17)) == math.ceil(53 * 6 / 17)
    assert rendered_rows(53, scale=6, cell=None) == math.ceil(53 * 6 / 17)
    assert rendered_rows(53, scale=6, cell=(10, 30)) == math.ceil(53 * 6 / 30)
