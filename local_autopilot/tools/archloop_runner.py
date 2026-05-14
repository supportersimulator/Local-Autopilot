#!/usr/bin/env python3
"""
archloop_runner.py — The Synaptic-driven autopilot arch-loop orchestrator.

Aaron's framing (verbatim, from the F2 brief):

  "Synaptic fully deeply integrated within the ContextDNA ecosystem is exploited
   for all contribution capacity with atlas spawning the agents parellelized to
   proceed with Synaptic's specific insight prompts then 3s looping until
   satisfied then again returning to Synaptic directly to begin again and have
   this cycle be the arch-looping of the autopilot mode"

One cycle = 8 stages:

  PULL_LIVE_STATE → SYNAPTIC_REVIEW → PARSE_PROMPTS → SPAWN_AGENTS
                                                            ↓
       SYNAPTIC_RE_EVAL ← CROSS_EXAM ← AWAIT_AGENT_RESULTS
                ↓
            DECISION  (loop or stop)

The runner is a *shell-level* process. It does not invoke Claude Code's Task
tool directly — instead, SPAWN_AGENTS writes prompts to disk and a separate
Claude session (`/autopilot tick` in skill mode) picks them up and writes back
result files. See `agent_dispatch.py` for the contract.

Invocation:
  PYTHONPATH=/Users/aarontjomsland/dev/er-simulator-superrepo \\
    .venv/bin/python3 archloop_runner.py --cycles 10 --cost-cap-usd 5.0

Dry-run (no LLM, no 3s, just stage timing + state machine wiring check):
  python3 archloop_runner.py --dry-run --cycles 1
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# Make in-tree imports work whether invoked as a script or a module.
# Layout in the standalone Local Autopilot repo:
#   <repo>/local_autopilot/tools/<this>.py
# So parents[1] == local_autopilot/, parents[2] == repo root. Adding both
# lets `import agent_dispatch` (sibling) and `import memory.<x>` (sibling
# package) both resolve.
_THIS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = _THIS_DIR.parents[1]            # local_autopilot/
_REPO_ROOT = _THIS_DIR.parents[2]           # repo root
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent_dispatch import (  # noqa: E402
    AgentJob,
    poll_results,
    summarise as summarise_results,
    write_prompts,
)
from synaptic_client import (  # noqa: E402
    SynapticResponse,
    AgentPrompt,
    ComplexityClass,
    ask_for_vectors,
    ask_satisfaction,
    classify_complexity,
)
from deep_exploration import (  # noqa: E402
    DeepExplorer,
    DeepExplorationSummary,
    run_deep_exploration_stage,
)


# ---------------------------------------------------------------------------
# Path resolvers — read env every call so test monkeypatches take effect.
# ---------------------------------------------------------------------------


def _counters_path() -> Path:
    return Path(os.environ.get(
        "AUTOPILOT_COUNTERS_PATH", "/tmp/autopilot-counters.json"
    ))


def _kill_file() -> Path:
    return Path(os.environ.get(
        "AUTOPILOT_KILL_FILE", "/tmp/autopilot.stop"
    ))


def _db_path() -> Path:
    return Path(os.environ.get(
        "AUTOPILOT_COMPLEXITY_DB",
        str(Path.home() / ".context-dna/complexity_vectors.db"),
    ))


def _metrics_url() -> str:
    return os.environ.get(
        "AUTOPILOT_METRICS_URL", "http://127.0.0.1:8855/metrics"
    )


def _3s_cli() -> str:
    return os.environ.get("AUTOPILOT_3S_CLI", "/usr/local/bin/3s")


def _log_root() -> Path:
    return Path(os.environ.get(
        "AUTOPILOT_LOG_ROOT",
        str(Path.home() / ".context-dna" / "autopilot-logs"),
    ))


# Backwards-compatible constants (kept for callers + tests that monkeypatch
# the names directly). NOT consulted by the runner — only the resolvers above.
COUNTERS_PATH = _counters_path()
KILL_FILE = _kill_file()
DEFAULT_DB = _db_path()
DEFAULT_METRICS_URL = _metrics_url()
DEFAULT_3S_CLI = _3s_cli()
LOG_ROOT = _log_root()

STAGES = (
    "PULL_LIVE_STATE",
    "SYNAPTIC_REVIEW",
    "PARSE_PROMPTS",
    "DEEP_EXPLORATION",
    "SPAWN_AGENTS",
    "AWAIT_AGENT_RESULTS",
    "CROSS_EXAM",
    "SYNAPTIC_RE_EVAL",
    "DECISION",
)

# G1 — conditional pre-stage budget. The brainstorm subprocess is the slow
# limb of the deep-exploration path; the counter-probe adds at most ~3 min.
COST_DEEP_EXPLORATION_BUDGET_S = int(os.environ.get(
    "AUTOPILOT_DEEP_STAGE_BUDGET_S", "600"
))

# Cost-cap heuristic constants (per skill SKILL.md §"Cost-cap accounting").
COST_CROSS_EXAM_DEFAULT_USD = 0.05
COST_SYNAPTIC_FALLBACK_USD = 0.005
COST_PER_AGENT_USD = float(os.environ.get("AUTOPILOT_PER_AGENT_USD", "0.27"))


# ---------------------------------------------------------------------------
# F1 integration — autopilot_state API
# ---------------------------------------------------------------------------
#
# F1 (`tools/autopilot_state.py`) owns the user-vs-Atlas state machine
# (off | on_permanent | on_temporary). It exposes:
#     read_state() -> State        # State.mode in VALID_MODES
#     set_state_paths(...)         # for tests
# F1's state does NOT include cycle accounting. We track cycle_count /
# last_satisfaction in our own sidecar file so we don't pollute F1's contract.


@dataclass
class _RunnerProgress:
    cycle_count: int = 0
    last_satisfaction: Optional[bool] = None
    last_cycle_ts: Optional[float] = None


def _progress_path() -> Path:
    return Path(os.environ.get(
        "AUTOPILOT_PROGRESS_FILE",
        str(Path.home() / ".context-dna" / "autopilot-logs" / "runner_progress.json"),
    ))


def _read_autopilot_mode() -> str:
    """Call F1's read_state() and return mode. On any failure, returns 'off'."""
    try:
        from autopilot_state import read_state  # type: ignore  # F1's module
        # If AUTOPILOT_STATE_FILE env is set, redirect F1's paths so tests work.
        env_file = os.environ.get("AUTOPILOT_STATE_FILE")
        if env_file:
            try:
                import autopilot_state as _as  # type: ignore
                _as.set_state_paths(Path(env_file))
            except Exception:
                pass
        s = read_state()
        return getattr(s, "mode", "off")
    except Exception:
        return "off"


