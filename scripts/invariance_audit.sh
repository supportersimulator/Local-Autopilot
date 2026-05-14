#!/bin/bash
# ============================================================================
# AUTOPILOT INVARIANCE AUDIT
# ============================================================================
# Runs the full invariance test harness and writes a green/red dashboard
# to /tmp/autopilot-invariance-audit.txt
#
# Exit code = number of FAILING invariants (0-8). 0 = all green.
#
# Hooked into gains-gate as check #24.
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
TESTS_DIR="$PLUGIN_DIR/tests/invariance"

# Find repo root: walk up until .git
REPO_DIR="$PLUGIN_DIR"
while [[ "$REPO_DIR" != "/" ]] && [[ ! -d "$REPO_DIR/.git" ]]; do
    REPO_DIR="$(dirname "$REPO_DIR")"
done

if [[ -x "$REPO_DIR/.venv/bin/python3" ]]; then
    PYTHON="$REPO_DIR/.venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

REPORT="/tmp/autopilot-invariance-audit.txt"
JSON_REPORT="/tmp/autopilot-invariance-audit.json"

declare -a INVARIANTS=(
    "1:USER_ONLY_DEACTIVATE:test_invariant_user_only_deactivate.py"
    "2:NO_ATLAS_PROMOTION:test_invariant_user_only_deactivate.py"
    "3:TEMPORARY_BOUNDED:test_invariant_temporary_bounded.py"
    "4:CRASH_RECOVERY:test_invariant_crash_recovery.py"
    "5:CONCURRENT_SAFE:test_invariant_concurrent_safe.py"
    "6:OBSERVABILITY:test_invariant_observability.py"
    "7:RESOURCE_CAPS:test_invariant_resource_caps.py"
    "8:LIVE_DATA:test_invariant_live_data.py"
)

# Run pytest once, capture per-file results
RAW_OUT="$(mktemp)"
trap 'rm -f "$RAW_OUT"' EXIT

cd "$REPO_DIR"
"$PYTHON" -m pytest "$TESTS_DIR" \
    --tb=no -q \
    -o "cache_dir=/tmp/.autopilot-invariance-pytest-cache" \
    > "$RAW_OUT" 2>&1
PYTEST_RC=$?

# Build the dashboard
FAILING_INVARIANTS=0
TOTAL_TESTS=0
TOTAL_PASSED=0
TOTAL_FAILED=0
TOTAL_SKIPPED=0

{
    echo "════════════════════════════════════════════════════════════════════"
    echo "  AUTOPILOT INVARIANCE AUDIT  —  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "════════════════════════════════════════════════════════════════════"
    echo ""
} > "$REPORT"

# Per-invariant breakdown using pytest's collection output
declare -a JSON_ROWS=()
SEEN_FILES=""

for spec in "${INVARIANTS[@]}"; do
    IFS=":" read -r num name file <<< "$spec"

    # Skip duplicate file listings (invariants 1+2 share a file)
    if echo "$SEEN_FILES" | grep -q ":$file:"; then
        # For shared files, infer status from prior run on the file
        :
    else
        SEEN_FILES="$SEEN_FILES:$file:"
    fi

    # Run only this file and parse results
    FILE_OUT="$(mktemp)"
    "$PYTHON" -m pytest "$TESTS_DIR/$file" --tb=line -q \
        -o "cache_dir=/tmp/.autopilot-invariance-pytest-cache" \
        > "$FILE_OUT" 2>&1
    FILE_RC=$?

    # Parse summary line, e.g. "10 passed, 1 skipped in 0.5s" or "2 failed, 8 passed"
    SUMMARY=$(tail -25 "$FILE_OUT" | grep -E "passed|failed|skipped|error" | tail -1)
    PASSED=$(echo "$SUMMARY" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+" || echo "0")
    FAILED=$(echo "$SUMMARY" | grep -oE "[0-9]+ failed" | grep -oE "[0-9]+" || echo "0")
    SKIPPED=$(echo "$SUMMARY" | grep -oE "[0-9]+ skipped" | grep -oE "[0-9]+" || echo "0")
    ERRORS=$(echo "$SUMMARY" | grep -oE "[0-9]+ error" | grep -oE "[0-9]+" || echo "0")

    PASSED=${PASSED:-0}
    FAILED=${FAILED:-0}
    SKIPPED=${SKIPPED:-0}
    ERRORS=${ERRORS:-0}

    THIS_TOTAL=$((PASSED + FAILED + SKIPPED + ERRORS))
    TOTAL_TESTS=$((TOTAL_TESTS + THIS_TOTAL))
    TOTAL_PASSED=$((TOTAL_PASSED + PASSED))
    TOTAL_FAILED=$((TOTAL_FAILED + FAILED + ERRORS))
    TOTAL_SKIPPED=$((TOTAL_SKIPPED + SKIPPED))

    if [[ "$FAILED" -gt 0 ]] || [[ "$ERRORS" -gt 0 ]]; then
        STATUS="FAIL"
        FAILING_INVARIANTS=$((FAILING_INVARIANTS + 1))
    else
        STATUS="PASS"
    fi

    LINE="INVARIANT $num ($name): $STATUS — $THIS_TOTAL tests ($PASSED pass, $FAILED fail, $SKIPPED skip)"
    echo "$LINE" >> "$REPORT"

    JSON_ROWS+=("{\"invariant\":$num,\"name\":\"$name\",\"status\":\"$STATUS\",\"total\":$THIS_TOTAL,\"passed\":$PASSED,\"failed\":$FAILED,\"skipped\":$SKIPPED}")

    rm -f "$FILE_OUT"
done

{
    echo ""
    echo "════════════════════════════════════════════════════════════════════"
    echo "  TOTAL: $TOTAL_TESTS tests  |  $TOTAL_PASSED passed  |  $TOTAL_FAILED failed  |  $TOTAL_SKIPPED skipped"
    echo "  FAILING INVARIANTS: $FAILING_INVARIANTS / 8"
    echo "════════════════════════════════════════════════════════════════════"
    if [[ "$FAILING_INVARIANTS" -eq 0 ]]; then
        echo "  RESULT: GREEN — all 8 autopilot invariants verified"
    else
        echo "  RESULT: RED — $FAILING_INVARIANTS invariant(s) failing, autopilot UNSAFE to enable"
    fi
    echo "════════════════════════════════════════════════════════════════════"
} >> "$REPORT"

# JSON sidecar
{
    echo "{"
    echo "  \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
    echo "  \"failing_invariants\": $FAILING_INVARIANTS,"
    echo "  \"total_tests\": $TOTAL_TESTS,"
    echo "  \"passed\": $TOTAL_PASSED,"
    echo "  \"failed\": $TOTAL_FAILED,"
    echo "  \"skipped\": $TOTAL_SKIPPED,"
    echo "  \"invariants\": ["
    for i in "${!JSON_ROWS[@]}"; do
        comma=","
        [[ "$i" -eq "$((${#JSON_ROWS[@]} - 1))" ]] && comma=""
        echo "    ${JSON_ROWS[$i]}$comma"
    done
    echo "  ]"
    echo "}"
} > "$JSON_REPORT"

cat "$REPORT"
exit "$FAILING_INVARIANTS"
