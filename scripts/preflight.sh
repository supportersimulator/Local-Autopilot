#!/usr/bin/env bash
# preflight.sh — verify Local Autopilot can actually run on this machine.
#
# Probes every external dependency the autopilot relies on. Prints a green/red
# dashboard. Exit code = number of failing checks (0 = all green).
#
# Use:
#   bash scripts/preflight.sh                # run all probes
#   bash scripts/preflight.sh --quiet        # only print failures
#   bash scripts/preflight.sh --json         # machine-readable

set -uo pipefail

QUIET=false
JSON=false
for arg in "$@"; do
    case "$arg" in
        --quiet) QUIET=true ;;
        --json)  JSON=true ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILS=0
declare -a RESULTS=()

if [ -t 1 ] && ! $JSON; then
    G=$(tput setaf 2); Y=$(tput setaf 3); R=$(tput setaf 1); D=$(tput dim); B=$(tput bold); X=$(tput sgr0)
else G=""; Y=""; R=""; D=""; B=""; X=""; fi

_check() {
    local label="$1" status="$2" detail="$3"
    case "$status" in
        ok)   icon="${G}✓${X}" ;;
        warn) icon="${Y}⚠${X}" ;;
        fail) icon="${R}✗${X}"; FAILS=$((FAILS + 1)) ;;
    esac
    RESULTS+=("$status|$label|$detail")
    if $JSON; then return; fi
    if $QUIET && [ "$status" = "ok" ]; then return; fi
    printf "  %s %-45s %s\n" "$icon" "$label" "${D}$detail${X}"
}

$JSON || echo ""
$JSON || echo "${B}Local Autopilot — preflight${X}"
$JSON || echo ""

# ── 1. Python ────────────────────────────────────────────────────────────────
if command -v python3.12 >/dev/null; then
    _check "Python 3.12" ok "$(python3.12 --version 2>&1)"
elif command -v python3.13 >/dev/null; then
    _check "Python 3.13" ok "$(python3.13 --version 2>&1)"
else
    _check "Python 3.12 or 3.13" fail "install: brew install python@3.12"
fi

# ── 2. venv ──────────────────────────────────────────────────────────────────
if [ -x "$REPO_DIR/.venv/bin/python3" ]; then
    _check "venv" ok "$REPO_DIR/.venv"
else
    _check "venv" fail "missing — run 'bash install.sh'"
fi

# ── 3. Package importable ───────────────────────────────────────────────────
if [ -x "$REPO_DIR/.venv/bin/python3" ]; then
    if "$REPO_DIR/.venv/bin/python3" -c "import local_autopilot" 2>/dev/null; then
        _check "local_autopilot package" ok "importable from venv"
    else
        _check "local_autopilot package" fail "not installed — run 'pip install -e .'"
    fi
fi

# ── 4. Console scripts ──────────────────────────────────────────────────────
if [ -x "$REPO_DIR/.venv/bin/autopilot" ]; then
    _check "autopilot CLI" ok "$($REPO_DIR/.venv/bin/autopilot --help 2>&1 | head -1)"
else
    _check "autopilot CLI" fail "missing in .venv/bin"
fi

# ── 5. ~/.context-dna directory + DB + state ────────────────────────────────
DATA_DIR="${CONTEXT_DNA_DIR:-$HOME/.context-dna}"
DB="$DATA_DIR/complexity_vectors.db"
STATE="$DATA_DIR/autopilot_state.json"

if [ -d "$DATA_DIR" ]; then
    _check "~/.context-dna directory" ok "$DATA_DIR"
else
    _check "~/.context-dna directory" fail "missing — run 'bash install.sh'"
fi

if [ -f "$DB" ]; then
    count=$(sqlite3 "$DB" "SELECT count(*) FROM complexity_vectors" 2>/dev/null || echo 0)
    if [ "${count:-0}" -ge 1 ]; then
        _check "complexity_vectors.db" ok "$count vectors"
    else
        _check "complexity_vectors.db" warn "exists but empty (re-seed: sqlite3 ... < seeds/complexity_vectors.sql)"
    fi
else
    _check "complexity_vectors.db" fail "missing — run 'bash install.sh'"
fi

