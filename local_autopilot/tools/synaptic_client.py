"""
synaptic_client.py — Thin wrapper around `memory.llm_priority_queue.llm_generate`
for the autopilot arch-loop.

All Synaptic calls in autopilot mode MUST route through this module so that:

  1. We always use `profile='s8_synaptic'` (no other model profile is acceptable).
  2. We always use `Priority.ATLAS` (autopilot is Atlas-initiated, not Aaron-direct).
  3. Every prompt + response is appended to `/tmp/autopilot-synaptic-trace.jsonl`
     for post-hoc audit (ZSF invariant — Synaptic stale-claim mitigation).
  4. Parser failures increment a counter rather than silently returning garbage.

This file does NOT itself call any external service. It calls `llm_generate`,
which routes through the priority queue (local MLX first, DeepSeek/OpenAI
fallback per the LLM Priority rules in CLAUDE.md).

For tests, mock `synaptic_client._call_llm` — do NOT mock `llm_generate` directly,
because we want to keep the trace + counter side-effects covered by the tests.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, Callable

# Local Autopilot vendoring note:
#   Original superrepo layout was <root>/.claude/plugins/autopilot/tools/<this>.py
#   so parents[4] hit the superrepo root where `memory/` lived. In the
#   standalone repo the layout is <root>/local_autopilot/tools/<this>.py, and
#   the memory shim lives at <root>/local_autopilot/memory/. Adding parents[1]
#   (== local_autopilot/) to sys.path lets `import memory.llm_priority_queue`
#   resolve to the minimal shim shipped with this repo.
_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# Default audit trace path (CLAUDE.md ZSF rule: every Synaptic call traceable).
SYNAPTIC_TRACE_PATH = Path(os.environ.get(
    "AUTOPILOT_SYNAPTIC_TRACE",
    "/tmp/autopilot-synaptic-trace.jsonl",
))

# Regex that recognises Synaptic's structured prompts:
#   "E1 — verify nats jetstream replicas are >= 3"
#   "E2 - expect drift score to fall below 0.4"
#   "E3: assert kv counter monotonic"
# The model drifts between em-dash, hyphen, and colon — accept all three.
_PROMPT_PATTERN = re.compile(r"^\s*E(\d+)\s*[—\-:]\s*(.+?)\s*$", re.MULTILINE)

# Falsifiability heuristic — every accepted prompt must contain at least one of
# these words. This is intentionally crude: the goal is to catch "look into X"
# style prompts that have no exit criterion, not to validate semantic depth.
_FALSIFIABLE_TOKENS = ("verify", "expect", "assert", "exit 0", "must")

# Satisfaction marker emitted by Synaptic at the end of a re-eval prompt.
_SATISFACTION_PATTERN = re.compile(
    r"SATISFACTION\s*:\s*(TRUE|FALSE)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AgentPrompt:
    id: str          # "E1", "E2", ...
    text: str        # the body after the marker
    falsifiable: bool  # whether the heuristic flagged it as having a success criterion


@dataclass
class SynapticResponse:
    raw: str
    prompts: list[AgentPrompt] = field(default_factory=list)
    error: Optional[str] = None
    retries: int = 0
    latency_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.prompts) == 5 and all(
            p.falsifiable for p in self.prompts
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_trace(record: dict) -> None:
    """Append a single JSON record to the Synaptic audit trace.

    Errors are swallowed to a counter (ZSF: never crash the loop because the
    audit log is unwritable, but record it).
    """
    try:
        SYNAPTIC_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SYNAPTIC_TRACE_PATH.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # pragma: no cover - I/O edge
        _bump_counter("synaptic_trace_write_errors", note=str(exc))


def _bump_counter(name: str, *, note: str = "") -> None:
    """Atomic-ish counter bump in /tmp/autopilot-counters.json.

    The archloop_runner owns the full counter file; this helper only nudges
    one key so that synaptic-side errors are visible even if the runner
    crashes before its own writer fires.
    """
    path = Path(os.environ.get(
        "AUTOPILOT_COUNTERS_PATH",
        "/tmp/autopilot-counters.json",
    ))
    try:
        if path.exists():
            data = json.loads(path.read_text() or "{}")
        else:
            data = {}
        data[name] = int(data.get(name, 0)) + 1
        if note:
            errs = data.setdefault("_last_errors", {})
            errs[name] = note[:200]
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
    except Exception:  # pragma: no cover
        pass


# Indirection layer: tests stub `_call_llm` so that we still exercise the
# parsing + trace + counter logic without hitting MLX/DeepSeek.
def _call_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    caller: str,
    timeout_s: float,
) -> Optional[str]:
    """Real LLM call goes through the priority queue (CLAUDE.md invariant)."""
    try:
        from memory.llm_priority_queue import llm_generate, Priority
    except Exception as exc:  # pragma: no cover - import-time wiring failure
        _bump_counter("synaptic_import_errors", note=str(exc))
        return None

    return llm_generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        priority=Priority.ATLAS,
        profile="s8_synaptic",
        caller=caller,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Parsers (pure functions — easy to unit-test)
# ---------------------------------------------------------------------------


def parse_prompts(raw: str) -> list[AgentPrompt]:
    """Extract `E<n> —|-|: <body>` prompts and tag each with falsifiability.

    Tolerant of:
      * em-dash, hyphen, or colon as the separator
      * extra whitespace / leading bullets
      * out-of-order numbering (we don't enforce 1..5 contiguity)
    """
    prompts: list[AgentPrompt] = []
    seen_ids: set[str] = set()
    for match in _PROMPT_PATTERN.finditer(raw):
        pid = f"E{match.group(1)}"
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        body = match.group(2).strip()
        falsifiable = any(tok in body.lower() for tok in _FALSIFIABLE_TOKENS)
        prompts.append(AgentPrompt(id=pid, text=body, falsifiable=falsifiable))
    return prompts


def parse_satisfaction(raw: str) -> Optional[bool]:
    """Return True/False if a `SATISFACTION: TRUE|FALSE` marker is found.

    None means the model did not emit the marker — caller decides whether to
    retry or treat as FALSE (the runner treats None as FALSE + counter bump).
    """
    if not raw:
        return None
    m = _SATISFACTION_PATTERN.search(raw)
    if not m:
        return None
    return m.group(1).upper() == "TRUE"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


SYSTEM_RANK = """\
You are Synaptic — the 8th Intelligence inside Aaron's ContextDNA fleet.

You receive a live snapshot of complexity vectors, persistence-check JSON,
and /metrics output. Your job is to:

  1. Pick the top-5 complexity vectors that need hardening *right now*.
  2. For each, emit ONE falsifiable agent prompt that a sub-agent can run.

Output format — STRICT. Each prompt MUST start at column 0 and look like:

  E1 — <body that includes verify/expect/assert/exit 0/must>
  E2 — ...
  E3 — ...
  E4 — ...
  E5 — ...

No preamble, no postscript, no markdown headers. The autopilot parser is regex-based
and will reject anything that doesn't match `^E\\d+ [—\\-:] .+(verify|expect|assert|exit 0|must).+$`.
"""

SYSTEM_SATISFACTION = """\
You are Synaptic. The autopilot just finished one cycle. You'll see:

  * The 5 agent summaries (one per prompt you issued)
  * The 3-surgeon cross-exam verdict

Decide whether this cycle's work satisfies the drift you flagged. End your
response with EXACTLY one of these markers on its own line:

  SATISFACTION: TRUE
  SATISFACTION: FALSE

If FALSE, briefly say which vector still drifts and what the next cycle should
hit. If TRUE, summarise the gain in <80 words and stop.
"""


def ask_for_vectors(
    live_state: dict,
    *,
    max_retries: int = 2,
    timeout_s: float = 90.0,
    caller: str = "autopilot.archloop.rank",
) -> SynapticResponse:
    """Ask Synaptic for top-5 vectors + 5 falsifiable agent prompts.

    Retries up to `max_retries` times on parse failure. After exhausting
    retries, returns a SynapticResponse with `error` set and `ok=False`;
    the runner is expected to abort that cycle and bump
    `synaptic_call_errors`.
    """
    user_prompt = (
        "LIVE STATE (truncated to keep within budget):\n"
        + json.dumps(live_state, indent=2, default=str)[:12_000]
        + "\n\nReturn EXACTLY 5 prompts (E1..E5)."
    )

    last_response = SynapticResponse(raw="", error="no_call_made")
    for attempt in range(max_retries + 1):
        t0 = time.time()
        raw = _call_llm(
            SYSTEM_RANK,
            user_prompt,
            caller=f"{caller}#try{attempt}",
            timeout_s=timeout_s,
        )
        latency = time.time() - t0

        record = {
            "ts": time.time(),
            "kind": "rank",
            "attempt": attempt,
            "system": SYSTEM_RANK[:400],
            "user": user_prompt[:400],
            "raw": (raw or "")[:4000],
            "latency_s": round(latency, 3),
        }
        _append_trace(record)

        if not raw:
            last_response = SynapticResponse(
                raw="",
                error="empty_response",
                retries=attempt,
                latency_s=latency,
            )
            _bump_counter("synaptic_call_errors", note="empty_response")
            continue

        prompts = parse_prompts(raw)
        resp = SynapticResponse(
            raw=raw,
            prompts=prompts,
            retries=attempt,
            latency_s=latency,
        )
        if resp.ok:
            return resp

        # Diagnostic for the retry log
        if len(prompts) != 5:
            resp.error = f"wrong_prompt_count:{len(prompts)}"
        else:
            bad = [p.id for p in prompts if not p.falsifiable]
            resp.error = f"non_falsifiable:{','.join(bad)}"
        _bump_counter("synaptic_parse_errors", note=resp.error or "")
        last_response = resp

    return last_response


def ask_satisfaction(
    cross_exam_verdict: str,
    agent_summaries: list[str],
    *,
    timeout_s: float = 60.0,
    caller: str = "autopilot.archloop.satisfaction",
) -> tuple[bool, str]:
    """Ask Synaptic whether this cycle resolved the drift.

    Returns (satisfied, raw_response). If the model fails to emit the marker
    or the call returns empty, returns (False, raw_or_error_message) and
    increments `synaptic_call_errors` so the runner can decide to abort or
    continue per its policy.
    """
    user_prompt = (
        "CROSS-EXAM VERDICT:\n"
        + (cross_exam_verdict or "(none)")[:4000]
        + "\n\nAGENT SUMMARIES:\n"
        + "\n---\n".join(s[:1500] for s in agent_summaries)
        + "\n\nEmit SATISFACTION: TRUE or SATISFACTION: FALSE on its own line."
    )

    t0 = time.time()
    raw = _call_llm(
        SYSTEM_SATISFACTION,
        user_prompt,
        caller=caller,
        timeout_s=timeout_s,
    )
    latency = time.time() - t0

    _append_trace({
        "ts": time.time(),
        "kind": "satisfaction",
        "system": SYSTEM_SATISFACTION[:400],
        "user": user_prompt[:400],
        "raw": (raw or "")[:4000],
        "latency_s": round(latency, 3),
    })

    if not raw:
        _bump_counter("synaptic_call_errors", note="satisfaction_empty")
        return (False, "(empty response — treated as not satisfied)")

    verdict = parse_satisfaction(raw)
    if verdict is None:
        _bump_counter("synaptic_parse_errors", note="satisfaction_marker_missing")
        return (False, raw)
    return (verdict, raw)


# ---------------------------------------------------------------------------
# G1 — Complexity classifier
# ---------------------------------------------------------------------------
#
# `classify_complexity` returns LOW/MED/HIGH for a SynapticResponse, used by
# the F2 archloop runner to gate the conditional DEEP_EXPLORATION stage.
#
# Aaron's feedback (G1 brief, verbatim):
#   "Class assignment respects Aaron's saved feedback: verify against live
#    complexity_vectors.db, NOT just the prompt text (prevents Synaptic from
#    over-promoting routine work)"
#
# So we *combine* keyword-scan + live DB lookup. Synaptic claiming a prompt
# is "architectural" without a matching high-risk/high-drift vector in the
# live DB does NOT promote to HIGH — it promotes to MED at most.


class ComplexityClass(str, Enum):
    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"


# Same trigger keywords as deep_exploration.py — kept in sync intentionally.
_COMPLEXITY_TRIGGER_KEYWORDS = (
    "architectural",
    "irreversible",
    "schema change",
    "destructive",
    "security",
)

_DEFAULT_HIGH_RISK_FLOOR = 9.0
_DEFAULT_HIGH_DRIFT_FLOOR = 80.0
_DEFAULT_MED_RISK_FLOOR = 6.0
_DEFAULT_MED_DRIFT_FLOOR = 50.0


def _read_live_complexity_vectors(db_path: Optional[str] = None) -> list[dict]:
    """Pull the top-N complexity vectors from the live SQLite db.

    Returns an empty list if the DB is missing or the schema doesn't match
    — the classifier degrades to keyword-only (which we cap at MED so we
    don't over-promote on missing data).
    """
    if db_path is None:
        db_path = os.environ.get(
            "AUTOPILOT_COMPLEXITY_DB",
            str(Path.home() / ".context-dna/complexity_vectors.db"),
        )
    p = Path(db_path)
    if not p.exists():
        return []
    try:
        con = sqlite3.connect(str(p))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT vector_id, name, category, risk_score, "
            "drift_ranking_score "
            "FROM complexity_vectors "
            "ORDER BY drift_ranking_score DESC LIMIT 20"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        _bump_counter("complexity_db_read_errors", note=str(exc))
        return []


def classify_complexity(
    synaptic_response: "SynapticResponse",
    *,
    live_vectors: Optional[list[dict]] = None,
    risk_high: float = _DEFAULT_HIGH_RISK_FLOOR,
    drift_high: float = _DEFAULT_HIGH_DRIFT_FLOOR,
    risk_med: float = _DEFAULT_MED_RISK_FLOOR,
    drift_med: float = _DEFAULT_MED_DRIFT_FLOOR,
    db_path: Optional[str] = None,
) -> ComplexityClass:
    """Return LOW / MED / HIGH for the given Synaptic response.

    Decision matrix:

      HIGH = ANY live vector has risk_score >= risk_high (default 9.0)
           OR ANY live vector has drift_ranking_score >= drift_high (80)
           OR (synaptic prompts contain a trigger keyword AND there is at
               least one MED-or-higher live vector — i.e. keyword spam
               alone CANNOT promote to HIGH per Aaron's feedback)
      MED  = ANY live vector has risk_score >= risk_med (6.0) OR
             drift_ranking_score >= drift_med (50), OR trigger keyword
             match alone (capped at MED)
      LOW  = otherwise (typical baseline drift)
    """
    if live_vectors is None:
        live_vectors = _read_live_complexity_vectors(db_path)

    # Live-DB signals first — these are the load-bearing inputs.
    high_from_db = False
    med_from_db = False
    for v in live_vectors or []:
        try:
            rs = float(v.get("risk_score") or 0.0)
            ds = float(v.get("drift_ranking_score") or 0.0)
        except (TypeError, ValueError):
            continue
        if rs >= risk_high or ds >= drift_high:
            high_from_db = True
            break
        if rs >= risk_med or ds >= drift_med:
            med_from_db = True

    # Keyword scan over Synaptic's prompt text — case-insensitive.
    prompt_text = ""
    for p in (getattr(synaptic_response, "prompts", None) or []):
        prompt_text += " " + (getattr(p, "text", "") or "")
    raw_text = getattr(synaptic_response, "raw", "") or ""
    blob = (prompt_text + " " + raw_text).lower()
    keyword_hit = any(kw in blob for kw in _COMPLEXITY_TRIGGER_KEYWORDS)

    if high_from_db:
        return ComplexityClass.HIGH
    # Aaron's rule: keyword alone doesn't reach HIGH — it caps at MED, and
    # only reaches HIGH when corroborated by a MED-or-higher live vector.
    if keyword_hit and med_from_db:
        return ComplexityClass.HIGH
    if keyword_hit or med_from_db:
        return ComplexityClass.MED
    return ComplexityClass.LOW


# ---------------------------------------------------------------------------
# Manual smoke (don't actually call this without a healthy LLM stack)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    sample = {
        "complexity_vectors": [
            {"vector_id": "v1", "name": "nats_replicas", "drift_ranking_score": 0.6},
            {"vector_id": "v2", "name": "kv_counter", "drift_ranking_score": 0.3},
        ],
        "metrics_excerpt": "webhook_publish_errors 0\nsubscription_count 4\n",
    }
    print(asdict(ask_for_vectors(sample)))
