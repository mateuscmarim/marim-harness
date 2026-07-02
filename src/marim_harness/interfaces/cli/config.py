"""`marim config ...` — show and set persistent configuration."""

import argparse
import json
import sys

from ...config import global_config_path, load_config
from ...config.persist import write_env_values

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
    "MARIM_DEFAULT_MODE",
    "MARIM_TOOL_SEARCH",
    "MARIM_TOOL_SEARCH_THRESHOLD",
)

# Keys whose value is constrained to a fixed set, validated before persisting so a
# typo can't write a value the loader will silently reject at startup.
_ENUM_KEYS = {
    "MARIM_DEFAULT_MODE": ("ask", "auto", "plan"),
    "MARIM_TOOL_SEARCH": ("off", "auto", "on"),
}

# Keys whose value must be a positive integer (> 0).
_POSITIVE_INT_KEYS_CLI = {"MARIM_TOOL_SEARCH_THRESHOLD"}


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
            "context_window": cfg.context_window,
            "context_budgets": cfg.context_budgets,
            "proactive_memory": cfg.proactive_memory,
            "default_mode": cfg.default_mode,
            "tool_search": cfg.tool_search,
            "tool_search_threshold": cfg.tool_search_threshold,
            "api_key_set": api_key_set,
            "global_config_path": str(path),
        }
        print(json.dumps(obj), file=out)
        return 0

    print(f"provider:            {cfg.provider}", file=out)
    print(f"model:               {cfg.model}", file=out)
    print(f"base_url:            {cfg.base_url}", file=out)
    print(f"max_context_tokens:  {cfg.max_context_tokens}", file=out)
    print(f"context_window:      {cfg.context_window}", file=out)
    print(f"context_budgets:     {cfg.context_budgets}", file=out)
    print(f"proactive_memory:    {'on' if cfg.proactive_memory else 'off'}", file=out)
    print(f"default_mode:        {cfg.default_mode}", file=out)
    print(f"tool_search:         {cfg.tool_search}", file=out)
    print(f"tool_search_thresh:  {cfg.tool_search_threshold}", file=out)
    print(f"api_key:             {'set' if api_key_set else 'not set'}", file=out)
    print(f"global_config_path:  {path}", file=out)
    print(f"  exists:            {'yes' if path.exists() else 'no'}", file=out)
    return 0


def _persist(key: str, value: str) -> None:
    """Write ``KEY=VALUE`` to the global config file, updating the line in place
    if the key already exists and preserving all other lines. Delegates to the
    shared ``write_env_values`` writer so the CLI and TUI paths produce identical
    on-disk output: quoted-when-needed, atomic (temp + ``replace``), 0600."""
    write_env_values({key: value}, global_config_path())


def _cmd_set(args, *, out, err) -> int:
    key = args.key
    if key not in _ALLOWED_KEYS:
        print(
            f"error: unknown key {key!r}; allowed keys: {', '.join(_ALLOWED_KEYS)}",
            file=err,
        )
        return 2
    value = args.value
    allowed = _ENUM_KEYS.get(key)
    if allowed is not None:
        value = value.strip().lower()
        if value not in allowed:
            print(
                f"error: {key} must be one of: {', '.join(allowed)}",
                file=err,
            )
            return 2
    if key in _POSITIVE_INT_KEYS_CLI:
        try:
            n = int(value)
        except ValueError:
            print(f"error: {key} must be a positive integer", file=err)
            return 2
        if n <= 0:
            print(f"error: {key} must be a positive integer", file=err)
            return 2
        value = str(n)
    _persist(key, value)
    shown = "***" if _is_secret(key) else value
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
