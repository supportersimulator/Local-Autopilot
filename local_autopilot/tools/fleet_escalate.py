#!/usr/bin/env python3
"""
fleet_escalate.py — Chief-node adjudication for unsatisfied autopilot runs.

When the archloop runner exhausts its `--cycles` budget without reaching
`satisfied=True`, it can opt-in to escalate the unresolved question to the
Multi-Fleet chief node (mac1) for judgment. The chief node has the most
mature ContextDNA database and a different vendor LLM (Claude Opus via
mac1's Atlas session), so it can break ties or surface considerations the
local Synaptic + 3-Surgeons missed.

Transport: POST to the local fleet daemon at
http://127.0.0.1:8855/message — the daemon forwards to mac1 via the
7-priority channel ladder (NATS → HTTP → chief relay → seed → SSH → WoL →
git).

ZSF: every failure path bumps a named counter; we never raise into the
runner. The runner just keeps moving and emits a JSON event so the caller
(launchd/systemd) sees the escalation outcome.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Optional

try:
    import urllib.request as _urlreq
    import urllib.error as _urlerr
except Exception:  # pragma: no cover — stdlib always present
    _urlreq = None
    _urlerr = None


DEFAULT_FLEET_URL = "http://127.0.0.1:8855/message"
DEFAULT_TIMEOUT_S = 10
_EXCERPT_CHARS = 2000


def _read_excerpt(path: Path, limit: int = _EXCERPT_CHARS) -> str:
    try:
        if path.exists():
            return path.read_text(errors="replace")[:limit]
    except Exception:
        return ""
    return ""


def _summarise_agent_results(cycle_dir: Path) -> list[dict[str, Any]]:
    """Read agent_*.result files and produce a tiny summary list.

    Each entry: {"id": int, "status": "PASS"|"FAIL"|..., "len": int}
    """
    out: list[dict[str, Any]] = []
    try:
        files = sorted(cycle_dir.glob("agent_*.result"))
    except Exception:
        return out
    for f in files:
        try:
            body = f.read_text(errors="replace")
        except Exception:
            continue
        status = "UNKNOWN"
        for line in body.splitlines()[:5]:
            s = line.strip().upper()
            if s.startswith("STATUS:"):
                status = s.split(":", 1)[1].strip()
                break
        # Try to extract an integer id out of "agent_3.result"
        try:
            stem = f.stem  # agent_3
            agent_id = int(stem.split("_", 1)[1])
        except Exception:
            agent_id = -1
        out.append({"id": agent_id, "status": status, "len": len(body)})
    return out


def build_packet(
    *,
    cycle_dir: Path,
    cycles_run: int,
    node_id: Optional[str] = None,
) -> dict[str, Any]:
    """Construct the fleet escalation packet."""
    if node_id is None:
        node_id = os.environ.get("MULTIFLEET_NODE_ID") or socket.gethostname()

    cross_exam_excerpt = _read_excerpt(cycle_dir / "cross_exam.txt")
    synaptic_excerpt = _read_excerpt(cycle_dir / "synaptic_re_eval.md")
    agent_summary = _summarise_agent_results(cycle_dir)

    return {
        "type": "autopilot_escalation",
        "to": "mac1",
        "from": node_id,
        "payload": {
            "subject": (
                "autopilot: "
                f"{cycles_run} cycles without satisfaction — "
                "chief judgment requested"
            ),
            "priority": "P2",
            "cycle_dir": str(cycle_dir),
            "cycles_run": cycles_run,
            "cross_exam_excerpt": cross_exam_excerpt,
            "synaptic_re_eval_excerpt": synaptic_excerpt,
            "agent_results_summary": agent_summary,
            "ask": (
                "Chief: please review the unresolved question and either "
                "(a) sign off if you see a path forward, "
                "(b) suggest the next experiment, or "
                "(c) return UNRESOLVABLE if this needs Aaron."
            ),
        },
    }


def _bump(counters: Optional[dict], key: str, note: str = "") -> None:
    if counters is None:
        return
    try:
        counters[key] = int(counters.get(key, 0)) + 1
        if note:
            errs = counters.setdefault("_last_errors", {})
            errs[key] = note[:200]
    except Exception:
        # Even the counter bump must not raise — ZSF
        pass


def _post_json(url: str, packet: dict[str, Any], timeout_s: int) -> tuple[bool, int, str]:
    """POST a JSON packet. Returns (ok, status, body). Never raises."""
    if _urlreq is None:
        return False, 0, "urllib_unavailable"
    try:
        data = json.dumps(packet).encode("utf-8")
        req = _urlreq.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=timeout_s) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
            body = resp.read().decode("utf-8", errors="replace")
            return (200 <= status < 300), status, body
    except _urlerr.HTTPError as exc:  # type: ignore[union-attr]
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        return False, int(getattr(exc, "code", 0) or 0), body
    except Exception as exc:
        return False, 0, f"{type(exc).__name__}:{exc}"


def escalate_to_fleet_chief(
    *,
    cycle_dir: Path,
    cycles_run: int,
    url: str = DEFAULT_FLEET_URL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    counters: Optional[dict] = None,
    node_id: Optional[str] = None,
) -> dict[str, Any]:
    """Send the escalation packet. Returns an outcome dict (never raises).

    Outcome shape:
        {
          "delivered": bool,
          "status": int,       # HTTP status (0 if no response)
          "channel": str,      # "http_fleet_daemon" or "skipped"
          "detail": str,
          "packet": {...},
        }
    """
    outcome: dict[str, Any] = {
        "delivered": False,
        "status": 0,
        "channel": "http_fleet_daemon",
        "detail": "",
        "packet": {},
    }
    try:
        cycle_dir = Path(cycle_dir)
        if not cycle_dir.exists():
            _bump(counters, "fleet_escalate_errors", note=f"cycle_dir_missing:{cycle_dir}")
            outcome["channel"] = "skipped"
            outcome["detail"] = f"cycle_dir_missing:{cycle_dir}"
            _write_skipped(cycle_dir, outcome["detail"])
            return outcome

        packet = build_packet(cycle_dir=cycle_dir, cycles_run=cycles_run, node_id=node_id)
        outcome["packet"] = packet

        ok, status, body = _post_json(url, packet, timeout_s)
        outcome["status"] = status

        if ok:
            outcome["delivered"] = True
            outcome["detail"] = f"http_{status}"
            try:
                (cycle_dir / "fleet_escalation_sent.json").write_text(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "url": url,
                            "status": status,
                            "response_body": body[:4000],
                            "packet": packet,
                        },
                        indent=2,
                        default=str,
                    )
                )
            except Exception as exc:
                # File write failure shouldn't flip delivered=True off — the
                # packet did reach the daemon. Just record on a sub-counter.
                _bump(counters, "fleet_escalate_errors",
                      note=f"sent_file_write:{exc}")
            return outcome

        # Failure path
        outcome["detail"] = f"http_{status}:{body[:300]}"
        _bump(counters, "fleet_escalate_errors", note=outcome["detail"])
        _write_skipped(cycle_dir, outcome["detail"])
        return outcome
    except Exception as exc:
        _bump(counters, "fleet_escalate_errors", note=f"unexpected:{exc}")
        outcome["detail"] = f"unexpected:{exc}"
        outcome["channel"] = "skipped"
        try:
            _write_skipped(Path(cycle_dir), outcome["detail"])
        except Exception:
            pass
        return outcome


def _write_skipped(cycle_dir: Path, reason: str) -> None:
    try:
        if cycle_dir.exists():
            (cycle_dir / "fleet_escalation_skipped.txt").write_text(
                f"ts={time.time()}\nreason={reason}\n"
            )
    except Exception:
        pass