if [ -f "$STATE" ]; then
    mode=$(python3 -c "import json; print(json.load(open('$STATE')).get('mode','?'))" 2>/dev/null || echo "?")
    _check "autopilot_state.json" ok "mode=$mode"
else
    _check "autopilot_state.json" fail "missing — run 'bash install.sh'"
fi

# ── 6. LLM provider chain ──────────────────────────────────────────────────
# Probe each backend in priority order
mlx_ok=false; ds_ok=false; ai_ok=false
if curl -sf -m 3 http://127.0.0.1:5044/v1/models >/dev/null 2>&1; then
    mlx_ok=true
    model=$(curl -sf -m 3 http://127.0.0.1:5044/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',[{}])[0].get('id','?'))" 2>/dev/null)
    _check "MLX local LLM (:5044)" ok "$model"
else
    _check "MLX local LLM (:5044)" warn "not running — autopilot will use remote provider"
fi

if [ -n "${DEEPSEEK_API_KEY:-}" ] && [ "${DEEPSEEK_API_KEY:0:6}" != "YOUR_K" ]; then
    if curl -sf -m 5 -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
        https://api.deepseek.com/v1/models >/dev/null 2>&1; then
        ds_ok=true
        _check "DeepSeek API" ok "key valid"
    else
        _check "DeepSeek API" warn "DEEPSEEK_API_KEY set but probe failed"
    fi
else
    _check "DeepSeek API" warn "DEEPSEEK_API_KEY unset — fallback path"
fi

if [ -n "${OPENAI_API_KEY:-}" ] && [ "${OPENAI_API_KEY:0:6}" != "YOUR_K" ]; then
    if curl -sf -m 5 -H "Authorization: Bearer $OPENAI_API_KEY" \
        https://api.openai.com/v1/models >/dev/null 2>&1; then
        ai_ok=true
        _check "OpenAI API" ok "key valid"
    else
        _check "OpenAI API" warn "OPENAI_API_KEY set but probe failed"
    fi
else
    _check "OpenAI API" warn "OPENAI_API_KEY unset — fallback path"
fi

if ! $mlx_ok && ! $ds_ok && ! $ai_ok; then
    FAILS=$((FAILS + 1))
    _check "any working LLM provider" fail "no MLX, no DeepSeek key, no OpenAI key — real cycles will fail"
fi

# ── 7. Daemon ───────────────────────────────────────────────────────────────
if [ "$(uname -s)" = "Darwin" ]; then
    if [[ "$(launchctl list 2>/dev/null)" == *"com.localautopilot.tick"* ]]; then
        _check "launchd timer" ok "com.localautopilot.tick loaded"
    else
        _check "launchd timer" warn "not installed — run 'bash scripts/install-daemon-macos.sh'"
    fi
elif [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null; then
    if systemctl --user is-active local-autopilot-tick.timer >/dev/null 2>&1; then
        _check "systemd timer" ok "active"
    else
        _check "systemd timer" warn "not running — run 'bash scripts/install-daemon-linux.sh'"
    fi
fi

# ── 8. Cycle output dir ────────────────────────────────────────────────────
LOGS_DIR="$DATA_DIR/autopilot-logs"
if [ -d "$LOGS_DIR" ]; then
    cycle_count=$(ls "$LOGS_DIR" 2>/dev/null | grep -c "^cycle-" || echo 0)
    _check "cycle logs" ok "$cycle_count cycles recorded"
else
    _check "cycle logs" ok "no cycles yet (normal for fresh install)"
fi

# ── Output ──────────────────────────────────────────────────────────────────
if $JSON; then
    {
        echo "["
        first=true
        for r in "${RESULTS[@]}"; do
            IFS='|' read -r status label detail <<< "$r"
            $first && first=false || echo ","
            printf '  {"status":"%s","check":"%s","detail":"%s"}' "$status" "$label" "${detail//\"/\\\"}"
        done
        echo ""
        echo "]"
    }
else
    echo ""
    if [ "$FAILS" -eq 0 ]; then
        echo "  ${G}${B}✓ all critical checks passed${X}"
    else
        echo "  ${R}${B}✗ $FAILS critical check(s) failed${X}"
    fi
    echo ""
fi

exit "$FAILS"
