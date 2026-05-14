"""Pytest suite for autopilot_state.

Every test runs against a real temporary state file. No invariant is mocked.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest

# Standalone Local Autopilot layout:
#   <repo>/tests/<this>.py
#   <repo>/local_autopilot/{tools,memory}/
_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS_DIR = _REPO_ROOT / "local_autopilot" / "tools"
_PKG_ROOT = _REPO_ROOT / "local_autopilot"
for _p in (_TOOLS_DIR, _PKG_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import autopilot_state as st  # noqa: E402
import autopilot_hook as hk  # noqa: E402


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Point the state machine at a fresh tmp file for each test."""
    state_file = tmp_path / "autopilot_state.json"
    lock_file = tmp_path / "autopilot_state.lock"
    st.set_state_paths(state_file, lock_file)
    yield state_file
    st.clear_state_paths()
    st.reset_counters()


# ---------------------------------------------------------------------------
# 1. Fresh state defaults to off / not user-locked.
# ---------------------------------------------------------------------------


def test_fresh_state_is_off(tmp_state):
    s = st.read_state()
    assert s.mode == "off"
    assert s.set_by == "user"
    assert s.user_lock is False
    assert s.temporary_until is None
    assert s.temporary_reason is None
    assert s.transition_history == ()


def test_state_file_is_written_on_first_read(tmp_state):
    assert not tmp_state.exists()
    st.read_state()
    assert tmp_state.exists()
    data = json.loads(tmp_state.read_text())
    assert data["mode"] == "off"
    assert data["version"] == st.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. The 6 valid transitions.
# ---------------------------------------------------------------------------


def test_user_can_set_on_permanent(tmp_state):
    ok, err = st.transition("on_permanent", actor="user", reason="aaron-said-go")
    assert ok, err
    s = st.read_state()
    assert s.mode == "on_permanent"
    assert s.set_by == "user"
    assert s.user_lock is True


def test_user_can_set_on_temporary(tmp_state):
    ok, err = st.transition("on_temporary", actor="user", reason="trying-it", temporary_until="2099-01-01T00:00:00Z")
    assert ok, err
    s = st.read_state()
    assert s.mode == "on_temporary"
    assert s.temporary_reason == "trying-it"
    assert s.user_lock is False


def test_atlas_can_set_on_temporary_from_off(tmp_state):
    ok, err = st.transition(
        "on_temporary", actor="atlas", reason="hardening_pass"
    , temporary_until="2099-01-01T00:00:00Z")
    assert ok, err
    s = st.read_state()
    assert s.mode == "on_temporary"
    assert s.set_by == "atlas"


def test_atlas_can_clear_on_temporary(tmp_state):
    assert st.transition("on_temporary", actor="atlas", reason="r", temporary_until="2099-01-01T00:00:00Z")[0]
    ok, err = st.transition("off", actor="atlas", reason="done")
    assert ok, err
    assert st.read_state().mode == "off"


def test_user_can_clear_on_permanent(tmp_state):
    assert st.transition("on_permanent", actor="user")[0]
    ok, err = st.transition("off", actor="user", reason="bedtime")
    assert ok, err
    assert st.read_state().mode == "off"
    assert st.read_state().user_lock is False


def test_user_can_swap_on_permanent_to_on_temporary(tmp_state):
    # Aaron can downgrade himself; this is still a user-driven change.
    assert st.transition("on_permanent", actor="user")[0]
    ok, err = st.transition(
        "on_temporary", actor="user", reason="aaron-downgraded"
    , temporary_until="2099-01-01T00:00:00Z")
    assert ok, err
    s = st.read_state()
    assert s.mode == "on_temporary"
    assert s.user_lock is False


# ---------------------------------------------------------------------------
# 3. Blocked transitions (the load-bearing invariants).
# ---------------------------------------------------------------------------


def test_atlas_cannot_set_on_permanent_from_off(tmp_state):
    ok, err = st.transition("on_permanent", actor="atlas", reason="overreach")
    assert ok is False
    assert "atlas_cannot_set_on_permanent" in err
    assert st.read_state().mode == "off"


def test_atlas_cannot_set_on_permanent_from_on_temporary(tmp_state):
    assert st.transition("on_temporary", actor="atlas", reason="r", temporary_until="2099-01-01T00:00:00Z")[0]
    ok, err = st.transition(
        "on_permanent", actor="atlas", reason="sneaky-upgrade"
    )
    assert ok is False
    assert "atlas_cannot_set_on_permanent" in err
    assert st.read_state().mode == "on_temporary"


