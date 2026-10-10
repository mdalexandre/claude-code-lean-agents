#!/usr/bin/env bash
# Remove lean-worker and lean-checker installed by install.sh.
#   ./uninstall.sh            user level: ~/.claude/agents
#   ./uninstall.sh --project  project level: ./.claude/agents
#   ./uninstall.sh --hook     also remove ~/.claude/hooks/lean_dispatch_nudge.py
# A file you have edited is not deleted: it is renamed to <name>.bak-<timestamp>.
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
for f in lean-worker.md lean-checker.md; do
  target="$dest/$f"
  [ -e "$target" ] || continue
  if cmp -s "$here/agents/$f" "$target"; then
    rm "$target"
    echo "removed $target"
  else
    mv "$target" "$target.bak-$stamp"
    echo "kept your edited copy as $target.bak-$stamp"
  fi
done

if [ "$with_hook" = 1 ]; then
  hook="${HOME}/.claude/hooks/lean_dispatch_nudge.py"
  if [ -e "$hook" ]; then
    rm "$hook"
    echo "removed $hook (also delete its entry from ~/.claude/settings.json)"
  fi
fi
