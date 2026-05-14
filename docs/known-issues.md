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
