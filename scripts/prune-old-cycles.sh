#!/usr/bin/env bash
# prune-old-cycles.sh — bounded retention for ~/.context-dna/autopilot-logs/
#
# Each autopilot tick writes a directory:
#   ~/.context-dna/autopilot-logs/cycle-<ISO8601>/
# containing 12+ files (prompts, results, cross_exam, cycle_summary.json, ...).
# At ~48 dirs/day disk growth is small but unbounded. This script prunes
# old cycles with policy controls.
#
# Default: keep last 30 days; delete older. Failed cycles (cycle_summary.json
# verdict_decision != "SIGN-OFF") can be preserved via --keep-failed.
#
# Flags:
#   --keep-days N      keep cycles newer than N days (default 30)
#   --keep-count N     keep N most-recent cycles regardless of age
#                      (overrides --keep-days when set)
#   --keep-failed      never delete cycles whose verdict_decision != "SIGN-OFF"
#   --dry-run          report what would be deleted; change nothing
#   --json             emit summary as JSON
#   --logs-dir PATH    override autopilot logs dir
#                      (default ~/.context-dna/autopilot-logs)
#   -h | --help        show this help
#
# ZSF: every per-cycle error is reported + counted; the run never aborts on
# a single bad cycle. Final summary always printed.

set -uo pipefail

KEEP_DAYS=30
KEEP_COUNT=""
KEEP_FAILED=false
DRY_RUN=false
JSON_OUT=false
LOGS_DIR="${HOME}/.context-dna/autopilot-logs"

_usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --keep-days)    KEEP_DAYS="$2"; shift 2 ;;
        --keep-count)   KEEP_COUNT="$2"; shift 2 ;;
        --keep-failed)  KEEP_FAILED=true; shift ;;
        --dry-run)      DRY_RUN=true; shift ;;
        --json)         JSON_OUT=true; shift ;;
        --logs-dir)     LOGS_DIR="$2"; shift 2 ;;
        -h|--help)      _usage; exit 0 ;;
        *)              echo "[prune] unknown arg: $1" >&2; exit 2 ;;
    esac
done

_log() {
    # Suppress chatter when emitting JSON; errors still go to stderr.
    if $JSON_OUT; then
        echo "[prune] $*" >&2
    else
        echo "[prune] $*"
    fi
}
_err() { echo "[prune] ERR: $*" >&2; }

# Validate numeric args
case "$KEEP_DAYS" in
    ''|*[!0-9]*) _err "--keep-days must be a non-negative integer"; exit 2 ;;
esac
if [ -n "$KEEP_COUNT" ]; then
    case "$KEEP_COUNT" in
        ''|*[!0-9]*) _err "--keep-count must be a non-negative integer"; exit 2 ;;
    esac
fi

if [ ! -d "$LOGS_DIR" ]; then
    _log "no logs dir at $LOGS_DIR — nothing to do"
    if $JSON_OUT; then
        echo '{"kept":0,"deleted":0,"errors":0,"bytes_freed":0,"logs_dir":"'"$LOGS_DIR"'","dry_run":'"$DRY_RUN"'}'
    fi
    exit 0
fi

# Portable disk-usage in bytes for a path (macOS + Linux).
_dir_bytes() {
    local p="$1"
    if [ ! -e "$p" ]; then echo 0; return; fi
    # BSD du (-A) reports 512-byte blocks; GNU has --bytes.
    if du --bytes -s "$p" >/dev/null 2>&1; then
        du --bytes -s "$p" 2>/dev/null | awk '{print $1}'
    else
        # macOS: -k reports KiB; multiply.
        du -ks "$p" 2>/dev/null | awk '{print $1 * 1024}'
    fi
}

# Epoch (seconds) of dir mtime — portable.
_mtime_epoch() {
    local p="$1"
    if stat -f %m "$p" >/dev/null 2>&1; then
        stat -f %m "$p"            # BSD/macOS
    else
        stat -c %Y "$p"            # GNU
    fi
}

# Is a cycle a non-SIGN-OFF (i.e. failed/incomplete)?
# Returns 0 (true) if NOT sign-off — meaning we should preserve under --keep-failed.
_is_failed() {
    local cdir="$1"
    local summary="$cdir/cycle_summary.json"
    [ -f "$summary" ] || return 0   # missing summary == treat as failed/incomplete
    grep -q '"verdict_decision"[[:space:]]*:[[:space:]]*"SIGN-OFF"' "$summary" && return 1
    return 0
}

