# `marim serve` QR pairing — design

**Date:** 2026-07-27
**Status:** approved (discussed in conversation 2026-07-27)

## Problem

Connecting the Android client to a daemon is a typing exercise. `marim-mobile`'s
add-server sheet asks for **name, base URL, and token** — and the token is a
43-character `secrets.token_urlsafe(32)` string that currently has to reach the
phone by hand (`ServerProfileEntity` in `marim-mobile/.../data/db/Entities.kt`
holds `id`/`name`/`baseUrl`; the token lives separately in the Keystore-backed
`TokenStore`). The mobile design doc has named the fix since day one — *"name,
base URL, token (paste; QR pairing is v2)"* — and lists "QR token pairing" as a
v2 candidate in three places
(`marim-mobile/docs/superpowers/specs/2026-07-10-marim-mobile-android-design.md`).

Nothing on the server side produces such a code. That is the gap this doc
closes: **the harness generates and prints the pairing QR; the scanner screen
stays `marim-mobile`'s work.** What this spec fixes for both repos is the wire
format the phone will parse.

A second, quieter problem rides along: the daemon binds `127.0.0.1` by default
and nothing in the CLI ever tells you what address a phone should actually use.
Pairing is where that question gets asked, so this is where it gets answered.

## Decision

Print a QR encoding a `marim://pair?v=1&…` URI carrying **url + token + name**,
so one scan fully provisions a server profile. Three constraints shape
everything below:

1. **The QR is a credential.** It gets the opposite treatment from the startup
   wordmark shipped in `f8af66b`: never unconditional output, explicit
   invocation only, and *refused outright when stdout is not a terminal* — a QR
   in a log file is both useless and a token leak.
2. **A loopback QR is a lie.** The encoded address must be one the scanner can
   reach, which means resolving it, showing it in plain text, and warning when
   the daemon is bound somewhere unreachable.
3. **An inverted or low-contrast QR is a broken QR.** Rendering forces real
   black-on-white rather than inheriting the terminal theme.

Rejected alternatives:

- **URL-only QR** (no secret, safe at startup): the token still has to travel by
  hand, so it removes the annoyance the feature exists to remove.
- **One-time pairing code** exchanged for the bearer via a new `POST /v1/pair`:
  the right long-term shape — screenshot-safe and revocable — but it is a new
  auth surface in `server/auth.py` + `server/http.py` plus a client-side
  exchange step, which is a different project from printing a code. Revisit if
  QR sharing turns out to leak in practice.
- **`https://marim.dev/pair#…` link** so stock camera apps show something
  human-readable: depends on a hosted page and inserts a public round-trip into
  a purely local pairing.
- **Short keys / scheme-less URL** (`u=192.168.0.3:8642`) to shrink the code by
  four terminal rows: breaks `--advertise https://marim.example.com`, and costs
  a self-describing contract, for four rows.

## Design

### Surface

```
marim serve qr [--port N] [--advertise HOST[:PORT]] [--name NAME]
marim serve --qr        # print once after the startup block, then serve
```

`router.py` already forwards `argv[1:]` to `serve.main`, so the subcommand is a
dispatch at the top of `serve.main`: `argv[0] == "qr"` → `_qr_main(argv[1:])`.
It follows the `trust_cmd.py` action shape rather than argparse subparsers,
which would restructure the existing flag parsing for one leaf command.

The subcommand deliberately does **not** talk to a running daemon. The bearer
token is long-lived and on disk, so pairing needs no cooperation from the
process: `_qr_main` reads it with the existing `load_or_create_token(state_dir)`.
That also means pairing works *before* the first `marim serve` — the token file
is the contract, not the process.

`--port` exists because the command genuinely cannot know which port a running
daemon chose; there is no pidfile and no persisted record of it. It defaults to
`8642`, matching `serve`'s own default. Discovering the live port is out of
scope.

### Payload — `server/pairing.py`