def _read_progress() -> _RunnerProgress:
    p = _progress_path()
    if p.exists():
        try:
            raw = json.loads(p.read_text() or "{}")
            return _RunnerProgress(
                cycle_count=int(raw.get("cycle_count", 0)),
                last_satisfaction=raw.get("last_satisfaction"),
                last_cycle_ts=raw.get("last_cycle_ts"),
            )
        except Exception:
            pass
    return _RunnerProgress()


def _write_progress(progress: _RunnerProgress) -> None:
    p = _progress_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(progress), indent=2, default=str))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Counters — ZSF invariant
# ---------------------------------------------------------------------------


COUNTER_KEYS = (
    "cycles_started",
    "cycles_satisfied",
    "cycles_aborted_cost",
    "cycles_aborted_user_off",
    "cycles_aborted_kill_file",
    "cycles_aborted_agent_timeout",
    "cycles_aborted_synaptic_parse",
    "cycles_aborted_cycle_cap",
    "synaptic_call_total",
    "synaptic_call_errors",
    "synaptic_parse_errors",
    "cross_exam_total",
    "cross_exam_errors",
    "cross_exam_holds",
    "cross_exam_signoff",
    "agent_spawn_total",
    "agent_result_pass",
    "agent_result_fail",
    "agent_result_timeout",
    "live_state_fetch_errors",
    # G1 — deep_exploration stage counters
    "cycles_deep_explored",
    "cycles_shallow",
    "deep_exploration_errors",
    "deep_exploration_cost_total",
    "deep_exploration_timeouts",
)


