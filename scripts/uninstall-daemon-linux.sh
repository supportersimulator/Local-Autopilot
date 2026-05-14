#!/usr/bin/env bash
# uninstall-daemon-linux.sh — remove the systemd timer/service.

set -uo pipefail
[ "$(uname -s)" = "Linux" ] || { echo "Linux only"; exit 2; }

USER_UNIT_DIR="$HOME/.config/systemd/user"

systemctl --user disable --now local-autopilot-tick.timer 2>/dev/null || true
systemctl --user stop local-autopilot-tick.service 2>/dev/null || true
rm -f "$USER_UNIT_DIR/local-autopilot-tick.service" "$USER_UNIT_DIR/local-autopilot-tick.timer"
systemctl --user daemon-reload 2>/dev/null || true

echo "[uninstall] units removed"
echo "[uninstall] autopilot state at ~/.context-dna/autopilot_state.json is preserved"
