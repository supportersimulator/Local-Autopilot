#!/usr/bin/env bash
# Local Autopilot — symmetric inverse of install.sh.
#
# What install.sh creates, this script removes (in reverse-safe order):
#   1. launchd / systemd daemon (delegates to uninstall-daemon-{macos,linux}.sh)
#   2. ./.venv/                                          (skip with --keep-venv)
#   3. ~/.context-dna/autopilot_state.json               (only with --purge-data)
#   4. ~/.context-dna/complexity_vectors.db              (only with --purge-data)
#   5. ~/.context-dna/autopilot-logs/                    (only with --purge-logs)
#
# Default behaviour PRESERVES ~/.context-dna/ — it may contain cycle logs and
# state-transition history the user wants to keep. Opt into deletion explicitly.
#
# Flags:
#   --keep-venv      do not delete ./.venv/
#   --purge-data     also delete autopilot_state.json + complexity_vectors.db
#   --purge-logs     also delete autopilot-logs/
#   --all            equivalent to --purge-data --purge-logs (and removes venv)
#   --dry-run        print what would happen; change nothing
#   -y | --yes       non-interactive; assume yes to every prompt
#   -h | --help      show this help
#
# ZSF (zero silent failures): every deletion is reported. No skip is silent.
# Final summary prints what was removed + what was preserved.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
CONTEXT_DNA_DIR="${CONTEXT_DNA_DIR:-$HOME/.context-dna}"
DB_PATH="$CONTEXT_DNA_DIR/complexity_vectors.db"
STATE_PATH="$CONTEXT_DNA_DIR/autopilot_state.json"
LOGS_DIR="$CONTEXT_DNA_DIR/autopilot-logs"

KEEP_VENV=0
PURGE_DATA=0
PURGE_LOGS=0
DRY_RUN=0
ASSUME_YES=0

# Counters / trackers for the final summary
REMOVED=()
PRESERVED=()
SKIPPED=()
ERRORS=0

log()  { printf '[uninstall] %s\n' "$*"; }
warn() { printf '[uninstall][warn] %s\n' "$*" >&2; }
err()  { printf '[uninstall][error] %s\n' "$*" >&2; ERRORS=$((ERRORS+1)); }

usage() {
    sed -n '2,27p' "$0"
    exit 0
}

# --- Parse args --------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --keep-venv)  KEEP_VENV=1 ;;
        --purge-data) PURGE_DATA=1 ;;
        --purge-logs) PURGE_LOGS=1 ;;
        --all)        PURGE_DATA=1; PURGE_LOGS=1; KEEP_VENV=0 ;;
        --dry-run)    DRY_RUN=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        -h|--help)    usage ;;
        *)            err "unknown flag: $arg"; exit 64 ;;
    esac
done

# --- TTY / interactive guard -------------------------------------------------
INTERACTIVE=0
if [ -t 0 ] && [ -t 1 ]; then
    INTERACTIVE=1
fi

if [ $INTERACTIVE -eq 0 ] && [ $ASSUME_YES -eq 0 ] && [ $DRY_RUN -eq 0 ]; then
    err "no TTY available and --yes/--dry-run not passed; refusing to proceed"
    err "  re-run with -y for non-interactive, or --dry-run to preview"
    exit 65
fi

confirm() {
    # $1 = prompt
    local prompt="$1"
    if [ $DRY_RUN -eq 1 ]; then return 0; fi
    if [ $ASSUME_YES -eq 1 ]; then return 0; fi
    if [ $INTERACTIVE -eq 0 ]; then return 1; fi
    local reply=""
    printf '[uninstall] %s [y/N] ' "$prompt"
    read -r reply || return 1
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Action helpers ----------------------------------------------------------
do_rm() {
    # $1 = path, $2 = label
    local path="$1" label="$2"
    if [ $DRY_RUN -eq 1 ]; then
        log "DRY-RUN would remove: $path ($label)"
        REMOVED+=("(dry) $path")
        return 0
    fi
    if rm -rf -- "$path"; then
        log "removed $path ($label)"
        REMOVED+=("$path")
    else
        err "failed to remove $path"
    fi
}

# --- 1. Daemon ---------------------------------------------------------------
detect_and_uninstall_daemon() {
    local os; os="$(uname -s)"
    case "$os" in
        Darwin)
            local tick="com.localautopilot.tick"
            local rerank="com.localautopilot.rerank"
            if launchctl list 2>/dev/null | grep -Eq "$tick|$rerank"; then
                log "launchd agent(s) loaded — running uninstall-daemon-macos.sh"
                if [ $DRY_RUN -eq 1 ]; then
                    log "DRY-RUN would invoke: $REPO_DIR/scripts/uninstall-daemon-macos.sh"
                    REMOVED+=("(dry) launchd agents")
                else
                    if bash "$REPO_DIR/scripts/uninstall-daemon-macos.sh"; then
                        REMOVED+=("launchd agents (tick+rerank)")
                    else
                        err "uninstall-daemon-macos.sh exited non-zero"
                    fi
                fi
            else
                log "no launchd agent loaded — skipping daemon step"
                SKIPPED+=("launchd (not loaded)")
            fi
            ;;
        Linux)
            if systemctl --user list-unit-files 2>/dev/null | grep -q '^local-autopilot-tick'; then
                log "systemd unit present — running uninstall-daemon-linux.sh"
                if [ $DRY_RUN -eq 1 ]; then
                    log "DRY-RUN would invoke: $REPO_DIR/scripts/uninstall-daemon-linux.sh"
                    REMOVED+=("(dry) systemd units")
                else
                    if bash "$REPO_DIR/scripts/uninstall-daemon-linux.sh"; then
                        REMOVED+=("systemd units (tick.service+timer)")
                    else
                        err "uninstall-daemon-linux.sh exited non-zero"
                    fi
                fi
            else
                log "no systemd unit installed — skipping daemon step"
                SKIPPED+=("systemd (not installed)")
            fi
            ;;
        *)
            warn "unknown OS '$os' — cannot auto-detect daemon; skip"
            SKIPPED+=("daemon (unknown OS: $os)")
            ;;
    esac
}

