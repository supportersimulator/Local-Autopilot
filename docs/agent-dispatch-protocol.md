# Local Autopilot — Agent Dispatch Protocol

**Status:** stable
**Source of truth:** `local_autopilot/tools/agent_dispatch.py`
**Audience:** authors of non-Claude agent runners (Codex, Gemini, OpenInterpreter, custom scripts)

This document specifies the disk-based protocol Local Autopilot uses to fan out
parallel agent work during the arch-loop. It is intentionally simple: the
runner writes prompt files plus a signal file; an *agent runner* (any process
capable of executing an LLM agent) reads them, does the work, and writes
result files plus a second signal file. No sockets, no RPC, no shared memory.

If you can read files, run a model, and write files, you can implement this.

---

## 1. Why it exists

Local Autopilot's `archloop_runner` is a **shell-level Python process**. It
runs unattended (cron, systemd, manual `autopilot tick`) and cannot invoke
Claude Code's `Task` tool — that tool is only callable from inside an
orchestrating Claude Code session.

So the runner cannot say "spawn 5 sub-agents in parallel" directly. Instead:

1. The runner stages five prompt files on disk and raises a `RUN_NOW.signal`.
2. *Something else* (a Claude Code session, a custom poller, a Codex worker,
   etc.) sees the signal, spawns the parallel agents however it likes, and
   writes back result files.
3. The runner polls for results and continues the cycle.

This file-based contract makes the runner **agent-runner agnostic**. Claude
Code is the reference implementation; this doc describes how anyone else can
plug in.

---

## 2. File layout per cycle

Each arch-loop cycle creates one directory:

```
.fleet/autopilot/cycle-<unix_ts>/
├── manifest.json          # cycle metadata + agent list
├── agent_1.prompt         # full instruction for agent 1
├── agent_2.prompt
├── agent_3.prompt
├── agent_4.prompt
├── agent_5.prompt
├── RUN_NOW.signal         # empty marker — "prompts are ready, go"
├── agent_1.result         # written by the agent runner
├── agent_2.result
├── agent_3.result
├── agent_4.result
├── agent_5.result
├── RESULTS_READY.signal   # empty marker — "all 5 results written"
└── TIMEOUT.signal         # only if the runner aborts (mutually exclusive)
```

Cycle count is fixed at five agents today (the Synaptic fan-out width).
Implementations should not hard-code 5 — read `manifest.json`.

`manifest.json` schema:

```json
{
  "cycle_dir": "/abs/path/.fleet/autopilot/cycle-1715600000",
  "ts": 1715600000.123,
  "agents": [
    {"index": 1, "prompt_id": "E1", "prompt": "agent_1.prompt", "result": "agent_1.result"},
    {"index": 2, "prompt_id": "E2", "prompt": "agent_2.prompt", "result": "agent_2.result"}
  ]
}
```

---

## 3. Exact filenames and limits

| File | Direction | Encoding | Size limit | Required |
|------|-----------|----------|-----------|----------|
| `agent_<N>.prompt` | runner → agent | UTF-8 | ≤ 32 KiB | yes |
| `agent_<N>.result` | agent → runner | UTF-8 | ≤ 32 KiB recommended | yes |
| `RUN_NOW.signal` | runner → agent | empty | 0 bytes | yes |
| `RESULTS_READY.signal` | agent → runner | empty | 0 bytes | yes (fast path) |
| `TIMEOUT.signal` | runner internal | UTF-8 (unix ts) | < 64 bytes | written on failure |
| `manifest.json` | runner → agent | UTF-8 JSON | < 4 KiB | yes |

`<N>` is a 1-indexed integer; agent count varies per cycle (read manifest).

Each `agent_<N>.prompt` is wrapped by the runner with the **non-destructive
contract preamble** (see §9). Agent runners must surface this preamble to the
underlying LLM verbatim.

---

## 4. STATUS header (mandatory)

The first line of every `agent_<N>.result` file MUST be one of:

```
STATUS: PASS
STATUS: FAIL
STATUS: SKIP
```

Rules:

- Matching is case-insensitive after `STATUS:`.
- `STATUS:PASS` (no space) is also accepted.
- A missing or malformed header is silently re-classified as `FAIL`.
- The remainder of the file is freeform markdown; the runner truncates to
  2 KiB when feeding cross-exam, so put the summary near the top.

`SKIP` is the correct response when a task would require a destructive
operation forbidden by the non-destructive contract (§9).

---

## 5. Atomic write contract