def _read_counters() -> dict:
    p = _counters_path()
    if p.exists():
        try:
            return json.loads(p.read_text() or "{}")
        except Exception:
            return {}
    return {}


def _write_counters(c: dict) -> None:
    p = _counters_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(c, indent=2, sort_keys=True))
    except Exception:
        pass


def _bump(c: dict, key: str, *, by: int = 1, note: str = "") -> None:
    c[key] = int(c.get(key, 0)) + by
    if note:
        errs = c.setdefault("_last_errors", {})
        errs[key] = note[:200]


def _ensure_counter_keys(c: dict) -> dict:
    for k in COUNTER_KEYS:
        c.setdefault(k, 0)
    return c


# ---------------------------------------------------------------------------
# Stage 1 — PULL_LIVE_STATE
# ---------------------------------------------------------------------------


def pull_live_state(
    *,
    db_path: Optional[Path] = None,
    metrics_url: Optional[str] = None,
    counters: Optional[dict] = None,
) -> dict:
    if db_path is None:
        db_path = _db_path()
    if metrics_url is None:
        metrics_url = _metrics_url()
    """Read live complexity vectors + most-recent persistence-check + /metrics.

    Live data ONLY — no caching across cycles. This is the Synaptic
    stale-claim mitigation per skill SKILL.md.
    """
    state: dict[str, Any] = {
        "ts": time.time(),
        "complexity_vectors": [],
        "persistence_check": {},
        "metrics_excerpt": "",
        "errors": [],
    }

    # complexity_vectors.db
    try:
        if db_path.exists():
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT vector_id, name, category, risk_score, "
                "drift_ranking_score, current_alert_level, trigger_count "
                "FROM complexity_vectors "
                "ORDER BY drift_ranking_score DESC LIMIT 20"
            ).fetchall()
            state["complexity_vectors"] = [dict(r) for r in rows]
            con.close()
        else:
            state["errors"].append(f"db_missing:{db_path}")
            if counters is not None:
                _bump(counters, "live_state_fetch_errors", note=f"db_missing:{db_path}")
    except Exception as exc:
        state["errors"].append(f"db_read:{exc}")
        if counters is not None:
            _bump(counters, "live_state_fetch_errors", note=f"db_read:{exc}")

    # most recent persistence-check
    try:
        pc_files = sorted(Path("/tmp").glob("persistence-check-*.json"))
        if pc_files:
            latest = pc_files[-1]
            state["persistence_check"] = {
                "file": latest.name,
                "payload": json.loads(latest.read_text() or "{}"),
            }
    except Exception as exc:
        state["errors"].append(f"persistence_read:{exc}")
        if counters is not None:
            _bump(counters, "live_state_fetch_errors", note=f"persistence_read:{exc}")

    # /metrics — best-effort. Curl is more forgiving than urllib here.
    try:
        proc = subprocess.run(
            ["curl", "-sf", "--max-time", "3", metrics_url],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            # Trim to keep within Synaptic context budget
            state["metrics_excerpt"] = proc.stdout[:8000]
        else:
            state["errors"].append(f"metrics_rc:{proc.returncode}")
    except Exception as exc:
        state["errors"].append(f"metrics_call:{exc}")

    return state


# ---------------------------------------------------------------------------
# Stage 6 — CROSS_EXAM (3s CLI)
# ---------------------------------------------------------------------------


def run_cross_exam(
    aggregate_text: str,
    *,
    cli_path: Optional[str] = None,
    timeout_s: int = 600,
) -> dict:
    if cli_path is None:
        cli_path = _3s_cli()
    """Invoke `3s cross-exam --mode continuous "<aggregate>"`.

    Returns:
        {"verdict": str, "consensus": float, "cost_usd": float, "raw": str,
         "decision": "HOLD"|"SIGN-OFF"|"UNKNOWN", "error": Optional[str]}
    """
    if not Path(cli_path).exists():
        return {
            "verdict": "",
            "consensus": 0.0,
            "cost_usd": 0.0,
            "raw": "",
            "decision": "UNKNOWN",
            "error": f"3s_cli_missing:{cli_path}",
        }
    try:
        proc = subprocess.run(
            [cli_path, "cross-exam", "--mode", "continuous", aggregate_text[:60_000]],
            capture_output=True, text=True, timeout=timeout_s,
        )
        raw = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    except Exception as exc:
        return {
            "verdict": "",
            "consensus": 0.0,
            "cost_usd": 0.0,
            "raw": "",
            "decision": "UNKNOWN",
            "error": f"3s_exec:{exc}",
        }

    return parse_3s_verdict(raw)


def parse_3s_verdict(raw: str) -> dict:
    """Pure parser for the 3s CLI output — easy to unit-test.

    Strategy: look for explicit `SIGN-OFF` / `HOLD` tokens, plus optional
    `consensus: 0.83` and `cost_usd: 0.041` lines. Anything else → UNKNOWN.
    """
    out = {
        "verdict": "",
        "consensus": 0.0,
        "cost_usd": COST_CROSS_EXAM_DEFAULT_USD,
        "raw": raw,
        "decision": "UNKNOWN",
        "error": None,
    }
    if not raw:
        out["error"] = "empty_3s_output"
        return out

    # Try JSON-line first (3s emits structured JSON when possible)
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                data = json.loads(s)
                out["verdict"] = data.get("verdict", "")
                out["consensus"] = float(data.get("consensus", 0.0))
                out["cost_usd"] = float(data.get("cost_usd", COST_CROSS_EXAM_DEFAULT_USD))
                v = (data.get("decision") or out["verdict"]).upper()
                if "SIGN-OFF" in v or "SIGNOFF" in v:
                    out["decision"] = "SIGN-OFF"
                elif "HOLD" in v:
                    out["decision"] = "HOLD"
                return out
            except Exception:
                pass

    upper = raw.upper()
    if "SIGN-OFF" in upper or "SIGNOFF" in upper:
        out["decision"] = "SIGN-OFF"
    elif "HOLD" in upper:
        out["decision"] = "HOLD"

    for line in raw.splitlines():
        low = line.strip().lower()
        if low.startswith("consensus"):
            try:
                out["consensus"] = float(
                    low.split(":", 1)[1].strip().rstrip(",;")
                )
            except Exception:
                pass
        elif low.startswith("cost_usd") or low.startswith("cost:"):
            try:
                out["cost_usd"] = float(
                    low.split(":", 1)[1].strip().rstrip(",;")
                )
            except Exception:
                pass

    out["verdict"] = raw[:2000]
    return out


# ---------------------------------------------------------------------------
# Cycle driver
# ---------------------------------------------------------------------------


@dataclass
class CycleResult:
    cycle: int
    cycle_dir: Path
    stage_timings: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0
    satisfied: bool = False
    aborted: Optional[str] = None     # reason string if abort
    verdict_decision: str = "UNKNOWN"
    agent_pass: int = 0
    agent_fail: int = 0
    agent_timeout: int = 0
    # G1 — deep_exploration accounting
    deep_exploration_triggered: bool = False
    deep_exploration_reason: str = ""
    deep_exploration_cost_usd: float = 0.0
    complexity_class: str = "LOW"


def _ts_dirname() -> str:
    return time.strftime("cycle-%Y%m%dT%H%M%SZ", time.gmtime())


@contextmanager
def _stage(timings: dict[str, float], name: str):
    t0 = time.time()
    try:
        yield
    finally:
        timings[name] = round(time.time() - t0, 3)


def _check_kill() -> bool:
    return _kill_file().exists()


def run_cycle(
    cycle_idx: int,
    *,
    counters: dict,
    cost_so_far: float,
    cost_cap: float,
    dry_run: bool = False,
    log_root: Optional[Path] = None,
    force_complexity: Optional[str] = None,
) -> CycleResult:
    """Execute one cycle of the arch-loop, returning a CycleResult."""
    if log_root is None:
        log_root = _log_root()
    log_root.mkdir(parents=True, exist_ok=True)
    cycle_dir = log_root / _ts_dirname()
    cycle_dir.mkdir(parents=True, exist_ok=True)

    result = CycleResult(cycle=cycle_idx, cycle_dir=cycle_dir)
    timings = result.stage_timings
    _bump(counters, "cycles_started")

    # --- Stage 1: PULL_LIVE_STATE ---
    with _stage(timings, "PULL_LIVE_STATE"):
        live_state = pull_live_state(counters=counters)
        (cycle_dir / "live_state.json").write_text(
            json.dumps(live_state, indent=2, default=str)
        )

    if _check_kill():
        result.aborted = "kill_file"
        _bump(counters, "cycles_aborted_kill_file")
        return result

    # --- Stage 2: SYNAPTIC_REVIEW ---
    with _stage(timings, "SYNAPTIC_REVIEW"):
        if dry_run:
            synaptic_resp = SynapticResponse(
                raw="(dry-run stub)",
                prompts=[
                    AgentPrompt(id=f"E{i}", text=f"verify dry-run stub {i} exit 0", falsifiable=True)
                    for i in range(1, 6)
                ],
            )
        else:
            synaptic_resp = ask_for_vectors(live_state)
            _bump(counters, "synaptic_call_total")
        (cycle_dir / "synaptic_review.md").write_text(
            f"# Synaptic review (cycle {cycle_idx})\n\n"
            f"retries={synaptic_resp.retries} ok={synaptic_resp.ok}\n\n"
            f"```\n{synaptic_resp.raw}\n```\n"
        )

    # --- Stage 3: PARSE_PROMPTS ---
    with _stage(timings, "PARSE_PROMPTS"):
        if not synaptic_resp.ok:
            (cycle_dir / "parse_error.txt").write_text(
                f"synaptic ok=False error={synaptic_resp.error}\n"
            )
            _bump(counters, "cycles_aborted_synaptic_parse",
                  note=str(synaptic_resp.error))
            result.aborted = f"synaptic_parse:{synaptic_resp.error}"
            return result
        (cycle_dir / "prompts.json").write_text(json.dumps(
            [asdict(p) for p in synaptic_resp.prompts], indent=2
        ))

    # --- Stage 3.5: DEEP_EXPLORATION (G1 — conditional) ---
    #
    # Classify the cycle's complexity from the live vectors + Synaptic
    # prompts. HIGH → run brainstorm + counter-position, enrich the agent
    # prompts. LOW/MED → skip the stage entirely so routine cycles stay
    # cheap.
    #
    # Hard timing budget: COST_DEEP_EXPLORATION_BUDGET_S (default 600s).
    # If we exceed it, we ABORT the stage (not the cycle) and fall through
    # to SPAWN_AGENTS with the raw Synaptic prompts. Failures during the
    # stage are counted, not raised — ZSF.
    prompts_for_spawn = synaptic_resp.prompts
    deep_summary: Optional[DeepExplorationSummary] = None
    with _stage(timings, "DEEP_EXPLORATION"):
        live_vectors = live_state.get("complexity_vectors", []) or []

        if force_complexity:
            klass = ComplexityClass(force_complexity.upper())
        else:
            try:
                klass = classify_complexity(
                    synaptic_resp, live_vectors=live_vectors
                )
            except Exception as exc:  # noqa: BLE001 - counter, not crash
                _bump(counters, "deep_exploration_errors",
                      note=f"classify:{exc}")
                klass = ComplexityClass.LOW
        result.complexity_class = klass.value

        if klass == ComplexityClass.HIGH:
            t_stage = time.time()
            try:
                explorer = DeepExplorer()
                # If force_complexity=HIGH was supplied, synthesize a vector
                # so the inner should_deep_explore gate fires even when the
                # live DB is empty (e.g. dry-run on a host with no
                # complexity_vectors.db). This honours the operator's
                # explicit override.
                vectors_for_stage = live_vectors
                if force_complexity and force_complexity.upper() == "HIGH" and not live_vectors:
                    vectors_for_stage = [{
                        "vector_id": "force-high",
                        "name": "force_complexity_override",
                        "risk_score": 9.9,
                        "drift_ranking_score": 99.0,
                    }]
                enriched, deep_summary = run_deep_exploration_stage(
                    synaptic_resp,
                    vectors_for_stage,
                    explorer=explorer,
                    dry_run=dry_run,
                )
                if enriched:
                    prompts_for_spawn = enriched
                if deep_summary is not None:
                    result.deep_exploration_triggered = deep_summary.triggered
                    result.deep_exploration_reason = deep_summary.reason
                    result.deep_exploration_cost_usd = deep_summary.cost_usd
                    result.cost_usd += deep_summary.cost_usd
                    cost_so_far += deep_summary.cost_usd
                _bump(
                    counters,
                    "cycles_deep_explored" if (
                        deep_summary and deep_summary.triggered
                    ) else "cycles_shallow",
                )
                stage_elapsed = time.time() - t_stage
                if stage_elapsed > COST_DEEP_EXPLORATION_BUDGET_S:
                    _bump(
                        counters, "deep_exploration_timeouts",
                        note=f"elapsed={stage_elapsed:.1f}s"
                    )
            except Exception as exc:  # noqa: BLE001
                _bump(counters, "deep_exploration_errors",
                      note=f"stage:{exc}")
                result.deep_exploration_triggered = False
                result.deep_exploration_reason = f"error:{exc}"
        else:
            _bump(counters, "cycles_shallow")
            result.deep_exploration_triggered = False
            result.deep_exploration_reason = (
                f"skipped:complexity={klass.value}"
            )

        if deep_summary is not None:
            (cycle_dir / "deep_exploration.json").write_text(
                json.dumps(asdict(deep_summary), indent=2, default=str)
            )

    # --- Stage 4: SPAWN_AGENTS ---
    with _stage(timings, "SPAWN_AGENTS"):
        jobs = write_prompts(cycle_dir, prompts_for_spawn)
        _bump(counters, "agent_spawn_total", by=len(jobs))
        result.cost_usd += COST_PER_AGENT_USD * len(jobs)
        cost_so_far += COST_PER_AGENT_USD * len(jobs)

    # Cost check after the most expensive stage commits.
    if cost_so_far >= cost_cap and not dry_run:
        result.aborted = "cost_cap"
        _bump(counters, "cycles_aborted_cost", note=f"cost={cost_so_far:.2f}")
        return result

    # --- Stage 5: AWAIT_AGENT_RESULTS ---
    with _stage(timings, "AWAIT_AGENT_RESULTS"):
        if dry_run:
            # Synthesize PASS results so downstream stages have something to chew on.
            for j in jobs:
                j.result_path.write_text(
                    f"STATUS: PASS\n(dry-run synthetic result for {j.prompt_id})\n"
                )
            poll = poll_results(jobs, per_agent_timeout_s=2, poll_interval_s=0.01)
        else:
            poll = poll_results(jobs)
        agent_results = poll["results"]
        for r in agent_results:
            if r["status"] == "PASS":
                _bump(counters, "agent_result_pass")
                result.agent_pass += 1
            elif r["status"] == "TIMEOUT":
                _bump(counters, "agent_result_timeout")
                result.agent_timeout += 1
            else:
                _bump(counters, "agent_result_fail")
                result.agent_fail += 1
        if poll["timed_out"] and not dry_run:
            result.aborted = "agent_timeout"
            _bump(counters, "cycles_aborted_agent_timeout")
            return result

    # --- Stage 6: CROSS_EXAM ---
    with _stage(timings, "CROSS_EXAM"):
        aggregate = summarise_results(agent_results)
        if dry_run:
            verdict = {
                "verdict": "DRY-RUN: SIGN-OFF (stub)",
                "consensus": 1.0,
                "cost_usd": 0.0,
                "decision": "SIGN-OFF",
                "raw": "(dry-run synthetic)",
                "error": None,
            }
        else:
            verdict = run_cross_exam(aggregate)
            _bump(counters, "cross_exam_total")
        (cycle_dir / "cross_exam.txt").write_text(
            json.dumps(verdict, indent=2)
        )
        if verdict.get("error") and not dry_run:
            _bump(counters, "cross_exam_errors", note=str(verdict["error"]))
        result.cost_usd += float(verdict.get("cost_usd") or 0.0)
        cost_so_far += float(verdict.get("cost_usd") or 0.0)
        result.verdict_decision = verdict.get("decision", "UNKNOWN")
        if result.verdict_decision == "HOLD":
            _bump(counters, "cross_exam_holds")
        elif result.verdict_decision == "SIGN-OFF":
            _bump(counters, "cross_exam_signoff")

    # --- Stage 7: SYNAPTIC_RE_EVAL ---
    with _stage(timings, "SYNAPTIC_RE_EVAL"):
        if dry_run:
            satisfied, raw_sat = True, "DRY-RUN STUB\nSATISFACTION: TRUE\n"
        else:
            satisfied, raw_sat = ask_satisfaction(
                verdict.get("verdict", ""),
                [r["body"] for r in agent_results],
            )
            _bump(counters, "synaptic_call_total")
            result.cost_usd += COST_SYNAPTIC_FALLBACK_USD
            cost_so_far += COST_SYNAPTIC_FALLBACK_USD
        (cycle_dir / "synaptic_re_eval.md").write_text(
            f"# Synaptic re-eval (cycle {cycle_idx})\n\n"
            f"satisfied={satisfied}\n\n```\n{raw_sat}\n```\n"
        )
        result.satisfied = satisfied
        if satisfied:
            _bump(counters, "cycles_satisfied")

    # --- Stage 8: DECISION ---
    # We close the _stage timer first so the DECISION timing makes it into
    # the serialized summary. The actual decision logic (loop vs stop) lives
    # in main(); this stage just builds + writes the audit artifact.
    with _stage(timings, "DECISION"):
        # No-op body — we want DECISION timing > 0 but the JSON build
        # happens *after* the contextmanager fires `finally`.
        pass

    summary = {
        "cycle": cycle_idx,
        "cycle_dir": str(cycle_dir),
        "stage_timings": timings,
        "stages": list(STAGES),
        "satisfied": result.satisfied,
        "verdict_decision": result.verdict_decision,
        "cost_usd_cycle": round(result.cost_usd, 4),
        "cost_usd_total_after_cycle": round(cost_so_far, 4),
        "agents": {
            "pass": result.agent_pass,
            "fail": result.agent_fail,
            "timeout": result.agent_timeout,
        },
        # G1 — deep_exploration accounting
        "complexity_class": result.complexity_class,
        "deep_exploration_triggered": result.deep_exploration_triggered,
        "deep_exploration_reason": result.deep_exploration_reason,
        "deep_exploration_cost_usd": round(result.deep_exploration_cost_usd, 4),
        "aborted": result.aborted,
        "dry_run": dry_run,
        "ts_end": time.time(),
        # schema_version stays at 1 because the new deep_exploration fields
        # are ADDITIVE — old consumers ignore them. Bump to 2 only if we
        # remove a field or reshape an existing one.
        "schema_version": 1,
    }
    (cycle_dir / "cycle_summary.json").write_text(
        json.dumps(summary, indent=2)
    )

    return result


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Synaptic arch-loop autopilot runner")
    parser.add_argument(
        "--cycles", type=int,
        default=int(os.environ.get("AUTOPILOT_MAX_CYCLES", "10")),
        help="Max cycles before forced stop (default 10)",
    )
    parser.add_argument(
        "--cost-cap-usd", type=float,
        default=float(os.environ.get("AUTOPILOT_COST_CAP_USD", "5.0")),
        help="Cumulative cost cap in USD (default $5.00)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="No LLM, no 3s, no real spawns — just exercise the state machine.",
    )
    parser.add_argument(
        "--force-complexity", type=str, default=None,
        choices=("LOW", "MED", "HIGH", "low", "med", "high"),
        help=(
            "G1 override: force the cycle's complexity class. HIGH triggers "
            "deep_exploration; LOW/MED skip it. Useful for testing + "
            "manual override when Aaron knows the cycle deserves the deep loop."
        ),
    )
    args = parser.parse_args(argv)

    counters = _ensure_counter_keys(_read_counters())
    mode = _read_autopilot_mode()
    progress = _read_progress()

    # Bypass the off-check in dry-run so CI can prove the state machine works
    # without flipping the real autopilot switch.
    if mode == "off" and not args.dry_run:
        _bump(counters, "cycles_aborted_user_off")
        _write_counters(counters)
        print(json.dumps({
            "event": "exit",
            "reason": "state.mode == 'off'",
            "mode": mode,
        }))
        return 0

    cost_so_far = 0.0
    last_result: Optional[CycleResult] = None

    for i in range(1, args.cycles + 1):
        if _check_kill():
            _bump(counters, "cycles_aborted_kill_file")
            _write_counters(counters)
            print(json.dumps({"event": "exit", "reason": "kill_file", "cycle": i}))
            return 0

        if cost_so_far >= args.cost_cap_usd:
            _bump(counters, "cycles_aborted_cost", note=f"cost={cost_so_far:.2f}")
            _write_counters(counters)
            print(json.dumps({"event": "exit", "reason": "cost_cap", "cycle": i,
                              "cost_usd_total": cost_so_far}))
            return 0

        # Pass force_complexity only when set, so downstream test
        # monkeypatches of run_cycle that don't accept the kwarg keep
        # working. (Tests in test_archloop.py override run_cycle with a
        # narrower signature; we don't want to break them.)
        run_kwargs = dict(
            counters=counters,
            cost_so_far=cost_so_far,
            cost_cap=args.cost_cap_usd,
            dry_run=args.dry_run,
        )
        if args.force_complexity:
            run_kwargs["force_complexity"] = args.force_complexity
        result = run_cycle(i, **run_kwargs)
        cost_so_far += result.cost_usd
        last_result = result

        # Persist counters + progress every cycle (ZSF).
        _write_counters(counters)
        progress.cycle_count += 1
        progress.last_satisfaction = result.satisfied
        progress.last_cycle_ts = time.time()
        _write_progress(progress)

        print(json.dumps({
            "event": "cycle_done",
            "cycle": i,
            "cycle_dir": str(result.cycle_dir),
            "satisfied": result.satisfied,
            "verdict": result.verdict_decision,
            "aborted": result.aborted,
            "cost_usd_total": round(cost_so_far, 4),
        }))

        if result.aborted:
            return 0
        if result.satisfied:
            # SATISFIED=TRUE → stop loop. (on_temporary expires via F1 — we
            # don't try to keep looping past satisfaction without an
            # explicit continuous mode, which F1 doesn't currently expose.)
            print(json.dumps({"event": "exit", "reason": "satisfied", "cycle": i}))
            return 0
    else:
        _bump(counters, "cycles_aborted_cycle_cap")
        _write_counters(counters)
        print(json.dumps({"event": "exit", "reason": "cycle_cap_hit",
                          "cycles_run": args.cycles}))

    return 0


if __name__ == "__main__":
    sys.exit(main())
