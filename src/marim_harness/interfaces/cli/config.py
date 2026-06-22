"""`marim config ...` — show and set persistent configuration."""

import argparse
import json
import sys

from ...config import global_config_path, load_config

# Keys that may be persisted to the global config file. Anything else is
# rejected so a typo can't silently write an ignored line.
_ALLOWED_KEYS = (
    "MARIM_PROVIDER",
    "MARIM_MODEL",
    "MARIM_BASE_URL",
    "MARIM_API_KEY",
    "OPENROUTER_API_KEY",
    "MARIM_MAX_CONTEXT_TOKENS",
    "MARIM_PROACTIVE_MEMORY",
)


def _is_secret(key: str) -> bool:
    return "API_KEY" in key


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marim config", add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    show = sub.add_parser("show", help="Show the resolved configuration.")
    show.add_argument("--json", action="store_true", help="Emit JSON.")

    setp = sub.add_parser("set", help="Persist KEY=VALUE to the global config.")
    setp.add_argument("key", help="One of: " + ", ".join(_ALLOWED_KEYS))
    setp.add_argument("value", help="The value to store.")

    return parser


def _cmd_show(args, *, out, err) -> int:
    cfg = load_config()
    path = global_config_path()
    api_key_set = bool(cfg.api_key)
    if args.json:
        obj = {
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "max_context_tokens": cfg.max_context_tokens,
            "proactive_memory": cfg.proactive_memory,
            "api_key_set": api_key_set,
            "global_config_path": str(path),
        }
        print(json.dumps(obj), file=out)
        return 0

    print(f"provider:            {cfg.provider}", file=out)
    print(f"model:               {cfg.model}", file=out)
    print(f"base_url:            {cfg.base_url}", file=out)
    print(f"max_context_tokens:  {cfg.max_context_tokens}", file=out)
    print(f"proactive_memory:    {'on' if cfg.proactive_memory else 'off'}", file=out)
    print(f"api_key:             {'set' if api_key_set else 'not set'}", file=out)
    print(f"global_config_path:  {path}", file=out)
    print(f"  exists:            {'yes' if path.exists() else 'no'}", file=out)
    return 0


def _format_value(value: str) -> str:
    """Render a value for a dotenv line, quoting only when a bare value would not
    survive reload. dotenv strips an unquoted value at the first ``#`` (inline
    comment) and trims surrounding whitespace, so a value containing whitespace,
    ``#``, or quote chars must be double-quoted (with ``\\`` and ``"`` escaped).
    Simple values (model ids, booleans, keys) stay unquoted as before."""
    if any(c in value for c in ' \t#"\'\n\r'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _persist(key: str, value: str) -> None:
    """Write ``KEY=VALUE`` to the global config file, updating the line in place
    if the key already exists and preserving all other lines. Writes atomically
    (temp file + ``replace``) so a crash mid-write can't truncate the config."""
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    line = f"{key}={_format_value(value)}"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    replaced = False
    new_lines = []
    for raw in existing:
        stripped = raw.lstrip()
        # Match `KEY=` or `export KEY=` for the same key.
        head = stripped[len("export ") :] if stripped.startswith("export ") else stripped
        if head.split("=", 1)[0].strip() == key:
            new_lines.append(line)
            replaced = True
        else:
            new_lines.append(raw)
    if not replaced:
        new_lines.append(line)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _cmd_set(args, *, out, err) -> int:
    key = args.key
    if key not in _ALLOWED_KEYS:
        print(
            f"error: unknown key {key!r}; allowed keys: {', '.join(_ALLOWED_KEYS)}",
            file=err,
        )
        return 2
    _persist(key, args.value)
    shown = "***" if _is_secret(key) else args.value
    print(f"set {key}={shown} in {global_config_path()}", file=out)
    return 0


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "show":
        return _cmd_show(args, out=out, err=err)
    if args.cmd == "set":
        return _cmd_set(args, out=out, err=err)
    parser.print_help(err)
    return 2