Every file written by either side MUST be atomic. Implementations:

1. Write to `<final-path>.tmp`.
2. `flush()` then `fsync()` the file descriptor.
3. `os.replace(tmp, final)` — POSIX-atomic rename on the same filesystem.

A naive `open(path, "w")` is **not acceptable**; pollers will otherwise race
the writer and observe a half-written file. The reference implementation in
`agent_dispatch._atomic_write` is 8 lines — copy it.

Signal files (`RUN_NOW.signal`, `RESULTS_READY.signal`) are also written
atomically even though they're empty, so a watcher cannot observe the file
existing-but-locked.

---

## 6. Runner polling loop

The runner side (`poll_results`) blocks until results are in or it gives up.

| Knob | Default | Source |
|------|---------|--------|
| `per_agent_timeout_s` | `1800` (30 min) | env `AUTOPILOT_PER_AGENT_TIMEOUT_S` |
| Overall budget | `per_agent_timeout_s × n_agents` | computed |
| `poll_interval_s` | `2.0` s | argument |

Fast path: if `RESULTS_READY.signal` exists, exit the loop immediately.
Fallback: check whether every `agent_<N>.result` exists.

On deadline:

1. Write `TIMEOUT.signal` (contents = unix timestamp).
2. Mark each missing result as `status="TIMEOUT"` with empty body.
3. Bump counter `cycles_aborted_agent_timeout`.
4. Abort the cycle.

The runner never reads `RUN_NOW.signal` — it wrote it. Likewise the agent
runner never reads `RESULTS_READY.signal` — it wrote it. Each side is the
producer of its own marker.

---

## 7. Implementing an agent runner

A generic agent runner only needs to do four things:

1. Watch one or more cycle directories for `RUN_NOW.signal`.
2. Read the manifest, load each `agent_<N>.prompt`.
3. Run the prompts (sequentially or in parallel) through some LLM.
4. Atomically write `agent_<N>.result` for each, then `RESULTS_READY.signal`.

Pseudocode:

```python
import os, time, json
from pathlib import Path

POLL_INTERVAL_S = 2.0
WATCH_DIR = Path(".fleet/autopilot")

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def run_agent(prompt_text: str) -> str:
    """Override this — invoke your LLM of choice. MUST return text whose
    first line is `STATUS: PASS|FAIL|SKIP`."""
    raise NotImplementedError

def handle_cycle(cycle_dir: Path) -> None:
    manifest = json.loads((cycle_dir / "manifest.json").read_text())
    for agent in manifest["agents"]:
        prompt = (cycle_dir / agent["prompt"]).read_text(encoding="utf-8")
        try:
            body = run_agent(prompt)
        except Exception as exc:
            body = f"STATUS: FAIL\nrunner_exception: {exc!r}\n"
        atomic_write(cycle_dir / agent["result"], body)
    atomic_write(cycle_dir / "RESULTS_READY.signal", "")

def main() -> None:
    seen: set[Path] = set()
    while True:
        for cycle in WATCH_DIR.glob("cycle-*"):
            if cycle in seen:
                continue
            if (cycle / "RUN_NOW.signal").exists() \
               and not (cycle / "RESULTS_READY.signal").exists() \
               and not (cycle / "TIMEOUT.signal").exists():
                handle_cycle(cycle)
                seen.add(cycle)
        time.sleep(POLL_INTERVAL_S)
```

Concurrency: if multiple agent-runner processes watch the same directory,
add a `cycle_dir / "CLAIMED.<runner_id>"` lockfile written atomically before
work begins. The reference protocol does not specify this; only one runner
should watch a given directory.

---

## 8. Minimum viable reference implementation (<50 lines)

Drop-in poller that shells out to the `claude` CLI for each prompt:

