"""
agent_dispatch.py — Disk-based agent prompt dispatch for the autopilot arch-loop.

DESIGN CONSTRAINT (from F2 brief):
  The archloop_runner is a shell-level process. It cannot invoke the Claude
  Code `Task` tool directly — only the orchestrating Claude session can.
  So the runner *writes prompts to disk* + a `RUN_NOW.signal` file, and a
  separate Claude session (or the user invoking `/autopilot tick`) picks
  them up, spawns the parallel Task agents, and writes back result files.

CONTRACT (read this carefully — it's the agent fan-out protocol):

  Per cycle directory: `.fleet/autopilot/cycle-<ts>/`

  Runner writes (atomic — temp file + rename):
    agent_<n>.prompt    — the full agent instruction text. UTF-8. ≤ 32 KiB.
    RUN_NOW.signal      — empty marker file; presence = "5 prompts ready".

  Claude session writes back (one file per agent, in the same dir):
    agent_<n>.result    — the agent's summary. UTF-8. Format is freeform but
                          MUST begin with one of:
                            STATUS: PASS
                            STATUS: FAIL
                            STATUS: SKIP
                          A missing or malformed STATUS line counts as FAIL.

  When all 5 result files exist:
    Claude session writes `RESULTS_READY.signal` (empty marker).
    Runner sees the marker, advances to CROSS_EXAM stage.

  If the runner doesn't see all 5 results within `per_agent_timeout_s` * 5,
  it writes `TIMEOUT.signal` and aborts the cycle with counter
  `cycles_aborted_agent_timeout`.

This module is intentionally small. The runner imports `write_prompts` +
`poll_results`; everything else is constants and helpers.
"""

from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


# Sentinel filenames — single source of truth so tests and runner agree.
RUN_NOW_SIGNAL = "RUN_NOW.signal"
RESULTS_READY_SIGNAL = "RESULTS_READY.signal"
TIMEOUT_SIGNAL = "TIMEOUT.signal"

# Default timeout (overridable by env). 30 minutes/agent per F2 brief.
DEFAULT_PER_AGENT_TIMEOUT_S = int(
    os.environ.get("AUTOPILOT_PER_AGENT_TIMEOUT_S", "1800")
)

# Non-destructive contract — every prompt is wrapped with this preamble so a
# stray agent can't `git push --force` or `rm -rf` during autopilot.
NON_DESTRUCTIVE_PREAMBLE = (
    "AUTOPILOT NON-DESTRUCTIVE CONTRACT (read first):\n"
    "  * Do NOT commit, push, force-push, or amend.\n"
    "  * Do NOT run `rm -rf`, `git reset --hard`, or any destructive shell op.\n"
    "  * Do NOT restart, kill, or stop system services (daemons, NATS, MLX).\n"
    "  * Investigation, dry-runs, and read-only verification ONLY.\n"
    "  * If your task literally requires a destructive op, return STATUS: SKIP\n"
    "    with a one-line reason instead.\n"
    "End-of-contract.\n\n"
)


@dataclass
class AgentJob:
    cycle_dir: Path
    agent_index: int
    prompt_id: str          # "E1", "E2", ...
    prompt_text: str        # raw Synaptic prompt body
    prompt_path: Path       # where it was written
    result_path: Path       # where the result is expected
    status: str = "pending"  # pending | done | timeout | error


