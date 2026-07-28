# `marim serve` QR Pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `marim serve qr` (and `marim serve --qr`) prints a terminal QR code encoding a `marim://pair?v=1&url=…&token=…&name=…` URI, so scanning it once provisions a `marim-mobile` server profile instead of typing a 43-character bearer token on a phone.

**Architecture:** Three new units with one responsibility each. `server/pairing.py` builds the payload and resolves the address to encode (pure, except one socket probe). `interfaces/qr.py` turns a URI into terminal art (segno as encoder only; rendering is ours, in forced black-on-white). `interfaces/cli/serve.py` wires them into the CLI and owns the refusal rules. Nothing talks to a running daemon — the bearer token is a file on disk, so pairing works before the first start.

**Tech Stack:** Python ≥3.10, stdlib `socket`/`urllib.parse`/`shutil`, `segno` (BSD, pure Python, no runtime deps) as an addition to the existing `serve` extra, pytest.

Spec: `docs/superpowers/specs/2026-07-27-serve-qr-pairing-design.md` (commit `b70cda3`).

## Global Constraints

- Ruff line length is 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity capped at 10. Run `uv run ruff check src tests`.
- `requires-python = ">=3.10"` — no 3.11+ only syntax.
- Type-checks under `uv run pyright` (standard mode, `src` only), zero errors.
- Use `uv` for everything (`uv run …`). Never bare `python`/`pytest`/`pip`.
- Gate order before claiming a task done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Tool/CLI help strings and module docstrings are product surface — write them for a reader, and keep the codebase's habit of explaining *why* a non-obvious invariant holds.
- **The QR encodes a bearer token.** No code path may write it to a non-tty stream. Every task preserves this.
- Exact URI format, fixed by the spec as a cross-repo contract with `marim-mobile`: `marim://pair?v=1&url=<full URL, escaped>&token=<43-char>&name=<profile name>`.
- Exit codes for `marim serve qr`: `0` printed (or segno absent → URI text + hint), `1` refusal/no-address, `2` argparse usage error.

---

### Task 1: The pairing payload and the address to encode

**Files:**
- Create: `src/marim_harness/server/pairing.py`
- Test: `tests/test_serve_pairing.py`

**Interfaces:**
- Consumes: nothing (leaf module — stdlib only).
- Produces, all imported by Task 3 and Task 4:
  - `pairing_uri(url: str, token: str, name: str) -> str`
  - `parse_advertise(value: str, *, default_port: int) -> str` — note the spec's
    signature sketch says `-> (scheme, host, port)` in one line and "normalizes
    to a URL" in the next; the URL string is the one every caller wants, so
    that's what this returns.
  - `advertised_address(port: int) -> str` (raises `OSError` when there is no route)
  - `default_name() -> str`
  - `loopback_warning(url: str) -> str | None`
  - `bind_loopback_warning(host: str) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_serve_pairing.py`:

```python
"""The `marim serve qr` payload: building the marim://pair URI, normalizing
--advertise, and the loopback warnings. All pure — the one socket probe
(`advertised_address`) is exercised through its caller in test_cli_serve_qr.py."""

import urllib.parse

import pytest

from marim_harness.server.pairing import (
    bind_loopback_warning,
    default_name,
    loopback_warning,
    pairing_uri,
    parse_advertise,
)


def _query(uri: str) -> dict[str, str]:
    split = urllib.parse.urlsplit(uri)
    return dict(urllib.parse.parse_qsl(split.query))


def test_pairing_uri_carries_the_versioned_contract():
    uri = pairing_uri("http://192.168.0.3:8642", "tok-123", "workstation")
    assert uri.startswith("marim://pair?")
    assert _query(uri) == {
        "v": "1",
        "url": "http://192.168.0.3:8642",
        "token": "tok-123",
        "name": "workstation",
    }


def test_pairing_uri_escapes_the_url_and_the_name():
    uri = pairing_uri("http://192.168.0.3:8642", "t", "Mateus' box")
    assert "http%3A%2F%2F192.168.0.3%3A8642" in uri
    assert " " not in uri
    assert _query(uri)["name"] == "Mateus' box"  # round-trips despite escaping


def test_advertise_accepts_a_bare_host():
    assert parse_advertise("192.168.0.3", default_port=8642) == "http://192.168.0.3:8642"


def test_advertise_accepts_host_and_port():
    assert parse_advertise("192.168.0.3:9000", default_port=8642) == "http://192.168.0.3:9000"


def test_advertise_passes_a_full_url_through_untouched():
    """The reverse-proxy / tailnet case — no port is invented for https."""
    assert parse_advertise(
        "https://marim.example.com", default_port=8642
    ) == "https://marim.example.com"
    assert parse_advertise(
        "https://marim.example.com/", default_port=8642
    ) == "https://marim.example.com"


def test_advertise_brackets_a_bare_ipv6_and_respects_an_explicit_one():
    assert parse_advertise("::1", default_port=8642) == "http://[::1]:8642"
    assert parse_advertise("[fd00::5]:9000", default_port=8642) == "http://[fd00::5]:9000"


def test_advertise_rejects_junk():
    with pytest.raises(ValueError):
        parse_advertise("   ", default_port=8642)
    with pytest.raises(ValueError):
        parse_advertise("http://", default_port=8642)


def test_loopback_warning_fires_only_for_unreachable_hosts():
    assert loopback_warning("http://127.0.0.1:8642")
    assert loopback_warning("http://localhost:8642")
    assert loopback_warning("http://[::1]:8642")
    assert loopback_warning("http://192.168.0.3:8642") is None
    assert loopback_warning("https://marim.example.com") is None


def test_bind_loopback_warning_names_the_fix():
    note = bind_loopback_warning("127.0.0.1")
    assert note and "--host 0.0.0.0" in note
    assert bind_loopback_warning("0.0.0.0") is None
    assert bind_loopback_warning("192.168.0.3") is None


def test_default_name_is_a_non_empty_string():
    assert default_name()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_serve_pairing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.server.pairing'`

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/server/pairing.py`:

```python
"""The pairing payload a client scans to provision a server profile.

A `marim://pair?v=1&url=…&token=…&name=…` URI carries everything
`marim-mobile` needs for one profile (name + base URL in Room, token in the
Keystore-backed store), so pairing is one scan instead of typing a 43-character
bearer token on a phone keyboard. The format is a cross-repo contract: `v=1`
exists so the client can reject a future shape instead of mis-parsing it, and
`url` keeps its scheme so a reverse-proxy or tailnet address round-trips.

Everything here is pure except `advertised_address`, whose whole job is to ask
the kernel a question.
"""

import socket
import urllib.parse

PAIR_VERSION = "1"

# TEST-NET-1 (RFC 5737): guaranteed not to be a real destination. A UDP
# "connect" sends no packets — it only makes the kernel pick, and report back,
# the source address it would route from. That is exactly the address a phone
# on the same network should scan, and unlike walking the interface list it
# never returns a docker0/bridge address that nothing off-box can reach.
_PROBE_TARGET = ("192.0.2.1", 80)

_LOOPBACK_HOSTS = frozenset({"localhost", "::1"})


def pairing_uri(url: str, token: str, name: str) -> str:
    """The scannable URI. Values are percent-escaped; the client parses it with
    any standard URI parser."""
    query = urllib.parse.urlencode(
        {"v": PAIR_VERSION, "url": url, "token": token, "name": name}
    )
    return f"marim://pair?{query}"


def parse_advertise(value: str, *, default_port: int) -> str:
    """Normalize an ``--advertise`` value into a full URL.

    Accepts a bare host, ``host:port``, a bracketed IPv6 literal, or a complete
    URL. A full URL passes through untouched — no port is invented for it,
    because ``https://marim.example.com`` behind a proxy is already complete.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("expected a host, host:port, or URL")
    if "://" in raw:
        if not urllib.parse.urlsplit(raw).hostname:
            raise ValueError(f"no host in {value!r}")
        return raw.rstrip("/")
    host, port = _split_host_port(raw, default_port)
    return f"http://{host}:{port}"


def advertised_address(port: int) -> str:
    """The URL a client on the same network should use, derived from the source
    address of the default route. Raises ``OSError`` when there is no route to
    probe — the caller turns that into "pass --advertise"."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(_PROBE_TARGET)
        host = sock.getsockname()[0]
    finally:
        sock.close()
    return f"http://{host}:{port}"