```python
#!/usr/bin/env python3
"""mvp_agent_runner.py — drop-in agent runner for Local Autopilot."""
import json, os, subprocess, sys, time
from pathlib import Path

WATCH = Path(os.environ.get("AUTOPILOT_WATCH_DIR", ".fleet/autopilot"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
INTERVAL = float(os.environ.get("AUTOPILOT_POLL_S", "2.0"))

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def run_one(prompt: str) -> str:
    proc = subprocess.run(
        [CLAUDE_BIN, "-p", prompt],
        capture_output=True, text=True, timeout=1800,
    )
    out = (proc.stdout or "").strip()
    if not out.upper().startswith("STATUS:"):
        out = f"STATUS: FAIL\nmalformed_or_empty_output\nstderr={proc.stderr[:500]}\n{out}"
    return out

def handle(cycle: Path) -> None:
    manifest = json.loads((cycle / "manifest.json").read_text())
    for agent in manifest["agents"]:
        try:
            body = run_one((cycle / agent["prompt"]).read_text(encoding="utf-8"))
        except Exception as exc:
            body = f"STATUS: FAIL\nexception: {exc!r}"
        atomic_write(cycle / agent["result"], body)
    atomic_write(cycle / "RESULTS_READY.signal", "")

def main() -> None:
    seen: set[Path] = set()
    while True:
        for cycle in sorted(WATCH.glob("cycle-*")):
            if cycle in seen: continue
            if (cycle / "RUN_NOW.signal").exists() \
               and not (cycle / "RESULTS_READY.signal").exists() \
               and not (cycle / "TIMEOUT.signal").exists():
                handle(cycle); seen.add(cycle)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    sys.exit(main())
```

Run it: `python3 mvp_agent_runner.py` from the autopilot repo root.

---

## 9. Failure modes

| Failure | Detection | Runner behavior |
|---------|-----------|-----------------|
| Missing `agent_<N>.result` after deadline | `poll_results` deadline fires | write `TIMEOUT.signal`, mark agent `TIMEOUT`, bump `cycles_aborted_agent_timeout` |
| Malformed first line (no `STATUS:` prefix) | `_parse_status` returns `FAIL` | counted as `agent_result_fail` |
| Partial write observed | impossible if writer uses atomic rename; otherwise treated as malformed → `FAIL` | n/a |
| Agent runner crashes mid-cycle | no `RESULTS_READY.signal`, results incomplete | overall deadline triggers TIMEOUT path |
| `RUN_NOW.signal` missing | agent runner never starts work | runner times out as above; investigate disk/filesystem |
| Result exceeds buffer size | runner truncates to 2 KiB in cross-exam summary; full file preserved | warn only |
| Stale results from a previous run | `write_prompts` unlinks any pre-existing `agent_<N>.result` before writing prompts | n/a |
| Destructive op requested by task | agent runner must return `STATUS: SKIP` per the non-destructive preamble | counted as `agent_result_pass` if STATUS valid, but task not executed |

The **non-destructive contract** (preamble injected into every prompt)
forbids: commits, pushes, force-pushes, amends, `rm -rf`, `git reset --hard`,
service restarts, and any destructive shell op. Agent runners do not enforce
this — the LLM is asked to comply and emit `SKIP` if it can't. Sandboxing
remains the runner operator's responsibility.

---

## 10. Counters bumped during dispatch (ZSF audit)

The runner bumps the following counters in `/tmp/autopilot-counters.json`
(override path with `AUTOPILOT_COUNTERS_PATH`). All counters are
monotonically non-decreasing JSON integers; ZSF (zero-silent-failures)
requires that every failure path increments at least one counter.

| Counter | When |
|---------|------|
| `cycles_started` | every cycle, before prompts are written |
| `agent_spawn_total` | += N once prompts written (N = agent count) |
| `agent_result_pass` | per result with `STATUS: PASS` |
| `agent_result_fail` | per result with `STATUS: FAIL` (incl. malformed) |
| `agent_result_timeout` | per missing result after deadline |
| `cycles_aborted_agent_timeout` | once, when `TIMEOUT.signal` is written |
| `cycles_aborted_cost` | budget exceeded before dispatch |
| `cycles_aborted_kill_file` | kill switch tripped |
| `cycles_aborted_synaptic_parse` | prompts couldn't be generated |
| `cycles_aborted_cycle_cap` | cycle cap reached |
| `cycles_satisfied` | cycle completed with all PASS |

Agent runners themselves are encouraged — but not required — to expose their
own counters (prompts handled, LLM errors, retries) over a separate channel.

---

## 11. Known runners

| Runner | Status | Notes |
|--------|--------|-------|
| Claude Code `Task` tool (in an orchestrating session) | reference, primary | Picks up `RUN_NOW.signal`, fans out via parallel `Task` calls. |
| Reference Python poller (§7 pseudocode) | reference, generic | Backend-agnostic; implementer plugs in `run_agent()`. |
| MVP `mvp_agent_runner.py` (§8) | reference, drop-in | Shells to the `claude` CLI. |
| Codex / Gemini / OpenInterpreter / custom | placeholder — community | Open an issue or PR linking your implementation; it will be listed here. |

To add your runner here: submit a PR adding a row above with a one-line
description and a link to source.
