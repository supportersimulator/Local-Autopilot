# Local Autopilot

**A self-hardening loop for AI coding sessions. Local-first. Reversible. Always interruptible.**

Six layers of judgment, eight rounds of cross-examination per cycle, three distinct LLM vendors required by invariant, one kill switch only the user can revoke. Local Autopilot watches your codebase between work sessions and applies the next-right hardening task -- tests, edge cases, drift cleanup -- without spiking cost, overwriting unsigned work, or going rogue.

Local Autopilot is the first autonomous coding loop built around an explicit *epistemic stack*. Every cycle's verdict comes from a distinct chain of layers, each bounded in scope, each observable on disk, each reversible. The default config is opt-in at every escalation point.

<!-- Banner placeholder: commission an SVG/PNG and drop it at docs/banner.svg, then reference it here. -->

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 140+ passing](https://img.shields.io/badge/tests-140%2B%20passing-success.svg)](#tests)
[![Invariants: 8](https://img.shields.io/badge/invariants-8%20enforced-success.svg)](#invariants)
[![ZSF](https://img.shields.io/badge/Zero_Silent_Failures-invariant-success.svg)](#zero-silent-failures)

---

## What it does

Local Autopilot is a **single-process autonomous loop** that drives AI coding sessions to keep hardening themselves without user input. Every cycle, it:

1. **Pulls live state** -- complexity vectors from your local SQLite, current invariant snapshot, drift signals.
2. **Asks Synaptic** (local MLX `Qwen3.6-35B-A3B-4bit-DWQ` on port 5044) for the next 5 highest-leverage hardening prompts.
3. **Spawns 5 agents in parallel** via `claude --print` headless (or your own agent runner).
4. **Cross-examines** results with 3-Surgeons (Cardiologist + Neurologist + Atlas, three distinct vendors required).
5. **Re-evaluates** with Synaptic for a satisfaction verdict.
6. **Decides:** sign off, iterate, or escalate to the fleet chief after 8 unsatisfied rounds.

It runs as a launchd or systemd timer, ticks every 30 minutes (configurable), and **respects an explicit state machine** -- only you can flip it to `on_permanent`, and only you can clear it.

> **Full autonomy is opt-in.** By default, cycles stop at AWAIT for your review. To let Atlas execute prompts itself via `claude --print`, see [BOOTSTRAP.md -> Optional: full autonomy via headless executor](BOOTSTRAP.md#optional-full-autonomy-via-headless-executor).

---

## Why?

You have a complex AI-driven codebase. After every coding session, dozens of small hardening tasks accumulate: tests to add, edge cases to fuzz, invariants to enforce, dead code to remove. You don't want to babysit every one. You want an agent that does the next-right thing while you sleep -- but you also want it to **never go rogue, never spike costs, never overwrite work you didn't sign off on, and always be reversible**.

That's Local Autopilot.

---

## Quick start

```bash
# 1. Clone + install (creates a venv, seeds the DB, runs 140 tests as smoke check)
git clone https://github.com/supportersimulator/Local-Autopilot.git ~/dev/local-autopilot
cd ~/dev/local-autopilot
bash install.sh

# 2. Verify it works (dry-run, no LLM calls, exits in ~1s)
.venv/bin/python3 -m local_autopilot.tools.archloop_runner --dry-run --cycles 1

# 3. Check current state (defaults to off)
.venv/bin/autopilot status

# 4. Turn it on
.venv/bin/autopilot on
```

Run cycles manually (one-off, for testing):

```bash
.venv/bin/python3 -m local_autopilot.tools.archloop_runner --cycles 5 --cost-cap-usd 1.00
```

Or schedule it:

```bash
# macOS -- install the launchd timer (ticks every 30 min)
bash scripts/install-daemon-macos.sh

# Linux -- install the systemd timer
bash scripts/install-daemon-linux.sh
```

To stop:

```bash
.venv/bin/autopilot off                  # halts the loop next tick
bash scripts/uninstall-daemon-macos.sh   # removes the launchd entry entirely
```

---

## The epistemic stack

Six layers, each with strict scope. No layer is trusted to decide the cycle on its own. Full reasoning: [docs/epistemic-stack.md](docs/epistemic-stack.md).

```
                     YOU (Aaron) -- always-on kill switch (`autopilot off`)
                                          |
                                          v
                     LAUNCHD TICK every 30 min  (StartInterval=1800)
                                          |
                                          v
                  +---- archloop_runner (--cycles 8) ----+
                  |                                       |
   1. SYNAPTIC REVIEW         -- local MLX, picks 5 prompts
                  |
                  v
   2. HEADLESS EXECUTE        -- 5x claude --print in parallel
                  |
                  v
   3. 3-SURGEONS CROSS-EXAM   -- Cardiologist (DeepSeek)
                                 Neurologist  (Qwen3.6 local)
                                 Atlas        (Claude)
                  |
                  v
   4. SYNAPTIC RE-EVALUATE    -- satisfied? sign off : iterate
                  |
                  v
   5. FLEET ESCALATE          -- after 8 unsatisfied rounds, ask chief
                  |
                  v
   6. FINAL VERDICT           -- SIGN-OFF | PARTIAL | UNRESOLVABLE
```

**Distinctness invariant (constitutional):** Cardiologist, Neurologist, and Atlas must be three different vendors. A single-vendor panel is structurally a single brain wearing three hats. The value is in the disagreements, and disagreements only happen across genuinely distinct training.

---

## The state machine

| Mode | Meaning | Who can set it | Effect |
|------|---------|----------------|--------|
| `off` | Loop exits immediately on tick | user, atlas, system | Daemon stays loaded, does nothing |
| `on_temporary` | Loop runs until `temporary_until` timestamp | user, atlas | Auto-reverts to `off` after timer |
| `on_permanent` | Loop runs indefinitely | **user only** | Only the user can clear this -- Atlas cannot revoke its own permission grant |

The state machine is **the safety mechanism**. Atlas can elevate itself to `on_temporary` for a bounded reason, but it **cannot** flip itself to `on_permanent` or back to `off` from `on_permanent`. That's the user's sole prerogative. See [`tests/invariance/CONTRACT.md`](tests/invariance/CONTRACT.md) for the formal contract.

---

## Invariants

Eight invariants are enforced by the test suite. Each has its own test file under `tests/invariance/`. CI fails if any invariant fails.

| # | Invariant | Test |
|---|-----------|------|
| 1 | USER_ONLY_DEACTIVATE -- only `actor=user` can clear `on_permanent` | `test_invariant_user_only_deactivate.py` |
| 2 | NO_ATLAS_PROMOTION -- `actor=atlas` cannot set `to=on_permanent` | `test_invariant_user_only_deactivate.py` |
| 3 | TEMPORARY_BOUNDED -- every `on_temporary` MUST have a deadline | `test_invariant_temporary_bounded.py` |
| 4 | CRASH_RECOVERY -- state survives kill -9 mid-write (atomic + flock) | `test_invariant_crash_recovery.py` |
| 5 | CONCURRENT_SAFE -- N writers don't lose transitions | `test_invariant_concurrent_safe.py` |
| 6 | OBSERVABILITY -- every transition bumps a named counter | `test_invariant_observability.py` |
| 7 | LIVE_DATA -- no module-level caching of state (every read is fresh) | `test_invariant_live_data.py` |
| 8 | RESOURCE_CAPS -- `--cost-cap-usd` is honored | `test_invariant_resource_caps.py` |

Run the audit:

```bash
bash scripts/invariance_audit.sh
# -> /tmp/autopilot-invariance-audit.txt (green/red dashboard)
```

---

## Architecture

```
            +-----------------------------------------------------+
            |  archloop_runner.py  (single sync process)          |
            |                                                     |
            |  +--------------+    +------------+    +---------+  |
            |  | autopilot_   |    | synaptic_  |    | agent_  |  |
            |  | state.py     |    | client.py  |    | dispatch|  |
            |  |              |    |            |    |         |  |
            |  | JSON+flock   |    | MLX/DS/AI  |    | disk    |  |
            |  | atomic       |    | fallback   |    | proto   |  |
            |  +------+-------+    +-----+------+    +----+----+  |
            +---------+------------------+----------------+-------+
                      |                  |                |
                      v                  v                v
   ~/.context-dna/             local MLX or             .fleet/
   autopilot_state.json        DeepSeek API             autopilot/
                                                        cycle-<ts>/
                                                          agent_n.prompt
                                                          agent_n.result
                                                          RUN_NOW.signal
                                                          RESULTS_READY.signal
```

Cycles are persisted to `~/.context-dna/autopilot-logs/cycle-<timestamp>/` -- every prompt, every result, every Synaptic review, every cost-cap check. **Nothing is ephemeral.** Audit any past cycle by reading its directory.

---

## LLM provider chain

`config.yaml` controls where Synaptic calls go. Default is `local-first`:

| Provider | Used for | Env var |
|----------|---------|---------|
| **MLX** (port 5044, Apple Silicon) | First try (free, local) | `LOCAL_LLM_URL` |
| **DeepSeek** (`deepseek-chat`) | Cheap remote fallback (~$0.27/1M tok) | `DEEPSEEK_API_KEY` |
| **OpenAI** (`gpt-4o-mini`) | Premium fallback | `OPENAI_API_KEY` |

To switch:

```bash
# Edit config.yaml or set env var:
export LLM_EXTERNAL_PROVIDER=deepseek-first   # try DS first, then MLX
export LLM_EXTERNAL_PROVIDER=local-only        # MLX only, no remote calls
```

If you don't have MLX running, the autopilot will fall back to DeepSeek (cheap) or OpenAI. To install MLX:

```bash
pip install mlx-lm
python -m mlx_lm.server --port 5044 --model mlx-community/Qwen3-4B-4bit
```

Or use Ollama / LM Studio (just point `LOCAL_LLM_URL` at their OpenAI-compatible endpoint).

---

## Cost envelope

Realistic per-cycle spend:

| Component                | Cost          | Notes                              |
| ------------------------ | ------------- | ---------------------------------- |
| Synaptic (MLX local)     | $0.00         | Qwen3.6 on 5044, $0 marginal       |
| Headless agents x 5      | $0.05 - $0.30 | `--max-budget-usd` per agent       |
| Cardiologist (DeepSeek)  | ~$0.27/1M tok | one cross-exam round per cycle     |
| Neurologist (Qwen local) | $0.00         | unless DeepSeek-fallback enabled   |
| Re-evaluate (MLX)        | $0.00         | local                              |
| **Per-cycle total**      | **$0.05 - $0.40** | depending on agent depth       |

The cumulative `cost_cap_usd` (default $5.00) is a hard wall enforced per `archloop_runner` invocation. Hitting it bumps `cycles_aborted_cost` and aborts cleanly -- no layer can extend the cap mid-cycle. Fleet escalation (Layer 5) hits the chief's Opus, the most expensive single call in the system; it is gated by the 8-cycle cap so it only fires when local thinking has provably failed.

Full cost accounting: [docs/epistemic-stack.md#7-cost-accounting](docs/epistemic-stack.md#7-cost-accounting).

---

## Kill switch

The kill switch is the **floor of the trust ladder**. Every other layer can be wrong, captured, or hallucinating; this one is structurally tamper-resistant by an OS file lock plus an actor check.

```bash
autopilot off                       # halts at next tick boundary
autopilot on                        # sets on_permanent (user only)
autopilot temp "<reason>"           # bounded elevation
autopilot status                    # current mode + last 5 transitions
```

Mechanics, verified in `autopilot_state.py`:

* State is a closed enum: `{off, on_permanent, on_temporary}`.
* All transitions are flock-serialised (`fcntl.flock` + atomic rename). Concurrent CLI calls cannot race.
* Atlas cannot set `on_permanent`. Hard-coded refusal.
* Only `actor=user` can clear `on_permanent -> off`. Hard-coded refusal.

A kill file at `/tmp/autopilot.stop` (override via `AUTOPILOT_KILL_FILE`) is a belt-and-suspenders emergency stop: any cycle observes it and aborts (`cycles_aborted_kill_file`).

---

## Daily commands

```bash
.venv/bin/autopilot status                  # current mode + last 5 transitions
.venv/bin/autopilot on                      # turn on permanently (user only)
.venv/bin/autopilot off                     # turn off
.venv/bin/autopilot temp "<reason>"         # temporary elevation

# Run cycles manually
.venv/bin/python3 -m local_autopilot.tools.archloop_runner --cycles 5 --cost-cap-usd 1.0
.venv/bin/python3 -m local_autopilot.tools.archloop_runner --dry-run --cycles 1
.venv/bin/python3 -m local_autopilot.tools.archloop_runner --force-complexity HIGH --cycles 1
```

---

## Example cycle output

A dry-run cycle prints two JSON event lines and writes a full artefact directory.

```bash
$ .venv/bin/python3 -m local_autopilot.tools.archloop_runner --dry-run --cycles 1
{"event": "cycle_done", "cycle": 1, "cycle_dir": "/Users/you/.context-dna/autopilot-logs/cycle-20260514T170704Z", "satisfied": true, "verdict": "SIGN-OFF", "aborted": null, "cost_usd_total": 1.35, "watch": false}
{"event": "exit", "reason": "satisfied", "cycle": 1}
```

The cycle directory contains every artefact produced by the loop:

```text
RUN_NOW.signal       cross_exam.txt        manifest.json
agent_1.prompt       cycle_summary.json    prompts.json
agent_1.result       deep_exploration.json synaptic_re_eval.md
agent_2.prompt ...   live_state.json       synaptic_review.md
```

Every spawned agent receives the same non-destructive contract preamble (`agent_1.prompt`, first 5 lines):

```text
AUTOPILOT NON-DESTRUCTIVE CONTRACT (read first):
  * Do NOT commit, push, force-push, or amend.
  * Do NOT run `rm -rf`, `git reset --hard`, or any destructive shell op.
  * Do NOT restart, kill, or stop system services (daemons, NATS, MLX).
  * Investigation, dry-runs, and read-only verification ONLY.
```

**Run it yourself:**

```bash
.venv/bin/python3 -m local_autopilot.tools.archloop_runner --dry-run --cycles 1
ls ~/.context-dna/autopilot-logs/cycle-*/   # newest cycle dir
```

---

## Integration with the ContextDNA ecosystem

Local Autopilot is **a tool**, not a platform. It composes:

- **[ContextDNA IDE](https://github.com/supportersimulator/contextdna-ide)** -- provides the `~/.context-dna/` data directory, Synaptic intelligence layer, and webhook injection.
- **[3-Surgeons](https://github.com/supportersimulator/3-surgeons)** -- provides cross-examination via the `3s` CLI (Cardiologist + Neurologist consensus checks).
- **[Multi-Fleet](https://github.com/supportersimulator/multi-fleet)** -- optional; if installed, autopilot can dispatch work to peer machines instead of running everything locally, and escalate stuck cycles to a chief node.

It runs **standalone** without any of those -- the agent dispatch is just disk-based prompts that any Claude Code session (or other agent runner) can pick up. The other systems make it more powerful but aren't required.

---

## Project layout

```
local_autopilot/
  tools/
    archloop_runner.py     <- the main loop (single sync process)
    autopilot_state.py     <- persistent state (JSON + flock + atomic)
    autopilot_cli.py       <- `autopilot status/on/off/temp` CLI
    autopilot_hook.py      <- state-check hook for daemon scripts
    synaptic_client.py     <- LLM provider chain + complexity classification
    agent_dispatch.py      <- disk-based agent prompt protocol
    deep_exploration.py    <- HIGH-complexity drill-down with brainstorm + counter-probe
    headless_executor.py   <- `claude --print` subprocess runner (opt-in)
scripts/
  install-daemon-macos.sh     <- installs the launchd timer
  install-daemon-linux.sh     <- installs the systemd timer
  uninstall-daemon-macos.sh
  uninstall-daemon-linux.sh
  invariance_audit.sh         <- runs all 8 invariants + writes a dashboard
  rerank_complexity_vectors.py
  3s-brainstorm.sh            <- 3-Surgeons brainstorm wrapper (deep exploration)
seeds/
  complexity_vectors.sql      <- 20 starter complexity vectors
daemons/
  autopilot-tick.plist.template
  autopilot-tick.service.template
  autopilot-tick.timer.template
  autopilot-rerank.plist.template
docs/
  epistemic-stack.md          <- the six-layer judgment system, in depth
  agent-dispatch-protocol.md
  known-issues.md
tests/
  test_archloop.py
  test_autopilot_state.py
  test_deep_exploration.py
  invariance/                 <- the 8 invariant tests + CONTRACT.md
```

---

## Tests

```bash
bash scripts/invariance_audit.sh    # full invariance dashboard
.venv/bin/python3 -m pytest tests/  # all 140+ tests
.venv/bin/python3 -m pytest tests/invariance/  # just the invariants
```

Three tests are skipped intentionally: they require a real runner with live data (would slow CI). Run them manually before shipping production changes.

---

## Zero Silent Failures

Every error path bumps a named counter. There is no `except Exception: pass`. Run:

```bash
cat ~/.context-dna/autopilot_state.json | jq '.counters'
```

If any counter is climbing, you have a real bug. If they're stable, the loop is healthy.

---

## Survival audit

| Scenario | Recoverable? |
|----------|--------------|
| Daemon crash mid-write | YES -- atomic write via temp file + rename |
| Two daemons race to write | YES -- fcntl.flock serialises |
| You forget you turned it on | `autopilot status` shows every transition + actor + reason |
| Atlas hallucinates and asks to be permanent | Atlas cannot. Only `actor=user` may set `on_permanent` |
| Cost runs away | `--cost-cap-usd` is honored; tested per invariant #8 |
| You want to undo a cycle | Every cycle dir under `~/.context-dna/autopilot-logs/` has the full audit trail; revert in git |

---

## Documentation

| Document | Contents |
|----------|----------|
| [BOOTSTRAP.md](BOOTSTRAP.md) | First-run setup, full autonomy opt-in |
| [docs/epistemic-stack.md](docs/epistemic-stack.md) | Six-layer judgment system, distinctness invariant, trust ladder |
| [docs/agent-dispatch-protocol.md](docs/agent-dispatch-protocol.md) | Disk-based prompt/result protocol |
| [docs/known-issues.md](docs/known-issues.md) | Current limitations and gotchas |
| [tests/invariance/CONTRACT.md](tests/invariance/CONTRACT.md) | Formal invariance contract |

---

## License

MIT -- see [LICENSE](LICENSE).

---

## Contributing

Pull requests welcome. The invariance contract is non-negotiable -- any PR that would break one of the 8 invariants will be rejected, even if it fixes a bug. Add a new invariant if your change reveals a missing constraint. See [`tests/invariance/CONTRACT.md`](tests/invariance/CONTRACT.md) for the formal contract.

Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
