#!/usr/bin/env bash
# 3s-brainstorm.sh — Autonomous brainstorming wrapper.
#
# Q1 of P5's gap analysis: stand in for the operator at every clarifying-
# question gate of the superpowers brainstorming flow by routing each
# question to 3-surgeons (cardio / neuro / consensus) instead of stdin.
#
# Usage:
#   bash scripts/3s-brainstorm.sh "<topic>"
#   echo "<topic>" | bash scripts/3s-brainstorm.sh
#   bash scripts/3s-brainstorm.sh --dry-run "<topic>"
#   bash scripts/3s-brainstorm.sh --cost-cap 0.05 "<topic>"
#   bash scripts/3s-brainstorm.sh --open-ended "<topic>"   # adds anchor-bias check (Wave C C2)
#   bash scripts/3s-brainstorm.sh --no-fleet-brainstorm "<topic>"  # opt out of .fleet/brainstorm/ canonical write
#   bash scripts/3s-brainstorm.sh --no-parallel "<topic>"  # sequential fallback (R5 A/B comparison)
#
# Counter-probe (SS1 sycophancy gate): default ON via
#   CONTEXT_DNA_CONSENSUS_COUNTER_PROBE=on
# (WW5 / ZZ3 ship). The consensus gate auto-probes the negation of the
# recommendation claim and demotes sycophantic verdicts. Caller can override
# with `CONTEXT_DNA_CONSENSUS_COUNTER_PROBE=off bash scripts/3s-brainstorm.sh ...`.
#
# Output:
#   /tmp/3s-brainstorm-<UTC-stamp>.md  (full brainstorm, idempotent — new file each run)
#   stdout: same brainstorm
#   /tmp/3s-brainstorm.err             (per-call failures, ZSF — never silenced)
#   .fleet/brainstorm/<date>-3s-<slug>.md (+ .json)  (canonical fleet artefact, on by
#                                                    default; opt out with --no-fleet-brainstorm)
#
# ZSF: every 3s invocation captures stdout+stderr+exit. Failures append to
# the .err log AND increment a counter in the output header so partial
# brainstorms are visibly partial, never falsely complete.

set -u  # NOTE: deliberately not -e — we route failures, never abort silently
set -o pipefail

# ----- args -----
DRY_RUN=0
COST_CAP="0.01"
OPEN_ENDED=0
# JJ4 ship 1 (HH5 #1): write a canonical artefact to .fleet/brainstorm/ so
# the result rides P7 git push to the rest of the fleet.
# Local Autopilot vendoring note: the fleet artefact writer
# (`memory.fleet_brainstorm_writer`) is superrepo-only — there is no
# multi-machine fleet here. So we default to OFF and let users opt back in
# with --out-fleet-brainstorm + AUTOPILOT_FLEET_BRAINSTORM_DIR if they
# explicitly want the canonical write.
FLEET_BRAINSTORM=0
# R5 (VV1): parallel mode is DEFAULT (58s → ~10-15s wall-clock). --no-parallel
# falls back to the original sequential flow for A/B comparison and as a
# safety hatch when parallel scheduling misbehaves on a host.
PARALLEL=1
TOPIC=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --cost-cap) COST_CAP="${2:-0.01}"; shift 2 ;;
    --open-ended) OPEN_ENDED=1; shift ;;
    --out-fleet-brainstorm) FLEET_BRAINSTORM=1; shift ;;
    --no-fleet-brainstorm) FLEET_BRAINSTORM=0; shift ;;
    --no-parallel) PARALLEL=0; shift ;;
    --parallel)    PARALLEL=1; shift ;;
    --help|-h)
      sed -n '2,24p' "$0"; exit 0 ;;
    *) TOPIC="$1"; shift ;;
  esac
done

# stdin fallback when no topic arg
if [[ -z "$TOPIC" ]] && [[ ! -t 0 ]]; then
  TOPIC="$(cat)"
fi
TOPIC="${TOPIC%%$'\n'*}"  # first line only

if [[ -z "$TOPIC" ]]; then
  echo "ERROR: no topic provided. Pass as \$1 or via stdin." >&2
  echo "Usage: bash scripts/3s-brainstorm.sh [--dry-run] [--cost-cap 0.01] \"<topic>\"" >&2
  exit 2
fi

# ----- env / auth -----
# DeepSeek key — try keychain if not already in env (mac-only fallback).
if [[ -z "${DEEPSEEK_API_KEY:-}" ]] && command -v security >/dev/null 2>&1; then
  DEEPSEEK_API_KEY="$(security find-generic-password -s fleet-nerve -a Context_DNA_Deepseek -w 2>/dev/null || true)"
  export DEEPSEEK_API_KEY
fi
export Context_DNA_Deepseek="${Context_DNA_Deepseek:-${DEEPSEEK_API_KEY:-}}"

# ZZ3 / WW5 ship: counter-probe sycophancy gate ON by default for the
# autonomous brainstorm flow. The recommendation gate runs `3s consensus`,
# which honours this env var (see three_surgeons.core.counter_probe.is_enabled)
# and probes the negation of the claim. Sycophantic verdicts are demoted to
# effective_score=0.0 and surfaced in stderr. Caller can opt out by exporting
# CONTEXT_DNA_CONSENSUS_COUNTER_PROBE=off before invoking the wrapper.
export CONTEXT_DNA_CONSENSUS_COUNTER_PROBE="${CONTEXT_DNA_CONSENSUS_COUNTER_PROBE:-on}"