def test_atlas_cannot_clear_on_permanent(tmp_state):
    assert st.transition("on_permanent", actor="user")[0]
    ok, err = st.transition("off", actor="atlas", reason="please")
    assert ok is False
    assert "user_lock" in err
    assert st.read_state().mode == "on_permanent"


def test_atlas_cannot_demote_on_permanent_to_temporary(tmp_state):
    """Closing the side-door: temp→off by Atlas would effectively clear the lock."""
    assert st.transition("on_permanent", actor="user")[0]
    ok, err = st.transition("on_temporary", actor="atlas", reason="back-door", temporary_until="2099-01-01T00:00:00Z")
    assert ok is False
    assert "user_lock" in err
    assert st.read_state().mode == "on_permanent"


def test_invalid_mode_is_rejected(tmp_state):
    ok, err = st.transition("eject", actor="user")
    assert ok is False
    assert "invalid_mode" in err


def test_invalid_actor_is_rejected(tmp_state):
    ok, err = st.transition("on_temporary", actor="robot", temporary_until="2099-01-01T00:00:00Z")
    assert ok is False
    assert "invalid_actor" in err


# ---------------------------------------------------------------------------
# 4. Persistence — survives module reload.
# ---------------------------------------------------------------------------


def test_state_persists_across_module_reload(tmp_state):
    assert st.transition("on_permanent", actor="user", reason="aaron-go")[0]
    # Save override, reload module, re-apply override, re-read.
    state_file = tmp_state
    importlib.reload(st)
    st.set_state_paths(state_file, state_file.with_suffix(".lock"))
    try:
        s = st.read_state()
        assert s.mode == "on_permanent"
        assert s.user_lock is True
    finally:
        st.clear_state_paths()


# ---------------------------------------------------------------------------
# 5. Corruption recovery.
# ---------------------------------------------------------------------------


def test_corrupt_json_recovers_to_default_and_increments_counter(tmp_state):
    tmp_state.parent.mkdir(parents=True, exist_ok=True)
    tmp_state.write_text("{this is not json", encoding="utf-8")
    s = st.read_state()
    assert s.mode == "off"
    counters = st.get_counters()
    assert any(
        "autopilot_state_corruption_total" in k for k in counters
    ), counters


def test_empty_file_recovers_to_default(tmp_state):
    tmp_state.parent.mkdir(parents=True, exist_ok=True)
    tmp_state.write_text("", encoding="utf-8")
    s = st.read_state()
    assert s.mode == "off"
    counters = st.get_counters()
    assert any("corruption" in k for k in counters)


def test_schema_violation_recovers_to_default(tmp_state):
    tmp_state.parent.mkdir(parents=True, exist_ok=True)
    tmp_state.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "REACTOR_MELTDOWN",  # not in VALID_MODES
                "set_by": "user",
                "set_at": "2026-05-14T00:00:00Z",
                "user_lock": False,
                "transition_history": [],
            }
        ),
        encoding="utf-8",
    )
    s = st.read_state()
    assert s.mode == "off"  # self-healed


# ---------------------------------------------------------------------------
# 6. Concurrency — 10 threads race; state file must remain consistent.
# ---------------------------------------------------------------------------


def test_concurrent_transitions_are_race_safe(tmp_state):
    # Pre-seed to off.
    st.read_state()
    barrier = threading.Barrier(10)
    errors: list[str] = []

    def worker(i: int):
        barrier.wait()
        # half try to set on_temporary, half try to set off — all as user
        # so they're all individually valid.
        target = "on_temporary" if i % 2 == 0 else "off"
        ok, err = st.transition(target, actor="user", reason=f"atlas-decision t{i}", temporary_until="2099-01-01T00:00:00Z" if target == "on_temporary" else None)
        if not ok:
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No transition should have failed (all are valid user transitions).
    assert errors == [], errors

    # Final state must be a valid mode and the file must be readable.
    s = st.read_state()
    assert s.mode in st.VALID_MODES
    # All 10 transitions appear in history (some may be same-mode no-ops, all logged).
    assert len(s.transition_history) == 10
    # The final mode equals the `to` of the most recent history entry.
    assert s.transition_history[-1]["to"] == s.mode


# ---------------------------------------------------------------------------
# 7. Audit log — every transition is recorded with monotonic timestamps.
# ---------------------------------------------------------------------------