def default_name() -> str:
    """The profile name the client will show. The machine's hostname beats an
    IP address on a phone screen."""
    return socket.gethostname() or "marim"


def loopback_warning(url: str) -> str | None:
    """Set when the *encoded* address can only be reached from this machine."""
    if not _is_loopback(urllib.parse.urlsplit(url).hostname or ""):
        return None
    return (
        "this code encodes a loopback address, which only this machine can reach — "
        "pass --advertise <host> to encode one a phone can use"
    )


def bind_loopback_warning(host: str) -> str | None:
    """Set when the *daemon* is bound somewhere nothing off-box can reach, which
    makes even a correctly-encoded LAN address refuse the connection."""
    if not _is_loopback(host):
        return None
    return (
        f"the daemon is bound to {host}, so nothing off this machine can connect — "
        "restart it with --host 0.0.0.0 to pair a phone"
    )


def _is_loopback(host: str) -> bool:
    return host.strip("[]").lower() in _LOOPBACK_HOSTS or host.startswith("127.")


def _split_host_port(raw: str, default_port: int) -> tuple[str, int]:
    """Split ``host``/``host:port``/``[v6]:port``, keeping IPv6 literals bracketed
    so the assembled URL stays parseable."""
    if raw.startswith("["):
        host, _, rest = raw.partition("]")
        host += "]"
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    head, sep, tail = raw.rpartition(":")
    if sep and tail.isdigit() and ":" not in head and head:
        return head, int(tail)
    if ":" in raw:  # a bare IPv6 literal — bracket it
        return f"[{raw}]", default_port
    return raw, default_port
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_serve_pairing.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Run the gates**

```bash
uv run ruff check src tests
uv run pyright
```
Expected: "All checks passed!" and "0 errors".

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/server/pairing.py tests/test_serve_pairing.py
git commit -m "feat(serve): pairing URI and advertised-address resolution"
```

---

### Task 2: QR encoding and terminal rendering

**Files:**
- Create: `src/marim_harness/interfaces/qr.py`
- Modify: `pyproject.toml:52` (the `serve` extra)
- Test: `tests/test_qr_render.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces, imported by Task 3:
  - `encode(uri: str) -> list[tuple[int, ...]]` (raises `ImportError` when segno is absent)
  - `render_matrix(matrix: list[tuple[int, ...]]) -> str`
  - `rendered_rows(matrix: list[tuple[int, ...]]) -> int`
  - `height_note(*, rendered: int, terminal_lines: int) -> str | None`
  - `QUIET_ZONE: int` (= 4)

- [ ] **Step 1: Add segno to the serve extra**

In `pyproject.toml`, replace the `serve` extra (line 50-52) with:

```toml
# The `marim serve` HTTP daemon (REST + SSE). Bare installs print an install
# hint when starlette/uvicorn are absent. segno encodes the pairing QR
# (`marim serve qr`) — pure Python, no runtime deps, and used only as an
# encoder: the terminal rendering is ours, in interfaces/qr.py.
serve = ["starlette>=0.40", "uvicorn>=0.30", "segno>=1.6"]
```

Then run `uv sync --extra serve` so segno is importable in the dev venv.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_qr_render.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_qr_render.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.qr'`

- [ ] **Step 4: Write the implementation**

Create `src/marim_harness/interfaces/qr.py`:

```python
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
        cells = "".join(_BLOCKS[(t, b)] for t, b in zip(top, bottom))
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_qr_render.py -q`
Expected: PASS (8 tests)

- [ ] **Step 6: Run the gates**

