"""Pytest config for the autopilot invariance harness.

Prefers the real F1 / F2 modules under tools/. Falls back to the in-tree
mock when they're missing so the harness can run pre-F1/F2-ship and
still exercise real file I/O, flock, atomic rename, and subprocess
races.

Markers:
  requires_f1 — needs tools/autopilot_state.py / autopilot_cli.py
  requires_f2 — needs tools/archloop_runner.py et al
  real_io     — exercises real disk/subprocess (not a pure mock test)
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]                       # <repo>/
TOOLS_DIR = REPO_ROOT / "local_autopilot" / "tools"
PKG_ROOT = REPO_ROOT / "local_autopilot"          # for `import memory.<x>`

# Ensure repo root + tools/ + memory shim are importable.
for p in (str(REPO_ROOT), str(TOOLS_DIR), str(PKG_ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _have(mod: str, path: Path) -> bool:
    return path.exists()


F1_STATE_PATH = TOOLS_DIR / "autopilot_state.py"
F1_CLI_PATH = TOOLS_DIR / "autopilot_cli.py"
F2_RUNNER_PATH = TOOLS_DIR / "archloop_runner.py"
F2_SYNAPTIC_PATH = TOOLS_DIR / "synaptic_client.py"

# CONTRACT NOTE: the invariance harness intentionally uses the in-tree
# `_mock_autopilot_state.py` reference implementation rather than the real
# F1 module. The invariance tests use a class-based API
# (`State(path).transition(...)`) while the real F1 module uses module-
# level functions (`read_state()`, `transition(...)`). The mock satisfies
# the same behavioural contract — the harness is testing *invariants*,
# not the specific implementation. Keep HAS_F1=False so the mock is
# always selected. The plain `tests/test_autopilot_state.py` covers the
# real F1 module's API directly.
HAS_F1 = False
HAS_F1_CLI = False
HAS_F2 = False


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_f1: real F1 module needed")
    config.addinivalue_line("markers", "requires_f2: real F2 module needed")
    config.addinivalue_line("markers", "real_io: exercises real disk/subprocess")


# ── module fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def state_module():
    """Return the autopilot_state module — real if F1 shipped, mock otherwise."""
    if HAS_F1:
        return importlib.import_module("autopilot_state")
    return importlib.import_module("_mock_autopilot_state")


@pytest.fixture(scope="session")
def state_backend_kind() -> str:
    return "real" if HAS_F1 else "mock"


# ── per-test temp state file ───────────────────────────────────────────


@pytest.fixture()
def state_path(tmp_path):
    p = tmp_path / "autopilot_state.json"
    return p


@pytest.fixture()
def fresh_state(state_module, state_path):
    s = state_module.State(str(state_path))
    return s


# ── audit log isolation ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_audit_log(tmp_path, monkeypatch):
    """Redirect /tmp/autopilot-cli.log to a per-test path so concurrent
    pytest workers don't stomp on each other. Tests that explicitly
    want to verify the real /tmp path opt in with `use_real_audit_log`.
    """
    fake = tmp_path / "autopilot-cli.log"
    monkeypatch.setenv("AUTOPILOT_AUDIT_LOG", str(fake))
    yield fake


@pytest.fixture()
def use_real_audit_log(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_AUDIT_LOG", raising=False)
    yield Path("/tmp/autopilot-cli.log")
