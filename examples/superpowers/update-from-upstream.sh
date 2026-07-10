#!/usr/bin/env bash
# Re-vendor this marim adaptation of obra/superpowers from an upstream copy.
#
# The vendored plugin is a *trimmed* mirror: only the parts marim loads are
# checked in (skills/, the marim-native hook, the .marim-plugin manifest, and
# LICENSE). The skills themselves carry no marim edits, so an update is
# mechanical — replace skills/ and LICENSE from upstream, then bump the manifest
# version. This script does exactly that and leaves the two marim-specific files
# (hooks/marim-session-start.sh, .marim-plugin/plugin.json) otherwise untouched.
#
# Usage:
#   ./update-from-upstream.sh <upstream-superpowers-dir>
#
# <upstream-superpowers-dir> is a checkout or marketplace cache of
# obra/superpowers at the target version, e.g.
#   ~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1
# It must contain skills/using-superpowers/SKILL.md, LICENSE, and a version in
# either .claude-plugin/plugin.json or package.json.
set -euo pipefail

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

[ $# -eq 1 ] || die "usage: $(basename "$0") <upstream-superpowers-dir>"
UP="${1%/}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -d "$UP" ] || die "not a directory: $UP"
[ -f "$UP/skills/using-superpowers/SKILL.md" ] || die "no skills/using-superpowers/SKILL.md under $UP — is this a superpowers tree?"
[ -f "$UP/LICENSE" ] || die "no LICENSE under $UP"

# Read the upstream version from its own manifest (Claude Code plugin.json first,
# then package.json). We only trust these two; the marim manifest is what we write.
read_version() {
  local f
  for f in "$UP/.claude-plugin/plugin.json" "$UP/package.json"; do
    [ -f "$f" ] || continue
    python3 - "$f" <<'PY' && return 0
import json, sys
v = json.load(open(sys.argv[1])).get("version")
if v:
    print(v); sys.exit(0)
sys.exit(1)
PY
  done
  return 1
}
VERSION="$(read_version)" || die "could not read a version from upstream .claude-plugin/plugin.json or package.json"

printf 'Vendoring superpowers %s from %s\n' "$VERSION" "$UP"

# 1) Replace skills/ wholesale (preserves per-skill scripts/ and references/,
#    including their executable bits). rsync --delete keeps our tree in lockstep
#    with upstream — a skill removed upstream is removed here too.
rm -rf "$HERE/skills"
cp -a "$UP/skills" "$HERE/skills"

# 2) Refresh the upstream MIT license verbatim.
cp -a "$UP/LICENSE" "$HERE/LICENSE"

# 3) Bump the marim manifest's version to match upstream; leave every other
#    field (the marim SessionStart hook wiring, description) as-is.
python3 - "$HERE/.marim-plugin/plugin.json" "$VERSION" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
data["version"] = version
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY

# Sanity: the marim files must still be present after the overlay.
[ -x "$HERE/hooks/marim-session-start.sh" ] || die "marim hook missing or not executable after update"
[ -f "$HERE/.marim-plugin/plugin.json" ]    || die "marim manifest missing after update"

SKILLS=$(find "$HERE/skills" -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')
printf 'Done: %s skills vendored at version %s.\n' "$SKILLS" "$VERSION"
printf 'Review the diff (README provenance line may need a manual bump), then commit.\n'
