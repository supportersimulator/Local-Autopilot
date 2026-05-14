"""
Tests for fleet_escalate.py — chief-node adjudication packet builder + POST.

Pure unit tests: we monkeypatch the urllib POST so no real HTTP fires.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
TOOLS = REPO_ROOT / "local_autopilot" / "tools"
PKG_ROOT = REPO_ROOT / "local_autopilot"
for _p in (TOOLS, PKG_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fleet_escalate  # noqa: E402


def _seed_cycle_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "cycle-test"
    cd.mkdir()
    (cd / "cross_exam.txt").write_text("HOLD\nconsensus: 0.4\n" + ("x" * 5000))
    (cd / "synaptic_re_eval.md").write_text(
        "# Synaptic\nsatisfied=False\n" + ("y" * 5000)
    )
    (cd / "agent_1.result").write_text("STATUS: PASS\nbody here\n")
    (cd / "agent_2.result").write_text("STATUS: FAIL\nbroken\n")
    (cd / "agent_3.result").write_text("STATUS: PASS\nok\n")
    return cd


def test_build_packet_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac2")
    cd = _seed_cycle_dir(tmp_path)
    pkt = fleet_escalate.build_packet(cycle_dir=cd, cycles_run=8)

    assert pkt["type"] == "autopilot_escalation"
    assert pkt["to"] == "mac1"
    assert pkt["from"] == "mac2"
    payload = pkt["payload"]
    assert payload["priority"] == "P2"
    assert payload["cycles_run"] == 8
    assert payload["cycle_dir"] == str(cd)
    # Excerpts are truncated to 2000 chars
    assert len(payload["cross_exam_excerpt"]) == 2000
    assert len(payload["synaptic_re_eval_excerpt"]) == 2000
    # Agent results summarized
    summary = payload["agent_results_summary"]
    assert len(summary) == 3
    ids = sorted(r["id"] for r in summary)
    assert ids == [1, 2, 3]
    statuses = {r["id"]: r["status"] for r in summary}
    assert statuses[1] == "PASS"
    assert statuses[2] == "FAIL"
    assert "ask" in payload and "UNRESOLVABLE" in payload["ask"]


def test_escalate_success_writes_sent_file(tmp_path, monkeypatch):
    cd = _seed_cycle_dir(tmp_path)

    captured = {}

    def fake_post(url, packet, timeout_s):
        captured["url"] = url
        captured["packet"] = packet
        captured["timeout"] = timeout_s
        return True, 202, '{"ack":"queued"}'

    monkeypatch.setattr(fleet_escalate, "_post_json", fake_post)
    counters: dict = {}
    out = fleet_escalate.escalate_to_fleet_chief(
        cycle_dir=cd, cycles_run=8, counters=counters, node_id="mac2",
    )
    assert out["delivered"] is True
    assert out["status"] == 202
    assert captured["url"].endswith("/message")
    assert captured["packet"]["payload"]["cycles_run"] == 8

    sent = cd / "fleet_escalation_sent.json"
    assert sent.exists()
    body = json.loads(sent.read_text())
    assert body["status"] == 202
    assert body["packet"]["to"] == "mac1"

    # On success we don't bump the error counter
    assert counters.get("fleet_escalate_errors", 0) == 0


def test_escalate_failure_writes_skipped_and_bumps_counter(tmp_path, monkeypatch):
    cd = _seed_cycle_dir(tmp_path)

    def fake_post(url, packet, timeout_s):
        return False, 0, "ConnectionRefusedError:nope"

    monkeypatch.setattr(fleet_escalate, "_post_json", fake_post)
    counters: dict = {}
    out = fleet_escalate.escalate_to_fleet_chief(
        cycle_dir=cd, cycles_run=8, counters=counters, node_id="mac2",
    )
    assert out["delivered"] is False
    assert counters.get("fleet_escalate_errors", 0) == 1
    skipped = cd / "fleet_escalation_skipped.txt"
    assert skipped.exists()
    assert "ConnectionRefusedError" in skipped.read_text()


def test_escalate_missing_cycle_dir_is_zsf(tmp_path, monkeypatch):
    """Missing cycle_dir must not raise — just bump + report."""
    monkeypatch.setattr(
        fleet_escalate, "_post_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not POST")),
    )
    counters: dict = {}
    out = fleet_escalate.escalate_to_fleet_chief(
        cycle_dir=tmp_path / "does-not-exist",
        cycles_run=8,
        counters=counters,
        node_id="mac2",
    )
    assert out["delivered"] is False
    assert out["channel"] == "skipped"
    assert counters.get("fleet_escalate_errors", 0) == 1