```bash
uv run ruff check src tests
uv run pyright
```
Expected: "All checks passed!" and "0 errors".

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/qr.py tests/test_qr_render.py pyproject.toml uv.lock
git commit -m "feat(serve): terminal QR rendering with a forced-contrast palette"
```

---

### Task 3: The `marim serve qr` subcommand

**Files:**
- Modify: `src/marim_harness/interfaces/cli/serve.py` (dispatch + the whole qr path; also the `out`/`err` default fix at line 87)
- Test: `tests/test_cli_serve_qr.py`

**Interfaces:**
- Consumes: everything Task 1 and Task 2 produce, plus the existing `_default_state_dir()` (`serve.py:11`) and `load_or_create_token` (`server/auth.py`). Note `server/__init__.py` re-exports nothing and only `server/http.py` imports starlette, so the qr path works on a bare install — segno is its only optional dependency.
- Produces, used by Task 4: `_qr_refusal(*, isatty, env) -> str | None`, `_pairing_block(*, url, token, name, warning, terminal_lines) -> str`, `_resolve_pair_url(advertise, port) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_serve_qr.py`:

```python
"""`marim serve qr`: address resolution, the refusal rules, and the one
invariant that matters most — the bearer token never reaches a non-tty stream."""

import io

import pytest

from marim_harness.interfaces.cli import serve


class _Tty(io.StringIO):
    """stdout that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def state(tmp_path, monkeypatch):
    """An isolated server state dir; returns the token the QR should carry."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(serve, "advertised_address", lambda port: f"http://192.168.0.3:{port}")
    from marim_harness.server.auth import load_or_create_token

    return load_or_create_token(tmp_path / "xdg-data" / "marim-harness" / "server")


def test_qr_prints_a_code_and_the_resolved_address(state):
    out, err = _Tty(), io.StringIO()
    assert serve.main(["qr"], out=out, err=err) == 0
    text = out.getvalue()
    assert "█" in text or "▀" in text  # the code itself
    assert "http://192.168.0.3:8642" in text
    assert "treat this like a password" in text


def test_qr_refuses_when_stdout_is_not_a_terminal_and_leaks_nothing(state):
    """The security invariant: a redirected QR would write the token to a file."""
    out, err = io.StringIO(), io.StringIO()
    assert serve.main(["qr"], out=out, err=err) == 1
    assert state not in out.getvalue()
    assert state not in err.getvalue()
    assert out.getvalue() == ""
    assert "terminal" in err.getvalue()


