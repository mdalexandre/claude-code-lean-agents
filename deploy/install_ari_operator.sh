#!/usr/bin/env bash
# Install or remove the ARI 3.0 operator schedule and its SessionStart hook.
#
#   deploy/install_ari_operator.sh install     timer + hook (backs up settings.json first)
#   deploy/install_ari_operator.sh uninstall   remove both, keep ~/.ari/operator state
#   deploy/install_ari_operator.sh status      show what is installed
#
# Nothing in ARI runs this script. Running it is the owner's decision.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
HOOK_CMD="python3 $REPO/scripts/ari_operator.py --state-dir $HOME/.ari/operator hook"

hook_edit() {  # $1 = add | remove
  python3 - "$SETTINGS" "$HOOK_CMD" "$1" <<'PY'
import json, sys
path, cmd, mode = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
except FileNotFoundError:
    cfg = {}
groups = cfg.setdefault("hooks", {}).setdefault("SessionStart", [])
def ours(h): return h.get("type") == "command" and h.get("command") == cmd
for g in groups:
    g["hooks"] = [h for h in g.get("hooks", []) if not ours(h)]
groups[:] = [g for g in groups if g.get("hooks")]
if mode == "add":
    groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
if not groups:
    del cfg["hooks"]["SessionStart"]
if not cfg["hooks"]:
    del cfg["hooks"]
import os, tempfile
fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".ari-tmp",
                           dir=os.path.dirname(os.path.abspath(path)))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
except BaseException:
    os.unlink(tmp)
    raise
print(f"SessionStart hook {'registered' if mode == 'add' else 'removed'} in {path}")
PY
}

case "${1:-}" in
  install)
    mkdir -p "$UNIT_DIR" "$HOME/.ari/operator"
    [ -f "$SETTINGS" ] && cp -p --update=none "$SETTINGS" "$SETTINGS.bak-ari-$(date +%Y%m%d%H%M%S%N)"
    sed "s|@REPO@|$REPO|g" "$REPO/deploy/ari-operator.service" > "$UNIT_DIR/ari-operator.service"
    cp "$REPO/deploy/ari-operator.timer" "$UNIT_DIR/ari-operator.timer"
    systemctl --user daemon-reload
    hook_edit add  # the hook first: a timer that fires before it exists shows nothing
    systemctl --user enable --now ari-operator.timer
    systemctl --user list-timers ari-operator.timer --no-pager
    ;;
  uninstall)
    systemctl --user disable --now ari-operator.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/ari-operator.service" "$UNIT_DIR/ari-operator.timer"
    systemctl --user daemon-reload
    [ -f "$SETTINGS" ] && cp -p --update=none "$SETTINGS" "$SETTINGS.bak-ari-$(date +%Y%m%d%H%M%S%N)" && hook_edit remove
    echo "state kept in $HOME/.ari/operator"
    ;;
  status)
    systemctl --user list-timers ari-operator.timer --no-pager || true
    n="$(grep -c "ari_operator.py" "$SETTINGS" 2>/dev/null || true)"
    echo "hook entries: ${n:-0}"
    ;;
  *) echo "usage: $0 install|uninstall|status" >&2; exit 2 ;;
esac