# 3s binary — order of preference:
#   1. $AUTOPILOT_3S_CLI env var (explicit override, set by archloop_runner.py)
#   2. `command -v 3s` (PATH lookup, works after `pip install three-surgeons`)
#   3. Common system-wide install location (/usr/local/bin/3s)
#   4. The legacy Claude Code plugin cache (Aaron's superrepo fallback)
# If all four fail and we're not in dry-run, exit with a named error.
THREE_S="${AUTOPILOT_3S_CLI:-}"
if [[ -z "$THREE_S" ]]; then
  THREE_S="$(command -v 3s 2>/dev/null || true)"
fi
if [[ -z "$THREE_S" || ! -x "$THREE_S" ]]; then
  if [[ -x "/usr/local/bin/3s" ]]; then
    THREE_S="/usr/local/bin/3s"
  else
    THREE_S="/Users/aarontjomsland/.claude/plugins/cache/3-surgeons-marketplace/3-surgeons/1.0.0/.venv/bin/3s"
  fi
fi
if [[ ! -x "$THREE_S" ]] && [[ "$DRY_RUN" -eq 0 ]]; then
  echo "ERROR: 3s binary not found. Install via 'pip install three-surgeons'" \
       "or set AUTOPILOT_3S_CLI to an absolute path." >&2
  exit 3
fi

# ----- paths -----
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/3s-brainstorm-${STAMP}.md"
ERR_LOG="/tmp/3s-brainstorm.err"
TX_LOG="/tmp/3s-brainstorm-${STAMP}.transcript.txt"
# R5 (VV1) ZSF fix: ERR_COUNT and TOTAL_COST are mutated inside command
# substitutions ($(...)) which are subshells, so their increments were
# previously lost. We mirror each bump to per-run files; the parent reads
# the aggregate at render time. This makes ZSF observable AND survives the
# parallel/subshell boundary.
ERR_COUNT_FILE="/tmp/3s-brainstorm-${STAMP}.errcount"
COST_FILE="/tmp/3s-brainstorm-${STAMP}.costs"
: >"$TX_LOG"
: >"$ERR_COUNT_FILE"
: >"$COST_FILE"

# ----- counters / accumulator -----
ERR_COUNT=0
TOTAL_COST="0.0000"
declare -a SECTIONS=()

# err_bump <n> -> increment the shared err counter file (subshell-safe ZSF).
err_bump() {
  local n="${1:-1}"
  printf '%s\n' "$n" >>"$ERR_COUNT_FILE"
}

# cost_bump <usd> -> append a cost line (subshell-safe accumulator).
cost_bump() {
  local c="${1:-0.0000}"
  printf '%s\n' "$c" >>"$COST_FILE"
}

# err_total -> echo the aggregated ERR_COUNT across all subshell writes.
err_total() {
  awk '{s+=$1} END {printf "%d", s+0}' "$ERR_COUNT_FILE" 2>/dev/null || echo 0
}

# cost_total -> echo the aggregated TOTAL_COST across all subshell writes.
cost_total() {
  awk '{s+=$1} END {printf "%.4f", s+0}' "$COST_FILE" 2>/dev/null || echo "0.0000"
}

# ----- KK1 fix knobs (per-call timeout + retry; bash-native, no `timeout` binary) -----
# Timeouts (seconds): cardio uses DeepSeek (network), neuro uses Qwen3 (GPU lock contention).
# Consensus invokes both — use the larger budget.
TIMEOUT_REMOTE_S="${BRAINSTORM_TIMEOUT_REMOTE_S:-60}"
TIMEOUT_LOCAL_S="${BRAINSTORM_TIMEOUT_LOCAL_S:-90}"
TIMEOUT_CONSENSUS_S="${BRAINSTORM_TIMEOUT_CONSENSUS_S:-120}"
# Retries: total attempts = MAX_ATTEMPTS (1 = no retry, 3 = original + 2 retries).
MAX_ATTEMPTS="${BRAINSTORM_MAX_ATTEMPTS:-3}"
RETRY_SLEEP_S="${BRAINSTORM_RETRY_SLEEP_S:-2}"

# ----- helpers -----

# route_timeout_s <route> -> echoes seconds budget for that route
route_timeout_s() {
  case "$1" in
    consensus)               echo "$TIMEOUT_CONSENSUS_S" ;;
    ask-local)               echo "$TIMEOUT_LOCAL_S" ;;
    ask-remote|*)            echo "$TIMEOUT_REMOTE_S" ;;
  esac
}

# soft_fail_in_raw <raw-text> -> exits 0 (true) if the text indicates a
# silent surgeon failure even when 3s exit code is 0:
#   - "Failed to parse consensus JSON" warning leaked from cross_exam.py
#   - Neurologist or Cardiologist marked "unavailable (confidence=0.00)"
#     (Qwen3 GPU lock / DeepSeek network blip — retryable)
soft_fail_in_raw() {
  local raw="$1"
  if grep -q "Failed to parse consensus JSON" <<<"$raw"; then return 0; fi
  if grep -Eq "(Neurologist|Cardiologist):[[:space:]]+unavailable[[:space:]]+\(confidence=0\.00\)" <<<"$raw"; then return 0; fi
  return 1
}

