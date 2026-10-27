"""Item 4 — kill switch out of /tmp, user-only control dir, fail closed."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from tests._fix_helpers import isolate_runner, need, opt, teardown_overrides

import archloop_runner  # noqa: E402

control_dir = opt("control_dir")


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = isolate_runner(tmp_path, monkeypatch, mode="none")
    yield paths
    teardown_overrides()


def test_default_control_dir_not_tmp():
    need(control_dir, "control_dir")
    d = str(control_dir.default_control_dir())
    for t in ("/tmp", "/private/tmp", "/var/tmp", tempfile.gettempdir()):
        assert not d.startswith(t), d


def test_env_override_ignored(env):
    """AUTOPILOT_KILL_FILE must no longer redirect (or be the) kill switch."""
    need(control_dir, "control_dir")
    env["old_kill"].write_text("")  # the env-pointed file exists
    assert archloop_runner._check_kill() is False
    control_dir.request_stop("test")
    assert archloop_runner._check_kill() is True


def test_missing_control_dir_stops(env, tmp_path):
    need(control_dir, "control_dir")
    control_dir.set_control_dir(tmp_path / "does-not-exist")
    assert archloop_runner._check_kill() is True
    assert control_dir.kill_state()[1] == "control_dir_missing"


def test_loose_perms_stop(env):
    need(control_dir, "control_dir")
    os.chmod(env["control"], 0o755)
    state, why = control_dir.kill_state()
    assert state == "stop" and why.startswith("unsafe")
    assert archloop_runner._check_kill() is True


def test_symlinked_control_dir_stops(env, tmp_path):
    need(control_dir, "control_dir")
    link = tmp_path / "link"
    link.symlink_to(env["control"])
    control_dir.set_control_dir(link)
    assert control_dir.kill_state()[0] == "stop"


def test_missing_run_marker_stops(env):
    need(control_dir, "control_dir")
    (env["control"] / "RUN").unlink()
    assert control_dir.kill_state() == ("stop", "run_marker_missing")


def test_stat_error_stops(env, monkeypatch):
    need(control_dir, "control_dir")

    def eacces(*a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(control_dir.os, "lstat", eacces)
    state, why = control_dir.kill_state()
    assert state == "stop" and why.startswith("unreadable")


def test_runner_counts_unreadable(env):
    need(control_dir, "control_dir")
    os.chmod(env["control"], 0o777)
    rc = archloop_runner.main(["--dry-run", "--cycles", "2"])
    assert rc == 0
    c = json.loads(env["counters"].read_text())
    assert c["cycles_aborted_kill_file"] >= 1
    assert c.get("kill_switch_unreadable", 0) >= 1


def test_healthy_dir_runs_and_resume(env):
    need(control_dir, "control_dir")
    assert control_dir.kill_state() == ("run", "ok")
    control_dir.request_stop("x")
    assert control_dir.kill_state() == ("stop", "stop_file")
    control_dir.clear_stop()
    assert control_dir.kill_state() == ("run", "ok")


def test_cli_status_exit_codes(env, capsys):
    need(control_dir, "control_dir")
    assert control_dir.main(["status"]) == 0
    assert control_dir.main(["stop"]) == 0
    assert control_dir.main(["status"]) == 1
    assert control_dir.main(["resume"]) == 0
    assert control_dir.main(["status"]) == 0
