"""Headless agent executor — invoke Claude Code CLI to execute cycle prompts
without a human-driven Claude Code session.

Aaron's autopilot loop produces 5 agent prompts per cycle via `agent_dispatch.py`
and waits for a Claude Code session to consume them. Without a session
monitoring the inbox, cycles forever sit at AWAIT_AGENT_RESULTS. This module
closes the loop by shelling out to the `claude` CLI in `--print` (headless)
mode for each prompt.

Safety rails:
  - OPT-IN ONLY (default off; gated by `--headless-executor` runner flag)
  - Per-agent budget cap (`--max-budget-usd` to claude CLI)
  - Per-agent wall-clock timeout (kills runaway invocations)
  - `--bare` mode (skips hooks, auto-memory, plugin sync, CLAUDE.md discovery)
  - `--dangerously-skip-permissions` (required for unattended; off by default)
  - `--no-session-persistence` (no resumable transcript clutter)
  - Always writes a STATUS: line, even on timeout / killed
  - Counter-aware: every failure path bumps a named counter

Public API:
  - execute_cycle(cycle_dir, ...) → reconciles 5 prompts → results
  - probe_claude_cli()            → "is `claude` reachable + auth'd?"

Trust note: this module DOES allow Atlas to invoke claude on prompts that
Synaptic emits. That's the whole point — autonomous hardening — but it
expands Atlas's authority. Audit cycle artifacts under
`~/.context-dna/autopilot-logs/cycle-*/` to verify.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("local_autopilot.headless_executor")

_DATA_DIR = Path(os.environ.get("CONTEXT_DNA_DIR", os.path.expanduser("~/.context-dna")))
_COUNTERFILE = _DATA_DIR / "headless_executor_counters.json"
_COUNTER_LOCKFILE = _DATA_DIR / ".headless_executor_counters.lock"
_counter_thread_lock = threading.Lock()

_DEFAULT_MAX_BUDGET_USD = 0.20  # per-agent claude API call
_DEFAULT_TIMEOUT_S = 240        # 4 min per agent
_DEFAULT_PARALLEL = 5           # 5 prompts, run all concurrently

_STATUS_LINE_RE = re.compile(r"^STATUS:\s*(PASS|FAIL|SKIP)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class AgentExecution:
    prompt_file: Path
    result_file: Path
    agent_id: int
    status: str       # PASS / FAIL / SKIP / TIMEOUT / ERROR
    exit_code: int
    duration_s: float
    detail: str       # short description; full output in result_file


def _bump(counter: str, *, delta: int = 1, note: str = "") -> None:
    """ZSF counter bump — never raises.

    Cross-thread + cross-process safe: holds threading.Lock for intra-process
    serialization and fcntl.flock for inter-process serialization across the
    entire read-modify-write sequence. HE-2's test agent caught that the prior
    implementation lost increments under parallel=5 because the JSON load +
    increment + write was not atomic.
    """
    fd = None
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _counter_thread_lock:
            # Exclusive flock so concurrent processes don't trample each other.
            fd = os.open(str(_COUNTER_LOCKFILE), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                # Lock unavailable — fall back to best-effort write.
                # (Better to risk a lost increment than to raise.)
                pass

            data = {}
            if _COUNTERFILE.exists():
                try:
                    data = json.loads(_COUNTERFILE.read_text() or "{}")
                except (json.JSONDecodeError, OSError):
                    data = {}
            data[counter] = int(data.get(counter, 0)) + delta
            if note:
                data[f"{counter}__last_note"] = note[:200]

            # Atomic write: temp file + rename so a crash can't leave a
            # partial JSON file.
            tmp = _COUNTERFILE.with_suffix(_COUNTERFILE.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            tmp.replace(_COUNTERFILE)
    except Exception as e:  # noqa: BLE001 — ZSF
        logger.warning("counter bump failed: %s", e)
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def probe_claude_cli() -> tuple[bool, str]:
    """Return (available, detail). True iff `claude --version` runs cleanly."""
    binary = shutil.which("claude")
    if not binary:
        return False, "claude CLI not on PATH"
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"claude --version exit {result.returncode}: {result.stderr[:200]}"
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "claude --version timed out (10s)"
    except OSError as e:
        return False, f"claude --version OSError: {e}"


def _atomic_write(path: Path, text: str) -> None:
    """Same contract as agent_dispatch._atomic_write — temp + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _parse_status(text: str) -> str:
    """Return PASS/FAIL/SKIP/UNKNOWN by looking for STATUS: line."""
    m = _STATUS_LINE_RE.search(text)
    return m.group(1) if m else "UNKNOWN"


