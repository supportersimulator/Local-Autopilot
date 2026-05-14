"""macOS caffeinate manager — keep the system awake while autopilot is active.

When the autopilot transitions to `on_temporary` or `on_permanent`, we want
the laptop to stay awake so the daemon's 30-min tick actually fires. When the
autopilot transitions back to `off`, we want sleep to be allowed again.

This module is the single source of truth for that side-effect. Idempotent,
PID-tracked, survives crashes, no-ops on non-macOS, ZSF on every failure.

Public API
----------
- ``sync_with_state(mode)`` — call this after every successful transition.
  Spawns or stops caffeinate as needed. Returns (changed?, detail).
- ``ensure_running()`` — explicit spawn (for daemon startup / reboot recovery).
- ``stop()``           — explicit stop.
- ``status()``         — return current pid + alive bool, no side effects.

PID file: ``~/.context-dna/caffeinate.pid`` (one line, the spawned PID).

ZSF: every failure path increments a counter in
``~/.context-dna/caffeinate_counters.json``. Never raises.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("local_autopilot.caffeinate")

_DATA_DIR = Path(os.environ.get("CONTEXT_DNA_DIR", os.path.expanduser("~/.context-dna")))
_PIDFILE = _DATA_DIR / "caffeinate.pid"
_COUNTERFILE = _DATA_DIR / "caffeinate_counters.json"
_IS_DARWIN = platform.system() == "Darwin"

# caffeinate -d  prevent display sleep
# caffeinate -i  prevent idle sleep
# caffeinate -m  prevent disk sleep
# caffeinate -s  keep system awake (AC-power only — gracefully degrades on battery)
# caffeinate -u  declare user activity (broader wake; lets Spotlight etc. run)
_CAFFEINATE_ARGS = ["caffeinate", "-dims"]
# Note: -u removed because it can interfere with sleep-on-battery; -s alone is
# enough to keep the laptop awake while plugged in. Users who explicitly want
# battery-safe always-awake can override via CAFFEINATE_ARGS_OVERRIDE.


def _bump(counter: str, *, delta: int = 1, note: str = "") -> None:
    """ZSF counter increment. Never raises."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if _COUNTERFILE.exists():
            try:
                data = json.loads(_COUNTERFILE.read_text() or "{}")
            except (json.JSONDecodeError, OSError):
                data = {}
        data[counter] = int(data.get(counter, 0)) + delta
        if note:
            data[f"{counter}__last_note"] = note
        _COUNTERFILE.write_text(json.dumps(data, indent=2, sort_keys=True))
    except Exception as e:  # noqa: BLE001 — ZSF
        logger.warning("caffeinate counter bump failed: %s", e)


def _is_alive(pid: int) -> bool:
    """Return True iff the PID is a running process. No side effects."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 → permission/existence check only
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM means it exists but we can't signal it; treat as alive
        return True


def _read_pid() -> Optional[int]:
    """Return PID from file if file exists AND that PID is alive. Else None."""
    if not _PIDFILE.exists():
        return None
    try:
        text = _PIDFILE.read_text().strip()
        pid = int(text)
    except (ValueError, OSError):
        # Stale or corrupt pidfile — remove it
        try:
            _PIDFILE.unlink()
        except FileNotFoundError:
            pass
        _bump("caffeinate_pidfile_corrupt", note="non-int or unreadable")
        return None

    if not _is_alive(pid):
        # Stale pid → clean up
        try:
            _PIDFILE.unlink()
        except FileNotFoundError:
            pass
        _bump("caffeinate_pidfile_stale", note=f"pid {pid} dead")
        return None
    return pid


def status() -> tuple[bool, Optional[int]]:
    """Return (running?, pid|None). No side effects."""
    pid = _read_pid()
    return (pid is not None, pid)


def ensure_running() -> tuple[bool, str]:
    """Idempotent: spawn caffeinate if not already running.

    Returns (started?, detail).
      started=True  → we spawned a new process this call
      started=False → already running, or no-op platform
    """
    if not _IS_DARWIN:
        return False, "skipped:not-darwin"

    pid = _read_pid()
    if pid is not None:
        _bump("caffeinate_ensure_already_running")
        return False, f"already-running:{pid}"

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    args = os.environ.get("CAFFEINATE_ARGS_OVERRIDE", "").split() or _CAFFEINATE_ARGS

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from the CLI's controlling tty
        )
    except (OSError, FileNotFoundError) as e:
        _bump("caffeinate_spawn_error", note=str(e))
        return False, f"spawn-error:{e}"

    # Tiny grace period to catch immediate exit (caffeinate binary missing, etc.)
    time.sleep(0.05)
    if proc.poll() is not None:
        _bump("caffeinate_died_immediately", note=f"exit={proc.returncode}")
        return False, f"died-immediately:exit={proc.returncode}"

    try:
        _PIDFILE.write_text(str(proc.pid))
    except OSError as e:
        # Process is running but we can't track it — kill to avoid orphan
        try:
            proc.terminate()
        except OSError:
            pass
        _bump("caffeinate_pidfile_write_error", note=str(e))
        return False, f"pidfile-write-error:{e}"

    _bump("caffeinate_started")
    logger.info("caffeinate spawned pid=%d args=%s", proc.pid, args)
    return True, f"started:{proc.pid}"


def stop() -> tuple[bool, str]:
    """Idempotent: stop caffeinate if running.

    Returns (stopped?, detail).
      stopped=True  → we sent SIGTERM and it exited (or was already gone)
      stopped=False → nothing was running
    """
    if not _IS_DARWIN:
        return False, "skipped:not-darwin"

    pid = _read_pid()
    if pid is None:
        # Already not running; remove any stray pidfile and return
        try:
            _PIDFILE.unlink()
        except FileNotFoundError:
            pass
        return False, "not-running"

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Race: died between _read_pid and now. Fine.
        pass
    except OSError as e:
        _bump("caffeinate_term_error", note=str(e))
        # Try removing pidfile anyway — we no longer trust the state
        try:
            _PIDFILE.unlink()
        except FileNotFoundError:
            pass
        return False, f"term-error:{e}"

    # Wait up to 1s for clean exit, then SIGKILL
    for _ in range(20):
        if not _is_alive(pid):
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
            _bump("caffeinate_force_killed")
        except (ProcessLookupError, OSError):
            pass

    try:
        _PIDFILE.unlink()
    except FileNotFoundError:
        pass

    _bump("caffeinate_stopped")
    logger.info("caffeinate stopped pid=%d", pid)
    return True, f"stopped:{pid}"


def sync_with_state(mode: str) -> tuple[bool, str]:
    """Reconcile caffeinate state with autopilot mode.

    on_temporary / on_permanent → ensure caffeinate is running
    off                         → ensure caffeinate is stopped

    Idempotent: re-runs are no-ops if already in the desired state.
    Returns (changed?, detail).
    """
    if mode in ("on_temporary", "on_permanent"):
        return ensure_running()
    if mode == "off":
        return stop()
    # Unknown mode — no action, but log for ZSF audit
    _bump("caffeinate_sync_unknown_mode", note=str(mode))
    return False, f"unknown-mode:{mode}"
