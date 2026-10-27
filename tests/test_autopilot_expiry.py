"""Item 2 — temporary mode actually expires (FIX-DESIGNS.md §2)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from tests._fix_helpers import isolate_runner, teardown_overrides

import archloop_runner  # noqa: E402
import autopilot_cli  # noqa: E402
import autopilot_hook as hk  # noqa: E402
import autopilot_state as st  # noqa: E402


def _iso(delta_s: float, base=None) -> str:
    base = base or dt.datetime.now(dt.timezone.utc)
    return (base + dt.timedelta(seconds=delta_s)).isoformat()


@pytest.fixture
def state(tmp_path):
    st.set_state_paths(tmp_path / "state.json")
    yield tmp_path / "state.json"
    st.clear_state_paths()


# ---------------------------------------------------------------- NEG


def test_until_garbage_rejected(state):
    ok, err = st.transition("on_temporary", "user", reason="r", temporary_until="garbage")
    assert not ok, "unparseable --until accepted"
    assert "temporary_bounded" in err


def test_until_in_past_rejected(state):
    ok, _ = st.transition("on_temporary", "user", reason="r", temporary_until="2001-01-01T00:00:00Z")
    assert not ok, "past deadline accepted"


def test_until_beyond_max_hours_rejected(state):
    ok, _ = st.transition("on_temporary", "user", reason="r", temporary_until="2099-01-01T00:00:00Z")
    assert not ok, "deadline 70+ years out accepted as 'temporary'"


def test_atlas_decision_no_longer_bypasses(state):
    ok, _ = st.transition("on_temporary", "user", reason="atlas-decision: trust me")
    assert not ok, "reason text 'atlas-decision' bypassed the deadline"
    ok, err = st.transition("on_temporary", "atlas", reason="atlas elevation")
    assert ok, f"atlas without --until should get a stamped default: {err}"
    s = st.read_state()
    until = dt.datetime.fromisoformat(s.temporary_until)
    left = (until - dt.datetime.now(dt.timezone.utc)).total_seconds()
    assert 0 < left <= st.DEFAULT_TEMP_MINUTES * 60 + 5


def test_hook_stops_after_deadline(state, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    ok, err = st.transition("on_temporary", "user", reason="r", temporary_until=_iso(120, now))
    assert ok, err
    monkeypatch.setattr(st, "_now_dt", lambda: now + dt.timedelta(seconds=200), raising=False)
    assert hk.should_run_next_cycle() == (False, "expired")
    s = st.read_state()
    assert s.mode == "off"
    last = s.transition_history[-1]
    assert last["actor"] == "system" and last["reason"].startswith("expired:")


def test_clock_rollback_still_expires(state, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    ok, err = st.transition("on_temporary", "user", reason="r", temporary_until=_iso(120, now))
    assert ok, err
    real_mono = st._monotonic_ns()
    # wall clock rolled back an hour, but 200 s really elapsed (same boot)
    monkeypatch.setattr(st, "_now_dt", lambda: now - dt.timedelta(hours=1), raising=False)
    monkeypatch.setattr(st, "_monotonic_ns", lambda: real_mono + 200 * 10**9)
    assert st.read_state().mode == "off"


def test_crash_restart_reverts(state):
    raw = {
        "version": 1, "mode": "on_temporary", "set_by": "user",
        "set_at": _iso(-7200), "temporary_until": _iso(-60),
        "temporary_reason": "r", "user_lock": False, "transition_history": [],
    }
    state.write_text(json.dumps(raw))
    assert st.read_state().mode == "off"


def test_corrupt_deadline_fails_closed(state):
    raw = {
        "version": 1, "mode": "on_temporary", "set_by": "atlas",
        "set_at": _iso(0), "temporary_until": "sometext",
        "temporary_reason": "r", "user_lock": False, "transition_history": [],
    }
    state.write_text(json.dumps(raw))
    ok, why = hk.should_run_next_cycle()
    assert (ok, why) == (False, "expired")


def test_system_actor_only_expires(state):
    ok, err = st.transition("on_temporary", "user", reason="r", temporary_until=_iso(600))
    assert ok, err
    assert not st.transition("off", "system", reason="because")[0]
    assert st.transition("off", "system", reason="expired:manual")[0]


def test_runner_aborts_mid_watch_on_expiry(tmp_path, monkeypatch):
    isolate_runner(tmp_path, monkeypatch, mode="none")
    try:
        now = dt.datetime.now(dt.timezone.utc)
        ok, err = st.transition("on_temporary", "user", reason="r", temporary_until=_iso(120, now))
        assert ok, err
        calls = []

        def fake(cycle_idx, **kw):
            calls.append(cycle_idx)
            # deadline passes during the first cycle
            monkeypatch.setattr(st, "_now_dt", lambda: now + dt.timedelta(seconds=300), raising=False)
            if len(calls) >= 3:  # safety stop for pre-fix code
                (tmp_path / "old-autopilot.stop").write_text("")
                cd = __import__("tests._fix_helpers", fromlist=["opt"]).opt("control_dir")
                if cd:
                    cd.request_stop("test")
            d = tmp_path / f"c{len(calls)}"
            d.mkdir()
            r = archloop_runner.CycleResult(cycle=cycle_idx, cycle_dir=d)
            r.satisfied = False
            return r

        monkeypatch.setattr(archloop_runner, "run_cycle", fake)
        archloop_runner.main(["--watch", "--watch-interval", "0", "--max-watch-hours", "1"])
        assert len(calls) == 1, f"runner kept cycling after deadline: {len(calls)} cycles"
    finally:
        teardown_overrides()


# ---------------------------------------------------------------- POS


def test_system_actor_cannot_turn_on(state):
    assert not st.transition("on_temporary", "system", reason="expired:x", temporary_until=_iso(600))[0]
    assert not st.transition("on_permanent", "system", reason="expired:x")[0]


def test_cli_rejects_actor_system(state):
    with pytest.raises(SystemExit) as ei:
        autopilot_cli.main(["--state-file", str(state), "temp", "r", "--actor", "system"])
    assert ei.value.code == 2


def test_on_permanent_unaffected(state, monkeypatch):
    assert st.transition("on_permanent", "user", reason="r")[0]
    monkeypatch.setattr(st, "_now_dt",
                        lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
                        raising=False)
    assert st.read_state().mode == "on_permanent"
    assert hk.should_run_next_cycle() == (True, "on_permanent")


def test_valid_future_deadline_runs(state):
    assert st.transition("on_temporary", "user", reason="r", temporary_until=_iso(3600))[0]
    assert hk.should_run_next_cycle() == (True, "on_temporary")