# run_with_timeout <seconds> <cmd...> -> runs cmd, kills it after N seconds.
# Captures combined stdout+stderr to stdout. Echoes the captured output and
# returns the cmd's exit code, or 124 if timed out (matches GNU `timeout`).
# Pure bash — macOS has no `timeout` binary.
run_with_timeout() {
  local secs="$1"; shift
  local tmp_out tmp_rc cmd_pid watcher_pid rc=0
  tmp_out="$(mktemp -t 3s-brainstorm-out.XXXXXX)"
  tmp_rc="$(mktemp -t 3s-brainstorm-rc.XXXXXX)"

  # Run cmd in background, write its rc to tmp_rc.
  ( "$@" >"$tmp_out" 2>&1; echo "$?" >"$tmp_rc" ) &
  cmd_pid=$!

  # Watcher: sleeps then kills cmd if still alive.
  ( sleep "$secs"; kill -0 "$cmd_pid" 2>/dev/null && kill -TERM "$cmd_pid" 2>/dev/null; sleep 1; kill -0 "$cmd_pid" 2>/dev/null && kill -KILL "$cmd_pid" 2>/dev/null ) &
  watcher_pid=$!

  if wait "$cmd_pid" 2>/dev/null; then
    rc="$(cat "$tmp_rc" 2>/dev/null || echo 1)"
  else
    # cmd exited (possibly via signal); fall back to recorded rc if any.
    rc="$(cat "$tmp_rc" 2>/dev/null || echo 124)"
  fi

  # If watcher already fired, mark timeout.
  if ! kill -0 "$watcher_pid" 2>/dev/null; then
    if [[ "$rc" != "0" ]]; then rc=124; fi
  fi
  # R5 (VV1) fix: bash defers SIGTERM to a subshell currently running a
  # foreground `sleep`, so `kill $watcher_pid` doesn't return promptly and
  # `wait $watcher_pid` blocks for the full timeout. Kill the watcher's
  # `sleep` child first via `pkill -P` so the watcher subshell can exit on
  # its own; then `wait` returns immediately. ZSF: errors from these kills
  # are non-fatal (watcher may already be gone).
  pkill -P "$watcher_pid" 2>/dev/null || true
  kill "$watcher_pid" 2>/dev/null || true
  wait "$watcher_pid" 2>/dev/null || true

  cat "$tmp_out"
  rm -f "$tmp_out" "$tmp_rc"
  return "$rc"
}

# invoke_3s_with_retry <route> <prompt> [retry-counter-file] -> echoes raw combined output,
# returns final rc.
# Retries up to MAX_ATTEMPTS on:
#   - non-zero rc (process error)
#   - rc 124 (our timeout sentinel)
#   - soft_fail_in_raw (parse warning / unavailable surgeon under load)
# Every retry/timeout/soft-fail appends a row to $ERR_LOG (ZSF — never silenced).
# If a retry-counter-file path is provided, the count of failed attempts is written there
# so harvest_parallel (which can't see this subshell's ERR_COUNT) can roll it into the parent.
invoke_3s_with_retry() {
  local route="$1" prompt="$2" rc_file="${3:-}"
  local secs raw rc attempt=1 max="$MAX_ATTEMPTS" retries=0
  secs="$(route_timeout_s "$route")"

  while :; do
    raw="$(run_with_timeout "$secs" "$THREE_S" "$route" "$prompt")"; rc=$?

    if [[ $rc -eq 0 ]] && ! soft_fail_in_raw "$raw"; then
      [[ -n "$rc_file" ]] && printf '%d\n' "$retries" >"$rc_file"
      printf '%s' "$raw"
      return 0
    fi

    # Classify failure for the err log.
    local reason="rc=$rc"
    if [[ $rc -eq 124 ]]; then
      reason="timeout-${secs}s"
    elif [[ $rc -eq 0 ]]; then
      if grep -q "Failed to parse consensus JSON" <<<"$raw"; then
        reason="soft-fail-parse-json"
      else
        reason="soft-fail-surgeon-unavailable"
      fi
    fi

    retries=$((retries+1))
    # ZSF: append to the subshell-safe counter file. The legacy shell-var
    # ERR_COUNT++ bump is left intentionally absent here — all paths in this
    # script run inside $(...) command substitution (subshell), so the shell-var
    # increment was lost. The file is the source of truth at render time.
    err_bump 1
    printf '%s\troute=%s\tattempt=%d/%d\treason=%s\tprompt=%s\n' \
      "$(date -u +%FT%TZ)" "$route" "$attempt" "$max" "$reason" "$prompt" >>"$ERR_LOG"

    if [[ $attempt -ge $max ]]; then
      [[ -n "$rc_file" ]] && printf '%d\n' "$retries" >"$rc_file"
      # Final attempt failed — surface the last raw output to the caller; rc is the original rc
      # (or 124 for timeout). Caller decides whether to render fallback text.
      printf '%s' "$raw"
      return "$rc"
    fi

    attempt=$((attempt+1))
    sleep "$RETRY_SLEEP_S"
  done
}

