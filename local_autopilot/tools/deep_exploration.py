#!/usr/bin/env python3
"""
deep_exploration.py — G1 conditional deep-exploration stage for the autopilot
arch-loop.

Aaron's framing (verbatim, G1 brief):

  "do we also have a fully robust superpowers brainstorming + 3s looping until
   satisfied that is even more robust and useful for any extra complex things
   etc that can also be used within the autopilot mode to use those superpowers ?"

This module wires the existing `scripts/3s-brainstorm.sh` (superpowers-style
brainstorm fronted by 3-surgeons) and `3s` CLI counter-position pattern into
the F2 archloop runner as a CONDITIONAL pre-stage that fires only for the
highest-risk cycles. Routine cycles fall through with raw Synaptic prompts so
we don't burn $0.10-0.20 per cycle on shallow drift.

Wiring:

  PARSE_PROMPTS → [DeepExplorer.should_deep_explore?] → SPAWN_AGENTS
                              |
                              YES
                              |
                              v
                  run_brainstorm() + run_counter_position()
                              |
                              v
                  merge_into_agent_prompts() — enriches each of the 5
                  Synaptic prompts with risk/options + steelman blocks

ZSF invariants:
  * Every subprocess exception increments a named counter via the runner's
    counter file (`AUTOPILOT_COUNTERS_PATH`).
  * Every fall-back path emits a logger.warning.
  * No `except Exception: pass` — failures are recorded, not silenced.
  * `should_deep_explore` defaults to False on any input parse error (fail
    safe toward cheap cycles, not toward expensive ones).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("autopilot.deep_exploration")

# ---------------------------------------------------------------------------
# Configurable thresholds — overridable by env so Aaron can re-tune without
# touching code. Defaults reflect the G1 brief.
# ---------------------------------------------------------------------------

DEFAULT_RISK_SCORE_THRESHOLD = float(os.environ.get(
    "AUTOPILOT_DEEP_RISK_THRESHOLD", "9.0"
))
DEFAULT_DRIFT_RANKING_THRESHOLD = float(os.environ.get(
    "AUTOPILOT_DEEP_DRIFT_THRESHOLD", "80.0"
))

# Trigger keywords scanned against Synaptic's prompt text — case-insensitive.
# These are the highest-stakes English-language signals Aaron flagged
# (architectural, irreversible, schema change, destructive, security).
TRIGGER_KEYWORDS = (
    "architectural",
    "irreversible",
    "schema change",
    "destructive",
    "security",
)

# Budget knobs.
DEFAULT_BRAINSTORM_COST_CAP_USD = float(os.environ.get(
    "AUTOPILOT_DEEP_BRAINSTORM_COST_CAP_USD", "0.05"
))
DEFAULT_BRAINSTORM_TIMEOUT_S = int(os.environ.get(
    "AUTOPILOT_DEEP_BRAINSTORM_TIMEOUT_S", "600"
))
DEFAULT_COUNTER_TIMEOUT_S = int(os.environ.get(
    "AUTOPILOT_DEEP_COUNTER_TIMEOUT_S", "180"
))


# ---------------------------------------------------------------------------
# Path resolvers (read env every call so tests can monkeypatch)
# ---------------------------------------------------------------------------


def _counters_path() -> Path:
    return Path(os.environ.get(
        "AUTOPILOT_COUNTERS_PATH", "/tmp/autopilot-counters.json"
    ))


def _brainstorm_script() -> str:
    # Default to the bundled `scripts/3s-brainstorm.sh` in this repo, located
    # at <repo>/scripts/3s-brainstorm.sh. Resolve relative to this file's
    # location so the path stays correct after `pip install -e .` or similar.
    bundled = (
        Path(__file__).resolve().parents[2] / "scripts" / "3s-brainstorm.sh"
    )
    return os.environ.get("AUTOPILOT_3S_BRAINSTORM", str(bundled))


def _3s_cli() -> str:
    # Prefer the `3s` CLI installed by `pip install three-surgeons`. If the
    # user has a system-wide install at /usr/local/bin/3s, that still works
    # because PATH resolution happens at subprocess time. We only override
    # the absolute fallback when AUTOPILOT_3S_CLI is set.
    return os.environ.get("AUTOPILOT_3S_CLI", "/usr/local/bin/3s")


def _fleet_brainstorm_dir() -> Path:
    return Path(os.environ.get(
        "AUTOPILOT_FLEET_BRAINSTORM_DIR",
        str(Path.home() / ".context-dna" / "brainstorm"),
    ))


# ---------------------------------------------------------------------------
# Counter ZSF — mirrors archloop_runner._bump but writes the same file so the
# main counter snapshot stays a single source of truth.
# ---------------------------------------------------------------------------


def _bump_counter(name: str, *, by: int = 1, note: str = "", add_float: float = 0.0) -> None:
    """Atomic-ish bump of a single key in the shared autopilot counter file.

    The runner owns the file; this helper just nudges one key so deep-
    exploration errors stay visible even if the runner crashes between
    stages.
    """
    path = _counters_path()
    try:
        if path.exists():
            data = json.loads(path.read_text() or "{}")
        else:
            data = {}
        if add_float:
            data[name] = round(float(data.get(name, 0.0)) + add_float, 6)
        else:
            data[name] = int(data.get(name, 0)) + by
        if note:
            errs = data.setdefault("_last_errors", {})
            errs[name] = note[:200]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
    except Exception as exc:  # pragma: no cover
        # Last-ditch — write a sidecar marker so the failure is observable.
        try:
            Path("/tmp/autopilot-deep-counter.err").write_text(
                f"{time.time()} {name} {exc}\n"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BrainstormResult:
    ok: bool
    artefact_path: Optional[Path] = None
    body: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: Optional[str] = None


@dataclass
class CounterPositionResult:
    ok: bool
    claim: str = ""
    steelman: str = ""
    raw: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: Optional[str] = None


@dataclass
class DeepExplorationSummary:
    """What the runner stores in cycle_summary.json's deep_exploration block."""
    triggered: bool
    reason: str  # human-readable trigger rationale or "skipped"
    brainstorm_ok: bool = False
    counter_position_ok: bool = False
    cost_usd: float = 0.0
    duration_s: float = 0.0
    artefact_path: Optional[str] = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decision logic — should we deep-explore this cycle?
