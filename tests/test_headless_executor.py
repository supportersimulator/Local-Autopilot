"""Unit tests for local_autopilot.tools.headless_executor.

These tests never invoke the real `claude` CLI. subprocess.run and
shutil.which are monkeypatched. CONTEXT_DNA_DIR is redirected to tmp_path
so the counters file is sandboxed.

Run from repo root:
    .venv/bin/python3 -m pytest tests/test_headless_executor.py -v
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
PKG_ROOT = REPO_ROOT / "local_autopilot"
TOOLS = PKG_ROOT / "tools"
for _p in (TOOLS, PKG_ROOT, REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture
def he(tmp_path, monkeypatch):
    """Reload headless_executor with CONTEXT_DNA_DIR pointed at tmp_path."""
    monkeypatch.setenv("CONTEXT_DNA_DIR", str(tmp_path / "ctxdna"))
    # Force fresh module-level constants reading the env var
    if "headless_executor" in sys.modules:
        del sys.modules["headless_executor"]
    if "local_autopilot.tools.headless_executor" in sys.modules:
        del sys.modules["local_autopilot.tools.headless_executor"]
    import headless_executor as mod  # type: ignore
    importlib.reload(mod)
    return mod


def _counters(he_mod) -> dict:
    if not he_mod._COUNTERFILE.exists():
        return {}
    return json.loads(he_mod._COUNTERFILE.read_text())


def _make_cycle(tmp_path: Path, n: int = 3) -> Path:
    d = tmp_path / "cycle"
    d.mkdir()
    for i in range(1, n + 1):
        (d / f"agent_{i}.prompt").write_text(f"do task {i}\n")
    return d


def _mock_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# probe_claude_cli
# ---------------------------------------------------------------------------

def test_probe_no_binary(he, monkeypatch):
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: None)
    ok, detail = he.probe_claude_cli()
    assert ok is False
    assert "not on PATH" in detail


def test_probe_ok(he, monkeypatch):
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    monkeypatch.setattr(he.subprocess, "run",
                        lambda *a, **k: _mock_proc(stdout="claude 1.2.3\n", returncode=0))
    ok, detail = he.probe_claude_cli()
    assert ok is True
    assert "1.2.3" in detail


def test_probe_nonzero(he, monkeypatch):
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    monkeypatch.setattr(he.subprocess, "run",
                        lambda *a, **k: _mock_proc(stderr="boom", returncode=2))
    ok, detail = he.probe_claude_cli()
    assert ok is False
    assert "exit 2" in detail


def test_probe_timeout(he, monkeypatch):
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    def _to(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=10)
    monkeypatch.setattr(he.subprocess, "run", _to)
    ok, detail = he.probe_claude_cli()
    assert ok is False
    assert "timed out" in detail


# ---------------------------------------------------------------------------
# _parse_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("STATUS: PASS\nrest", "PASS"),
    ("Some preamble\nSTATUS: FAIL\nrest", "FAIL"),
    ("STATUS: SKIP", "SKIP"),
    ("no status here\njust output", "UNKNOWN"),
])
def test_parse_status(he, text, expected):
    assert he._parse_status(text) == expected


# ---------------------------------------------------------------------------
# execute_cycle — happy path
# ---------------------------------------------------------------------------

def test_execute_cycle_happy(he, tmp_path, monkeypatch):
    cycle = _make_cycle(tmp_path, n=3)
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    monkeypatch.setattr(he.subprocess, "run",
                        lambda *a, **k: _mock_proc(stdout="STATUS: PASS\nok\n", returncode=0))

    summary = he.execute_cycle(cycle, parallel=1, timeout_per_agent_s=5)

    assert summary["executed"] == 3
    assert summary["passed"] == 3
    assert summary["failed"] == 0 and summary["skipped"] == 0
    assert summary["timeout"] == 0 and summary["error"] == 0
    for i in (1, 2, 3):
        rf = cycle / f"agent_{i}.result"
        assert rf.exists()
        assert "STATUS: PASS" in rf.read_text()
    assert (cycle / "RESULTS_READY.signal").exists()
    assert (cycle / "headless_executor_summary.json").exists()
    c = _counters(he)
    assert c.get("agent_status_pass") == 3
    assert c.get("results_ready_written") == 1


# ---------------------------------------------------------------------------
# execute_cycle — failure paths
# ---------------------------------------------------------------------------

def test_execute_cycle_timeout(he, tmp_path, monkeypatch):
    cycle = _make_cycle(tmp_path, n=1)
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    def _to(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(he.subprocess, "run", _to)

    summary = he.execute_cycle(cycle, parallel=1, timeout_per_agent_s=1)
    assert summary["timeout"] == 1
    body = (cycle / "agent_1.result").read_text()
    assert body.startswith("STATUS: FAIL")
    assert "timed out" in body
    assert summary["executions"][0]["status"] == "TIMEOUT"
    assert _counters(he).get("agent_timeout") == 1


def test_execute_cycle_nonzero_exit(he, tmp_path, monkeypatch):
    cycle = _make_cycle(tmp_path, n=1)
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    monkeypatch.setattr(he.subprocess, "run",
                        lambda *a, **k: _mock_proc(stdout="garbage", stderr="boom!", returncode=7))

    summary = he.execute_cycle(cycle, parallel=1)
    assert summary["failed"] == 1
    body = (cycle / "agent_1.result").read_text()
    assert body.startswith("STATUS: FAIL")
    assert "boom!" in body
    assert "exited 7" in body
    assert _counters(he).get("agent_nonzero_exit") == 1


def test_execute_cycle_missing_status_line(he, tmp_path, monkeypatch):
    cycle = _make_cycle(tmp_path, n=1)
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    monkeypatch.setattr(he.subprocess, "run",
                        lambda *a, **k: _mock_proc(stdout="just some output\n", returncode=0))

    summary = he.execute_cycle(cycle, parallel=1)
    assert summary["skipped"] == 1
    body = (cycle / "agent_1.result").read_text()
    assert body.startswith("STATUS: SKIP")
    assert "just some output" in body
    assert _counters(he).get("agent_status_missing") == 1


# ---------------------------------------------------------------------------
# execute_cycle — edge cases
# ---------------------------------------------------------------------------

def test_execute_cycle_no_prompts(he, tmp_path):
    cycle = tmp_path / "empty"
    cycle.mkdir()
    summary = he.execute_cycle(cycle)
    assert summary == {"executed": 0, "detail": "no agent_*.prompt files"}
    assert not (cycle / "RESULTS_READY.signal").exists()
    assert _counters(he).get("execute_cycle_no_prompts") == 1


def test_execute_cycle_bad_dir(he, tmp_path):
    summary = he.execute_cycle(tmp_path / "nope")
    assert summary["executed"] == 0
    assert summary.get("error") == 1
    assert _counters(he).get("execute_cycle_bad_dir") == 1


# ---------------------------------------------------------------------------
# Atomic write contract
# ---------------------------------------------------------------------------

def test_atomic_write_uses_rename(he, tmp_path, monkeypatch):
    cycle = _make_cycle(tmp_path, n=1)
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")
    monkeypatch.setattr(he.subprocess, "run",
                        lambda *a, **k: _mock_proc(stdout="STATUS: PASS\ndone\n", returncode=0))

    he.execute_cycle(cycle, parallel=1)
    # Final file present, temp gone
    assert (cycle / "agent_1.result").exists()
    assert not (cycle / "agent_1.result.tmp").exists()
    assert (cycle / "agent_1.result").read_text().startswith("STATUS: PASS")


def test_atomic_write_helper_direct(he, tmp_path):
    target = tmp_path / "x.result"
    he._atomic_write(target, "hello")
    assert target.read_text() == "hello"
    assert not target.with_suffix(target.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# Counter increments — mixed cycle
# ---------------------------------------------------------------------------

def test_mixed_cycle_counters(he, tmp_path, monkeypatch):
    cycle = _make_cycle(tmp_path, n=3)
    monkeypatch.setattr(he.shutil, "which", lambda *_a, **_k: "/usr/bin/claude")

    calls = {"n": 0}
    def _run(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _mock_proc(stdout="STATUS: PASS\nok\n", returncode=0)
        if calls["n"] == 2:
            return _mock_proc(stdout="", stderr="err", returncode=1)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(he.subprocess, "run", _run)

    summary = he.execute_cycle(cycle, parallel=1, timeout_per_agent_s=1)
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["timeout"] == 1
    c = _counters(he)
    assert c.get("agent_status_pass") == 1
    assert c.get("agent_nonzero_exit") == 1
    assert c.get("agent_timeout") == 1
    assert c.get("cycles_started") == 1