def test_qr_refuses_under_no_color(state, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out, err = _Tty(), io.StringIO()
    assert serve.main(["qr"], out=out, err=err) == 1
    assert "NO_COLOR" in err.getvalue()
    assert out.getvalue() == ""


def test_qr_advertise_overrides_the_probe(state):
    out = _Tty()
    assert serve.main(
        ["qr", "--advertise", "https://marim.example.com"], out=out, err=io.StringIO()
    ) == 0
    assert "https://marim.example.com" in out.getvalue()
    assert "192.168.0.3" not in out.getvalue()


def test_qr_port_flag_reaches_the_encoded_url(state):
    out = _Tty()
    assert serve.main(["qr", "--port", "9000"], out=out, err=io.StringIO()) == 0
    assert "http://192.168.0.3:9000" in out.getvalue()


def test_qr_name_defaults_to_the_hostname_and_is_overridable(state, monkeypatch):
    monkeypatch.setattr(serve, "default_name", lambda: "workstation")
    out = _Tty()
    assert serve.main(["qr"], out=out, err=io.StringIO()) == 0
    assert "workstation" in out.getvalue()

    out = _Tty()
    assert serve.main(["qr", "--name", "desk-box"], out=out, err=io.StringIO()) == 0
    assert "desk-box" in out.getvalue()


def test_qr_without_a_route_tells_you_to_advertise(state, monkeypatch):
    def no_route(port):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(serve, "advertised_address", no_route)
    out, err = _Tty(), io.StringIO()
    assert serve.main(["qr"], out=out, err=err) == 1
    assert "--advertise" in err.getvalue()


def test_qr_warns_when_the_encoded_address_is_loopback(state):
    out = _Tty()
    assert serve.main(["qr", "--advertise", "127.0.0.1"], out=out, err=io.StringIO()) == 0
    assert "loopback" in out.getvalue()


def test_qr_without_segno_falls_back_to_the_uri(state, monkeypatch):
    def no_segno(uri):
        raise ImportError("No module named 'segno'")

    monkeypatch.setattr(serve, "encode", no_segno)
    out = _Tty()
    assert serve.main(["qr"], out=out, err=io.StringIO()) == 0
    text = out.getvalue()
    assert "marim://pair?" in text
    assert "segno" in text
    assert "█" not in text


def test_qr_creates_the_token_before_the_daemon_has_ever_run(tmp_path, monkeypatch):
    """The token file is the contract, not the process — pairing works first."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(serve, "advertised_address", lambda port: f"http://10.0.0.2:{port}")
    out = _Tty()
    assert serve.main(["qr"], out=out, err=io.StringIO()) == 0
    assert (tmp_path / "fresh" / "marim-harness" / "server" / "token").exists()


def test_qr_rejects_unknown_flags(state):
    with pytest.raises(SystemExit):
        serve.main(["qr", "--bogus"], out=_Tty(), err=io.StringIO())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_cli_serve_qr.py -q`
Expected: FAIL — `AttributeError: module 'marim_harness.interfaces.cli.serve' has no attribute 'advertised_address'`

- [ ] **Step 3: Add the imports and fix the stream defaults**

In `src/marim_harness/interfaces/cli/serve.py`, extend the import block at the top (currently `from ..branding import …` at line 13):

```python
from ...server.pairing import (
    advertised_address,
    bind_loopback_warning,
    default_name,
    loopback_warning,
    pairing_uri,
    parse_advertise,
)
from ..branding import banner_enabled, color_enabled, field_block, package_version, wordmark_block
from ..qr import encode, height_note, render_matrix, rendered_rows
```

and add `import shutil` to the stdlib imports.

Then change the `main` signature (line 87) from `out=sys.stdout, err=sys.stderr` to the resolved-inside form, matching `trust_cmd.main`:

```python
def main(argv: list[str], *, out=None, err=None) -> int:
    # Resolved here, not bound as a def-time default: a `def main(..., out=sys.stdout)`
    # default is evaluated once at first import, which can capture a stale stream
    # before pytest swaps in its own (see the same note in trust_cmd.py).
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if argv and argv[0] == "qr":
        return _qr_main(argv[1:], out=out, err=err)
    parser = argparse.ArgumentParser(
```

- [ ] **Step 4: Write the qr command**

Add to `src/marim_harness/interfaces/cli/serve.py`, above `main`:

```python
def _qr_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marim serve qr",
        description="Print a QR code that pairs a marim client with this machine's "
                    "daemon. The code carries the bearer token, so it prints to a "
                    "terminal only.",
    )
    parser.add_argument("--port", type=int, default=8642,
                        help="port the daemon listens on (default: 8642); this command "
                             "can't discover a running daemon's port")
    parser.add_argument("--advertise", default=None, metavar="HOST[:PORT]",
                        help="address to encode instead of the auto-detected one — a "
                             "bare host, host:port, or a full URL (the way to encode a "
                             "tailnet name or a reverse proxy)")
    parser.add_argument("--name", default=None,
                        help="profile name shown on the client (default: this machine's "
                             "hostname)")
    return parser


def _qr_refusal(*, isatty: bool, env) -> str | None:
    """Why we won't print a code, or None to go ahead.

    Both refusals protect the reader rather than the code: a redirected QR is a
    bearer token written verbatim into a file, and an unforced-color QR renders
    inverted on a dark theme and may not scan at all.
    """
    if not isatty:
        return (
            "refusing to print a pairing QR: stdout isn't a terminal, and the code "
            "encodes your bearer token — it would land in a file or log verbatim"
        )
    if (env.get("NO_COLOR") or "").strip():
        return (
            "refusing to print a pairing QR: NO_COLOR is set, and a code drawn in the "
            "terminal's own colors renders inverted on a dark theme and may not scan"
        )
    return None


def _resolve_pair_url(advertise: str | None, port: int) -> str:
    """The URL to encode: an explicit --advertise wins, else the default route."""
    if advertise:
        return parse_advertise(advertise, default_port=port)
    return advertised_address(port)


def _pairing_block(*, url: str, token: str, name: str, warning: str | None,
                   terminal_lines: int) -> str:
    """The whole printed block: optional warning, the code (or a typed-URI
    fallback), and the facts under it."""
    uri = pairing_uri(url, token, name)
    lines = [f"warning: {warning}", ""] if warning else []
    try:
        matrix = encode(uri)
    except ImportError:
        lines.extend([
            "QR rendering needs segno. Install with:",
            "  uv add 'marim-harness[serve]'   (or: pip install segno)",
            "",
            "or pair by hand with this URI:",
            f"  {uri}",
        ])
        return "\n".join(lines)
    note = height_note(rendered=rendered_rows(matrix), terminal_lines=terminal_lines)
    if note:
        lines.extend([note, ""])
    lines.extend([
        render_matrix(matrix),
        "",
        f"  {url}",
        f"  {name} · token included — treat this like a password",
        "",
        "  wrong address?  marim serve qr --advertise <host>",
    ])
    return "\n".join(lines)


def _qr_main(argv: list[str], *, out, err) -> int:
    args = _qr_parser().parse_args(argv)
    refusal = _qr_refusal(isatty=_isatty(out), env=os.environ)
    if refusal is not None:
        print(refusal, file=err)
        return 1
    try:
        url = _resolve_pair_url(args.advertise, args.port)
    except (OSError, ValueError) as exc:
        print(f"can't work out an address to encode: {exc}\n"
              f"pass one explicitly:  marim serve qr --advertise <host>", file=err)
        return 1
    # load_or_create, not load: the token file is the contract the daemon reads
    # at startup, so pairing works before `marim serve` has ever run.
    token = load_or_create_token(_default_state_dir())
    print(
        _pairing_block(
            url=url,
            token=token,
            name=args.name or default_name(),
            warning=loopback_warning(url),
            terminal_lines=shutil.get_terminal_size(fallback=(80, 0)).lines,
        ),
        file=out,
        flush=True,
    )
    return 0
```

Add the shared tty helper next to `_display_path`:

```python
def _isatty(stream) -> bool:
    """Whether ``stream`` is a terminal. A StringIO under test and a pipe under
    systemd both answer honestly, which is exactly the signal we want."""
    return bool(getattr(stream, "isatty", lambda: False)())
```

`load_or_create_token` must now be importable from the qr path, which runs
without the serve extra — move it out of the guarded `try:` block inside `main`
to a module-level import at the top:

```python
from ...server.auth import load_or_create_token
```

and delete that name from the `try:` import block (leaving `uvicorn`,
`create_app`, `SessionSupervisor`, `WorkspaceRegistry` there).

Finally, replace the inline `isatty = bool(getattr(out, "isatty", lambda: False)())`
in `main` (line 139) with `isatty = _isatty(out)` and drop the now-duplicated
comment above it.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_cli_serve_qr.py tests/test_cli_serve.py -q`
Expected: PASS — the 11 new tests, and every existing serve test still green
(the `out`/`err` default change touches all of them).

- [ ] **Step 6: Run the gates**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: clean, 0 errors, all tests pass.

- [ ] **Step 7: Smoke it in a real terminal**

```bash
XDG_DATA_HOME=/tmp/qr-smoke uv run script -qec "uv run marim serve qr" /dev/null | head -40
```
Expected: a black-on-white QR, `http://192.168.0.3:8642` under it. Scan it with any phone QR reader and confirm the decoded text starts `marim://pair?v=1&url=http%3A%2F%2F192.168.0.3%3A8642&token=`.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/cli/serve.py tests/test_cli_serve_qr.py
git commit -m "feat(serve): marim serve qr — pairing code for marim-mobile"
```

---

### Task 4: `marim serve --qr`, plus docs

**Files:**
- Modify: `src/marim_harness/interfaces/cli/serve.py` (the `--qr` flag and `--advertise` on the serve parser)
- Modify: `docs/reference/serve-api.md` (a "Pairing" section under "Startup output"), `docs/guides/headless.md:316-326`, `CHANGELOG.md`
- Test: `tests/test_cli_serve_qr.py` (append)

**Interfaces:**
- Consumes: `_qr_refusal`, `_pairing_block`, `_resolve_pair_url`, `_isatty` from Task 3; `bind_loopback_warning` from Task 1.
- Produces: nothing further.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_serve_qr.py`:

```python
@pytest.fixture
def stub_uvicorn(monkeypatch):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)


def test_serve_qr_flag_prints_the_code_after_the_startup_block(state, stub_uvicorn):
    out = _Tty()
    assert serve.main(["--qr", "--no-banner"], out=out, err=io.StringIO()) == 0
    text = out.getvalue()
    assert text.index("listening on") < text.index("treat this like a password")


def test_serve_qr_flag_warns_about_the_loopback_bind(state, stub_uvicorn):
    """The default bind means a phone can't connect even to a correct address."""
    out = _Tty()
    assert serve.main(["--qr"], out=out, err=io.StringIO()) == 0
    assert "--host 0.0.0.0" in out.getvalue()

    out = _Tty()
    assert serve.main(["--qr", "--host", "0.0.0.0"], out=out, err=io.StringIO()) == 0
    assert "--host 0.0.0.0" not in out.getvalue()


def test_serve_qr_flag_skips_the_code_but_still_serves_when_refused(state, stub_uvicorn):
    """A refused QR must never stop the daemon from starting."""
    out, err = io.StringIO(), io.StringIO()
    assert serve.main(["--qr"], out=out, err=err) == 0
    assert state not in out.getvalue()
    assert "terminal" in err.getvalue()
    assert "listening on" in out.getvalue()


def test_serve_qr_flag_honors_advertise(state, stub_uvicorn):
    out = _Tty()
    assert serve.main(
        ["--qr", "--advertise", "10.1.2.3:9000", "--no-banner"], out=out, err=io.StringIO()
    ) == 0
    assert "http://10.1.2.3:9000" in out.getvalue()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov -n 0 tests/test_cli_serve_qr.py -q -k "flag"`
Expected: FAIL — `SystemExit: 2` (argparse: unrecognized arguments: --qr)

- [ ] **Step 3: Add the flags and the print**

In `main`'s parser (after the `--no-banner` argument, `serve.py:103-105`):

```python
    parser.add_argument("--qr", action="store_true",
                        help="also print a QR code that pairs a client with this daemon "
                             "(encodes the bearer token; terminal only)")
    parser.add_argument("--advertise", default=None, metavar="HOST[:PORT]",
                        help="address for --qr to encode instead of the auto-detected "
                             "one (see: marim serve qr --help)")
```

Then, between the startup `print(...)` and `uvicorn.run(...)` (`serve.py:147-148`):

```python
    if args.qr:
        _print_startup_qr(args, token=token, out=out, err=err)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
```

And add the helper next to `_qr_main`:

```python
def _print_startup_qr(args, *, token: str, out, err) -> None:
    """The --qr block. Never fatal: a daemon that started fine must not be taken
    down by an unprintable pairing code, so every failure here is a note on
    stderr and the serve loop carries on."""
    refusal = _qr_refusal(isatty=_isatty(out), env=os.environ)
    if refusal is not None:
        print(refusal, file=err)
        return
    try:
        url = _resolve_pair_url(args.advertise, args.port)
    except (OSError, ValueError) as exc:
        print(f"--qr skipped: can't work out an address to encode ({exc}); "
              f"try marim serve qr --advertise <host>", file=err)
        return
    print(
        _pairing_block(
            url=url,
            token=token,
            name=default_name(),
            # The bind is known here, so warn about the daemon's own reachability
            # in preference to the encoded address: a correct LAN address still
            # gets connection-refused when the daemon is listening on loopback.
            warning=bind_loopback_warning(args.host) or loopback_warning(url),
            terminal_lines=shutil.get_terminal_size(fallback=(80, 0)).lines,
        ),
        file=out,
        flush=True,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov -n 0 tests/test_cli_serve_qr.py tests/test_cli_serve.py tests/test_serve_banner.py -q`
Expected: PASS

- [ ] **Step 5: Write the docs**

In `docs/reference/serve-api.md`, add `--qr` and `--advertise` to the flags table, and add this section immediately after the "Startup output" section:

````markdown
### Pairing a client (QR)

```
marim serve qr [--port N] [--advertise HOST[:PORT]] [--name NAME]
marim serve --qr            # print one at startup, then serve
```

Prints a QR encoding a pairing URI, so a client (e.g. `marim-mobile`) can
provision a server profile in one scan instead of retyping a 43-character
bearer token:

```
marim://pair?v=1&url=http%3A%2F%2F192.168.0.3%3A8642&token=<token>&name=workstation
```

That format is the cross-repo contract. `v=1` lets a client reject a future
shape rather than mis-parse it; `url` keeps its scheme so a reverse-proxy or
tailnet address round-trips; `name` defaults to the machine's hostname.

The subcommand doesn't talk to a running daemon — the token file *is* the
contract, so it works before the first `marim serve` and creates the token if
it doesn't exist yet. It can't discover a running daemon's port, so pass
`--port` if it isn't the default.

The encoded address comes from the source address of the default route (the
address a client on the same network should use — never a `docker0` or bridge
address), and always prints in plain text beneath the code so a wrong guess is
visible. `--advertise` overrides it and is the only way to encode a tailnet
name or a proxy domain. With no default route and no `--advertise`, the command
exits 1 and says so.

**The code is a credential.** It prints only on an explicit `marim serve qr` or
`--qr`, never as part of normal startup output, and it is **refused when stdout
isn't a terminal** (exit 1, no token bytes emitted) — a QR in a log file is
both useless and a leak. `NO_COLOR` is also refused rather than honored: a code
drawn in the terminal's own colors renders inverted on a dark theme and may not
scan. Under `--qr`, a refusal is a note on stderr and the daemon still starts.

Rendering needs `segno` (in the `serve` extra). Without it the command prints
the URI as text plus an install hint and exits 0 — a typed URI still pairs.
````

In `docs/guides/headless.md`, extend the `marim serve` paragraph's flag list to
`[--qr]` and add one sentence: "`marim serve qr` prints a QR that pairs a
client (e.g. the Android app) in one scan — see the
[serve API reference](../reference/serve-api.md)."

Add to `CHANGELOG.md` under `## [Unreleased]`:

```markdown
- `marim serve qr` prints a QR code that pairs a client with the daemon in one
  scan, encoding `marim://pair?v=1&url=…&token=…&name=…` — the URL, the bearer
  token, and a profile name (the machine's hostname by default), which is
  everything `marim-mobile` needs for a server profile. The address it encodes
  comes from the source address of the default route, so it's the one a phone
  on the same network should use rather than a `docker0` or bridge address, and
  it always prints in plain text under the code; `--advertise` overrides it for
  a tailnet name or a reverse proxy. `marim serve --qr` prints one at startup.
  Because the code carries a credential it is never part of normal startup
  output and is refused outright when stdout isn't a terminal. Needs `segno`
  (added to the `serve` extra); without it the pairing URI prints as text.
```

- [ ] **Step 6: Run the full gates**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: clean, 0 errors, all tests pass.

- [ ] **Step 7: Smoke `--qr` in a real terminal**

```bash
XDG_DATA_HOME=/tmp/qr-smoke2 timeout 6 script -qec "uv run marim serve --qr --port 8765" /dev/null | head -45
```
Expected: the wordmark startup block, a loopback-bind warning (the default bind is `127.0.0.1`), then the code. Re-run with `--host 0.0.0.0` and confirm the warning is gone.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/cli/serve.py tests/test_cli_serve_qr.py \
        docs/reference/serve-api.md docs/guides/headless.md CHANGELOG.md
git commit -m "feat(serve): --qr at startup, plus pairing docs"
```

---

## After the plan

Once merged, note the URI format in `marim-mobile`'s spec
(`docs/superpowers/specs/2026-07-10-marim-mobile-android-design.md`, which
lists "QR token pairing" as a v2 candidate) so the scanner work there starts
from the contract rather than re-deriving it. That is a separate change in a
separate repo and is not part of this plan.
