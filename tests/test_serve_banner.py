"""The startup wordmark: the pure gating predicates (`interfaces/branding.py`)
and the `marim serve` startup block, exercised without a terminal."""

from pathlib import Path

from marim_harness.interfaces.branding import (
    BANNER,
    TAGLINE,
    WORDMARK,
    banner_enabled,
    color_enabled,
    field_block,
    package_version,
    wordmark_block,
)
from marim_harness.interfaces.cli.serve import ServeStartup

ESC = "\033"


def _startup(**over) -> ServeStartup:
    base = dict(
        url="http://127.0.0.1:8642",
        token_path=Path("/state/marim-harness/server/token"),
        workspaces=Path("/state/marim-harness/server/workspaces"),
        idle_ttl=900.0,
        version="0.2.0",
    )
    return ServeStartup(**{**base, **over})


# --- the art -----------------------------------------------------------------


def test_banner_is_the_wordmark_plus_the_tagline():
    assert f"{WORDMARK}\n{TAGLINE}" == BANNER
    assert len(WORDMARK.splitlines()) == 6


def test_tui_tagline_and_serve_subtitle_share_one_idiom():
    """The strapline spacing is the family resemblance between the two surfaces —
    if `wordmark_block` ever stops reproducing the TUI's tagline they've drifted."""
    assert wordmark_block("a terminal harness", color=False) == BANNER


def test_wordmark_block_leaves_version_tokens_unspaced():
    block = wordmark_block("serve v0.2.0", color=False)
    assert block.splitlines()[-1] == "   · · ·   s e r v e   v0.2.0"


def test_wordmark_block_wraps_in_one_sgr_pair_when_colored():
    block = wordmark_block("serve v0.2.0", color=True)
    assert block.startswith(ESC) and block.endswith(f"{ESC}[0m")
    assert WORDMARK in block


# --- the gating predicates ---------------------------------------------------


def test_banner_needs_a_tty():
    assert banner_enabled(isatty=True, env={}) is True
    assert banner_enabled(isatty=False, env={}) is False


def test_no_banner_flag_wins_over_a_tty():
    assert banner_enabled(isatty=True, disabled=True, env={}) is False


def test_marim_no_banner_env_suppresses_it():
    assert banner_enabled(isatty=True, env={"MARIM_NO_BANNER": "1"}) is False
    assert banner_enabled(isatty=True, env={"MARIM_NO_BANNER": "yes"}) is False
    # An explicit falsey value is not a suppression — it's the default restated.
    assert banner_enabled(isatty=True, env={"MARIM_NO_BANNER": "0"}) is True
    assert banner_enabled(isatty=True, env={"MARIM_NO_BANNER": ""}) is True


def test_color_follows_the_tty_then_no_color_then_dumb_terminals():
    assert color_enabled(isatty=True, env={}) is True
    assert color_enabled(isatty=False, env={}) is False
    assert color_enabled(isatty=True, env={"NO_COLOR": "1"}) is False
    assert color_enabled(isatty=True, env={"TERM": "dumb"}) is False
    # no-color.org: only a non-empty value counts as set.
    assert color_enabled(isatty=True, env={"NO_COLOR": ""}) is True


def test_field_block_aligns_labels_and_stays_clean_without_color():
    block = field_block((("listening", "url"), ("idle ttl", "900s")), color=False)
    first, second = block.splitlines()
    assert ESC not in block
    assert first.index("url") == second.index("900s")


def test_package_version_is_a_string():
    assert isinstance(package_version(), str)


# --- the serve startup block -------------------------------------------------


def test_plain_startup_keeps_the_historical_first_line_and_adds_the_new_facts():
    text = _startup().render(banner=False, color=False)
    lines = text.splitlines()
    assert lines[0] == "marim serve v0.2.0 listening on http://127.0.0.1:8642"
    assert lines[1] == "bearer token: /state/marim-harness/server/token"
    assert lines[2] == "workspaces: /state/marim-harness/server/workspaces"
    assert lines[3] == "idle ttl: 900s"
    assert ESC not in text
    assert "█" not in text  # no art in a log file


def test_banner_startup_shows_the_wordmark_and_every_fact():
    text = _startup().render(banner=True, color=False)
    assert WORDMARK in text
    assert "   · · ·   s e r v e   v0.2.0" in text
    for expected in ("http://127.0.0.1:8642", "token", "workspaces", "900s"):
        assert expected in text


def test_startup_never_prints_the_token_value():
    """Only the path. Startup output lands in scrollback and screenshots."""
    startup = _startup()
    for banner in (True, False):
        text = startup.render(banner=banner, color=False)
        assert "token" in text and str(startup.token_path) in text.replace("~", "")


def test_banner_startup_collapses_home_to_tilde():
    home = Path.home()
    text = _startup(token_path=home / ".local/share/x/token").render(banner=True, color=False)
    assert "~/.local/share/x/token" in text
    assert str(home) not in text


def test_plain_startup_keeps_paths_absolute():
    """A `~` in journald doesn't say whose home it was."""
    home = Path.home()
    text = _startup(token_path=home / ".local/share/x/token").render(banner=False, color=False)
    assert str(home / ".local/share/x/token") in text


def test_source_checkout_version_reads_without_a_v_prefix():
    text = _startup(version="unknown").render(banner=False, color=False)
    assert text.splitlines()[0].startswith("marim serve unknown listening on")


def test_idle_ttl_renders_without_a_trailing_zero():
    assert "idle ttl: 900s" in _startup().render(banner=False, color=False)
    assert "idle ttl: 12.5s" in _startup(idle_ttl=12.5).render(banner=False, color=False)
