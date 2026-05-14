"""
Tests for the Synaptic arch-loop runner.

These tests never call a real LLM and never invoke the 3s CLI. Anywhere the
runner would touch an external service, we monkeypatch the indirection layer.

Invocation (from repo root):
    .venv/bin/python3 -m pytest .claude/plugins/autopilot/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Standalone Local Autopilot layout: <repo>/tests/<this>.py, modules at
# <repo>/local_autopilot/{tools,memory}/.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
TOOLS = REPO_ROOT / "local_autopilot" / "tools"
PKG_ROOT = REPO_ROOT / "local_autopilot"
for _p in (TOOLS, PKG_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import agent_dispatch          # noqa: E402
import archloop_runner         # noqa: E402
import synaptic_client         # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    """Redirect every file the runner writes into a per-test tmp dir.

    The runner reads paths from env vars on every call, so monkeypatch.setenv
    is enough; we also nudge the synaptic_client trace path because that's a
    module-level constant.
    """
    counters = tmp_path / "counters.json"
    kill = tmp_path / "autopilot.stop"
    trace = tmp_path / "synaptic-trace.jsonl"
    state_file = tmp_path / "autopilot_state.json"
    progress = tmp_path / "progress.json"
    log_root = tmp_path / "fleet-autopilot"

    monkeypatch.setenv("AUTOPILOT_COUNTERS_PATH", str(counters))
    monkeypatch.setenv("AUTOPILOT_KILL_FILE", str(kill))
    monkeypatch.setenv("AUTOPILOT_SYNAPTIC_TRACE", str(trace))
    monkeypatch.setenv("AUTOPILOT_STATE_FILE", str(state_file))
    monkeypatch.setenv("AUTOPILOT_PROGRESS_FILE", str(progress))
    monkeypatch.setenv("AUTOPILOT_LOG_ROOT", str(log_root))
    # Point the live-state DB at a non-existent path so the cycle just records
    # a "db_missing" note rather than reading the real one.
    monkeypatch.setenv("AUTOPILOT_COMPLEXITY_DB", str(tmp_path / "no_db.sqlite"))
    monkeypatch.setenv("AUTOPILOT_METRICS_URL", "http://127.0.0.1:1/none")
    # Force F1's state module (if loaded) to write to our tmp path. The runner
    # also calls set_state_paths() but a direct call here covers tests that
    # import autopilot_state themselves.
    try:
        import autopilot_state as _as  # type: ignore
        _as.set_state_paths(state_file)
    except Exception:
        pass

    monkeypatch.setattr(synaptic_client, "SYNAPTIC_TRACE_PATH", trace)

    yield SimpleNamespace(
        tmp=tmp_path, counters=counters, kill=kill, trace=trace,
        state_file=state_file, progress=progress, log_root=log_root,
    )


# ---------------------------------------------------------------------------
# Synaptic prompt parser
# ---------------------------------------------------------------------------


def test_parser_accepts_well_formed_prompts():
    raw = """\
E1 — verify nats replicas >= 3
E2 — expect drift score < 0.4
E3 — assert kv counter monotonic
E4 — exit 0 after webhook publish completes
E5 — must observe subscription_count > 0
"""
    prompts = synaptic_client.parse_prompts(raw)
    assert len(prompts) == 5
    assert all(p.falsifiable for p in prompts)
    assert [p.id for p in prompts] == ["E1", "E2", "E3", "E4", "E5"]


def test_parser_rejects_non_falsifiable_prompt():
    raw = """\
