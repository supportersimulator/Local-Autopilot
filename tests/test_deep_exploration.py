"""
Tests for the G1 deep_exploration stage.

Coverage map (≥15 tests per brief):

  should_deep_explore decision matrix
    * risk_score >= 9.0 → True
    * drift_ranking_score >= 80 → True
    * trigger-keyword match → True
    * routine vectors + plain prompts → False
    * None / empty input → False
    * synaptic_response with malformed risk_score → no crash

  run_brainstorm subprocess wiring
    * happy path: parses artefact path from stdout
    * cost cap forwarded to script
    * timeout → falls through cleanly, no crash
    * missing script → falls through cleanly with named error

  run_counter_position
    * happy path: extracts steelman from JSON output
    * timeout → returns ok=False with named error
    * missing CLI → returns ok=False

  merge_into_agent_prompts
    * preserves Synaptic prompt id + falsifiable flag
    * prepends brainstorm + steelman context
    * preserves the original prompt body verbatim

  Runner integration
    * --force-complexity HIGH (dry-run) runs DEEP_EXPLORATION stage
    * --force-complexity LOW (dry-run) skips the stage
    * cycle_summary records complexity_class + deep_exploration_triggered
    * counters cycles_deep_explored / cycles_shallow bumped correctly

Tests NEVER call the real 3s CLI or the real brainstorm script — every
subprocess invocation is monkeypatched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
TOOLS = REPO_ROOT / "local_autopilot" / "tools"
PKG_ROOT = REPO_ROOT / "local_autopilot"
for _p in (TOOLS, PKG_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import archloop_runner          # noqa: E402
import deep_exploration         # noqa: E402
import synaptic_client          # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    """Per-test redirect of every file the G1 stage touches."""
    counters = tmp_path / "counters.json"
    kill = tmp_path / "autopilot.stop"
    trace = tmp_path / "synaptic-trace.jsonl"
    state_file = tmp_path / "autopilot_state.json"
    progress = tmp_path / "progress.json"
    log_root = tmp_path / "fleet-autopilot"
    brainstorm_script = tmp_path / "fake-brainstorm.sh"
    threes_cli = tmp_path / "fake-3s"
    fleet_brainstorm_dir = tmp_path / "fleet-brainstorm"

    monkeypatch.setenv("AUTOPILOT_COUNTERS_PATH", str(counters))
    monkeypatch.setenv("AUTOPILOT_KILL_FILE", str(kill))
    monkeypatch.setenv("AUTOPILOT_SYNAPTIC_TRACE", str(trace))
    monkeypatch.setenv("AUTOPILOT_STATE_FILE", str(state_file))
    monkeypatch.setenv("AUTOPILOT_PROGRESS_FILE", str(progress))
    monkeypatch.setenv("AUTOPILOT_LOG_ROOT", str(log_root))
    monkeypatch.setenv("AUTOPILOT_COMPLEXITY_DB", str(tmp_path / "no_db.sqlite"))
    monkeypatch.setenv("AUTOPILOT_METRICS_URL", "http://127.0.0.1:1/none")
    monkeypatch.setenv("AUTOPILOT_3S_BRAINSTORM", str(brainstorm_script))
    monkeypatch.setenv("AUTOPILOT_3S_CLI", str(threes_cli))
    monkeypatch.setenv("AUTOPILOT_FLEET_BRAINSTORM_DIR", str(fleet_brainstorm_dir))

    # Reset the autopilot_state module so each test starts clean.
    try:
        import autopilot_state as _as
        _as.set_state_paths(state_file)
    except Exception:
        pass

    monkeypatch.setattr(synaptic_client, "SYNAPTIC_TRACE_PATH", trace)

    yield SimpleNamespace(
        tmp=tmp_path,
        counters=counters,
        kill=kill,
        trace=trace,
        state_file=state_file,
        log_root=log_root,
        brainstorm_script=brainstorm_script,
        threes_cli=threes_cli,
        fleet_brainstorm_dir=fleet_brainstorm_dir,
    )


def _make_synaptic_response(prompts: list[tuple[str, str]] | None = None,
                             raw: str = "") -> synaptic_client.SynapticResponse:
    """Helper — build a SynapticResponse with `(id, text)` pairs."""
    if prompts is None:
        prompts = [
            ("E1", "verify nats replicas >= 3"),
            ("E2", "expect drift score < 0.4"),
            ("E3", "assert kv counter monotonic"),
            ("E4", "exit 0 after webhook publish"),
            ("E5", "must observe subscription_count > 0"),
        ]
    return synaptic_client.SynapticResponse(
        raw=raw or "\n".join(f"{pid} — {t}" for pid, t in prompts),
        prompts=[
            synaptic_client.AgentPrompt(id=pid, text=t, falsifiable=True)
            for pid, t in prompts
        ],
    )


# ---------------------------------------------------------------------------
# 1. should_deep_explore — decision matrix
# ---------------------------------------------------------------------------


def test_should_deep_explore_triggers_on_high_risk_score():
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response()
    vectors = [
        {"vector_id": "v1", "name": "nats", "risk_score": 9.5,
         "drift_ranking_score": 30.0},
    ]
    triggered, reason = explorer.should_deep_explore(resp, vectors)
    assert triggered is True
    assert "9.5" in reason
    assert "nats" in reason


def test_should_deep_explore_triggers_on_drift_ranking():
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response()
    vectors = [
        {"vector_id": "v1", "name": "kv", "risk_score": 3.0,
         "drift_ranking_score": 85.0},
    ]
    triggered, reason = explorer.should_deep_explore(resp, vectors)
    assert triggered is True
    assert "drift_ranking_score" in reason


def test_should_deep_explore_triggers_on_keyword():
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response(prompts=[
        ("E1", "verify the architectural change to JetStream is reversible"),
        ("E2", "expect nothing else"),
        ("E3", "assert no panic"),
        ("E4", "exit 0 after settle"),
        ("E5", "must see drift < 0.4"),
    ])
    triggered, reason = explorer.should_deep_explore(resp, [])
    assert triggered is True
    assert "architectural" in reason


def test_should_deep_explore_skips_routine_vectors():
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response()
    vectors = [
        {"vector_id": "v1", "name": "minor",
         "risk_score": 2.0, "drift_ranking_score": 10.0},
        {"vector_id": "v2", "name": "trivia",
         "risk_score": 5.0, "drift_ranking_score": 30.0},
    ]
    triggered, reason = explorer.should_deep_explore(resp, vectors)
    assert triggered is False
    assert reason == ""


def test_should_deep_explore_handles_none_input():
    explorer = deep_exploration.DeepExplorer()
    triggered, reason = explorer.should_deep_explore(None, None)
    assert triggered is False
    assert reason == ""


def test_should_deep_explore_tolerates_malformed_vector():
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response()
    vectors = [
        {"vector_id": "v1", "name": "broken",
         "risk_score": "NaN-ish", "drift_ranking_score": None},
        {"vector_id": "v2", "name": "real",
         "risk_score": 9.1, "drift_ranking_score": 70.0},
    ]
    triggered, _ = explorer.should_deep_explore(resp, vectors)
    assert triggered is True


# ---------------------------------------------------------------------------
# 2. run_brainstorm — subprocess wiring
# ---------------------------------------------------------------------------


def test_run_brainstorm_happy_path(isolate_paths, monkeypatch):
    # Pretend the brainstorm script exists.
    isolate_paths.brainstorm_script.write_text("#!/bin/sh\n# stub\n")
    isolate_paths.brainstorm_script.chmod(0o755)

    artefact_path = (
        isolate_paths.fleet_brainstorm_dir / "2026-05-14-3s-test.md"
    )
    artefact_path.parent.mkdir(parents=True, exist_ok=True)
    artefact_path.write_text(
        "# Brainstorm output\n\n## Risks\n- broken JetStream\n"
    )

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "## Topic: test\n"
                f"writing artefact to {artefact_path}\n"
                "## Recommendation: harden\n"
                "cost_usd_total: 0.041\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(deep_exploration.subprocess, "run", fake_run)

    explorer = deep_exploration.DeepExplorer()
    result = explorer.run_brainstorm("test topic", cost_cap_usd=0.05)
    assert result.ok is True
    assert result.cost_usd == pytest.approx(0.041)
    assert "Risks" in result.body
    assert result.artefact_path == artefact_path


def test_run_brainstorm_respects_cost_cap(isolate_paths, monkeypatch):
    isolate_paths.brainstorm_script.write_text("#!/bin/sh\n")
    isolate_paths.brainstorm_script.chmod(0o755)

    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="all good\ncost_usd_total: 0.01\n",
            stderr="",
        )

    monkeypatch.setattr(deep_exploration.subprocess, "run", fake_run)

    explorer = deep_exploration.DeepExplorer(brainstorm_cost_cap_usd=0.07)
    explorer.run_brainstorm("topic")
    assert "--cost-cap" in captured["cmd"]
    idx = captured["cmd"].index("--cost-cap")
    assert captured["cmd"][idx + 1] == "0.07"


def test_run_brainstorm_handles_timeout(isolate_paths, monkeypatch):
    isolate_paths.brainstorm_script.write_text("#!/bin/sh\n")
    isolate_paths.brainstorm_script.chmod(0o755)

    def fake_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(deep_exploration.subprocess, "run", fake_run)

    explorer = deep_exploration.DeepExplorer(brainstorm_timeout_s=5)
    result = explorer.run_brainstorm("topic")
    assert result.ok is False
    assert result.error and "timeout" in result.error
    # Counter recorded the failure.
    c = json.loads(isolate_paths.counters.read_text())
    assert c.get("deep_exploration_errors", 0) >= 1


def test_run_brainstorm_missing_script_falls_through(isolate_paths):
    # Don't create the script; just point at a non-existent path.
    explorer = deep_exploration.DeepExplorer()
    result = explorer.run_brainstorm("topic")
    assert result.ok is False
    assert "missing" in (result.error or "")
    c = json.loads(isolate_paths.counters.read_text())
    assert c.get("deep_exploration_errors", 0) >= 1


# ---------------------------------------------------------------------------
# 3. run_counter_position
# ---------------------------------------------------------------------------


def test_run_counter_position_happy_path(isolate_paths, monkeypatch):
    isolate_paths.threes_cli.write_text("#!/bin/sh\n")
    isolate_paths.threes_cli.chmod(0o755)

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"claim": "do X", "counter_probe_verdict": '
                '"X is irreversible; consider Y", "cost_usd": 0.02}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(deep_exploration.subprocess, "run", fake_run)

    explorer = deep_exploration.DeepExplorer()
    result = explorer.run_counter_position("we should do X")
    assert result.ok is True
    assert "irreversible" in result.steelman
    assert result.cost_usd == pytest.approx(0.02)


def test_run_counter_position_timeout(isolate_paths, monkeypatch):
    isolate_paths.threes_cli.write_text("#!/bin/sh\n")
    isolate_paths.threes_cli.chmod(0o755)

    def fake_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(deep_exploration.subprocess, "run", fake_run)

    explorer = deep_exploration.DeepExplorer(counter_timeout_s=3)
    result = explorer.run_counter_position("claim")
    assert result.ok is False
    assert "timeout" in (result.error or "")


def test_run_counter_position_missing_cli(isolate_paths):
    explorer = deep_exploration.DeepExplorer()
    result = explorer.run_counter_position("any claim")
    assert result.ok is False
    assert "missing" in (result.error or "")


# ---------------------------------------------------------------------------
# 4. merge_into_agent_prompts
# ---------------------------------------------------------------------------


def test_merge_preserves_id_and_falsifiability():
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response()
    brainstorm = deep_exploration.BrainstormResult(
        ok=True, body="### Risks\n- bad thing", cost_usd=0.01,
    )
    counter = deep_exploration.CounterPositionResult(
        ok=True, claim="c", steelman="STEELMAN: do not deploy", cost_usd=0.01,
    )
    enriched = explorer.merge_into_agent_prompts(resp.prompts, brainstorm, counter)
    assert len(enriched) == 5
    for original, new in zip(resp.prompts, enriched):
        assert new.id == original.id
        assert new.falsifiable is True
        assert original.text in new.text  # original body preserved verbatim


def test_merge_includes_steelman_and_brainstorm():
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response()
    brainstorm = deep_exploration.BrainstormResult(
        ok=True, body="### Risks\n- destabilising JetStream", cost_usd=0.0,
    )
    counter = deep_exploration.CounterPositionResult(
        ok=True, claim="c", steelman="The opposition argues this is reversible",
    )
    enriched = explorer.merge_into_agent_prompts(resp.prompts, brainstorm, counter)
    body = enriched[0].text
    assert "DEEP-EXPLORATION CONTEXT" in body
    assert "Risks + options" in body
    assert "STEELMAN" in body
    assert "JetStream" in body
    assert "reversible" in body


def test_merge_handles_degraded_brainstorm_gracefully():
    """If brainstorm failed, the merge still produces an enriched prompt
    with an explanatory note rather than dropping the prompt entirely."""
    explorer = deep_exploration.DeepExplorer()
    resp = _make_synaptic_response()
    brainstorm = deep_exploration.BrainstormResult(
        ok=False, error="brainstorm_timeout:600",
    )
    counter = deep_exploration.CounterPositionResult(
        ok=False, error="counter_empty_output",
    )
    enriched = explorer.merge_into_agent_prompts(resp.prompts, brainstorm, counter)
    assert len(enriched) == 5
    body = enriched[0].text
    assert "degraded" in body
    # Original instruction still present.
    assert resp.prompts[0].text in body


# ---------------------------------------------------------------------------
# 5. run_deep_exploration_stage (top-level wrapper)
# ---------------------------------------------------------------------------


def test_top_level_skips_on_low_complexity():
    resp = _make_synaptic_response()
    enriched, summary = deep_exploration.run_deep_exploration_stage(
        resp, [], dry_run=True,
    )
    assert enriched is None
    assert summary.triggered is False
    assert summary.reason == "skipped"


def test_top_level_triggers_on_high_risk_score_dry_run():
    resp = _make_synaptic_response()
    vectors = [
        {"vector_id": "v1", "name": "x",
         "risk_score": 9.4, "drift_ranking_score": 75},
    ]
    enriched, summary = deep_exploration.run_deep_exploration_stage(
        resp, vectors, dry_run=True,
    )
    assert summary.triggered is True
    assert enriched is not None
    assert len(enriched) == 5
    # Dry-run path uses stub brainstorm + stub steelman, both ok=True.
    assert summary.brainstorm_ok is True
    assert summary.counter_position_ok is True


# ---------------------------------------------------------------------------
# 6. Runner integration — --force-complexity HIGH / LOW
# ---------------------------------------------------------------------------


def test_runner_force_complexity_high_runs_stage(isolate_paths):
    rc = archloop_runner.main([
        "--dry-run", "--cycles", "1", "--force-complexity", "HIGH",
    ])
    assert rc == 0
    summaries = list(isolate_paths.log_root.rglob("cycle_summary.json"))
    assert len(summaries) == 1
    data = json.loads(summaries[0].read_text())
    assert data["complexity_class"] == "HIGH"
    assert data["deep_exploration_triggered"] is True
    # Stage timing must appear.
    assert "DEEP_EXPLORATION" in data["stage_timings"]
    # Counter bumped.
    c = json.loads(isolate_paths.counters.read_text())
    assert c.get("cycles_deep_explored", 0) >= 1


def test_runner_force_complexity_low_skips_stage(isolate_paths):
    rc = archloop_runner.main([
        "--dry-run", "--cycles", "1", "--force-complexity", "LOW",
    ])
    assert rc == 0
    summaries = list(isolate_paths.log_root.rglob("cycle_summary.json"))
    data = json.loads(summaries[0].read_text())
    assert data["complexity_class"] == "LOW"
    assert data["deep_exploration_triggered"] is False
    # Stage timing still recorded (it ran the classifier + decided to skip).
    assert "DEEP_EXPLORATION" in data["stage_timings"]
    c = json.loads(isolate_paths.counters.read_text())
    assert c.get("cycles_shallow", 0) >= 1
    assert c.get("cycles_deep_explored", 0) == 0


def test_runner_default_classifies_low_with_empty_db(isolate_paths):
    """Without --force, a missing complexity DB + plain Synaptic prompts
    classify as LOW and the stage skips."""
    rc = archloop_runner.main(["--dry-run", "--cycles", "1"])
    assert rc == 0
    data = json.loads(
        next(isolate_paths.log_root.rglob("cycle_summary.json")).read_text()
    )
    assert data["complexity_class"] == "LOW"
    assert data["deep_exploration_triggered"] is False


def test_runner_summary_fields_present(isolate_paths):
    """Schema check — every new G1 field appears in cycle_summary.json."""
    archloop_runner.main([
        "--dry-run", "--cycles", "1", "--force-complexity", "HIGH",
    ])
    data = json.loads(
        next(isolate_paths.log_root.rglob("cycle_summary.json")).read_text()
    )
    required = {
        "complexity_class",
        "deep_exploration_triggered",
        "deep_exploration_reason",
        "deep_exploration_cost_usd",
    }
    assert required.issubset(data.keys()), required - data.keys()


# ---------------------------------------------------------------------------
# 7. ComplexityClass classifier
# ---------------------------------------------------------------------------


def test_classify_complexity_high_from_db():
    resp = _make_synaptic_response()
    vectors = [
        {"vector_id": "v1", "name": "n", "risk_score": 9.2,
         "drift_ranking_score": 70},
    ]
    k = synaptic_client.classify_complexity(resp, live_vectors=vectors)
    assert k == synaptic_client.ComplexityClass.HIGH


def test_classify_complexity_keyword_alone_caps_at_med():
    """Aaron's rule: Synaptic keyword alone (no DB corroboration) does NOT
    promote to HIGH — it caps at MED."""
    resp = _make_synaptic_response(prompts=[
        ("E1", "verify the architectural rollout"),
        ("E2", "expect nothing"), ("E3", "assert no-op"),
        ("E4", "exit 0"), ("E5", "must settle"),
    ])
    k = synaptic_client.classify_complexity(resp, live_vectors=[])
    assert k == synaptic_client.ComplexityClass.MED


def test_classify_complexity_low_default():
    resp = _make_synaptic_response()
    k = synaptic_client.classify_complexity(resp, live_vectors=[])
    assert k == synaptic_client.ComplexityClass.LOW


def test_classify_complexity_keyword_plus_med_db_promotes_to_high():
    """Keyword + MED-or-higher live vector = HIGH (Aaron's rule)."""
    resp = _make_synaptic_response(prompts=[
        ("E1", "verify the destructive cleanup"),
        ("E2", "expect nothing"), ("E3", "assert no-op"),
        ("E4", "exit 0"), ("E5", "must settle"),
    ])
    vectors = [
        {"vector_id": "v1", "name": "x", "risk_score": 6.5,
         "drift_ranking_score": 55},
    ]
    k = synaptic_client.classify_complexity(resp, live_vectors=vectors)
    assert k == synaptic_client.ComplexityClass.HIGH