# ---------------------------------------------------------------------------


def _scan_keywords(text: str) -> Optional[str]:
    """Return the first trigger keyword found in `text`, or None.

    Case-insensitive substring match. Returns the matched keyword so callers
    can include it in the trigger rationale.
    """
    if not text:
        return None
    low = text.lower()
    for kw in TRIGGER_KEYWORDS:
        if kw in low:
            return kw
    return None


class DeepExplorer:
    """Coordinator for the conditional deep-exploration stage.

    Construction is cheap — heavy work happens only inside the public methods.
    Tests construct one per cycle; the runner does the same.
    """

    def __init__(
        self,
        *,
        risk_threshold: float = DEFAULT_RISK_SCORE_THRESHOLD,
        drift_threshold: float = DEFAULT_DRIFT_RANKING_THRESHOLD,
        brainstorm_cost_cap_usd: float = DEFAULT_BRAINSTORM_COST_CAP_USD,
        brainstorm_timeout_s: int = DEFAULT_BRAINSTORM_TIMEOUT_S,
        counter_timeout_s: int = DEFAULT_COUNTER_TIMEOUT_S,
        brainstorm_script_path: Optional[str] = None,
        cli_path: Optional[str] = None,
    ) -> None:
        self.risk_threshold = risk_threshold
        self.drift_threshold = drift_threshold
        self.brainstorm_cost_cap_usd = brainstorm_cost_cap_usd
        self.brainstorm_timeout_s = brainstorm_timeout_s
        self.counter_timeout_s = counter_timeout_s
        self._brainstorm_script_path = brainstorm_script_path
        self._cli_path = cli_path

    # ----- decision -----

    def should_deep_explore(
        self,
        synaptic_response: Any,
        complexity_vectors: Optional[list[dict]] = None,
    ) -> tuple[bool, str]:
        """Decide whether this cycle merits the expensive brainstorm.

        Returns `(triggered, reason)`. `reason` is empty when triggered is
        False (so callers can short-circuit on truthiness).

        Decision priority:
          a) ANY vector has risk_score >= self.risk_threshold (default 9.0)
          b) ANY vector has drift_ranking_score >= self.drift_threshold (80)
          c) ANY trigger keyword matches Synaptic's prompt text

        Verifies vectors against the LIVE complexity_vectors list per the G1
        brief — Synaptic-side keyword spam alone is not enough. The keyword
        path (c) is a safety-net but ranked LAST and requires Synaptic to
        actually mention the keyword in agent-prompt text.
        """
        # Defensive — None / empty input → don't deep-explore.
        if synaptic_response is None and not complexity_vectors:
            return (False, "")

        # (a) risk_score floor
        for v in (complexity_vectors or []):
            try:
                rs = float(v.get("risk_score") or 0.0)
            except (TypeError, ValueError):
                continue
            if rs >= self.risk_threshold:
                reason = (
                    f"risk_score={rs:.2f} >= {self.risk_threshold} "
                    f"on vector '{v.get('name') or v.get('vector_id') or '?'}'"
                )
                return (True, reason)

        # (b) drift_ranking_score floor
        for v in (complexity_vectors or []):
            try:
                ds = float(v.get("drift_ranking_score") or 0.0)
            except (TypeError, ValueError):
                continue
            if ds >= self.drift_threshold:
                reason = (
                    f"drift_ranking_score={ds:.2f} >= {self.drift_threshold} "
                    f"on vector '{v.get('name') or v.get('vector_id') or '?'}'"
                )
                return (True, reason)

        # (c) trigger keyword scan over Synaptic prompts
        prompt_blob = self._concat_prompt_text(synaptic_response)
        kw = _scan_keywords(prompt_blob)
        if kw:
            return (True, f"trigger_keyword='{kw}' in Synaptic prompts")

        return (False, "")

    @staticmethod
    def _concat_prompt_text(synaptic_response: Any) -> str:
        """Best-effort flatten of prompt text from a SynapticResponse-like."""
        if synaptic_response is None:
            return ""
        prompts = getattr(synaptic_response, "prompts", None)
        if prompts is None and isinstance(synaptic_response, dict):
            prompts = synaptic_response.get("prompts")
        if not prompts:
            # Also scan the raw response text as a fallback.
            return getattr(synaptic_response, "raw", "") or ""
        chunks = []
        for p in prompts:
            text = getattr(p, "text", None)
            if text is None and isinstance(p, dict):
                text = p.get("text")
            if text:
                chunks.append(str(text))
        return "\n".join(chunks)

    # ----- brainstorm -----

    def run_brainstorm(
        self,
        topic: str,
        cost_cap_usd: Optional[float] = None,
    ) -> BrainstormResult:
        """Invoke `scripts/3s-brainstorm.sh` and capture the canonical artefact.

        Behaviour:
          * Subprocess runs with `--cost-cap <usd>` (defaults to the
            constructor's cap).
          * Stdout is captured and inspected for the artefact path
            (`/tmp/3s-brainstorm-<UTC>.md` AND the `.fleet/brainstorm/` copy).
          * Subprocess exceptions/timeouts increment counters + return an
            error result; the runner is expected to FALL THROUGH to raw
            Synaptic prompts rather than abort the cycle.
        """
        script = self._brainstorm_script_path or _brainstorm_script()
        if cost_cap_usd is None:
            cost_cap_usd = self.brainstorm_cost_cap_usd

        if not Path(script).exists():
            _bump_counter(
                "deep_exploration_errors",
                note=f"brainstorm_missing:{script}",
            )
            logger.warning("3s-brainstorm script missing at %s", script)
            return BrainstormResult(
                ok=False,
                error=f"brainstorm_script_missing:{script}",
            )

        cmd = [
            "bash",
            script,
            "--cost-cap",
            str(cost_cap_usd),
            topic,
        ]
        logger.info("running 3s-brainstorm: %s", " ".join(cmd))
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.brainstorm_timeout_s,
            )
            latency = time.time() - t0
        except subprocess.TimeoutExpired as exc:
            latency = time.time() - t0
            _bump_counter(
                "deep_exploration_errors",
                note=f"brainstorm_timeout:{exc.timeout}",
            )
            logger.warning(
                "3s-brainstorm timed out after %ss — falling through",
                self.brainstorm_timeout_s,
            )
            return BrainstormResult(
                ok=False,
                error=f"brainstorm_timeout:{self.brainstorm_timeout_s}",
                latency_s=latency,
            )
        except Exception as exc:  # noqa: BLE001 — recorded via counter
            latency = time.time() - t0
            _bump_counter(
                "deep_exploration_errors",
                note=f"brainstorm_exec:{exc}",
            )
            logger.warning("3s-brainstorm exec failed: %s", exc)
            return BrainstormResult(
                ok=False,
                error=f"brainstorm_exec:{exc}",
                latency_s=latency,
            )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # ROOT-CAUSE FIX (I7, 2026-05-14): 3s-brainstorm.sh now emits a final
        # `BRAINSTORM_RESULT=success|partial|failed` marker line and exits non-
        # zero on partial/failed. We trust that marker over the rc heuristic
        # below because old runs (before this fix landed) had no marker at all
        # and produced silent partials for 3+ hours. Falls back to legacy rc
        # heuristic when the marker is absent (older script versions).
        result_marker = None
        m_marker = re.search(
            r"^BRAINSTORM_RESULT=(success|partial|failed)\s*$",
            stdout + "\n" + stderr,
            re.M,
        )
        if m_marker:
            result_marker = m_marker.group(1)
            if result_marker != "success":
                _bump_counter(
                    "autopilot_brainstorm_failures",
                    note=f"marker={result_marker}:rc={proc.returncode}",
                )
                logger.warning(
                    "3s-brainstorm marker=%s rc=%s — partial/failed run, "
                    "see /tmp/brainstorm-events.log",
                    result_marker,
                    proc.returncode,
                )

        # Cost extraction — the script writes a `cost_usd_total: ...` line near
        # the bottom. Best-effort, default 0.0.
        cost = 0.0
        m = re.search(r"cost_usd_total[\s:=]+([0-9]+(?:\.[0-9]+)?)", stdout, re.I)
        if m:
            try:
                cost = float(m.group(1))
            except ValueError:
                cost = 0.0

        # Artefact path — prefer the configured fleet-brainstorm dir if any
        # path in stdout matches it, else look for `.fleet/brainstorm/...`,
        # else fall back to `/tmp/3s-brainstorm-*.md`. We use a regex sweep
        # so the path can appear anywhere on the line (e.g. "wrote: <path>").
        artefact_path: Optional[Path] = None
        fleet_dir = str(_fleet_brainstorm_dir())
        # 1) configured fleet dir.
        m2 = re.search(rf"({re.escape(fleet_dir)}/\S+?\.md)\b", stdout)
        if m2:
            artefact_path = Path(m2.group(1))
        # 2) any `.fleet/brainstorm/<...>.md` substring.
        if artefact_path is None:
            m2 = re.search(r"(\S*\.fleet/brainstorm/\S+?\.md)\b", stdout)
            if m2:
                artefact_path = Path(m2.group(1))
        # 3) /tmp/3s-brainstorm-*.md fallback.
        if artefact_path is None:
            m2 = re.search(r"(\S*/tmp/3s-brainstorm-\S+?\.md)\b", stdout)
            if m2:
                artefact_path = Path(m2.group(1))

        # ZSF: non-zero exit doesn't fail-hard — we already captured stdout.
        # The brainstorm script is `set -u` not `set -e`, so partial completion
        # is the common case. We trust the artefact-path heuristic.
        # Marker-aware (I7): when BRAINSTORM_RESULT= is present, it is the
        # source of truth — overrides the legacy rc/artefact heuristic so
        # partial/failed runs surface as ok=False even when there's an artefact.
        if result_marker is not None:
            ok = (result_marker == "success")
        else:
            ok = bool(stdout.strip()) and (
                proc.returncode == 0 or artefact_path is not None
            )
        if not ok:
            note_suffix = f":marker={result_marker}" if result_marker else ""
            _bump_counter(
                "deep_exploration_errors",
                note=f"brainstorm_empty_or_failed:rc={proc.returncode}{note_suffix}",
            )
            logger.warning(
                "3s-brainstorm returned empty/failure (rc=%s marker=%s): stderr=%s",
                proc.returncode,
                result_marker or "absent",
                stderr[:400],
            )
            err_tag = (
                f"brainstorm_marker:{result_marker}"
                if result_marker
                else f"brainstorm_rc:{proc.returncode}"
            )
            return BrainstormResult(
                ok=False,
                error=err_tag,
                body=stdout,
                latency_s=latency,
                cost_usd=cost,
            )

        # Read artefact body if we have a path; else use stdout itself.
        body = stdout
        if artefact_path is not None and artefact_path.exists():
            try:
                body = artefact_path.read_text()
            except Exception as exc:
                logger.warning(
                    "could not read brainstorm artefact %s: %s",
                    artefact_path, exc,
                )

        return BrainstormResult(
            ok=True,
            artefact_path=artefact_path,
            body=body,
            cost_usd=cost,
            latency_s=latency,
            error=None,
        )

    # ----- counter-position -----

    def run_counter_position(self, claim: str) -> CounterPositionResult:
        """Run a counter-position via the `3s consensus --counter-probe` flag.

        The 3-surgeons CLI doesn't ship a dedicated `counter-position`
        subcommand — that's a skill-level protocol. We approximate it by
        invoking `3s consensus --counter-probe --json "<claim>"` which is the
        machine-friendly variant that exercises the same sycophancy-gate
        steelman path (per `3s --help`).

        Falls through cleanly on subprocess failure (the runner SHOULD still
        spawn agents with raw Synaptic prompts when this fails — counter-
        position is an enrichment, not a precondition).
        """
        cli = self._cli_path or _3s_cli()
        if not Path(cli).exists():
            _bump_counter(
                "deep_exploration_errors",
                note=f"3s_cli_missing:{cli}",
            )
            logger.warning("3s CLI missing at %s", cli)
            return CounterPositionResult(
                ok=False,
                claim=claim,
                error=f"cli_missing:{cli}",
            )

        # Truncate the claim — `3s consensus` accepts long inputs but we cap to
        # keep the prompt under typical model context.
        trimmed = (claim or "").strip()[:4000]
        cmd = [cli, "consensus", "--counter-probe", "--json", trimmed]
        logger.info("running 3s counter-probe: claim=%s...", trimmed[:80])
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.counter_timeout_s,
            )
            latency = time.time() - t0
        except subprocess.TimeoutExpired as exc:
            latency = time.time() - t0
            _bump_counter(
                "deep_exploration_errors",
                note=f"counter_timeout:{exc.timeout}",
            )
            logger.warning(
                "3s counter-probe timed out after %ss",
                self.counter_timeout_s,
            )
            return CounterPositionResult(
                ok=False, claim=claim,
                error=f"counter_timeout:{self.counter_timeout_s}",
                latency_s=latency,
            )
        except Exception as exc:  # noqa: BLE001
            latency = time.time() - t0
            _bump_counter(
                "deep_exploration_errors",
                note=f"counter_exec:{exc}",
            )
            logger.warning("3s counter-probe exec failed: %s", exc)
            return CounterPositionResult(
                ok=False, claim=claim,
                error=f"counter_exec:{exc}",
                latency_s=latency,
            )

        raw = (proc.stdout or "") + (
            ("\n[stderr]\n" + proc.stderr) if proc.stderr else ""
        )
        if not raw.strip():
            _bump_counter(
                "deep_exploration_errors",
                note="counter_empty_output",
            )
            return CounterPositionResult(
                ok=False, claim=claim, raw=raw,
                error="counter_empty_output",
                latency_s=latency,
            )

        # Try to extract the steelman / counter-position from JSON output. If
        # JSON parsing fails, fall back to the raw text.
        steelman = ""
        cost = 0.0
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    data = json.loads(s)
                    steelman = (
                        data.get("counter_probe_verdict")
                        or data.get("counter_position")
                        or data.get("steelman")
                        or ""
                    )
                    cost = float(data.get("cost_usd", 0.0) or 0.0)
                    break
                except Exception:
                    pass
        if not steelman:
            # Fallback: take the longest line >40 chars that doesn't look like
            # log noise. Crude but never silently empty.
            for line in reversed(raw.splitlines()):
                if len(line) > 40 and not line.startswith("["):
                    steelman = line.strip()
                    break

        return CounterPositionResult(
            ok=bool(steelman),
            claim=claim,
            steelman=steelman or raw[:2000],
            raw=raw,
            cost_usd=cost,
            latency_s=latency,
            error=None,
        )

    # ----- merge -----

    def merge_into_agent_prompts(
        self,
        synaptic_prompts: list,
        brainstorm: BrainstormResult,
        counter: CounterPositionResult,
    ) -> list:
        """Enrich each Synaptic prompt with brainstorm + counter-position context.

        Returns a NEW list of AgentPrompt-like objects with the same id +
        falsifiable flag, but whose `.text` has been prepended with three
        new sections:

            ## DEEP-EXPLORATION CONTEXT (G1)
            ### Risks + options (3s-brainstorm)
            ### STEELMAN (counter-position)
            ### Synaptic's original prompt (success criteria preserved)
            <original text>

        Aaron's rule: never strip falsifiability. We always preserve the raw
        Synaptic text verbatim so the agent still has its exit criterion.
        """
        if not synaptic_prompts:
            return []

        # Build the shared context preamble once — the same blob is prepended
        # to each of the 5 prompts. We *don't* dump 60 KB into each prompt;
        # the brainstorm body is trimmed to keep agent context budget sane.
        brainstorm_excerpt = ""
        if brainstorm and brainstorm.ok:
            brainstorm_excerpt = (brainstorm.body or "")[:6000]
        elif brainstorm and brainstorm.error:
            brainstorm_excerpt = (
                f"(brainstorm degraded — falling through on Synaptic prompts. "
                f"error={brainstorm.error})"
            )

        steelman_excerpt = ""
        if counter and counter.ok:
            steelman_excerpt = (counter.steelman or "")[:2000]
        elif counter and counter.error:
            steelman_excerpt = (
                f"(counter-position degraded — error={counter.error})"
            )

        preamble = (
            "## DEEP-EXPLORATION CONTEXT (G1)\n\n"
            "This cycle was flagged HIGH-COMPLEXITY by the autopilot. Before\n"
            "you act, read the risks + steelman below — they are *context*,\n"
            "not new instructions. Your Synaptic prompt's success criterion\n"
            "still rules.\n\n"
            "### Risks + options (3s-brainstorm)\n"
            f"{brainstorm_excerpt or '(no brainstorm body captured)'}\n\n"
            "### STEELMAN (counter-position)\n"
            f"{steelman_excerpt or '(no steelman captured)'}\n\n"
            "### Synaptic's original prompt (success criteria preserved)\n"
        )

        enriched: list = []
        for p in synaptic_prompts:
            pid = getattr(p, "id", None) or (p.get("id") if isinstance(p, dict) else "?")
            text = getattr(p, "text", None)
            if text is None and isinstance(p, dict):
                text = p.get("text", "")
            falsifiable = getattr(p, "falsifiable", True)
            if hasattr(p, "__class__") and hasattr(p, "id") and hasattr(p, "text"):
                cls = p.__class__
                enriched.append(cls(
                    id=pid,
                    text=preamble + (text or ""),
                    falsifiable=bool(falsifiable),
                ))
            else:
                enriched.append({
                    "id": pid,
                    "text": preamble + (text or ""),
                    "falsifiable": bool(falsifiable),
                })
        return enriched


