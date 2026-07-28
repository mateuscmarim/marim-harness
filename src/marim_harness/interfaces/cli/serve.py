"""``marim serve`` — run marim as a long-lived HTTP daemon (REST + WebSocket).

Binds 127.0.0.1 by default; expose it via a reverse proxy or tailscale and
authenticate with the bearer token printed at startup (persisted 0600 under
the server state dir). Requires the ``serve`` extra (starlette + uvicorn)."""

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ...server.auth import load_or_create_token
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

# The daemon's default bind port. Shared by `_qr_parser` and the `serve` parser
# below so the two commands can't drift apart — a stale duplicate would have
# `marim serve qr` encode a port the daemon isn't actually listening on.
DEFAULT_PORT = 8642


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "marim-harness" / "server"


def _display_path(path: Path) -> str:
    """``path`` with ``$HOME`` collapsed to ``~`` — state-dir paths are long and
    the interesting part is the tail."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class ServeStartup:
    """What a launching operator needs to see: where it bound, where the bearer
    token landed, which workspaces root it adopted, and the eviction TTL. The
    last two come from flags that were previously invisible at startup, so you
    couldn't tell from the terminal which root a running daemon had picked up.

    Deliberately never prints the token *value* — only its path. Startup output
    lands in scrollback, screenshots, and logs; the secret shouldn't.
    """

    url: str
    token_path: Path
    workspaces: Path
    idle_ttl: float
    version: str

    def render(self, *, banner: bool, color: bool) -> str:
        """The startup block: wordmark + aligned facts on a tty, a flat
        line-per-fact preamble everywhere else (keeping the historical
        ``marim serve listening on …`` first line that scripts may grep)."""
        if not banner:
            head, *rest = self._fields(short=False)
            return "\n".join(
                [
                    f"marim serve {self._label} listening on {head[1]}",
                    *(f"{label}: {value}" for label, value in rest),
                ]
            )
        return "\n".join(
            [
                wordmark_block(f"serve {self._label}", color=color),
                "",
                field_block(self._fields(short=True), color=color),
                "",
            ]
        )

    @property
    def _label(self) -> str:
        """``v0.2.0`` for a real version, but no ``v`` in front of the
        ``unknown`` placeholder a source checkout reports."""
        return f"v{self.version}" if self.version[:1].isdigit() else self.version

    def _fields(self, *, short: bool) -> tuple[tuple[str, str], ...]:
        # Tilde-collapsed under the wordmark (a human is reading it), absolute in
        # the log preamble (a `~` in journald doesn't say whose home it was).
        show = _display_path if short else str
        return (
            ("listening", self.url),
            ("bearer token", show(self.token_path)),
            ("workspaces", show(self.workspaces)),
            ("idle ttl", f"{self.idle_ttl:g}s"),
        )


def _isatty(stream) -> bool:
    """Whether ``stream`` is a terminal. A StringIO under test and a pipe under
    systemd both answer honestly, which is exactly the signal we want."""
    return bool(getattr(stream, "isatty", lambda: False)())


def _qr_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marim serve qr",
        description="Print a QR code that pairs a marim client with this machine's "
                    "daemon. The code carries the bearer token, so it prints to a "
                    "terminal only.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port the daemon listens on (default: {DEFAULT_PORT}); this "
                             "command can't discover a running daemon's port")
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
    try:
        # load_or_create, not load: the token file is the contract the daemon reads
        # at startup, so pairing works before `marim serve` has ever run.
        token = load_or_create_token(_default_state_dir())
        block = _pairing_block(
            url=url,
            token=token,
            name=args.name or default_name(),
            warning=loopback_warning(url),
            terminal_lines=shutil.get_terminal_size(fallback=(80, 0)).lines,
        )
    except Exception as exc:  # never {exc}: see the note in _print_startup_qr
        print(f"couldn't build the pairing code ({type(exc).__name__})", file=err)
        return 1
    print(block, file=out, flush=True)
    return 0


def _print_startup_qr(args, *, token: str, out, err) -> None:
    """The ``--qr`` block. Never fatal: a daemon that started fine must not be
    taken down by an unprintable pairing code, so every failure here is a note
    on stderr and the serve loop carries on."""
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
    try:
        block = _pairing_block(
            url=url,
            token=token,
            name=default_name(),
            # The bind is known here, so warn about the daemon's own reachability
            # in preference to the encoded address: a correct LAN address still
            # gets connection-refused when the daemon is listening on loopback.
            warning=bind_loopback_warning(args.host) or loopback_warning(url),
            terminal_lines=shutil.get_terminal_size(fallback=(80, 0)).lines,
        )
    except Exception as exc:
        # Unlike the _resolve_pair_url handler above (whose exceptions come from
        # address parsing and never see the token), `token` and the pairing URI
        # built from it are in scope for everything this try covers — pairing_uri,
        # encode (segno.make can raise DataOverflowError, a ValueError subclass),
        # height_note, render_matrix. An exception that echoes its input (e.g. a
        # "value too long: <the uri>" message) would put the bearer token on
        # stderr, which the stdout-isatty refusal above does not cover — a user
        # can redirect stderr to a file while stdout stays a terminal. So: no
        # `{exc}` here, only the exception's type name.
        print(f"--qr skipped: couldn't build the pairing code ({type(exc).__name__}); "
              f"try marim serve qr --advertise <host>", file=err)
        return
    print(block, file=out, flush=True)


def main(argv: list[str], *, out=None, err=None) -> int:
    # Resolved here, not bound as a def-time default: a `def main(..., out=sys.stdout)`
    # default is evaluated once at first import, which can capture a stale stream
    # before pytest swaps in its own (see the same note in trust_cmd.py).
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if argv and argv[0] == "qr":
        return _qr_main(argv[1:], out=out, err=err)
    parser = argparse.ArgumentParser(
        prog="marim serve",
        description="Run the marim HTTP server daemon "
                    "(sessions over REST + WebSocket). "
                    "See `marim serve qr --help` to pair a client.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"bind port (default: {DEFAULT_PORT})")
    parser.add_argument("--workspaces-root", type=Path, default=None,
                        help="directory for managed workspaces "
                             "(default: <state-dir>/workspaces)")
    parser.add_argument("--idle-ttl", type=float, default=900.0,
                        help="seconds before an idle session's harness is evicted "
                             "(default: 900)")
    parser.add_argument("--no-banner", action="store_true",
                        help="skip the startup wordmark (also: MARIM_NO_BANNER=1); "
                             "it is already skipped when stdout isn't a terminal")
    parser.add_argument("--qr", action="store_true",
                        help="also print a QR code that pairs a client with this daemon "
                             "(encodes the bearer token; terminal only)")
    parser.add_argument("--advertise", default=None, metavar="HOST[:PORT]",
                        help="address for --qr to encode instead of the auto-detected "
                             "one (see: marim serve qr --help)")
    args = parser.parse_args(argv)

    try:
        import uvicorn

        from ...server.http import create_app
        from ...server.supervisor import SessionSupervisor
        from ...server.workspaces import WorkspaceRegistry
    except ImportError:
        print(
            "marim serve requires the server extra. Install with:\n"
            "  uv add 'marim-harness[serve]'  (or: pip install 'marim-harness[serve]')",
            file=err,
        )
        return 1

    state_dir = _default_state_dir()
    token = load_or_create_token(state_dir)
    workspaces = args.workspaces_root or state_dir / "workspaces"
    registry = WorkspaceRegistry(state_dir / "workspaces.json", workspaces)
    supervisor = SessionSupervisor(idle_ttl=args.idle_ttl)
    app = create_app(registry=registry, supervisor=supervisor, token=token)

    startup = ServeStartup(
        url=f"http://{args.host}:{args.port}",
        token_path=state_dir / "token",
        workspaces=workspaces,
        idle_ttl=args.idle_ttl,
        version=package_version(),
    )
    isatty = _isatty(out)
    print(
        startup.render(
            banner=banner_enabled(isatty=isatty, disabled=args.no_banner, env=os.environ),
            color=color_enabled(isatty=isatty, env=os.environ),
        ),
        file=out,
        flush=True,
    )
    if args.qr:
        _print_startup_qr(args, token=token, out=out, err=err)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0
