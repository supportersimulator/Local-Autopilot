#!/usr/bin/env bash
# install-daemon-linux.sh — install + enable the Local Autopilot systemd timer.
#
# Substitutes __REPO_DIR__ + __USER__ placeholders in the template,
# copies the result to ~/.config/systemd/user/, and enables it.
# Idempotent: re-run anytime.
#
# Usage:
#   bash scripts/install-daemon-linux.sh                  # default 30 min tick
#   bash scripts/install-daemon-linux.sh --interval 30m   # custom (systemd format)
#   bash scripts/install-daemon-linux.sh --headless-executor  # opt-in: Atlas invokes `claude --print` per cycle
#   bash scripts/install-daemon-linux.sh --dry-run        # print what would happen
#
# Uninstall:  bash scripts/uninstall-daemon-linux.sh
# ZSF: every failure exits non-zero.

set -uo pipefail

INTERVAL="30min"
DRY_RUN=false
HEADLESS_EXECUTOR=false
for arg in "$@"; do
    case "$arg" in
        --interval) shift; INTERVAL="$1"; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --headless-executor) HEADLESS_EXECUTOR=true; shift ;;
    esac
done

if $HEADLESS_EXECUTOR; then
    echo ""
    echo "  WARNING: Headless executor enabled — Atlas will invoke \`claude --print\` on every cycle prompt without human review. Audit cycle artifacts under ~/.context-dna/autopilot-logs/cycle-* regularly."
    echo ""
fi

[ "$(uname -s)" = "Linux" ] || { echo "Linux only"; exit 2; }
command -v systemctl >/dev/null || { echo "systemctl not found"; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(whoami)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
SERVICE_UNIT="$USER_UNIT_DIR/local-autopilot-tick.service"
TIMER_UNIT="$USER_UNIT_DIR/local-autopilot-tick.timer"

_fail() { echo "[install-daemon] FAIL: $*" >&2; exit 1; }
_ok()   { echo "[install-daemon] OK: $*"; }
_info() { echo "[install-daemon] $*"; }

[ -d "$REPO_DIR/daemons" ] || _fail "daemons/ dir missing"
[ -f "$REPO_DIR/daemons/autopilot-tick.service.template" ] || _fail "service template missing"
[ -f "$REPO_DIR/daemons/autopilot-tick.timer.template" ] || _fail "timer template missing"
[ -x "$REPO_DIR/.venv/bin/python3" ] || _fail "venv missing — run 'bash install.sh' first"

mkdir -p "$USER_UNIT_DIR"

# Render both units
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__USER__|$USER_NAME|g" \
    "$REPO_DIR/daemons/autopilot-tick.service.template" > "$SERVICE_UNIT.tmp"

# Opt-in: append --headless-executor to the ExecStart line.
if $HEADLESS_EXECUTOR; then
    sed -i.bak -e 's|^\(ExecStart=.*archloop_runner.*\)$|\1 --headless-executor|' "$SERVICE_UNIT.tmp"
    rm -f "$SERVICE_UNIT.tmp.bak"
    _info "headless executor flag appended to ExecStart"
fi
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__USER__|$USER_NAME|g" -e "s|OnUnitActiveSec=.*|OnUnitActiveSec=$INTERVAL|" \
    "$REPO_DIR/daemons/autopilot-tick.timer.template" > "$TIMER_UNIT.tmp"

if $DRY_RUN; then
    _info "DRY-RUN — would install:"
    echo "  $SERVICE_UNIT"
    echo "  $TIMER_UNIT (interval=$INTERVAL)"
    rm "$SERVICE_UNIT.tmp" "$TIMER_UNIT.tmp"
    exit 0
fi

mv "$SERVICE_UNIT.tmp" "$SERVICE_UNIT"
mv "$TIMER_UNIT.tmp" "$TIMER_UNIT"

systemctl --user daemon-reload || _fail "daemon-reload failed"
systemctl --user enable --now local-autopilot-tick.timer || _fail "enable+start failed"

if systemctl --user is-active local-autopilot-tick.timer >/dev/null 2>&1; then
    _ok "timer enabled + active (interval $INTERVAL)"
else
    _fail "timer not active — check 'systemctl --user status local-autopilot-tick.timer'"
fi

echo ""
echo "  Verify:"
echo "    systemctl --user list-timers | grep local-autopilot"
echo "    journalctl --user -u local-autopilot-tick.service -f"
echo ""
echo "  Status:"
echo "    $REPO_DIR/.venv/bin/autopilot status"
echo ""
echo "  The timer ticks every $INTERVAL but only does work when autopilot is on:"
echo "    $REPO_DIR/.venv/bin/autopilot on"
echo ""
echo "  Uninstall: bash $REPO_DIR/scripts/uninstall-daemon-linux.sh"