# extract_cost <stdout-text> -> "0.0005" (or "0.0000")
extract_cost() {
  python3 -c '
import sys, re
text = sys.stdin.read()
m = re.search(r"Cost:\s*\$([0-9]+\.[0-9]+)", text)
print(m.group(1) if m else "0.0000")
' <<<"$1"
}

# extract_body <stdout-text> -> drops trailing "Cost: $..." line(s)
extract_body() {
  python3 -c '
import sys, re
lines = sys.stdin.read().splitlines()
# drop trailing blank + cost lines
while lines and (not lines[-1].strip() or re.match(r"^\s*Cost:\s*\$", lines[-1]) or re.match(r"^\s*Total cost:\s*\$", lines[-1])):
    lines.pop()
print("\n".join(lines))
' <<<"$1"
}

# add_cost <a> <b> -> printf "%.4f"
add_cost() {
  python3 -c "print(f'{float(\"$1\") + float(\"$2\"):.4f}')"
}

# cost_over_cap <total> <cap> -> exits 0 if total > cap
cost_over_cap() {
  python3 -c "import sys; sys.exit(0 if float('$1') > float('$2') else 1)"
}

# call_3s <route> <prompt-or-claim>  -> echoes body, sets ERR_COUNT/TOTAL_COST
# route ∈ ask-remote | ask-local | consensus
# Used for SEQUENTIAL calls (Options, Recommendation) — mutates parent vars.
call_3s() {
  local route="$1" prompt="$2" raw rc cost body current_total
  printf '\n>>> [%s] %s\n' "$route" "$prompt" >>"$TX_LOG"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[DRY] %s -- %q\n' "$route" "$prompt"
    return 0
  fi

  # cost cap pre-check — read fresh aggregate from COST_FILE (ZSF: prior sequential
  # gates wrote into the same file via subshell, parent shell var is stale).
  current_total="$(cost_total)"
  if cost_over_cap "$current_total" "$COST_CAP"; then
    err_bump 1
    printf '%s\tcost-cap-hit\ttotal=%s\tcap=%s\troute=%s\n' \
      "$(date -u +%FT%TZ)" "$current_total" "$COST_CAP" "$route" >>"$ERR_LOG"
    echo "(skipped: cost cap \$${COST_CAP} reached, total \$${current_total})"
    return 0
  fi

  raw="$(invoke_3s_with_retry "$route" "$prompt")"; rc=$?
  printf '%s\n' "$raw" >>"$TX_LOG"

  if [[ $rc -ne 0 ]]; then
    printf '%s\troute=%s\trc=%d\tterminal\tprompt=%s\n%s\n---\n' \
      "$(date -u +%FT%TZ)" "$route" "$rc" "$prompt" "$raw" >>"$ERR_LOG"
    echo "(3s $route failed rc=$rc after $MAX_ATTEMPTS attempts — see $ERR_LOG)"
    return 0
  fi

  cost="$(extract_cost "$raw")"
  cost_bump "$cost"
  body="$(extract_body "$raw")"
  printf '%s' "$body"
}

# call_3s_parallel <gate-id> <route> <prompt> -> writes:
#   /tmp/3s-brainstorm-${STAMP}.${gate-id}.body  (clean answer body)
#   /tmp/3s-brainstorm-${STAMP}.${gate-id}.cost  ("0.0005" or "0.0000")
#   /tmp/3s-brainstorm-${STAMP}.${gate-id}.rc    ("0" success, non-zero failure)
#   /tmp/3s-brainstorm-${STAMP}.${gate-id}.raw   (full stdout+stderr, for transcript)
# ZSF: never silenced — failures land in .rc + .raw, harvested by caller after wait.
# Note: does NOT mutate parent ERR_COUNT/TOTAL_COST (subshell-safe).
call_3s_parallel() {
  local gate_id="$1" route="$2" prompt="$3"
  local base="/tmp/3s-brainstorm-${STAMP}.${gate_id}"
  local raw rc cost body

  # KK1 fix: full timeout + retry pipeline. Retries are tallied in ${base}.retries
  # so harvest_parallel can roll them into the parent ERR_COUNT (subshell-safe ZSF).
  : >"${base}.retries"
  raw="$(invoke_3s_with_retry "$route" "$prompt" "${base}.retries")"; rc=$?
  printf '%s' "$raw" >"${base}.raw"
  printf '%s\n' "$rc" >"${base}.rc"

  if [[ $rc -ne 0 ]]; then
    : >"${base}.body"  # empty body
    printf '0.0000\n' >"${base}.cost"
    return 0
  fi

  cost="$(extract_cost "$raw")"
  body="$(extract_body "$raw")"
  printf '%s' "$body" >"${base}.body"
  printf '%s\n' "$cost" >"${base}.cost"
}