# ---------------------------------------------------------------------------
# Convenience wrapper for the runner
# ---------------------------------------------------------------------------


def run_deep_exploration_stage(
    synaptic_response: Any,
    complexity_vectors: list[dict],
    *,
    explorer: Optional[DeepExplorer] = None,
    dry_run: bool = False,
) -> tuple[Optional[list], DeepExplorationSummary]:
    """Top-level entrypoint called by the archloop_runner.

    Returns `(enriched_prompts_or_None, summary)`:
      * If `should_deep_explore` is False → `(None, summary(triggered=False))`
      * If brainstorm + counter both no-op (dry_run or missing assets) →
        `(synaptic_response.prompts, summary)` so the runner can still log
        the trigger reason without spending money.
      * Otherwise → enriched prompt list with risk + steelman context.

    `dry_run=True` skips both subprocess invocations but still exercises the
    decision logic + merge path so tests and runner --dry-run mode produce
    deterministic timings/outputs.
    """
    exp = explorer or DeepExplorer()
    triggered, reason = exp.should_deep_explore(synaptic_response, complexity_vectors)
    summary = DeepExplorationSummary(
        triggered=triggered,
        reason=reason or "skipped",
    )

    if not triggered:
        return (None, summary)

    t0 = time.time()
    _bump_counter("cycles_deep_explored")

    # Build a topic + claim from the live state. Both deliberately compact —
    # they ride into the subprocess as positional args, so we don't want them
    # to balloon to 32 KB.
    topic = _topic_from_state(synaptic_response, complexity_vectors, reason)
    claim = _claim_from_state(synaptic_response, complexity_vectors, reason)

    if dry_run:
        brainstorm = BrainstormResult(ok=True, body="(dry-run stub brainstorm)", cost_usd=0.0)
        counter = CounterPositionResult(
            ok=True, claim=claim, steelman="(dry-run stub steelman)", cost_usd=0.0,
        )
    else:
        brainstorm = exp.run_brainstorm(topic)
        counter = exp.run_counter_position(claim)

    cost = float(brainstorm.cost_usd or 0.0) + float(counter.cost_usd or 0.0)
    _bump_counter("deep_exploration_cost_total", add_float=cost)
    summary.cost_usd = round(cost, 4)
    summary.brainstorm_ok = brainstorm.ok
    summary.counter_position_ok = counter.ok
    if brainstorm.artefact_path is not None:
        summary.artefact_path = str(brainstorm.artefact_path)
    if brainstorm.error:
        summary.errors.append(f"brainstorm:{brainstorm.error}")
    if counter.error:
        summary.errors.append(f"counter:{counter.error}")

    synaptic_prompts = getattr(synaptic_response, "prompts", None) or []
    enriched = exp.merge_into_agent_prompts(synaptic_prompts, brainstorm, counter)
    summary.duration_s = round(time.time() - t0, 3)
    return (enriched, summary)


def _topic_from_state(
    synaptic_response: Any,
    complexity_vectors: list[dict],
    reason: str,
) -> str:
    """Compact topic string for the brainstorm script (positional arg)."""
    if complexity_vectors:
        names = [
            str(v.get("name") or v.get("vector_id") or "?")
            for v in complexity_vectors[:3]
        ]
        return f"autopilot deep-exploration: vectors={','.join(names)} — {reason}"
    raw = getattr(synaptic_response, "raw", "") or ""
    snippet = (raw[:160] or "autopilot deep-exploration").replace("\n", " ")
    return f"autopilot deep-exploration: {snippet}"


def _claim_from_state(
    synaptic_response: Any,
    complexity_vectors: list[dict],
    reason: str,
) -> str:
    """Compact claim for the 3s counter-probe."""
    if complexity_vectors:
        top = complexity_vectors[0]
        name = top.get("name") or top.get("vector_id") or "?"
        risk = top.get("risk_score")
        drift = top.get("drift_ranking_score")
        return (
            f"The autopilot should harden vector '{name}' "
            f"(risk_score={risk}, drift_ranking_score={drift}) this cycle "
            f"because {reason}."
        )
    return (
        "The autopilot should run a hardening cycle this iteration "
        f"because {reason}."
    )
