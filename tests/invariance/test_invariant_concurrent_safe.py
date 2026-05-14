"""Invariant #5 — CONCURRENT-SAFE.

Last-writer-wins under flock. No torn writes. Parsed JSON always
satisfies schema even after a barrage of racers.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest


# ── 16 threads racing transitions ───────────────────────────────────────


@pytest.mark.real_io
def test_16_threads_reach_consistent_state(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")

    N = 16
    barrier = threading.Barrier(N)
    errors = []

    def worker(i):
        try:
            barrier.wait()
            if i % 2 == 0:
                s.transition(
                    to="on_temporary",
                    actor="user",
                    reason=f"t{i}",
                    temporary_until="2099-01-01T00:00:00Z",
                )
            else:
                s.transition(to="off", actor="user", reason=f"t{i}")
        except Exception as e:
            errors.append((i, type(e).__name__, str(e)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors under concurrency: {errors}"

    # JSON parses + schema valid
    cur = s.read()
    assert cur["mode"] in state_module.VALID_MODES

    # All N writes recorded
    history = cur["transition_history"]
    # >= N + 1 seed; allow >= because nothing should be lost
    assert len(history) >= N + 1, (
        f"expected ≥ {N + 1} entries, got {len(history)}"
    )

    # Counter monotonic
    assert cur["counters"]["autopilot_transitions_total"] >= N + 1


# ── monotonic timestamps under contention ───────────────────────────────


@pytest.mark.real_io
def test_history_timestamps_monotonic(state_module, state_path):
    s = state_module.State(str(state_path))
    for i in range(20):
        s.transition(
            to="on_temporary" if i % 2 == 0 else "off",
            actor="user",
            reason=f"r{i}",
            temporary_until="2099-01-01T00:00:00Z" if i % 2 == 0 else None,
        )
    history = s.read()["transition_history"]
    timestamps = [h["ts"] for h in history]
    assert timestamps == sorted(timestamps), (
        "transition_history ts not monotonic"
    )


# ── 10 subprocesses racing — exercises real flock across processes ──────


@pytest.mark.real_io
def test_10_subprocesses_race(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="off", actor="user", reason="init")

    backend_path = Path(state_module.__file__)
    child = textwrap.dedent(
        f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("m", r"{backend_path}")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        s = m.State(r"{state_path}")
        idx = int(sys.argv[1])
        try:
            if idx % 2 == 0:
                s.transition(to="on_temporary", actor="user",
                             reason=f"p{{idx}}",
                             temporary_until="2099-01-01T00:00:00Z")
            else:
                s.transition(to="off", actor="user", reason=f"p{{idx}}")
            print("ok")
        except Exception as e:
            print(type(e).__name__)
        """
    )

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", child, str(i)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(10)
    ]
    for p in procs:
        p.wait(timeout=15)

    statuses = [p.stdout.read().decode().strip() for p in procs]
    assert all(s == "ok" for s in statuses), f"subprocess statuses: {statuses}"

    cur = s.read()
    assert cur["mode"] in state_module.VALID_MODES
    # 10 successful subprocess writes + 1 init
    assert cur["counters"]["autopilot_transitions_total"] >= 11


# ── no torn writes: every intermediate snapshot is parseable ────────────


@pytest.mark.real_io
def test_no_torn_writes_during_contention(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")

    stop = threading.Event()
    parse_errors = []

    def reader():
        while not stop.is_set():
            try:
                raw = state_path.read_text()
                if raw:
                    json.loads(raw)
            except json.JSONDecodeError as e:
                parse_errors.append(str(e))
            except FileNotFoundError:
                # atomic rename — momentary gap is acceptable; loop again
                pass

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(30):
            s.transition(
                to="on_temporary" if i % 2 == 0 else "on_permanent",
                actor="user",
                reason=f"r{i}",
                temporary_until="2099-01-01T00:00:00Z" if i % 2 == 0 else None,
            )
    finally:
        stop.set()
        t.join()

    assert not parse_errors, (
        f"torn write observed during atomic rename — {parse_errors[:3]}"
    )


# ── permission-denied transitions also serialise cleanly ────────────────


@pytest.mark.real_io
def test_concurrent_denied_attempts_consistent(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")

    N = 8
    barrier = threading.Barrier(N)
    denied = []

    def attacker(i):
        barrier.wait()
        try:
            s.transition(to="off", actor="atlas", reason=f"a{i}")
        except state_module.PermissionDenied:
            denied.append(i)

    ts = [threading.Thread(target=attacker, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(denied) == N
    assert s.read()["mode"] == "on_permanent"