# harvest_parallel <gate-id> <route> <prompt> -> echoes body, mutates ERR_COUNT/TOTAL_COST/TX_LOG
# Called sequentially after `wait` to deterministically tally results from parallel branch files.
harvest_parallel() {
  local gate_id="$1" route="$2" prompt="$3"
  local base="/tmp/3s-brainstorm-${STAMP}.${gate_id}"
  local rc cost body raw

  printf '\n>>> [%s] %s\n' "$route" "$prompt" >>"$TX_LOG"
  if [[ -r "${base}.raw" ]]; then
    cat "${base}.raw" >>"$TX_LOG"
    printf '\n' >>"$TX_LOG"
  fi

  if [[ ! -r "${base}.rc" ]]; then
    err_bump 1
    printf '%s\troute=%s\tgate=%s\tmissing-rc-file\n' \
      "$(date -u +%FT%TZ)" "$route" "$gate_id" >>"$ERR_LOG"
    echo "(3s $route failed: parallel branch produced no rc file — see $ERR_LOG)"
    return 0
  fi

  # KK1 fix: roll subshell retry count into parent ERR_COUNT (ZSF — every retry observable).
  # invoke_3s_with_retry already calls err_bump per retry, so we DO NOT double-count
  # retries here; we only care about the terminal rc.
  rc="$(cat "${base}.rc")"
  if [[ "$rc" != "0" ]]; then
    err_bump 1
    raw="$(cat "${base}.raw" 2>/dev/null || echo '(no raw)')"
    printf '%s\troute=%s\tgate=%s\trc=%s\tterminal\tprompt=%s\n%s\n---\n' \
      "$(date -u +%FT%TZ)" "$route" "$gate_id" "$rc" "$prompt" "$raw" >>"$ERR_LOG"
    echo "(3s $route failed rc=$rc after $MAX_ATTEMPTS attempts — see $ERR_LOG)"
    return 0
  fi

  cost="$(cat "${base}.cost" 2>/dev/null || echo '0.0000')"
  cost_bump "$cost"
  body="$(cat "${base}.body" 2>/dev/null || true)"
  printf '%s' "$body"
}

# ----- the 5 clarifying gates -----
# Routing policy (Q1 of P5's gap):
#   factual / external / scoping  -> ask-remote (cardio)
#   code-context / pattern recog  -> ask-local  (neuro)
#   commitment / "is this right?" -> consensus  (both, weighted)

