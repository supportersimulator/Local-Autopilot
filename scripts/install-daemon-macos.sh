#!/usr/bin/env bash
# install-daemon-macos.sh — install + load the Local Autopilot launchd timer.
#
# Substitutes __REPO_DIR__ + __USER__ placeholders in the template,
# copies the result to ~/Library/LaunchAgents/, and loads it.
# Idempotent: re-run anytime; existing entry is unloaded + reloaded.
#
# Usage:
#   bash scripts/install-daemon-macos.sh                    # default 30 min tick
#   bash scripts/install-daemon-macos.sh --interval 1800    # custom interval (seconds)
#   bash scripts/install-daemon-macos.sh --also-rerank      # also install the daily rerank job
#   bash scripts/install-daemon-macos.sh --dry-run          # print what would happen
#
# Uninstall:   bash scripts/uninstall-daemon-macos.sh
# ZSF: every failure exits non-zero with a labelled error line.

set -uo pipefail

INTERVAL=1800
INSTALL_RERANK=false
INSTALL_PRUNE=false
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --interval) shift; INTERVAL="$1"; shift ;;
        --also-rerank) INSTALL_RERANK=true; shift ;;
        --install-prune) INSTALL_PRUNE=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "[install-daemon] this script is for macOS only"; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(whoami)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
TICK_PLIST="$LAUNCH_AGENTS/com.localautopilot.tick.plist"
RERANK_PLIST="$LAUNCH_AGENTS/com.localautopilot.rerank.plist"

_fail() { echo "[install-daemon] FAIL: $*" >&2; exit 1; }
_ok()   { echo "[install-daemon] OK: $*"; }
_info() { echo "[install-daemon] $*"; }

# Preflight
[ -d "$REPO_DIR/daemons" ] || _fail "daemons/ dir missing in $REPO_DIR"
[ -f "$REPO_DIR/daemons/autopilot-tick.plist.template" ] || _fail "autopilot-tick.plist.template missing"
[ -x "$REPO_DIR/.venv/bin/python3" ] || _fail "venv not built — run 'bash install.sh' first"

mkdir -p "$LAUNCH_AGENTS"

# Render the tick plist
TICK_TMP="$(mktemp)"
sed \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|<integer>1800</integer>|<integer>$INTERVAL</integer>|" \
    "$REPO_DIR/daemons/autopilot-tick.plist.template" > "$TICK_TMP"

if $DRY_RUN; then
    _info "DRY-RUN — would write $TICK_PLIST:"
    sed -n '1,5p;/__/p' "$TICK_TMP"
    rm "$TICK_TMP"
    exit 0
fi

# Unload existing (idempotent)
if [[ "$(launchctl list 2>/dev/null)" == *"com.localautopilot.tick"* ]]; then
    launchctl bootout "gui/$(id -u)" "$TICK_PLIST" 2>/dev/null || true
fi

cp "$TICK_TMP" "$TICK_PLIST"
chmod 644 "$TICK_PLIST"
rm "$TICK_TMP"

# Load — verify by list, not bootstrap exit code.
# macOS launchctl can return "Input/output error" (exit 5) on re-bootstrap even
# when the service successfully (re)loads. Source of truth = launchctl list.
# Registration is also async — retry the list check briefly.
launchctl bootstrap "gui/$(id -u)" "$TICK_PLIST" >/tmp/launchctl-install.log 2>&1 || true
_loaded=false
for _i in 1 2 3 4 5; do
    if [[ "$(launchctl list 2>/dev/null)" == *"com.localautopilot.tick"* ]]; then
        _loaded=true; break
    fi
    sleep 1
done
if $_loaded; then
    _ok "tick timer loaded (every ${INTERVAL}s)"
else
    _fail "service not loaded after bootstrap (waited 5s) — check /tmp/launchctl-install.log"
fi

# Optionally install the rerank job
if $INSTALL_RERANK && [ -f "$REPO_DIR/daemons/autopilot-rerank.plist.template" ]; then
    RERANK_TMP="$(mktemp)"
    sed \
        -e "s|__REPO_DIR__|$REPO_DIR|g" \
        -e "s|__USER__|$USER_NAME|g" \
        "$REPO_DIR/daemons/autopilot-rerank.plist.template" > "$RERANK_TMP"

    if launchctl list 2>/dev/null | grep -q com.localautopilot.rerank; then
        launchctl bootout "gui/$(id -u)" "$RERANK_PLIST" 2>/dev/null || true
    fi
    cp "$RERANK_TMP" "$RERANK_PLIST"
    chmod 644 "$RERANK_PLIST"
    rm "$RERANK_TMP"
    launchctl bootstrap "gui/$(id -u)" "$RERANK_PLIST" 2>/dev/null || true
    if [[ "$(launchctl list 2>/dev/null)" == *"com.localautopilot.rerank"* ]]; then
        _ok "rerank job loaded (daily)"
    else
        echo "[install-daemon] WARN: rerank plist install failed (non-fatal)"
    fi
fi

# Optionally install the prune job (daily cycle-log retention)
PRUNE_PLIST="$LAUNCH_AGENTS/com.localautopilot.prune.plist"
if $INSTALL_PRUNE && [ -f "$REPO_DIR/daemons/autopilot-prune.plist.template" ]; then
    PRUNE_TMP="$(mktemp)"
    sed \
        -e "s|__REPO_DIR__|$REPO_DIR|g" \
        -e "s|__USER__|$USER_NAME|g" \
        "$REPO_DIR/daemons/autopilot-prune.plist.template" > "$PRUNE_TMP"

    if launchctl list 2>/dev/null | grep -q com.localautopilot.prune; then
        launchctl bootout "gui/$(id -u)" "$PRUNE_PLIST" 2>/dev/null || true
    fi
    cp "$PRUNE_TMP" "$PRUNE_PLIST"
    chmod 644 "$PRUNE_PLIST"
    rm "$PRUNE_TMP"
    if launchctl bootstrap "gui/$(id -u)" "$PRUNE_PLIST" 2>/dev/null; then
        _ok "prune job loaded (daily 04:30)"
    else
        echo "[install-daemon] WARN: prune plist install failed (non-fatal)"
    fi
fi

echo ""
_ok "installation complete"
echo ""
echo "  Verify:"
echo "    launchctl list | grep localautopilot"
echo "    tail -f $HOME/.context-dna/autopilot-logs/launchd.out"
echo ""
echo "  Status:"
echo "    $REPO_DIR/.venv/bin/autopilot status"
echo ""
echo "  Remember: the daemon ticks every ${INTERVAL}s, but it ONLY does work when"
echo "  autopilot is on. Turn it on with:"
echo "    $REPO_DIR/.venv/bin/autopilot on"
echo ""
echo "  Uninstall: bash $REPO_DIR/scripts/uninstall-daemon-macos.sh"
