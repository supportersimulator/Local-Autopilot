# Known Issues

## Unreproducible flake — 2026-05-14

F-agent reported "1 pre-existing flaky test" during install on 2026-05-14. After commit `0a07bf5` (install.sh strict pipefail / PIPESTATUS capture) and mac1's fixes (`89c844a`, `1cb7e30`: SIGPIPE-safe `grep -q` + pipefail in install-daemon/preflight branches), the full pytest suite was run 5x consecutively on commit `0a07bf5`:

```
run 1: 145 passed, 3 skipped in 109.35s
run 2: 145 passed, 3 skipped in 109.75s
run 3: 145 passed, 3 skipped in 109.94s
run 4: 145 passed, 3 skipped in 109.83s
run 5: 145 passed, 3 skipped in 109.82s
```

Zero failures across 725 test executions. The reported flake is **not reproducible** on the current tip of `main` and is suspected to have been a SIGPIPE/`grep -q` false-negative in the install pipeline itself (now fixed by mac1), rather than a genuine test-level race condition.

If a flake re-emerges, capture: (1) exact test name, (2) full pytest `-vv` output, (3) host/load context. Re-open this entry and investigate the suspected race in `tests/invariance/` (crash recovery + concurrent writes are the highest-risk areas).

## UPDATE 2026-05-14T17:30 — flake IS reproducible under load

Round 2-X (commit `cc75c71`) reported the flake was not reproducible after
5 consecutive full-suite runs. That conclusion was wrong: on the very next
full-suite run after the Round 2 commits landed (`1c5a097` + `cc75c71`),
`tests/invariance/test_invariant_user_only_deactivate.py::test_race_user_off_vs_atlas_off`
failed once. Re-running the test in isolation passes immediately.

**Reproduction profile:**
- Only fails under full pytest suite (145 tests sharing process pool + flock)
- Does NOT fail when run in isolation
- Failure rate observed: ~1 in 6 full-suite runs (rough estimate from this session)

**Root-cause hypothesis (not yet confirmed):**
- The test simulates concurrent user-off + atlas-off attempting to clear
  `on_permanent`. Under flock contention from other invariance tests
  running in parallel, the ordering of the two off-attempts can let
  atlas-off appear *after* user-off has already cleared `on_permanent`,
  at which point atlas-off becomes "atlas clearing on_temporary" which
  IS permitted — but the test expects atlas to be rejected regardless.
- I.e. the test's setup may not be holding the precondition (`mode ==
  on_permanent`) tightly enough across the race.

**Next-investigator checklist:**
1. Add `pytest -p no:cacheprovider -x --maxfail=1 -n 4 tests/invariance/` to
   reliably reproduce under parallel load
2. Inspect `tools/autopilot_state.py:transition()` for the user-off →
   atlas-off ordering window
3. The fix is most likely to add an `expected_from_mode` arg to
   `transition()` so atlas-off can only succeed when its observation of
   `on_permanent` is still valid at the moment of write

**Why not fixing in this session:** invariant #1 is the *core* safety
contract. Atomic, flock-serialised, etc. A hasty fix that breaks the
invariant in a different way is worse than a flaky test. Need a focused
design pass with proper steelman + Synaptic review before touching the
production state machine.
