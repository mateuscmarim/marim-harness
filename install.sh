#!/usr/bin/env bash
#
# Install marim as a global command (`marim`, plus `marim-harness`).
#
# Usage:
#   ./install.sh                 # install, prompt for the OpenRouter API key
#   ./install.sh --key sk-or-... # install non-interactively with a key
#   ./install.sh --no-key        # install, don't touch the API key
#
# Re-run any time to upgrade; the install is editable, so source edits in this
# checkout take effect on the next `marim` run without reinstalling.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KEY=""
ASK_KEY=1
for arg in "$@"; do
    case "$arg" in
        --key=*) KEY="${arg#--key=}"; ASK_KEY=0 ;;
        --key) shift; KEY="${1:-}"; ASK_KEY=0 ;;
        --no-key) ASK_KEY=0 ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) ;;
    esac
done

# 1. Require uv.
if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is not installed. Install it first:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "  (see https://docs.astral.sh/uv/getting-started/installation/)" >&2
    exit 1
fi

# 2. Install (editable) — provides both `marim` and `marim-harness` on PATH.
echo "Installing marim from $SCRIPT_DIR ..."
uv tool install --force --editable "$SCRIPT_DIR"

# 3. Seed the global config from the template, if absent.
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/marim"
CONFIG_ENV="$CONFIG_DIR/.env"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_ENV" ]; then
    cp "$SCRIPT_DIR/.env.example" "$CONFIG_ENV"
    echo "Created $CONFIG_ENV from the template."
fi

# 4. Set the provider key. Prefer --key, then $OPENROUTER_API_KEY, else prompt.
if [ "$ASK_KEY" -eq 1 ]; then
    if [ -n "${OPENROUTER_API_KEY:-}" ]; then
        KEY="$OPENROUTER_API_KEY"
        echo "Using OPENROUTER_API_KEY from the environment."
    else
        printf "OpenRouter API key (leave blank to skip): "
        read -rs KEY
        printf "\n"
    fi
fi

if [ -n "$KEY" ]; then
    # Replace an existing OPENROUTER_API_KEY= line, or append one.
    if grep -q '^OPENROUTER_API_KEY=' "$CONFIG_ENV"; then
        tmp="$(mktemp)"
        grep -v '^OPENROUTER_API_KEY=' "$CONFIG_ENV" >"$tmp"
        printf 'OPENROUTER_API_KEY=%s\n' "$KEY" >>"$tmp"
        mv "$tmp" "$CONFIG_ENV"
    else
        printf 'OPENROUTER_API_KEY=%s\n' "$KEY" >>"$CONFIG_ENV"
    fi
    chmod 600 "$CONFIG_ENV"
    echo "Saved the API key to $CONFIG_ENV."
fi

# 5. Confirm reachability.
echo
if command -v marim >/dev/null 2>&1; then
    echo "Done. Run 'marim' in any project directory."
else
    BIN_DIR="$(uv tool dir 2>/dev/null)/../bin"
    echo "Installed, but 'marim' is not on your PATH yet."
    echo "Add uv's tool bin to PATH (then restart your shell):"
    echo "  uv tool update-shell"
    echo "  # or add to ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
echo "Config: $CONFIG_ENV"