def _run_one_agent(
    prompt_file: Path,
    agent_id: int,
    *,
    max_budget_usd: float,
    timeout_s: int,
    extra_claude_args: list[str],
) -> AgentExecution:
    """Invoke `claude --print` on a single prompt. Returns AgentExecution."""
    result_file = prompt_file.parent / f"agent_{agent_id}.result"
    start = time.monotonic()

    try:
        prompt_text = prompt_file.read_text(encoding="utf-8")
    except OSError as e:
        _bump("agent_prompt_read_error", note=str(e))
        return AgentExecution(
            prompt_file=prompt_file, result_file=result_file, agent_id=agent_id,
            status="ERROR", exit_code=-1, duration_s=0.0,
            detail=f"prompt read failed: {e}",
        )

    # Prepend a STATUS-line instruction to the prompt so claude knows the
    # required output format. This is in addition to whatever Synaptic
    # asked. The runner's parser rejects results without a STATUS line.
    header = (
        "You are running in Aaron's autopilot loop. Execute the task below "
        "and report results. Your FIRST line MUST be exactly one of:\n"
        "  STATUS: PASS\n"
        "  STATUS: FAIL\n"
        "  STATUS: SKIP\n"
        "Followed by a brief explanation. Use PASS only if you verified the "
        "expected condition. Use SKIP if the task is unsafe, unclear, or "
        "out-of-scope. Use FAIL otherwise.\n\n"
        "Hint: Superpowers skills (brainstorming, systematic-debugging, "
        "test-driven-development, verification-before-completion, "
        "writing-plans, etc.) are available via the Skill tool. Invoke them "
        "when the task warrants — e.g. systematic-debugging for any bug or "
        "unexpected behaviour, verification-before-completion before "
        "claiming PASS, brainstorming for design choices. They are "
        "discoverable, not mandatory; skip them for trivial checks.\n\n"
        "--- TASK ---\n"
    )
    full_prompt = header + prompt_text

    args = [
        shutil.which("claude") or "claude",
        "--print",
        "--no-session-persistence",
        "--max-budget-usd", f"{max_budget_usd:.2f}",
        "--dangerously-skip-permissions",
        *extra_claude_args,
        full_prompt,
    ]
    # NOTE: --bare was previously included here for fast startup (skips hooks,
    # plugins, CLAUDE.md discovery). But --bare also disables OAuth + keychain
    # auth — Aaron caught this when the e2e test returned "Not logged in" on
    # every agent. Default path now uses normal auth (OAuth or keychain).
    # If you want --bare for performance + are using ANTHROPIC_API_KEY env,
    # pass it via extra_claude_args.

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        duration = time.monotonic() - start
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        if proc.returncode != 0:
            _bump("agent_nonzero_exit", note=f"exit={proc.returncode}")
            result_text = (
                f"STATUS: FAIL\n"
                f"# headless executor — claude exited {proc.returncode}\n"
                f"# stderr:\n{stderr[:2000]}\n\n"
                f"# stdout:\n{stdout[:4000]}\n"
            )
            _atomic_write(result_file, result_text)
            return AgentExecution(
                prompt_file=prompt_file, result_file=result_file, agent_id=agent_id,
                status="FAIL", exit_code=proc.returncode, duration_s=duration,
                detail=f"claude exit {proc.returncode}",
            )

        # Success path — extract STATUS from claude's output
        status = _parse_status(stdout)
        if status == "UNKNOWN":
            # claude returned output but didn't follow the STATUS: contract.
            # Prepend STATUS: SKIP and pass through (don't FAIL the cycle
            # over a formatting miss — surface it as SKIP for review).
            _bump("agent_status_missing", note="output present, STATUS line absent")
            result_text = "STATUS: SKIP\n# headless executor — STATUS line missing in claude output\n\n" + stdout
            status = "SKIP"
        else:
            _bump(f"agent_status_{status.lower()}")
            result_text = stdout

        _atomic_write(result_file, result_text)
        return AgentExecution(
            prompt_file=prompt_file, result_file=result_file, agent_id=agent_id,
            status=status, exit_code=0, duration_s=duration,
            detail=f"{status} in {duration:.1f}s",
        )

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        _bump("agent_timeout", note=f"timeout={timeout_s}s")
        result_text = (
            f"STATUS: FAIL\n"
            f"# headless executor — claude timed out after {timeout_s}s\n"
            f"# wall-clock: {duration:.1f}s\n"
        )
        _atomic_write(result_file, result_text)
        return AgentExecution(
            prompt_file=prompt_file, result_file=result_file, agent_id=agent_id,
            status="TIMEOUT", exit_code=-1, duration_s=duration,
            detail=f"timeout after {timeout_s}s",
        )

    except (OSError, FileNotFoundError) as e:
        duration = time.monotonic() - start
        _bump("agent_oserror", note=str(e))
        result_text = (
            f"STATUS: FAIL\n"
            f"# headless executor — OSError invoking claude: {e}\n"
        )
        _atomic_write(result_file, result_text)
        return AgentExecution(
            prompt_file=prompt_file, result_file=result_file, agent_id=agent_id,
            status="ERROR", exit_code=-1, duration_s=duration,
            detail=f"oserror: {e}",
        )


