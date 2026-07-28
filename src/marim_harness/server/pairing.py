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