log "Local Autopilot uninstall starting"
[ $DRY_RUN -eq 1 ] && log "DRY-RUN MODE — no files will be changed"

if confirm "Step 1/4: detect & remove daemon (launchd/systemd)?"; then
    detect_and_uninstall_daemon
else
    log "daemon step declined"
    SKIPPED+=("daemon (declined)")
fi

# --- 2. venv -----------------------------------------------------------------
if [ $KEEP_VENV -eq 1 ]; then
    log "--keep-venv set — preserving $VENV"
    PRESERVED+=("$VENV")
elif [ ! -d "$VENV" ]; then
    log "$VENV does not exist — nothing to remove"
    SKIPPED+=("venv (absent)")
elif confirm "Step 2/4: delete venv at $VENV?"; then
    do_rm "$VENV" "venv"
else
    log "venv deletion declined"
    PRESERVED+=("$VENV (declined)")
fi

# --- 3. Data (state + DB) ----------------------------------------------------
if [ $PURGE_DATA -eq 1 ]; then
    for target in "$STATE_PATH" "$DB_PATH"; do
        if [ ! -e "$target" ]; then
            log "$target does not exist — nothing to remove"
            SKIPPED+=("$(basename "$target") (absent)")
            continue
        fi
        if confirm "Step 3/4: delete $target?"; then
            do_rm "$target" "user data"
        else
            log "deletion of $target declined"
            PRESERVED+=("$target (declined)")
        fi
    done
else
    log "--purge-data not set — preserving autopilot_state.json + complexity_vectors.db"
    [ -e "$STATE_PATH" ] && PRESERVED+=("$STATE_PATH")
    [ -e "$DB_PATH" ]    && PRESERVED+=("$DB_PATH")
fi

# --- 4. Logs -----------------------------------------------------------------
if [ $PURGE_LOGS -eq 1 ]; then
    if [ ! -d "$LOGS_DIR" ]; then
        log "$LOGS_DIR does not exist — nothing to remove"
        SKIPPED+=("autopilot-logs (absent)")
    elif confirm "Step 4/4: delete logs at $LOGS_DIR?"; then
        do_rm "$LOGS_DIR" "logs"
    else
        log "log deletion declined"
        PRESERVED+=("$LOGS_DIR (declined)")
    fi
else
    log "--purge-logs not set — preserving autopilot-logs/"
    [ -d "$LOGS_DIR" ] && PRESERVED+=("$LOGS_DIR")
fi

# Note: we deliberately NEVER auto-delete $CONTEXT_DNA_DIR itself, because
# other tools (context-dna proper) share that directory.

# --- Summary -----------------------------------------------------------------
printf '\n[uninstall] ===== Summary =====\n'
printf '[uninstall] Removed:\n'
if [ ${#REMOVED[@]} -eq 0 ]; then
    printf '[uninstall]   (nothing)\n'
else
    for item in "${REMOVED[@]}"; do printf '[uninstall]   - %s\n' "$item"; done
fi
printf '[uninstall] Preserved:\n'
if [ ${#PRESERVED[@]} -eq 0 ]; then
    printf '[uninstall]   (nothing)\n'
else
    for item in "${PRESERVED[@]}"; do printf '[uninstall]   - %s\n' "$item"; done
fi
printf '[uninstall] Skipped (not present / declined):\n'
if [ ${#SKIPPED[@]} -eq 0 ]; then
    printf '[uninstall]   (nothing)\n'
else
    for item in "${SKIPPED[@]}"; do printf '[uninstall]   - %s\n' "$item"; done
fi

if [ $ERRORS -gt 0 ]; then
    err "$ERRORS error(s) occurred during uninstall"
    exit 1
fi

[ $DRY_RUN -eq 1 ] && log "DRY-RUN complete — nothing was changed"
log "uninstall complete"
exit 0
