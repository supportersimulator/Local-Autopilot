#!/usr/bin/env bash
# uninstall-daemon-macos.sh — remove the Local Autopilot launchd timer.
# Safe to run even if nothing is installed.

set -uo pipefail
[ "$(uname -s)" = "Darwin" ] || { echo "macOS only"; exit 2; }

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
TICK_PLIST="$LAUNCH_AGENTS/com.localautopilot.tick.plist"
RERANK_PLIST="$LAUNCH_AGENTS/com.localautopilot.rerank.plist"

for plist in "$TICK_PLIST" "$RERANK_PLIST"; do
    name="$(basename "$plist" .plist)"
    if launchctl list 2>/dev/null | grep -q "$name"; then
        launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null && echo "[uninstall] $name unloaded"
    fi
    [ -f "$plist" ] && { rm "$plist"; echo "[uninstall] removed $plist"; }
done

echo "[uninstall] complete — autopilot state at ~/.context-dna/autopilot_state.json is preserved"
echo "[uninstall]            (delete it manually if you also want to wipe state)"