One pure function builds the URI:

```python
pairing_uri(url: str, token: str, name: str) -> str
# marim://pair?v=1&url=http%3A%2F%2F192.168.0.3%3A8642&token=<43>&name=workstation
```

- `v=1` lets the mobile parser reject a future format cleanly instead of
  mis-parsing it.
- `url` carries the **full** URL including scheme, so `--advertise
  https://marim.example.com` (reverse proxy / tailnet) round-trips.
- `name` defaults to `socket.gethostname()` (`workstation` on the dev box), so
  the phone shows a name instead of an IP.
- Values are `urllib.parse.urlencode`-escaped; the mobile side parses with any
  standard URI parser.

Measured with `segno`: the real payload is 119 characters → a **version 7-M,
45×45** code. With the spec-required 4-module quiet zone that is 53×53 modules
= **53 columns × 27 text rows** in half-block rendering.

The same module also owns the two pure decisions around the payload:

- `parse_advertise(value) -> (scheme, host, port)` — accepts `host`,
  `host:port`, or a full URL, and normalizes to a URL.
- `loopback_warning(url) -> str | None` — the message when the encoded host is
  loopback, `None` otherwise.

### Address selection

The dependency-free answer is also the correct one: open a UDP socket toward a
public address and read back `getsockname()[0]`. No packets are sent — the
kernel simply reports the source address it *would* route from. On the dev box
that yields `192.168.0.3` and never the `docker0` (`172.17.0.1`) or bridge
(`172.18.0.1`) addresses that a naive "first non-loopback interface" scan
picks. It also needs no `netifaces`/`psutil` dependency, which is the only other
way to enumerate interface→address pairs from Python.

`advertised_address() -> str` is the effectful half of `pairing.py`; the ranking
heuristic it replaces is not built at all.

- `--advertise` overrides it entirely and is the only path to a tailnet name or
  proxy domain.
- No default route → the probe raises `OSError`; the command exits 1 telling the
  user to pass `--advertise`.
- The resolved address always prints as plain text under the code, so a wrong
  guess is visible rather than silent.
- Consequence of the interface-name trade: output says `http://192.168.0.3:8642`
  with no `(enp5s0)` annotation, because no stdlib call maps an address back to
  an interface name.

### Rendering — `interfaces/qr.py`

`segno` (BSD, pure Python, no runtime deps on ≥3.10, ~1.6.x) is used **only as
an encoder**; `qr.matrix` is rendered here. Two functions:

- `encode(uri) -> list[tuple[int, ...]]` — thin `segno.make(uri, error="m")`
  wrapper returning `list(qr.matrix_iter(border=4))`. Note the API: `qr.matrix`
  excludes the quiet zone (45 rows for our payload), `matrix_iter(border=4)`
  includes it (53) — verified against segno 1.6.6. The zone is not optional
  padding; scanners need it, and the terminal's own background does not count
  once we paint the code white.
- `render_matrix(matrix) -> str` — pure. Each text row packs two module rows
  into one of `' '`, `'▀'`, `'▄'`, `'█'`, wrapped in a single black-on-white SGR
  pair.

Colors are **truecolor** (`38;2;0;0;0` / `48;2;255;255;255`), not the 4-bit
`30`/`47` pair: 4-bit "black" and "white" are palette entries the user's theme
redefines, and scannability depends on real contrast, not on a color's name.
For the same reason `NO_COLOR` is not honored here — an uncolored QR on a dark
terminal renders inverted, which older ZXing-based scanners reject. When
`NO_COLOR` is set the command explains why and exits 1 rather than emitting a
code that may not scan.

`segno` joins the `serve` extra. When it is absent the command prints the URI
as text plus an install hint and exits **0** — a typed URI still pairs.

`shutil.get_terminal_size().lines` below the needed 27 rows produces a leading
note ("needs 27 rows, terminal is N — scroll up"), not a refusal.