E1 — verify nats up
E2 — look into kv hashing
E3 — assert no panics
E4 — exit 0 after settle
E5 — must see drift < 0.4
"""
    prompts = synaptic_client.parse_prompts(raw)
    # E2 has no falsifiable token
    e2 = next(p for p in prompts if p.id == "E2")
    assert e2.falsifiable is False
    others = [p for p in prompts if p.id != "E2"]
    assert all(p.falsifiable for p in others)


def test_parser_returns_empty_on_unstructured_text():
    assert synaptic_client.parse_prompts("just a freeform paragraph") == []


def test_ask_for_vectors_retries_then_fails(monkeypatch):
    """Unstructured response → retries N times → returns ok=False."""
    calls = {"n": 0}

    def fake_call(*a, **kw):
        calls["n"] += 1
        return "no markers here, just prose"

    monkeypatch.setattr(synaptic_client, "_call_llm", fake_call)
    resp = synaptic_client.ask_for_vectors({}, max_retries=2)
    assert resp.ok is False
    assert calls["n"] == 3   # initial + 2 retries
    assert resp.error.startswith("wrong_prompt_count")


def test_ask_for_vectors_succeeds_first_try(monkeypatch):
    good = "\n".join(
        f"E{i} — verify gate {i} returns exit 0"
        for i in range(1, 6)
    )
    monkeypatch.setattr(synaptic_client, "_call_llm", lambda *a, **kw: good)
    resp = synaptic_client.ask_for_vectors({})
    assert resp.ok is True
    assert len(resp.prompts) == 5


def test_satisfaction_parser_true_false():
    assert synaptic_client.parse_satisfaction("blah\nSATISFACTION: TRUE\n") is True
    assert synaptic_client.parse_satisfaction("blah\nSATISFACTION: FALSE\n") is False
    assert synaptic_client.parse_satisfaction("no marker") is None


def test_ask_satisfaction_parses_marker(monkeypatch):
    monkeypatch.setattr(
        synaptic_client, "_call_llm",
        lambda *a, **kw: "summary text\nSATISFACTION: FALSE\nnext vector: kv",
    )
    sat, raw = synaptic_client.ask_satisfaction("v", ["a1", "a2"])
    assert sat is False
    assert "SATISFACTION: FALSE" in raw


# ---------------------------------------------------------------------------
# 3s cross-exam verdict parser
# ---------------------------------------------------------------------------


def test_3s_verdict_signoff_text():
    v = archloop_runner.parse_3s_verdict(
        "Round 3: consensus reached\nDecision: SIGN-OFF\nconsensus: 0.92\ncost_usd: 0.041"
    )
    assert v["decision"] == "SIGN-OFF"
    assert v["consensus"] == pytest.approx(0.92)
    assert v["cost_usd"] == pytest.approx(0.041)


def test_3s_verdict_hold_text():
    v = archloop_runner.parse_3s_verdict("HOLD — cardiologist objects to step 3.")
    assert v["decision"] == "HOLD"


def test_3s_verdict_json_line():
    v = archloop_runner.parse_3s_verdict(
        'preamble\n{"decision": "SIGN-OFF", "consensus": 0.81, "cost_usd": 0.02}\ntrailing'
    )
    assert v["decision"] == "SIGN-OFF"
    assert v["consensus"] == pytest.approx(0.81)


def test_3s_verdict_empty():
    v = archloop_runner.parse_3s_verdict("")
    assert v["decision"] == "UNKNOWN"
    assert v["error"] == "empty_3s_output"


# ---------------------------------------------------------------------------
# Dry-run cycle / state machine
# ---------------------------------------------------------------------------


def test_dry_run_cycle_completes_and_writes_summary(isolate_paths, monkeypatch):
    """A single dry-run cycle exits 0 and writes a cycle_summary.json with all
    8 stage timings populated."""
    rc = archloop_runner.main(["--dry-run", "--cycles", "1"])
    assert rc == 0

    summaries = list(isolate_paths.log_root.rglob("cycle_summary.json"))
    assert len(summaries) == 1
    data = json.loads(summaries[0].read_text())
    assert set(data["stage_timings"].keys()) == set(archloop_runner.STAGES)
    assert data["satisfied"] is True            # dry-run stub
    assert data["verdict_decision"] == "SIGN-OFF"
    assert data["agents"]["pass"] == 5
    assert data["dry_run"] is True
    assert data["schema_version"] == 1


def test_cycle_aborts_when_state_mode_off(isolate_paths, tmp_path):
    """Without --dry-run, mode=off must produce a no-op exit."""
    isolate_paths.state_file.write_text(json.dumps({"mode": "off", "cycle_count": 0}))
    rc = archloop_runner.main(["--cycles", "1"])
    assert rc == 0
    counters = json.loads(isolate_paths.counters.read_text())
    assert counters["cycles_aborted_user_off"] == 1
    # No cycle dir should be created.
    assert not isolate_paths.log_root.exists() or not any(
        isolate_paths.log_root.iterdir()
    )


def test_cycle_aborts_on_kill_file(isolate_paths):
    """Kill file before the first cycle starts → immediate exit, counter
    bumped, no cycle dir.

    We use --dry-run so the runner bypasses the state.mode=="off" check
    (which would otherwise short-circuit before the kill-file check). The
    point of this test is to verify the kill-file shortcut works inside the
    cycle loop.
    """
    isolate_paths.kill.write_text("")
    rc = archloop_runner.main(["--dry-run", "--cycles", "3"])
    assert rc == 0
    counters = json.loads(isolate_paths.counters.read_text())
    assert counters["cycles_aborted_kill_file"] >= 1


def test_cost_cap_accumulates_across_cycles(isolate_paths, monkeypatch):
    """Force satisfied=False so cycles keep running; the cost-cap must
    eventually fire and increment cycles_aborted_cost."""
    # Patch run_cycle to mark every result NOT satisfied + charge $0.60.
    real_run = archloop_runner.run_cycle

    def hijacked(cycle_idx, *, counters, cost_so_far, cost_cap, dry_run, log_root=None):
        r = real_run(
            cycle_idx,
            counters=counters,
            cost_so_far=cost_so_far,
            cost_cap=cost_cap,
            dry_run=dry_run,
            log_root=log_root,
        )
        r.satisfied = False
        # Inject an extra $0.60 so a cap of $1.00 fires after cycle 2.
        r.cost_usd = max(r.cost_usd, 0.60)
        return r

    monkeypatch.setattr(archloop_runner, "run_cycle", hijacked)

    rc = archloop_runner.main(["--dry-run", "--cycles", "5", "--cost-cap-usd", "1.00"])
    assert rc == 0
    counters = json.loads(isolate_paths.counters.read_text())
    assert counters["cycles_aborted_cost"] >= 1
    # At least one cycle ran before the cap fired.
    summaries = list(isolate_paths.log_root.rglob("cycle_summary.json"))
    assert len(summaries) >= 1


def test_cycle_summary_schema_keys(isolate_paths):
    """Lock the JSON schema for downstream consumers (Atlas, fleet dashboards)."""
    archloop_runner.main(["--dry-run", "--cycles", "1"])
    summary_path = next(isolate_paths.log_root.rglob("cycle_summary.json"))
    data = json.loads(summary_path.read_text())
    required = {
        "cycle", "cycle_dir", "stage_timings", "stages", "satisfied",
        "verdict_decision", "cost_usd_cycle", "cost_usd_total_after_cycle",
        "agents", "aborted", "dry_run", "ts_end", "schema_version",
    }
    assert required.issubset(data.keys()), required - data.keys()
    assert set(data["agents"].keys()) == {"pass", "fail", "timeout"}


# ---------------------------------------------------------------------------
# agent_dispatch contract
# ---------------------------------------------------------------------------


def test_write_prompts_and_poll_results_roundtrip(tmp_path):
    prompts = [
        synaptic_client.AgentPrompt(id=f"E{i}", text=f"verify thing {i}", falsifiable=True)
        for i in range(1, 6)
    ]
    cycle_dir = tmp_path / "cycle-x"
    jobs = agent_dispatch.write_prompts(cycle_dir, prompts)
    assert len(jobs) == 5
    assert (cycle_dir / "RUN_NOW.signal").exists()
    assert (cycle_dir / "manifest.json").exists()
    for j in jobs:
        assert j.prompt_path.exists()
        body = j.prompt_path.read_text()
        assert "NON-DESTRUCTIVE CONTRACT" in body
        assert j.prompt_id in body
        # Pretend the Claude session ran it
        j.result_path.write_text(f"STATUS: PASS\nresult body for {j.prompt_id}")
    poll = agent_dispatch.poll_results(
        jobs, per_agent_timeout_s=1, poll_interval_s=0.01
    )
    assert poll["all_done"] is True
    assert all(r["status"] == "PASS" for r in poll["results"])


def test_poll_results_times_out_cleanly(tmp_path):
    """No agent writes a result → poll loop exits, all results marked TIMEOUT."""
    prompts = [
        synaptic_client.AgentPrompt(id="E1", text="verify foo", falsifiable=True),
    ]
    jobs = agent_dispatch.write_prompts(tmp_path / "cyc", prompts)

    # Fake clock + sleep so we don't actually wait.
    clock = {"t": 0.0}
    def fake_now(): return clock["t"]
    def fake_sleep(s): clock["t"] += s + 10.0   # jump well past deadline

    poll = agent_dispatch.poll_results(
        jobs, per_agent_timeout_s=1, poll_interval_s=0.5,
        now_fn=fake_now, sleep_fn=fake_sleep,
    )
    assert poll["timed_out"] is True
    assert poll["results"][0]["status"] == "TIMEOUT"
    assert (jobs[0].cycle_dir / "TIMEOUT.signal").exists()


def test_status_parser_handles_malformed():
    assert agent_dispatch._parse_status("STATUS: PASS\nfoo") == "PASS"
    assert agent_dispatch._parse_status("STATUS: FAIL\nfoo") == "FAIL"
    assert agent_dispatch._parse_status("STATUS: SKIP because") == "SKIP"
    assert agent_dispatch._parse_status("no status line") == "FAIL"
    assert agent_dispatch._parse_status("") == "FAIL"


# ---------------------------------------------------------------------------
# Counter ZSF
# ---------------------------------------------------------------------------


def test_counters_file_initialised_with_all_keys(isolate_paths):
    """After one cycle, /tmp/autopilot-counters.json must contain every COUNTER_KEY."""
    archloop_runner.main(["--dry-run", "--cycles", "1"])
    c = json.loads(isolate_paths.counters.read_text())
    for k in archloop_runner.COUNTER_KEYS:
        assert k in c, f"missing counter key: {k}"


def test_watch_mode_runs_two_cycles_then_kill_file(isolate_paths, monkeypatch):
    """Watch mode runs cycles indefinitely; after 2 cycles we drop a kill
    file which should cause a clean exit at the next iteration boundary.

    Also exercises the mid-loop state-flip path by NOT using --dry-run
    (we hijack run_cycle to remain cheap, calling real_run with dry_run=True)
    — but to avoid the initial mode==off short-circuit we pre-write state."""
    real_run = archloop_runner.run_cycle
    isolate_paths.state_file.write_text(json.dumps({
        "mode": "on_permanent",
        "set_by": "user",
        "set_at": "2026-01-01T00:00:00Z",
    }))

    calls = {"n": 0}

    def hijacked(cycle_idx, *, counters, cost_so_far, cost_cap, dry_run,
                 log_root=None, force_complexity=None):
        calls["n"] += 1
        # Always run the underlying cycle as dry-run so no LLM is called.
        r = real_run(
            cycle_idx,
            counters=counters,
            cost_so_far=cost_so_far,
            cost_cap=cost_cap,
            dry_run=True,
            log_root=log_root,
        )
        r.satisfied = False  # force watch to keep looping
        if cycle_idx >= 2:
            isolate_paths.kill.write_text("")  # trigger kill-file exit
        return r

    monkeypatch.setattr(archloop_runner, "run_cycle", hijacked)

    rc = archloop_runner.main([
        "--watch",
        "--watch-interval", "0",
        "--cost-cap-usd", "100.0",
        "--max-watch-hours", "1",
    ])
    assert rc == 0
    assert calls["n"] >= 2
    c = json.loads(isolate_paths.counters.read_text())
    assert c.get("cycles_aborted_kill_file", 0) >= 1


def test_watch_mode_state_flip_to_off_exits(isolate_paths, monkeypatch):
    """If autopilot_state.json flips to 'off' between cycles, watch mode
    exits at the next iteration with cycles_aborted_user_off bumped."""
    real_run = archloop_runner.run_cycle
    isolate_paths.state_file.write_text(json.dumps({
        "mode": "on_permanent",
        "set_by": "user",
        "set_at": "2026-01-01T00:00:00Z",
    }))

    calls = {"n": 0}

    def hijacked(cycle_idx, *, counters, cost_so_far, cost_cap, dry_run,
                 log_root=None, force_complexity=None):
        calls["n"] += 1
        r = real_run(
            cycle_idx,
            counters=counters,
            cost_so_far=cost_so_far,
            cost_cap=cost_cap,
            dry_run=True,
            log_root=log_root,
        )
        r.satisfied = False
        if cycle_idx >= 2:
            isolate_paths.state_file.write_text(json.dumps({
                "mode": "off",
                "set_by": "user",
                "set_at": "2026-01-01T00:00:00Z",
            }))
            # F1 caches paths at module level — re-point to our tmp file.
            try:
                import autopilot_state as _as  # type: ignore
                _as.set_state_paths(isolate_paths.state_file)
            except Exception:
                pass
        return r

    monkeypatch.setattr(archloop_runner, "run_cycle", hijacked)

    rc = archloop_runner.main([
        "--watch",
        "--watch-interval", "0",
        "--cost-cap-usd", "100.0",
        "--max-watch-hours", "1",
    ])
    assert rc == 0
    assert calls["n"] >= 2
    c = json.loads(isolate_paths.counters.read_text())
    assert c.get("cycles_aborted_user_off", 0) >= 1


def test_watch_mode_dry_run_signal_exit(isolate_paths, monkeypatch):
    """Watch mode in dry-run + signal handler triggered after 2 cycles exits
    cleanly. Verifies SIGINT handling without actually delivering a signal:
    we flip the stop flag from inside the hijacked run_cycle."""
    real_run = archloop_runner.run_cycle
    calls = {"n": 0}

    # We need access to the stop_flag dict that main() creates. Trick: have
    # the hijacked run_cycle raise KeyboardInterrupt after cycle 2 — Python's
    # default SIGINT handler raises KI, which our watch handler converts to
    # stop_flag.requested=True. But our handler is installed via
    # signal.signal, not directly invoked. Instead, after cycle 2, send
    # SIGINT to our own process.
    import os as _os
    import signal as _signal

    def hijacked(cycle_idx, *, counters, cost_so_far, cost_cap, dry_run,
                 log_root=None, force_complexity=None):
        calls["n"] += 1
        r = real_run(
            cycle_idx,
            counters=counters,
            cost_so_far=cost_so_far,
            cost_cap=cost_cap,
            dry_run=dry_run,
            log_root=log_root,
        )
        # Force satisfied=False so watch keeps looping.
        r.satisfied = False
        if cycle_idx >= 2:
            _os.kill(_os.getpid(), _signal.SIGINT)
        return r

    monkeypatch.setattr(archloop_runner, "run_cycle", hijacked)

    rc = archloop_runner.main([
        "--watch", "--dry-run",
        "--watch-interval", "0",
        "--cost-cap-usd", "100.0",
        "--max-watch-hours", "1",
    ])
    assert rc == 0
    assert calls["n"] >= 2


def test_watch_mode_cost_cap_exits_with_code_6(isolate_paths, monkeypatch):
    """Cost cap reached in watch mode → exit code 6 (not 0)."""
    real_run = archloop_runner.run_cycle

    def hijacked(cycle_idx, *, counters, cost_so_far, cost_cap, dry_run,
                 log_root=None, force_complexity=None):
        r = real_run(
            cycle_idx,
            counters=counters,
            cost_so_far=cost_so_far,
            cost_cap=cost_cap,
            dry_run=dry_run,
            log_root=log_root,
        )
        r.satisfied = False
        r.cost_usd = max(r.cost_usd, 0.60)
        return r

    monkeypatch.setattr(archloop_runner, "run_cycle", hijacked)

    rc = archloop_runner.main([
        "--watch", "--dry-run",
        "--watch-interval", "0",
        "--cost-cap-usd", "1.00",
        "--max-watch-hours", "1",
    ])
    assert rc == 6


def test_watch_mode_rejects_explicit_cycles_arg(isolate_paths):
    """--watch with explicit --cycles N>1 → exit 2."""
    rc = archloop_runner.main([
        "--watch", "--cycles", "5", "--dry-run",
    ])
    assert rc == 2


def test_synaptic_parse_errors_increment_counter(isolate_paths, monkeypatch):
    """Parser failure path must bump synaptic_parse_errors."""
    # Force the parser to see garbage by stubbing _call_llm directly.
    monkeypatch.setattr(synaptic_client, "_call_llm", lambda *a, **kw: "freeform garbage")
    resp = synaptic_client.ask_for_vectors({}, max_retries=1)
    assert resp.ok is False
    c = json.loads(isolate_paths.counters.read_text())
    assert c.get("synaptic_parse_errors", 0) >= 2  # initial + 1 retry
