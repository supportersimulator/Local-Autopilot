"""Shared helpers for the cost-ledger / expiry / kill-switch tests.

Modules added by the fix are imported optionally so that, against the
pre-fix code, each test FAILS with an actionable message instead of the
whole file erroring at collection time.
"""
from __future__ import annotations

import importlib
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


def opt(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def need(mod, name: str):
    if mod is None:
        pytest.fail(f"module {name!r} does not exist (fix not applied)")
    return mod


def isolate_runner(tmp_path, monkeypatch, *, mode: str = "on_permanent"):
    """Point every runner path at tmp_path; returns a namespace dict."""
    import archloop_runner
    import autopilot_state as st

    paths = {
        "counters": tmp_path / "counters.json",
        "old_kill": tmp_path / "old-autopilot.stop",
        "state": tmp_path / "autopilot_state.json",
        "progress": tmp_path / "progress.json",
        "log_root": tmp_path / "logs",
        "ledger": tmp_path / "ledger",
        "control": tmp_path / "control",
    }
    monkeypatch.setenv("AUTOPILOT_COUNTERS_PATH", str(paths["counters"]))
    monkeypatch.setenv("AUTOPILOT_KILL_FILE", str(paths["old_kill"]))
    monkeypatch.setenv("AUTOPILOT_STATE_FILE", str(paths["state"]))
    monkeypatch.setenv("AUTOPILOT_PROGRESS_FILE", str(paths["progress"]))
    monkeypatch.setenv("AUTOPILOT_LOG_ROOT", str(paths["log_root"]))
    monkeypatch.setenv("AUTOPILOT_COMPLEXITY_DB", str(tmp_path / "no.db"))
    monkeypatch.setenv("AUTOPILOT_METRICS_URL", "http://127.0.0.1:1/none")
    monkeypatch.setenv("AUTOPILOT_3S_CLI", str(tmp_path / "no-3s"))
    monkeypatch.delenv("AUTOPILOT_COST_CAP_USD", raising=False)
    monkeypatch.setattr(archloop_runner, "_reconcile_caffeinate", lambda m: None)
    st.set_state_paths(paths["state"])

    cl = opt("cost_ledger")
    if cl is not None:
        cl.set_ledger_dir(paths["ledger"])
    cd = opt("control_dir")
    if cd is not None:
        cd.set_control_dir(paths["control"])
        cd.init_control_dir()
    if mode == "on_permanent":
        ok, err = st.transition("on_permanent", actor="user", reason="test")
        assert ok, err
    return paths


def teardown_overrides():
    import autopilot_state as st
    st.clear_state_paths()
    for name, fn in (("cost_ledger", "clear_ledger_dir"),
                     ("control_dir", "clear_control_dir")):
        m = opt(name)
        if m is not None:
            getattr(m, fn)()