NOW_EPOCH="$(date +%s)"
CUTOFF_EPOCH=$(( NOW_EPOCH - KEEP_DAYS * 86400 ))

# Build list of cycle dirs, newest-first (sorted by ISO8601 timestamp suffix).
CYCLES=()
while IFS= read -r d; do
    [ -n "$d" ] && CYCLES+=("$d")
done < <(find "$LOGS_DIR" -maxdepth 1 -type d -name 'cycle-*' 2>/dev/null | sort -r)

kept=0
deleted=0
errors=0
bytes_freed=0
deleted_list=()

idx=0
for cdir in "${CYCLES[@]}"; do
    idx=$((idx + 1))
    base="$(basename "$cdir")"

    # Determine action.
    action="delete"
    reason=""

    if [ -n "$KEEP_COUNT" ]; then
        if [ "$idx" -le "$KEEP_COUNT" ]; then
            action="keep"; reason="within keep-count=$KEEP_COUNT"
        else
            reason="beyond keep-count=$KEEP_COUNT"
        fi
    else
        mt="$(_mtime_epoch "$cdir" 2>/dev/null || echo 0)"
        if [ "$mt" -ge "$CUTOFF_EPOCH" ]; then
            action="keep"; reason="within keep-days=$KEEP_DAYS"
        else
            reason="older than keep-days=$KEEP_DAYS"
        fi
    fi

    # --keep-failed override: never delete failed cycles.
    if [ "$action" = "delete" ] && $KEEP_FAILED; then
        if _is_failed "$cdir"; then
            action="keep"; reason="failed cycle preserved (--keep-failed)"
        fi
    fi

    if [ "$action" = "keep" ]; then
        kept=$((kept + 1))
        continue
    fi

    # Action = delete.
    sz="$(_dir_bytes "$cdir" 2>/dev/null || echo 0)"
    case "$sz" in ''|*[!0-9]*) sz=0 ;; esac

    if $DRY_RUN; then
        _log "DRY-RUN would delete: $base ($sz bytes — $reason)"
        deleted=$((deleted + 1))
        bytes_freed=$((bytes_freed + sz))
        deleted_list+=("$base")
        continue
    fi

    if rm -rf -- "$cdir" 2>/tmp/prune-rm.err; then
        _log "deleted: $base ($sz bytes — $reason)"
        deleted=$((deleted + 1))
        bytes_freed=$((bytes_freed + sz))
        deleted_list+=("$base")
    else
        errors=$((errors + 1))
        _err "failed to delete $base: $(cat /tmp/prune-rm.err 2>/dev/null)"
    fi
done

# Human-readable bytes.
_humanize() {
    local b="$1"
    if [ "$b" -ge 1073741824 ]; then awk -v b="$b" 'BEGIN{printf "%.2f GiB", b/1073741824}'
    elif [ "$b" -ge 1048576 ];   then awk -v b="$b" 'BEGIN{printf "%.2f MiB", b/1048576}'
    elif [ "$b" -ge 1024 ];      then awk -v b="$b" 'BEGIN{printf "%.2f KiB", b/1024}'
    else echo "${b} B"
    fi
}

if $JSON_OUT; then
    # Emit JSON summary (deleted list inline).
    printf '{"logs_dir":"%s","dry_run":%s,"kept":%d,"deleted":%d,"errors":%d,"bytes_freed":%d,"deleted":[' \
        "$LOGS_DIR" "$DRY_RUN" "$kept" "$deleted" "$errors" "$bytes_freed"
    first=true
    for d in "${deleted_list[@]}"; do
        if $first; then first=false; else printf ','; fi
        printf '"%s"' "$d"
    done
    printf ']}\n'
else
    echo ""
    echo "[prune] summary:"
    echo "  logs_dir:    $LOGS_DIR"
    echo "  dry_run:     $DRY_RUN"
    echo "  cycles kept:    $kept"
    echo "  cycles deleted: $deleted"
    echo "  errors:         $errors"
    echo "  disk freed:     $(_humanize "$bytes_freed") (${bytes_freed} bytes)"
fi

# Exit 0 even on per-cycle errors (ZSF: reported + counted, not fatal).
exit 0