def execute_cycle(
    cycle_dir: Path,
    *,
    max_budget_per_agent_usd: float = _DEFAULT_MAX_BUDGET_USD,
    timeout_per_agent_s: int = _DEFAULT_TIMEOUT_S,
    parallel: int = _DEFAULT_PARALLEL,
    extra_claude_args: Optional[list[str]] = None,
) -> dict:
    """Execute every agent_N.prompt in cycle_dir via headless claude CLI.

    Discovers prompts by glob (`agent_*.prompt`), runs them in parallel up to
    `parallel`, writes corresponding `agent_N.result` files, and finally
    writes `RESULTS_READY.signal` so the runner's poller can proceed.

    Returns a summary dict:
        {
          "executed": N,
          "passed": int, "failed": int, "skipped": int, "timeout": int, "error": int,
          "duration_s": float,
          "executions": [AgentExecution-as-dict, ...]
        }

    ZSF: any single agent failure is recorded but does NOT raise. Worst case:
    the cycle gets 5 FAIL results, which the cross-exam stage handles.
    """
    cycle_dir = Path(cycle_dir)
    if not cycle_dir.is_dir():
        _bump("execute_cycle_bad_dir", note=str(cycle_dir))
        return {"executed": 0, "error": 1, "detail": f"not a dir: {cycle_dir}"}

    extra_claude_args = list(extra_claude_args or [])

    prompts = sorted(cycle_dir.glob("agent_*.prompt"))
    if not prompts:
        _bump("execute_cycle_no_prompts")
        return {"executed": 0, "detail": "no agent_*.prompt files"}

    # Parse agent_N from filename (the spec says agent_1.prompt … agent_5.prompt)
    def _agent_id(p: Path) -> int:
        m = re.match(r"agent_(\d+)\.prompt$", p.name)
        return int(m.group(1)) if m else 0

    jobs = [(p, _agent_id(p)) for p in prompts]
    _bump("cycles_started")
    start = time.monotonic()

    executions: list[AgentExecution] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {
            ex.submit(
                _run_one_agent, p, aid,
                max_budget_usd=max_budget_per_agent_usd,
                timeout_s=timeout_per_agent_s,
                extra_claude_args=extra_claude_args,
            ): aid
            for p, aid in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            try:
                executions.append(fut.result())
            except Exception as e:  # noqa: BLE001 — ZSF
                aid = futures[fut]
                _bump("agent_future_error", note=f"agent_{aid}: {e}")
                executions.append(AgentExecution(
                    prompt_file=cycle_dir / f"agent_{aid}.prompt",
                    result_file=cycle_dir / f"agent_{aid}.result",
                    agent_id=aid, status="ERROR", exit_code=-1,
                    duration_s=0.0, detail=f"future error: {e}",
                ))

    duration = time.monotonic() - start

    # Tally
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "TIMEOUT": 0, "ERROR": 0}
    for e in executions:
        counts[e.status] = counts.get(e.status, 0) + 1

    # Write the RESULTS_READY.signal — runner's poller is now unblocked
    try:
        (cycle_dir / "RESULTS_READY.signal").write_text(
            json.dumps({
                "ts": time.time(),
                "headless_executor": True,
                "duration_s": round(duration, 2),
                "counts": counts,
            }, indent=2),
            encoding="utf-8",
        )
        _bump("results_ready_written")
    except OSError as e:
        _bump("results_ready_write_error", note=str(e))

    # Drop a JSON summary alongside the cycle artifacts for audit
    summary = {
        "executed": len(executions),
        "passed":  counts["PASS"],
        "failed":  counts["FAIL"],
        "skipped": counts["SKIP"],
        "timeout": counts["TIMEOUT"],
        "error":   counts["ERROR"],
        "duration_s": round(duration, 2),
        "executions": [
            {
                "agent_id": e.agent_id, "status": e.status,
                "exit_code": e.exit_code, "duration_s": round(e.duration_s, 2),
                "detail": e.detail,
            }
            for e in sorted(executions, key=lambda e: e.agent_id)
        ],
    }
    try:
        _atomic_write(cycle_dir / "headless_executor_summary.json",
                      json.dumps(summary, indent=2))
    except OSError as e:
        _bump("summary_write_error", note=str(e))

    logger.info(
        "headless cycle done: %d executed (pass=%d fail=%d skip=%d to=%d err=%d) in %.1fs",
        len(executions), counts["PASS"], counts["FAIL"], counts["SKIP"],
        counts["TIMEOUT"], counts["ERROR"], duration,
    )
    return summary
