#!/usr/bin/env bash
# Local Autopilot — one-command bootstrap for a fresh laptop.
#
# Idempotent: re-running this script never destroys existing state.
#
# What it does (in order):
#   1. Build a Python 3.12 (or 3.13 fallback) venv at ./.venv
#   2. pip install requirements.txt
#   3. Initialise ~/.context-dna/complexity_vectors.db from seeds/ (skip if
#      the DB already exists — never clobber)
#   4. Initialise ~/.context-dna/autopilot_state.json to mode=off (skip if
#      it exists)
#   5. Stage daemon templates into ./daemons/ + print user-action steps to
#      enable them (we never run launchctl/systemctl for you)
#   6. Run pytest as a smoke check — ≥80 tests must pass for the install to
#      be considered green
#   7. Print final next-step commands
#
# Exit codes:
#   0 — install + smoke test all green
#   1 — Python 3.12 / 3.13 not found
#   2 — pip install failed
#   3 — DB / state initialisation failed
#   4 — pytest smoke check failed
#   5 — preflight check failed (no working LLM provider / missing runtime deps)
#       Set LOCAL_AUTOPILOT_INSTALL_LENIENT=1 to downgrade this to a warning
#       (useful for CI / fresh-machine setup before API keys are configured).
#
# Re-run anytime. Safe to invoke from any cwd; the script cd's to its own dir.

set -eu

# --- Resolve script dir (works whether invoked from elsewhere) ---------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

VENV="$REPO_DIR/.venv"
CONTEXT_DNA_DIR="${CONTEXT_DNA_DIR:-$HOME/.context-dna}"
DB_PATH="$CONTEXT_DNA_DIR/complexity_vectors.db"
STATE_PATH="$CONTEXT_DNA_DIR/autopilot_state.json"

log() { printf '[install] %s\n' "$*"; }
warn() { printf '[install][warn] %s\n' "$*" >&2; }
die()  { printf '[install][error] %s\n' "$*" >&2; exit "${2:-1}"; }

# --- 1. Pick a Python ---------------------------------------------------------
PY=""
for cand in python3.12 python3.13; do
    if command -v "$cand" >/dev/null 2>&1; then
        PY="$cand"; break
    fi
done
[ -n "$PY" ] || die "Python 3.12 or 3.13 not found on PATH. Install with: brew install python@3.12" 1
log "using $PY ($(command -v "$PY"))"

# --- 2. Create venv if missing -----------------------------------------------
if [ ! -d "$VENV" ]; then
    log "creating venv at $VENV"
    "$PY" -m venv "$VENV"
else
    log "venv exists at $VENV (skipping creation)"
fi

# --- 3. Install requirements + the project itself ----------------------------
# `pip install -e .` registers the `autopilot`, `autopilot-runner`, and
# `autopilot-rerank` console_scripts under .venv/bin/ so the daily-use
# commands in the README work verbatim.
log "installing requirements.txt"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt || die "pip install (deps) failed" 2
log "installing local_autopilot package (editable)"
"$VENV/bin/pip" install --quiet -e . || die "pip install -e . failed" 2

# --- 4. Initialise ~/.context-dna -------------------------------------------
mkdir -p "$CONTEXT_DNA_DIR" || die "cannot create $CONTEXT_DNA_DIR" 3

if [ ! -f "$DB_PATH" ]; then
    log "initialising complexity_vectors.db at $DB_PATH"
    if [ -f "$REPO_DIR/seeds/complexity_vectors.sql" ]; then
        sqlite3 "$DB_PATH" < "$REPO_DIR/seeds/complexity_vectors.sql" \
            || die "sqlite3 seed failed" 3
        log "seeded $(sqlite3 "$DB_PATH" 'SELECT count(*) FROM complexity_vectors') vectors"
    else
        warn "seeds/complexity_vectors.sql missing — creating empty DB"
        sqlite3 "$DB_PATH" \
            "CREATE TABLE IF NOT EXISTS complexity_vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vector_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                signal_keywords TEXT NOT NULL,
                risk_score REAL DEFAULT 5.0,
                drift_ranking_score REAL DEFAULT 0.0,
                current_alert_level TEXT DEFAULT 'none',
                last_triggered_at TEXT,
                trigger_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );"
    fi
else
    log "$DB_PATH exists (skipping seed — preserving your data)"
