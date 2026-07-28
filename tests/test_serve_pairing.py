"""The `marim serve qr` payload: building the marim://pair URI, normalizing
--advertise, and the loopback warnings. Mostly pure — `advertised_address` is
also exercised indirectly through its caller in test_cli_serve_qr.py, which
monkeypatches it; here it runs for real against whatever route this sandbox has."""

import urllib.parse

import pytest

from marim_harness.server.pairing import (
    advertised_address,
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


def test_advertise_rejects_malformed_host_port_and_ipv6():
    """A colon-containing value must be valid host:port or IPv6, not junk."""
    with pytest.raises(ValueError):
        parse_advertise("192.168.0.3:", default_port=8642)
    with pytest.raises(ValueError):
        parse_advertise("192.168.0.3:80x", default_port=8642)
    with pytest.raises(ValueError):
        parse_advertise("example.com:abc", default_port=8642)


def test_loopback_warning_fires_only_for_unreachable_hosts():
    assert loopback_warning("http://127.0.0.1:8642")
    assert loopback_warning("http://localhost:8642")
    assert loopback_warning("http://[::1]:8642")
    # Expanded and IPv4-mapped IPv6 loopback forms — ipaddress.is_loopback
    # catches these where a hand-rolled set of literals would miss them.
    assert loopback_warning("http://[0:0:0:0:0:0:0:1]:8642")
    assert loopback_warning("http://[::ffff:127.0.0.1]:8642")
    assert loopback_warning("http://192.168.0.3:8642") is None
    assert loopback_warning("https://marim.example.com") is None


def test_bind_loopback_warning_names_the_fix():
    note = bind_loopback_warning("127.0.0.1")
    assert note and "--host 0.0.0.0" in note
    assert bind_loopback_warning("0.0.0.0") is None
    assert bind_loopback_warning("192.168.0.3") is None


def test_default_name_is_a_non_empty_string():
    assert default_name()


def test_advertised_address_reports_a_routable_source_address():
    """Exercises the real UDP-probe path (every other test monkeypatches it
    away). Skipped rather than failed when the sandbox has no default route —
    some CI/container legs run fully offline — so this stays non-flaky."""
    try:
        url = advertised_address(8642)
    except OSError:
        pytest.skip("no default route in this sandbox")
    assert url.startswith("http://") and url.endswith(":8642")
