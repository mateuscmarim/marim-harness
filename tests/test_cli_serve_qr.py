"""`marim serve qr`: address resolution, the refusal rules, and the one
invariant that matters most — the bearer token never reaches a non-tty stream."""

import io

import pytest

from marim_harness.interfaces.cli import serve


@pytest.fixture
def stub_uvicorn(monkeypatch):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)


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
    assert state in text  # positive control: the real token reaches the fallback URI


def _has_sextant(text: str) -> bool:
    return any(0x1FB00 <= ord(char) <= 0x1FB3B for char in text)


def test_qr_defaults_to_the_short_form_and_says_how_to_escape_it(state):
    """A font without Unicode 13 draws the sextants as empty boxes, so the way
    back has to be printed with the code rather than left in --help."""
    out = _Tty()
    assert serve.main(["qr"], out=out, err=io.StringIO()) == 0
    text = out.getvalue()
    assert _has_sextant(text)
    assert "--wide" in text


def test_qr_wide_falls_back_to_half_blocks_and_is_taller(state):
    wide, short = _Tty(), _Tty()
    assert serve.main(["qr", "--wide"], out=wide, err=io.StringIO()) == 0
    assert serve.main(["qr"], out=short, err=io.StringIO()) == 0
    assert not _has_sextant(wide.getvalue())
    assert wide.getvalue().count("\n") > short.getvalue().count("\n")
    # Nothing to escape from, so no dangling hint about a flag already in use.
    assert "--wide" not in wide.getvalue()


def test_serve_qr_flag_honors_wide(state, stub_uvicorn):
    out = _Tty()
    assert serve.main(["--qr", "--wide", "--no-banner"], out=out, err=io.StringIO()) == 0
    assert not _has_sextant(out.getvalue())
    assert "treat this like a password" in out.getvalue()


@pytest.fixture
def sixel_terminal(monkeypatch):
    """A terminal that answers the DA1 probe with sixel among its attributes."""
    monkeypatch.setattr(serve.terminal, "device_attributes",
                        lambda stream, **kw: "\033[?62;4;22c")
    monkeypatch.setattr(serve.terminal, "cell_pixels", lambda stream: (8, 17))


@pytest.fixture
def unprobeable_terminal(monkeypatch):
    """A probe that fails the test if anything asks it a question."""
    def never(*args, **kwargs):
        raise AssertionError("the terminal was probed when the flag had already answered")

    monkeypatch.setattr(serve.terminal, "device_attributes", never)
    monkeypatch.setattr(serve.terminal, "cell_pixels", never)


def test_qr_draws_a_sixel_image_when_the_terminal_says_it_can(state, sixel_terminal):
    """Sixel is drawn in pixels, so the code comes out square and no font is
    involved — which is also why the --wide hint has nothing to escape."""
    out = _Tty()
    assert serve.main(["qr"], out=out, err=io.StringIO()) == 0
    text = out.getvalue()
    assert text.count("\033Pq") == 1 and "\033\\" in text
    assert not _has_sextant(text)
    assert "--wide" not in text
    assert "http://192.168.0.3:8642" in text


def test_qr_no_sixel_takes_the_characters_without_asking(state, unprobeable_terminal):
    out = _Tty()
    assert serve.main(["qr", "--no-sixel"], out=out, err=io.StringIO()) == 0
    assert _has_sextant(out.getvalue())
    assert "\033Pq" not in out.getvalue()


def test_qr_wide_wins_over_a_sixel_terminal(state, unprobeable_terminal):
    """--wide names a rendering, so it settles the question before the probe."""
    out = _Tty()
    assert serve.main(["qr", "--wide"], out=out, err=io.StringIO()) == 0
    assert "\033Pq" not in out.getvalue()
    assert not _has_sextant(out.getvalue())


def test_qr_sixel_can_be_forced_past_a_silent_terminal(state, monkeypatch):
    """Terminals that render sixel but don't advertise it are common enough to
    need a flag that doesn't take no for an answer."""
    monkeypatch.setattr(serve.terminal, "device_attributes", lambda stream, **kw: "")
    monkeypatch.setattr(serve.terminal, "cell_pixels", lambda stream: None)
    out = _Tty()
    assert serve.main(["qr", "--sixel"], out=out, err=io.StringIO()) == 0
    assert "\033Pq" in out.getvalue()


def test_serve_qr_flag_honors_sixel(state, stub_uvicorn, sixel_terminal):
    out = _Tty()
    assert serve.main(["--qr", "--no-banner"], out=out, err=io.StringIO()) == 0
    assert "\033Pq" in out.getvalue()
    assert "treat this like a password" in out.getvalue()


def test_qr_creates_the_token_before_the_daemon_has_ever_run(tmp_path, monkeypatch):
    """The token file is the contract, not the process — pairing works first."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(serve, "advertised_address", lambda port: f"http://10.0.0.2:{port}")
    out = _Tty()
    assert serve.main(["qr"], out=out, err=io.StringIO()) == 0
    assert (tmp_path / "fresh" / "marim-harness" / "server" / "token").exists()


def test_qr_rejects_unknown_flags(state):
    with pytest.raises(SystemExit) as exc:
        serve.main(["qr", "--bogus"], out=_Tty(), err=io.StringIO())
    assert exc.value.code == 2


def test_qr_reports_a_broken_pairing_block_without_leaking_the_token(state, monkeypatch):
    """`marim serve qr`'s own never-guarded-before path: a segno DataOverflowError
    (a ValueError subclass) or any other exception raised while building the
    block must exit 1 with a stderr note naming the exception's type, and the
    real token must reach neither stream."""
    def boom(**kwargs):
        raise ValueError(f"leaked token in the message: {kwargs['token']}")

    monkeypatch.setattr(serve, "_pairing_block", boom)
    out, err = _Tty(), io.StringIO()
    assert serve.main(["qr"], out=out, err=err) == 1
    assert state not in out.getvalue()
    assert state not in err.getvalue()
    assert "ValueError" in err.getvalue()


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


def test_serve_qr_flag_survives_a_broken_pairing_block(state, stub_uvicorn, monkeypatch):
    """The never-fatal contract covers building the code, not just resolving the
    address: a segno DataOverflowError (a ValueError subclass) raised while
    building the block must not stop uvicorn from starting (proven by `main`
    still returning 0 instead of the exception escaping), and the stderr note
    must not echo the exception's message — it could contain the token-bearing
    pairing URI that choked the encoder."""
    def boom(**kwargs):
        raise ValueError(f"leaked token in the message: {kwargs['token']}")

    monkeypatch.setattr(serve, "_pairing_block", boom)
    out, err = _Tty(), io.StringIO()
    assert serve.main(["--qr", "--no-banner"], out=out, err=err) == 0
    assert "listening on" in out.getvalue()
    assert state not in out.getvalue()
    assert state not in err.getvalue()
    assert "ValueError" in err.getvalue()