run_gates() {
  local goal_q constraints_q mvp_q risk_q user_q rec_claim

  goal_q="Brainstorming topic: \"${TOPIC}\". In 4-6 sentences, state the primary goal — what success looks like, the measurable outcome, and the one user-visible deliverable. No filler."
  constraints_q="Brainstorming topic: \"${TOPIC}\". List the hard constraints in priority order — budget, time, dependencies, infra, security, scope-out items. 5-7 bullets, terse."
  mvp_q="Brainstorming topic: \"${TOPIC}\". Describe the smallest faithful version: minimum components that still solve the user-visible goal, what is deferred, what is mocked. 4-6 bullets."
  risk_q="Brainstorming topic: \"${TOPIC}\". Identify the 3 riskiest assumptions — the ones that, if wrong, invalidate the design. For each: assumption / why-risky / cheap-test. Code-aware perspective."
  user_q="Brainstorming topic: \"${TOPIC}\". Who or what is the user? Single concrete persona or system, the trigger that makes them invoke this, and what they do with the output. 4-5 sentences."
  # NOTE: The rec_claim used to be a hardcoded MVP template — A5 audit 2026-05-13
  # found it was topic-independent (same canned line across 8+ different topics),
  # producing structurally sycophantic "consensus". Now the recommendation is
  # asked as an open question to both surgeons, then synthesized in Stage B.
  # rec_claim is kept as a fallback ONLY if Stage B fails entirely.
  rec_claim="For \"${TOPIC}\", recommend the single highest-leverage first move and explain in one sentence why it beats the alternatives. Disagree with prior surgeon answers if warranted — do not echo."
  REC_CLAIM="$rec_claim"

  # Wall-clock timers (printed at end for speedup ratio observability).
  GATES_T0=$(date +%s)

  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[DRY] ask-remote -- %q\n' "$goal_q"
    printf '[DRY] ask-remote -- %q\n' "$constraints_q"
    printf '[DRY] ask-local  -- %q\n' "$mvp_q"
    printf '[DRY] ask-local  -- %q\n' "$risk_q"
    printf '[DRY] ask-remote -- %q\n' "$user_q"
    printf '[DRY] consensus  -- %q\n' "$rec_claim"
    printf '[DRY] ask-remote -- "<options synthesized from goal+constraints answers>"\n'
    if [[ "$PARALLEL" -eq 0 ]]; then
      echo "[DRY] mode=sequential (--no-parallel)" >&2
    else
      echo "[DRY] mode=parallel (stage A: 6-wide fan-out + stage B: options)" >&2
    fi
    return 0
  fi

  # Cost cap pre-check (ZSF: surface skip explicitly).
  if cost_over_cap "$(cost_total)" "$COST_CAP"; then
    err_bump 7
    printf '%s\tcost-cap-hit-pre-stage-a\ttotal=%s\tcap=%s\n' \
      "$(date -u +%FT%TZ)" "$(cost_total)" "$COST_CAP" >>"$ERR_LOG"
    local skip_msg="(skipped: cost cap \$${COST_CAP} reached)"
    GOAL_A="$skip_msg"; CONSTRAINTS_A="$skip_msg"; MVP_A="$skip_msg"
    RISK_A="$skip_msg"; USER_A="$skip_msg"; OPTIONS_A="$skip_msg"; REC_A="$skip_msg"
    GATES_T1=$(date +%s)
    GATES_TOTAL_S=$((GATES_T1 - GATES_T0))
    return 0
  fi

  if [[ "$PARALLEL" -eq 1 ]]; then
    # ---------------- Parallel mode (R5 default) ----------------
    # Stage A: 6-wide fan-out — goal, constraints, mvp, risk, user, consensus.
    # All six are independent of each other (consensus' rec_claim is static).
    # Stage B: options (depends on Stage A's goal+constraints bodies).
    echo ">> [parallel] Stage A: launching 6 gates (goal/constraints/mvp/risks/user/consensus)..." >&2
    local _ta0 _ta1
    _ta0=$(date +%s)
    call_3s_parallel goal        ask-remote "$goal_q"        &
    local pid_goal=$!
    call_3s_parallel constraints ask-remote "$constraints_q" &
    local pid_constraints=$!
    call_3s_parallel mvp         ask-local  "$mvp_q"         &
    local pid_mvp=$!
    call_3s_parallel risk        ask-local  "$risk_q"        &
    local pid_risk=$!
    call_3s_parallel user        ask-remote "$user_q"        &
    local pid_user=$!
    call_3s_parallel rec         consensus  "$rec_claim"     &
    local pid_rec=$!

    wait "$pid_goal"        || true
    wait "$pid_constraints" || true
    wait "$pid_mvp"         || true
    wait "$pid_risk"        || true
    wait "$pid_user"        || true
    wait "$pid_rec"         || true
    _ta1=$(date +%s)
    STAGE_A_S=$((_ta1 - _ta0))
    echo ">> [parallel] Stage A complete in ${STAGE_A_S}s" >&2

    GOAL_A="$(harvest_parallel goal        ask-remote "$goal_q")"
    CONSTRAINTS_A="$(harvest_parallel constraints ask-remote "$constraints_q")"
    MVP_A="$(harvest_parallel mvp         ask-local  "$mvp_q")"
    RISK_A="$(harvest_parallel risk        ask-local  "$risk_q")"
    USER_A="$(harvest_parallel user        ask-remote "$user_q")"
    REC_A="$(harvest_parallel rec          consensus  "$rec_claim")"

    # Stage B: options uses GOAL_A + CONSTRAINTS_A bodies harvested above.
    local options_q _tb0 _tb1
    options_q="Brainstorming topic: \"${TOPIC}\". Goal: $(printf '%s' "$GOAL_A" | head -c 600). Constraints: $(printf '%s' "$CONSTRAINTS_A" | head -c 400). Propose 3-5 distinct approaches. For each: name, 2-line summary, top tradeoff, est. effort (S/M/L). No recommendation yet."
    echo ">> [parallel] Stage B: options (cardio/ask-remote)..." >&2
    _tb0=$(date +%s)
    OPTIONS_A="$(call_3s ask-remote "$options_q")"
    _tb1=$(date +%s)
    STAGE_B_S=$((_tb1 - _tb0))
    echo ">> [parallel] Stage B complete in ${STAGE_B_S}s" >&2
  else
    # ---------------- Sequential mode (--no-parallel) ----------------
    echo ">> [sequential] gate 1/7: goal (cardio/ask-remote)..." >&2
    GOAL_A="$(call_3s ask-remote "$goal_q")"
    echo ">> [sequential] gate 2/7: constraints (cardio/ask-remote)..." >&2
    CONSTRAINTS_A="$(call_3s ask-remote "$constraints_q")"
    echo ">> [sequential] gate 3/7: mvp (neuro/ask-local)..." >&2
    MVP_A="$(call_3s ask-local "$mvp_q")"
    echo ">> [sequential] gate 4/7: risk (neuro/ask-local)..." >&2
    RISK_A="$(call_3s ask-local "$risk_q")"
    echo ">> [sequential] gate 5/7: user (cardio/ask-remote)..." >&2
    USER_A="$(call_3s ask-remote "$user_q")"

    local options_q
    options_q="Brainstorming topic: \"${TOPIC}\". Goal: $(printf '%s' "$GOAL_A" | head -c 600). Constraints: $(printf '%s' "$CONSTRAINTS_A" | head -c 400). Propose 3-5 distinct approaches. For each: name, 2-line summary, top tradeoff, est. effort (S/M/L). No recommendation yet."
    echo ">> [sequential] gate 6/7: options (cardio/ask-remote)..." >&2
    OPTIONS_A="$(call_3s ask-remote "$options_q")"

    echo ">> [sequential] gate 7/7: recommendation (consensus)..." >&2
    REC_A="$(call_3s consensus "$rec_claim")"
  fi

  GATES_T1=$(date +%s)
  GATES_TOTAL_S=$((GATES_T1 - GATES_T0))

  # Optional anchor-bias / missing-categories probe (Wave C C2 — open-ended).
  # Default OFF for backward compat; ON adds a 2-call interrogation of the
  # recommendation itself — what did Atlas/operator NOT enumerate?
  ANCHOR_NEURO_A=""
  ANCHOR_CARDIO_A=""
  if [[ "$OPEN_ENDED" -eq 1 ]]; then
    local options_summary anchor_neuro_q anchor_cardio_q
    options_summary="$(printf '%s' "$OPTIONS_A" | head -c 1200)"
    anchor_neuro_q="Brainstorming topic: \"${TOPIC}\". The surgeons enumerated these options: $(printf '%s' "$options_summary"). PATTERN-RECOGNITION TASK: list 2-3 candidate priorities the operator may need to weigh that are NOT in this list — categories the framing missed (e.g., observability, deployability, security tooling, team/org concerns, docs, migration, kill-switch, dogfooding, or anything else that pattern-matches). For each: name + 1-line why-it-matters. No filler, no agreement with the existing list."
    anchor_cardio_q="Brainstorming topic: \"${TOPIC}\". Outside-view check: an operator (Atlas) just received this recommendation: \"${rec_claim}\" — and these options: $(printf '%s' "$options_summary"). What is the operator most likely OVERLOOKING? Name 2-3 specific blind spots — things outside the enumerated frame that experienced practitioners would flag. Each: blind-spot / why / cheap-detection. Adversarial, not affirming."
    echo ">> [open-ended] anchor-bias check (neuro pattern + cardio outside view)..." >&2
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[DRY] ask-local  -- %q\n' "$anchor_neuro_q"
      printf '[DRY] ask-remote -- %q\n' "$anchor_cardio_q"
    elif [[ "$PARALLEL" -eq 1 ]]; then
      # Parallelize the two anchor probes — independent (different routes).
      call_3s_parallel anchor_neuro  ask-local  "$anchor_neuro_q"  &
      local pid_an=$!
      call_3s_parallel anchor_cardio ask-remote "$anchor_cardio_q" &
      local pid_ac=$!
      wait "$pid_an" || true
      wait "$pid_ac" || true
      ANCHOR_NEURO_A="$(harvest_parallel  anchor_neuro  ask-local  "$anchor_neuro_q")"
      ANCHOR_CARDIO_A="$(harvest_parallel anchor_cardio ask-remote "$anchor_cardio_q")"
    else
      # Sequential fallback for --no-parallel A/B comparison.
      ANCHOR_NEURO_A="$(call_3s ask-local "$anchor_neuro_q")"
      ANCHOR_CARDIO_A="$(call_3s ask-remote "$anchor_cardio_q")"
    fi
  fi
}

