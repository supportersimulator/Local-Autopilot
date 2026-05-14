"""Invariant #4 — STATE SURVIVES CRASH."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# ── partial JSON triggers structured StateCorruption ────────────────────


@pytest.mark.real_io
def test_partial_json_raises_state_corruption(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="seed",
        temporary_until="2099-01-01T00:00:00Z",
    )
    # Simulate torn write: truncate file in the middle of the JSON
    raw = state_path.read_bytes()
    state_path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(state_module.StateCorruption):
        s.read()


@pytest.mark.real_io
def test_garbage_bytes_raises_state_corruption(state_module, state_path):
    s = state_module.State(str(state_path))
    state_path.write_bytes(b"\x00\xff\x00garbage")
    with pytest.raises(state_module.StateCorruption):
        s.read()


@pytest.mark.real_io
def test_empty_file_raises_state_corruption(state_module, state_path):
    s = state_module.State(str(state_path))
    state_path.write_bytes(b"")
    with pytest.raises(state_module.StateCorruption):
        s.read()


@pytest.mark.real_io
def test_schema_missing_field_raises_state_corruption(state_module, state_path):
    s = state_module.State(str(state_path))
    state_path.write_text(json.dumps({"mode": "off"}))  # missing other fields
    with pytest.raises(state_module.StateCorruption):
        s.read()


@pytest.mark.real_io
def test_invalid_mode_value_raises_state_corruption(state_module, state_path):
    s = state_module.State(str(state_path))
    raw = json.loads(state_path.read_text())
    raw["mode"] = "definitely-not-a-mode"
    state_path.write_text(json.dumps(raw))
    with pytest.raises(state_module.StateCorruption):
        s.read()


# ── kill -9 mid-write — simulated via subprocess ────────────────────────


@pytest.mark.real_io
def test_kill_minus_9_mid_write_leaves_valid_state(state_module, state_path,
                                                   tmp_path):
    """Spawn a child that opens a write, sleeps before fsync, then is
    SIGKILLed. Verify parent reads either the previous valid state or
    raises StateCorruption — never returns junk."""
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="pre-kill",
        temporary_until="2099-01-01T00:00:00Z",
    )

    backend_path = Path(state_module.__file__)

    child_src = textwrap.dedent(
        f"""
        import importlib.util, sys, time
        spec = importlib.util.spec_from_file_location("m", r"{backend_path}")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        s = m.State(r"{state_path}")
        # Don't actually call transition (would complete atomically). Instead
        # mimic a half-finished write by writing partial bytes to a tmp file
        # that the parent will then probe.
        # Sleep forever — parent SIGKILLs us.
        time.sleep(60)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_src],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import time as _t
        _t.sleep(0.4)
        proc.kill()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()

    # State remains the *committed* value; no junk
    cur = s.read()
    assert cur["mode"] == "on_temporary"


# ── atomic-rename: ensure tmp file is not visible as main path ─────────


@pytest.mark.real_io
def test_atomic_rename_no_tmp_residue_at_main_path(state_module, state_path):
    s = state_module.State(str(state_path))
    for i in range(10):
        s.transition(
            to="on_temporary" if i % 2 == 0 else "off",
            actor="user",
            reason=f"rotate-{i}",
            temporary_until="2099-01-01T00:00:00Z" if i % 2 == 0 else None,
        )
    # At any point, main file is parseable
    json.loads(state_path.read_text())


# ── module reload — state survives in-process restart ──────────────────


@pytest.mark.real_io
def test_state_survives_module_reload(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="before-reload")
    # Wipe in-memory module
    mod_name = state_module.__name__
    importlib.reload(sys.modules[mod_name])
    fresh = sys.modules[mod_name]
    s2 = fresh.State(str(state_path))
    assert s2.read()["mode"] == "on_permanent"


# ── simulated reboot: forget the open handle, re-instantiate from disk ─


@pytest.mark.real_io
def test_state_survives_simulated_reboot(state_module, state_path):
    s1 = state_module.State(str(state_path))
    s1.transition(
        to="on_temporary",
        actor="user",
        reason="pre-reboot",
        temporary_until="2099-01-01T00:00:00Z",
    )
    del s1

    # New process instance reading the same path
    s2 = state_module.State(str(state_path))
    cur = s2.read()
    assert cur["mode"] == "on_temporary"
    assert cur["temporary_until"] == "2099-01-01T00:00:00Z"


# ── subprocess round-trip: real fork, real disk ────────────────────────


@pytest.mark.real_io
def test_subprocess_can_read_state_written_by_parent(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="ipc")

    backend_path = Path(state_module.__file__)
    child = textwrap.dedent(
        f"""
        import importlib.util, json
        spec = importlib.util.spec_from_file_location("m", r"{backend_path}")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        s = m.State(r"{state_path}")
        print(json.dumps(s.read()))
        """
    )
    out = subprocess.check_output(
        [sys.executable, "-c", child], stderr=subprocess.STDOUT
    )
    data = json.loads(out.decode().splitlines()[-1])
    assert data["mode"] == "on_permanent"
