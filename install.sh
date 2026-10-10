#!/usr/bin/env bash
# Install lean-worker and lean-checker as Claude Code subagents.
#   ./install.sh            user level: ~/.claude/agents (all projects)
#   ./install.sh --project  project level: ./.claude/agents (this repo only)
#   ./install.sh --hook     also copy the optional dispatch hook to ~/.claude/hooks
# An existing file with the same name is backed up as <name>.bak-<timestamp> first.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${HOME}/.claude/agents"
with_hook=0
for arg in "$@"; do
  case "$arg" in
    --project) dest="$(pwd)/.claude/agents" ;;
    --hook) with_hook=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

stamp="$(date +%Y%m%d%H%M%S)-$$"
mkdir -p "$dest"
for f in lean-worker.md lean-checker.md; do
  if [ -e "$dest/$f" ] && ! cmp -s "$here/agents/$f" "$dest/$f"; then
    cp -p "$dest/$f" "$dest/$f.bak-$stamp"
    echo "backed up $dest/$f"
  fi
  cp "$here/agents/$f" "$dest/$f"
  echo "installed $dest/$f"
done

if [ "$with_hook" = 1 ]; then
  hooks="${HOME}/.claude/hooks"
  mkdir -p "$hooks"
  cp "$here/hooks/lean_dispatch_nudge.py" "$hooks/lean_dispatch_nudge.py"
  chmod +x "$hooks/lean_dispatch_nudge.py"
  echo "installed $hooks/lean_dispatch_nudge.py"
  echo
  echo "Register it in ~/.claude/settings.json. If you already have a \"hooks\" key or a"
  echo "\"PreToolUse\" list, add the entry inside it instead of pasting a second key:"
  cat <<JSON
  "hooks": {
    "PreToolUse": [
      { "matcher": "Agent|Task",
        "hooks": [ { "type": "command", "command": "python3 $hooks/lean_dispatch_nudge.py" } ] }
    ]
  }
JSON
fi

echo
echo "Done. Start a new Claude Code session and ask Claude which subagents it can use;"
echo "lean-worker and lean-checker should be in the list."