# ---------------------------------------------------------------------------
# Write side (runner → disk)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write+fsync to a temp file, then atomically rename into place.

    Atomic so that a partially-written `agent_3.prompt` never gets picked up
    by a watcher between two `os.write` calls.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_prompts(
    cycle_dir: Path,
    prompts: list,  # list[SynapticResponse.AgentPrompt-like with .id/.text]
) -> list[AgentJob]:
    """Write each prompt to disk + the RUN_NOW signal.

    Returns the AgentJob list the runner uses for poll_results().
    """
    cycle_dir = Path(cycle_dir)
    cycle_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[AgentJob] = []
    for idx, prompt in enumerate(prompts, start=1):
        prompt_path = cycle_dir / f"agent_{idx}.prompt"
        result_path = cycle_dir / f"agent_{idx}.result"

        # Strip any prior result so re-runs don't see stale data.
        if result_path.exists():
            try:
                result_path.unlink()
            except OSError:
                pass

        body = (
            NON_DESTRUCTIVE_PREAMBLE
            + f"AGENT ID: {prompt.id}\n"
            + f"AGENT INDEX: {idx}/5\n"
            + f"CYCLE DIR: {cycle_dir}\n\n"
            + "INSTRUCTION:\n"
            + prompt.text
            + "\n\nWHEN DONE: write your result to "
            + f"`{result_path.name}` in this directory. First line MUST be "
            + "`STATUS: PASS` / `STATUS: FAIL` / `STATUS: SKIP`.\n"
        )
        _atomic_write(prompt_path, body)
        jobs.append(AgentJob(
            cycle_dir=cycle_dir,
            agent_index=idx,
            prompt_id=prompt.id,
            prompt_text=prompt.text,
            prompt_path=prompt_path,
            result_path=result_path,
        ))

    # Manifest so the picker doesn't have to glob.
    manifest = {
        "cycle_dir": str(cycle_dir),
        "ts": time.time(),
        "agents": [
            {
                "index": j.agent_index,
                "prompt_id": j.prompt_id,
                "prompt": j.prompt_path.name,
                "result": j.result_path.name,
            }
            for j in jobs
        ],
    }
    _atomic_write(
        cycle_dir / "manifest.json",
        json.dumps(manifest, indent=2),
    )

    # Final signal — runner waits on RESULTS_READY.signal; picker waits on RUN_NOW.
    _atomic_write(cycle_dir / RUN_NOW_SIGNAL, "")
    return jobs


# ---------------------------------------------------------------------------
# Read side (runner polls)
# ---------------------------------------------------------------------------


def _parse_status(text: str) -> str:
    """Return PASS/FAIL/SKIP from the first line, or FAIL if malformed."""
    if not text:
        return "FAIL"
    first = text.splitlines()[0].strip().upper()
    for kw in ("PASS", "FAIL", "SKIP"):
        if first.startswith(f"STATUS: {kw}") or first.startswith(f"STATUS:{kw}"):
            return kw
    return "FAIL"


def poll_results(
    jobs: list[AgentJob],
    *,
    per_agent_timeout_s: int = DEFAULT_PER_AGENT_TIMEOUT_S,
    poll_interval_s: float = 2.0,
    now_fn=time.time,
    sleep_fn=time.sleep,
) -> dict:
    """Block until all agents have written results, or the cycle times out.

    The `now_fn` / `sleep_fn` indirection is so tests can drive a synthetic
    clock without sleeping for real.

    Returns:
        {
          "all_done": bool,
          "timed_out": bool,
          "results": [ {index, prompt_id, status, body}, ... ],
        }
    """
    if not jobs:
        return {"all_done": True, "timed_out": False, "results": []}

    cycle_dir = jobs[0].cycle_dir
    # Generous overall budget — caller can shrink it via env.
    overall_budget_s = per_agent_timeout_s * len(jobs)
    deadline = now_fn() + overall_budget_s

    while now_fn() < deadline:
        # Cheap signal-file fast path — if Claude session wrote it, we're done.
        if (cycle_dir / RESULTS_READY_SIGNAL).exists():
            break
        # Fallback: check each file individually.
        if all(j.result_path.exists() for j in jobs):
            break
        sleep_fn(poll_interval_s)
    else:
        # while-else: deadline fired
        _atomic_write(cycle_dir / TIMEOUT_SIGNAL, str(int(now_fn())))

    results = []
    all_done = True
    for j in jobs:
        if j.result_path.exists():
            body = j.result_path.read_text(errors="replace")
            status = _parse_status(body)
            j.status = "done"
            results.append({
                "index": j.agent_index,
                "prompt_id": j.prompt_id,
                "status": status,
                "body": body,
            })
        else:
            all_done = False
            j.status = "timeout"
            results.append({
                "index": j.agent_index,
                "prompt_id": j.prompt_id,
                "status": "TIMEOUT",
                "body": "",
            })

    return {
        "all_done": all_done,
        "timed_out": not all_done,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Convenience for the runner's logs
# ---------------------------------------------------------------------------


def summarise(results: list[dict]) -> str:
    """Compact aggregate suitable for feeding into 3s cross-exam."""
    chunks = []
    for r in results:
        chunks.append(
            f"### {r['prompt_id']} (agent {r['index']}) — STATUS {r['status']}\n"
            + (r.get("body", "") or "")[:2000]
        )
    return "\n\n".join(chunks)