fi

if [ ! -f "$STATE_PATH" ]; then
    log "initialising autopilot_state.json at $STATE_PATH (mode=off)"
    NOW="$(date -u +'%Y-%m-%dT%H:%M:%S.000000+00:00')"
    cat > "$STATE_PATH" <<JSON
{
  "version": 1,
  "mode": "off",
  "set_by": "user",
  "set_at": "$NOW",
  "temporary_until": null,
  "temporary_reason": null,
  "user_lock": false,
  "transition_history": []
}
JSON
else
    log "$STATE_PATH exists (skipping init)"
fi

# --- 5. Daemon templates ------------------------------------------------------
if [ -d "$REPO_DIR/daemons" ]; then
    log "daemon templates present at $REPO_DIR/daemons/ (not auto-enabled)"
    log "  macOS:   launchctl bootstrap gui/\$(id -u) $REPO_DIR/daemons/<plist>"
    log "  Linux:   systemctl --user enable --now $REPO_DIR/daemons/<unit>"
fi

# --- 6. Smoke test ------------------------------------------------------------
log "running pytest smoke check"
# Capture pytest's REAL exit code despite the `tee` pipeline.
# Without `pipefail`, `cmd | tee` returns tee's exit (always 0) and a
# failing pytest silently passes the install. That's the bug F's agent
# misread as a "flaky test" — install was masking real failures.
set -o pipefail
"$VENV/bin/python3" -m pytest tests/ -q --no-header 2>&1 | tee /tmp/local-autopilot-install-pytest.log
PYTEST_RC="${PIPESTATUS[0]}"
set +o pipefail

PASS_COUNT="$(grep -oE '[0-9]+ passed' /tmp/local-autopilot-install-pytest.log | head -1 | grep -oE '[0-9]+' || echo 0)"
if [ "$PYTEST_RC" -ne 0 ]; then
    die "pytest smoke check failed (exit $PYTEST_RC, $PASS_COUNT passed) — see /tmp/local-autopilot-install-pytest.log" 4
fi
if [ "${PASS_COUNT:-0}" -lt 80 ]; then
    die "pytest smoke check produced only $PASS_COUNT passing tests (need ≥80)" 4
fi
log "pytest smoke check green ($PASS_COUNT passed)"

# --- 7. Preflight: verify a working LLM provider + runtime deps -------------
log "running scripts/preflight.sh --quiet"
if bash "$REPO_DIR/scripts/preflight.sh" --quiet; then
    log "preflight green — all runtime dependencies satisfied"
else
    PREFLIGHT_RC=$?
    if [ "${LOCAL_AUTOPILOT_INSTALL_LENIENT:-0}" = "1" ]; then
        warn "preflight reported $PREFLIGHT_RC failing check(s), but LOCAL_AUTOPILOT_INSTALL_LENIENT=1 is set — continuing"
        warn "re-run \`bash scripts/preflight.sh\` after configuring API keys / MLX to see the dashboard"
    else
        printf '[install][error] preflight reported %s failing check(s) — Local Autopilot has no working LLM provider.\n' "$PREFLIGHT_RC" >&2
        printf '[install][error] Run \`bash scripts/preflight.sh\` to see the full dashboard and fix the missing deps.\n' >&2
        printf '[install][error] To install anyway (e.g. CI / before keys are set), re-run with LOCAL_AUTOPILOT_INSTALL_LENIENT=1.\n' >&2
        exit 5
    fi
fi

# --- 8. Next steps -----------------------------------------------------------
cat <<NEXT

  Local Autopilot installed.

  Daily commands:
    .venv/bin/autopilot status                  # current mode + last 5 transitions
    .venv/bin/autopilot on                      # turn autopilot on (permanent — only you can turn it off)
    .venv/bin/autopilot off                     # turn autopilot off
    .venv/bin/autopilot temp "<reason>"         # temporary elevation (timer-bound)

  Run a single cycle (dry-run — no LLM calls):
    .venv/bin/python3 -m local_autopilot.tools.archloop_runner --dry-run --cycles 1

  Run a real cycle (needs MLX or DeepSeek configured — see README.md):
    .venv/bin/python3 -m local_autopilot.tools.archloop_runner --cycles 5 --cost-cap-usd 1.0

  Re-run this installer anytime — it is idempotent.

NEXT