### Output shape

```
$ marim serve qr

  <27 rows of black-on-white QR>

  http://192.168.0.3:8642
  workstation · token included — treat this like a password

  wrong address?  marim serve qr --advertise <host>
```

Under `marim serve --qr` the same block prints after the existing startup block
(from `f8af66b`), before `uvicorn.run`.

When the daemon is bound to loopback, a warning precedes the QR: for `--qr` the
bind address is known directly; for the subcommand it is advisory, since it
cannot see a running daemon's bind.

### Security posture

- Explicit invocation only — never in default startup output.
- **Refuses when `out.isatty()` is false**, exit 1, emitting no token bytes.
  This covers the segno-missing text fallback too, so *no code path* writes the
  token to a pipe or file.
- Nothing is written to disk; the QR exists only in terminal output.
- The password warning is part of the output, not a doc footnote.

Exit codes for `marim serve qr`:

| Code | When                                                                  |
| ---- | --------------------------------------------------------------------- |
| 0    | QR printed — or segno absent, URI text + install hint printed instead  |
| 1    | stdout is not a terminal; `NO_COLOR` set; no default route and no `--advertise` |
| 2    | argparse usage error (unknown flag, bad `--port`)                     |

### Adjacent fix

`serve.main` binds `out=sys.stdout` as a def-time default. `trust_cmd.main`
documents at length why that is wrong (the binding is captured at first import,
before pytest swaps in `capsys`) and uses `out=None` resolved inside the call.
`serve.main` gets the same treatment as part of this work, since the new
`capsys`-based qr tests would otherwise hit exactly the documented trap.

## Testing

Pure, unit-tested directly:

- `pairing_uri` — escaping, `v=1` presence, hostname default, that an https
  advertise URL survives round-trip.
- `parse_advertise` — bare host, `host:port`, full URL, garbage.
- `loopback_warning` — `127.0.0.1`/`::1`/`localhost` warn, LAN and DNS names
  don't.
- `render_matrix` — a small hand-written matrix renders to the expected glyph
  string; every line carries the black-on-white SGR pair; row count is
  `ceil(modules / 2)`.
- The terminal-height note decision.

Integration, driving `serve.main(["qr", …])` against a tmp `XDG_DATA_HOME` with
the address probe monkeypatched:

- Emits the URI's QR plus the address line; exit 0.
- `--advertise` wins over the probe; `--name` overrides the hostname.
- Probe raising `OSError` with no `--advertise` → exit 1, actionable message.
- **The security test:** a non-tty `out` produces exit 1 and output containing
  **no** substring of the token — asserted against the real token read from the
  tmp state dir.
- segno absent (monkeypatched import failure) → exit 0, URI text, install hint.
- `NO_COLOR` set on a tty → exit 1, explanation, no QR.
- `marim serve --qr` prints the block after the startup block and still reaches
  `uvicorn.run` (stubbed, as the existing serve tests do).

Not covered in CI: that a phone actually decodes it. Manual smoke is scanning
the printed code with any QR reader and eyeballing the decoded URI — the
`marim-mobile` scanner does not exist yet.

## Docs

- `docs/reference/serve-api.md` — a "Pairing" section under the startup output:
  the command, the URI format (as the cross-repo contract), the address rules,
  and the tty refusal.
- `docs/guides/headless.md` — one line in the `marim serve` paragraph.
- `pyproject.toml` — `segno` in the `serve` extra, with the same
  comment style as its neighbors.
- `CHANGELOG.md` — Unreleased entry.
- Cross-repo: once implemented, note the format in `marim-mobile`'s spec so its
  v2 scanner work has the contract in front of it.

## Out of scope

The `marim-mobile` scanner screen, camera permission, and profile creation
(that repo, its own spec — this settles only the format it parses); one-time or
rotating pairing codes; token rotation; mDNS/Bonjour discovery; TLS.