# ----- render -----
render() {
  # ZSF: aggregate ERR_COUNT and TOTAL_COST from per-run files (subshell-safe).
  # The legacy shell vars only see writes that happened in the parent shell;
  # the files capture every bump from every $(...) command substitution too.
  ERR_COUNT="$(err_total)"
  TOTAL_COST="$(cost_total)"

  local status="complete"
  if [[ "$ERR_COUNT" -gt 0 ]]; then status="PARTIAL ($ERR_COUNT errors — see $ERR_LOG)"; fi

  local mode_label="parallel (stage A: 6-wide + stage B: options)"
  if [[ "$PARALLEL" -eq 0 ]]; then mode_label="sequential (7 gates)"; fi
  local timing_line=""
  if [[ -n "${GATES_TOTAL_S:-}" ]]; then
    if [[ "$PARALLEL" -eq 1 ]] && [[ -n "${STAGE_A_S:-}" ]] && [[ -n "${STAGE_B_S:-}" ]]; then
      timing_line="**Wall-clock:** ${GATES_TOTAL_S}s (stage A: ${STAGE_A_S}s · stage B: ${STAGE_B_S}s)"
    else
      timing_line="**Wall-clock:** ${GATES_TOTAL_S}s"
    fi
  fi

  cat <<EOF
# Brainstorm: ${TOPIC}

**Generated:** ${STAMP}
**Status:** ${status}
**Total cost:** \$${TOTAL_COST} (cap \$${COST_CAP})
**Mode:** ${mode_label}
${timing_line}
**Transcript:** ${TX_LOG}
**Counter-probe:** ${CONTEXT_DNA_CONSENSUS_COUNTER_PROBE:-on} (sycophancy gate on \`3s consensus\`)
**Routing:** goal/constraints/user/options → cardio · mvp/risks → neuro · recommendation → consensus

---

## Goal
${GOAL_A:-(no answer)}

## Constraints
${CONSTRAINTS_A:-(no answer)}

## MVP — Smallest Faithful Version
${MVP_A:-(no answer)}

## Risks — Top Assumptions
${RISK_A:-(no answer)}

## User
${USER_A:-(no answer)}

## Options
${OPTIONS_A:-(no answer)}

## Recommendation
**Consensus claim:** ${REC_CLAIM:-n/a}

${REC_A:-(no answer)}
EOF

  if [[ "$OPEN_ENDED" -eq 1 ]]; then
    cat <<EOF

## Anchor-Bias Check — What's Missing? (open-ended)
_Probe: "what did the surgeons NOT enumerate?" — counters the wrapper's tendency to remix the prompt's framing._

### Neurologist — Missing Categories (pattern recognition)
${ANCHOR_NEURO_A:-(no answer)}

### Cardiologist — Operator Blind Spots (outside view)
${ANCHOR_CARDIO_A:-(no answer)}
EOF
  fi

  cat <<EOF

---
_Generated by \`scripts/3s-brainstorm.sh\` — autonomous (no human gates). Q1 of P5 gap analysis._
EOF
}

# ----- dry-run path: print planned commands and exit -----
if [[ "$DRY_RUN" -eq 1 ]]; then
  _open_extra=""
  _gate_count=7
  if [[ "$OPEN_ENDED" -eq 1 ]]; then
    _open_extra="
  8. ${THREE_S} ask-local  \"<anchor-bias missing-categories>\"   # neuro (open-ended)
  9. ${THREE_S} ask-remote \"<anchor-bias operator blind spots>\" # cardio (open-ended)"
    _gate_count=9
  fi
  cat <<EOF
DRY RUN — would execute the following ${_gate_count} calls for topic: "${TOPIC}"

  1. ${THREE_S} ask-remote "<goal question>"        # cardio
  2. ${THREE_S} ask-remote "<constraints question>" # cardio
  3. ${THREE_S} ask-local  "<mvp question>"         # neuro
  4. ${THREE_S} ask-local  "<risks question>"       # neuro
  5. ${THREE_S} ask-remote "<user question>"        # cardio
  6. ${THREE_S} ask-remote "<options question>"     # cardio (uses Q1+Q2 answers)
  7. ${THREE_S} consensus  "<recommendation claim>" # both, weighted vote${_open_extra}

Output would be saved to: ${OUT}
Cost cap: \$${COST_CAP}
ERR log:  ${ERR_LOG}
Open-ended (anchor-bias) probe: $([[ "$OPEN_ENDED" -eq 1 ]] && echo ON || echo OFF)
Execution mode:                 $([[ "$PARALLEL" -eq 1 ]] && echo "parallel (stage A: 6-wide + stage B: options)" || echo "sequential (--no-parallel)")
Counter-probe (sycophancy):     ${CONTEXT_DNA_CONSENSUS_COUNTER_PROBE} (default ON; export CONTEXT_DNA_CONSENSUS_COUNTER_PROBE=off to disable)
EOF
  exit 0
fi

# ----- main -----
run_gates
render | tee "$OUT"

# R5 speedup observability — stderr only (preserves stdout/output format).
# Reference baseline: pre-R5 sequential wall-clock was ~58s for the 7-gate
# flow. The wrapper does not re-run sequential as a baseline (would double
# cost); we surface the ratio against the documented baseline so callers
# can sanity-check parallelization is paying off.
if [[ -n "${GATES_TOTAL_S:-}" ]] && [[ "${GATES_TOTAL_S}" -gt 0 ]]; then
  _baseline_s=58
  _speedup=$(python3 -c "print(f'{${_baseline_s}/${GATES_TOTAL_S}:.2f}x')" 2>/dev/null || echo "n/a")
  _mode_tag="$([[ "$PARALLEL" -eq 1 ]] && echo parallel || echo sequential)"
  echo ">> [timing] mode=${_mode_tag} wall=${GATES_TOTAL_S}s baseline=${_baseline_s}s speedup=${_speedup}" >&2
fi

# JJ4 ship 1: canonical .fleet/brainstorm/ artefact (default ON).
# ZSF: failures never abort the script — they go to $ERR_LOG and stderr.
if [[ "$FLEET_BRAINSTORM" -eq 1 ]]; then
  REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  if [[ -d "$REPO_ROOT" ]]; then
    if python3 -m memory.fleet_brainstorm_writer \
        --topic "$TOPIC" \
        --body "$OUT" \
        --transcript "$TX_LOG" \
        --fleet-dir "$REPO_ROOT/.fleet/brainstorm" \
        --stamp "$STAMP" >/tmp/3s-brainstorm-fleet-write.log 2>&1; then
      echo ">> [fleet] canonical artefact written to .fleet/brainstorm/" >&2
    else
      err_bump 1
      printf '%s\tfleet-brainstorm-write-failed\tstamp=%s\n%s\n---\n' \
        "$(date -u +%FT%TZ)" "$STAMP" \
        "$(cat /tmp/3s-brainstorm-fleet-write.log 2>/dev/null || echo '(no log)')" \
        >>"$ERR_LOG"
      echo "(fleet brainstorm write failed — see $ERR_LOG)" >&2
    fi
  fi
fi

# Final ZSF check: if every gate failed, exit non-zero.
# Re-read ERR_COUNT from the aggregate file — render() already did this,
# but the fleet-write above can add one more bump.
ERR_COUNT="$(err_total)"
FATAL_THRESHOLD=7
[[ "$OPEN_ENDED" -eq 1 ]] && FATAL_THRESHOLD=9
if [[ "$ERR_COUNT" -ge "$FATAL_THRESHOLD" ]]; then
  echo "FATAL: all ${FATAL_THRESHOLD} gates failed — see $ERR_LOG" >&2
  exit 4
fi

exit 0