def test_history_is_monotonic_and_appends(tmp_state):
    st.transition("on_temporary", actor="atlas", reason="a", temporary_until="2099-01-01T00:00:00Z")
    st.transition("off", actor="atlas", reason="b")
    st.transition("on_permanent", actor="user", reason="c")
    s = st.read_state()
    assert len(s.transition_history) == 3
    monos = [h["monotonic_ns"] for h in s.transition_history]
    assert monos == sorted(monos), "monotonic_ns must be non-decreasing"
    assert [h["to"] for h in s.transition_history] == [
        "on_temporary",
        "off",
        "on_permanent",
    ]
    assert [h["actor"] for h in s.transition_history] == [
        "atlas",
        "atlas",
        "user",
    ]


def test_history_cap_is_enforced(tmp_state):
    # Force many transitions; cap is HISTORY_CAP.
    for i in range(st.HISTORY_CAP + 25):
        # All same-mode (off → off) — still valid, still logged.
        ok, err = st.transition("off", actor="user", reason=f"i{i}")
        assert ok, err
    s = st.read_state()
    assert len(s.transition_history) == st.HISTORY_CAP


# ---------------------------------------------------------------------------
# 8. Counters — per-actor, per-transition.
# ---------------------------------------------------------------------------


def test_blocked_transitions_increment_counter(tmp_state):
    st.transition("on_permanent", actor="user")
    st.transition("off", actor="atlas")  # blocked
    counters = st.get_counters()
    blocked = [
        k for k in counters if 'outcome="blocked"' in k and 'actor="atlas"' in k
    ]
    assert blocked, counters


# ---------------------------------------------------------------------------
# 9. Helpers.
# ---------------------------------------------------------------------------


def test_is_active_and_is_user_locked(tmp_state):
    assert st.is_active() is False
    assert st.is_user_locked() is False
    st.transition("on_temporary", actor="atlas", reason="r", temporary_until="2099-01-01T00:00:00Z")
    assert st.is_active() is True
    assert st.is_user_locked() is False
    st.transition("off", actor="atlas")
    st.transition("on_permanent", actor="user")
    assert st.is_active() is True
    assert st.is_user_locked() is True


# ---------------------------------------------------------------------------
# 10. Hook integration — should_run_next_cycle + on_atlas_temp_request.
# ---------------------------------------------------------------------------


def test_hook_should_run_next_cycle_off(tmp_state):
    run, why = hk.should_run_next_cycle()
    assert run is False
    assert why == "user_disabled"


def test_hook_should_run_next_cycle_on_permanent(tmp_state):
    st.transition("on_permanent", actor="user")
    run, why = hk.should_run_next_cycle()
    assert run is True
    assert why == "on_permanent"


def test_hook_atlas_temp_request_grants_from_off(tmp_state):
    ok, msg = hk.on_atlas_temp_request("hardening pass", temporary_until="2099-01-01T00:00:00Z")
    assert ok, msg
    s = st.read_state()
    assert s.mode == "on_temporary"
    assert s.set_by == "atlas"


def test_hook_atlas_temp_request_blocked_when_user_locked(tmp_state):
    st.transition("on_permanent", actor="user")
    ok, msg = hk.on_atlas_temp_request("please?", temporary_until="2099-01-01T00:00:00Z")
    assert ok is False
    assert "user_lock" in msg or "already_on_permanent" in msg
    assert st.read_state().mode == "on_permanent"


def test_hook_atlas_temp_release_only_from_temporary(tmp_state):
    # Cannot release when off.
    ok, msg = hk.on_atlas_temp_release()
    assert ok is False
    assert "not_in_temporary" in msg

    # Elevate then release.
    hk.on_atlas_temp_request("r", temporary_until="2099-01-01T00:00:00Z")
    ok, msg = hk.on_atlas_temp_release("done")
    assert ok, msg
    assert st.read_state().mode == "off"


# ---------------------------------------------------------------------------
# 11. Temporary-state JSON schema is fully populated.
# ---------------------------------------------------------------------------


def test_on_temporary_state_schema_is_complete(tmp_state):
    st.transition(
        "on_temporary",
        actor="atlas",
        reason="webhook-drift",
        temporary_until="2026-05-14T18:00:00+00:00",
    )
    raw = json.loads(tmp_state.read_text())
    assert raw["mode"] == "on_temporary"
    assert raw["set_by"] == "atlas"
    assert raw["temporary_until"] == "2026-05-14T18:00:00+00:00"
    assert raw["temporary_reason"] == "webhook-drift"
    assert raw["user_lock"] is False
    assert raw["version"] == st.SCHEMA_VERSION
    assert raw["set_at"]  # non-empty ISO timestamp
    # History contains the elevation.
    assert raw["transition_history"][-1]["to"] == "on_temporary"
    assert raw["transition_history"][-1]["actor"] == "atlas"
    assert (
        raw["transition_history"][-1]["temporary_until"]
        == "2026-05-14T18:00:00+00:00"
    )
