"""Autopilot state machine — persistent, race-safe, invariant-enforced.

Implements Aaron's exact spec:

- Mode ∈ {"off", "on_permanent", "on_temporary"} (closed enum).
- ONLY user-issued action can transition `on_permanent → off` (user_lock invariant).
- Atlas can transition `off ↔ on_temporary` freely.
- Atlas CANNOT set `on_permanent`. Atlas CANNOT clear `on_permanent`.
- State persists across daemon crashes / reboots / session restarts.
- Every transition observable via a monotonic counter + audit log.
- Concurrent transitions are race-safe (flock + atomic rename).
- ZSF: every I/O error increments a named counter, no silent failures.

This module is intentionally dependency-free (stdlib only) so it can be
imported from the user CLI, the runner loop, the daemon, and tests without
dragging in the rest of the codebase.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("autopilot.state")

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

VALID_MODES: frozenset[str] = frozenset({"off", "on_permanent", "on_temporary"})
VALID_ACTORS: frozenset[str] = frozenset({"user", "atlas"})
SCHEMA_VERSION = 1
HISTORY_CAP = 1000

DEFAULT_STATE_DIR = Path(os.path.expanduser("~/.context-dna"))
DEFAULT_STATE_FILE = DEFAULT_STATE_DIR / "autopilot_state.json"
DEFAULT_LOCK_FILE = DEFAULT_STATE_DIR / "autopilot_state.lock"

# Override hooks (used by tests + the CLI when --state-file is passed).
_STATE_FILE_OVERRIDE: Optional[Path] = None
_LOCK_FILE_OVERRIDE: Optional[Path] = None


def set_state_paths(state_file: Path, lock_file: Optional[Path] = None) -> None:
    """Override the on-disk state paths (used by tests). Resets counters."""
    global _STATE_FILE_OVERRIDE, _LOCK_FILE_OVERRIDE
    _STATE_FILE_OVERRIDE = Path(state_file)
    _LOCK_FILE_OVERRIDE = (
        Path(lock_file) if lock_file else Path(str(state_file) + ".lock")
    )
    reset_counters()


def clear_state_paths() -> None:
    """Restore default state paths."""
    global _STATE_FILE_OVERRIDE, _LOCK_FILE_OVERRIDE
    _STATE_FILE_OVERRIDE = None
    _LOCK_FILE_OVERRIDE = None


def _state_path() -> Path:
    return _STATE_FILE_OVERRIDE if _STATE_FILE_OVERRIDE else DEFAULT_STATE_FILE


def _lock_path() -> Path:
    return _LOCK_FILE_OVERRIDE if _LOCK_FILE_OVERRIDE else DEFAULT_LOCK_FILE


# ----------------------------------------------------------------------------
# Counters (ZSF — every error is observable)
# ----------------------------------------------------------------------------

_counters_lock = threading.Lock()
_counters: dict[str, int] = {}


def reset_counters() -> None:
    with _counters_lock:
        _counters.clear()


def _incr(name: str, **labels: str) -> None:
    key = name
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        key = f"{name}{{{label_str}}}"
    with _counters_lock:
        _counters[key] = _counters.get(key, 0) + 1


def get_counters() -> dict[str, int]:
    with _counters_lock:
        return dict(_counters)


# ----------------------------------------------------------------------------
# Immutable state dataclass
# ----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class State:
    version: int
    mode: str
    set_by: str
    set_at: str  # ISO8601 UTC
    temporary_until: Optional[str]
    temporary_reason: Optional[str]
    user_lock: bool
    transition_history: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "set_by": self.set_by,
            "set_at": self.set_at,
            "temporary_until": self.temporary_until,
            "temporary_reason": self.temporary_reason,
            "user_lock": self.user_lock,
            "transition_history": list(self.transition_history),
        }


def _default_state() -> State:
    return State(
        version=SCHEMA_VERSION,
        mode="off",
        set_by="user",
        set_at=_now_iso(),
        temporary_until=None,
        temporary_reason=None,
        user_lock=False,
        transition_history=(),
    )


# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _monotonic_ns() -> int:
    return time.monotonic_ns()


# ----------------------------------------------------------------------------
# File lock (cross-process via fcntl.flock; cross-thread via threading.Lock)
# ----------------------------------------------------------------------------

_thread_lock = threading.Lock()


@contextlib.contextmanager
def _locked():
    """Acquire process + thread lock around state I/O. ZSF on lock failure."""
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # threading lock first to prevent intra-process re-entry on the same fd.
    _thread_lock.acquire()
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            _incr("autopilot_state_io_errors_total", op="flock")
            logger.warning("autopilot flock failed: %s", e)
            raise
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                _incr("autopilot_state_io_errors_total", op="flock_unlock")
            try:
                os.close(fd)
            except OSError:
                _incr("autopilot_state_io_errors_total", op="flock_close")
        _thread_lock.release()


# ----------------------------------------------------------------------------
# Disk I/O (atomic + corruption-tolerant)
# ----------------------------------------------------------------------------


def _ensure_parent() -> None:
    _state_path().parent.mkdir(parents=True, exist_ok=True)


def _read_raw() -> Optional[dict[str, Any]]:
    path = _state_path()
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        _incr("autopilot_state_io_errors_total", op="read")
        logger.warning("autopilot state read failed: %s", e)
        return None
    if not text.strip():
        _incr("autopilot_state_corruption_total", reason="empty")
        logger.warning("autopilot state file empty; treating as missing")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        _incr("autopilot_state_corruption_total", reason="json_decode")
        logger.warning("autopilot state corruption: %s", e)
        return None


def _validate_raw(raw: dict[str, Any]) -> Optional[State]:
    try:
        version = int(raw.get("version", 0))
        mode = raw["mode"]
        set_by = raw["set_by"]
        set_at = raw["set_at"]
        temporary_until = raw.get("temporary_until")
        temporary_reason = raw.get("temporary_reason")
        user_lock = bool(raw.get("user_lock", False))
        history = raw.get("transition_history", [])
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode {mode!r}")
        if set_by not in VALID_ACTORS:
            raise ValueError(f"invalid set_by {set_by!r}")
        if not isinstance(history, list):
            raise ValueError("history must be list")
        # user_lock must match mode == on_permanent (invariant).
        if mode == "on_permanent" and not user_lock:
            user_lock = True  # self-heal forward
            _incr("autopilot_state_corruption_total", reason="user_lock_mismatch")
        if mode != "on_permanent" and user_lock:
            user_lock = False
            _incr("autopilot_state_corruption_total", reason="user_lock_mismatch")
        return State(
            version=version,
            mode=mode,
            set_by=set_by,
            set_at=set_at,
            temporary_until=temporary_until,
            temporary_reason=temporary_reason,
            user_lock=user_lock,
            transition_history=tuple(history),
        )
    except (KeyError, ValueError, TypeError) as e:
        _incr("autopilot_state_corruption_total", reason="schema")
        logger.warning("autopilot state schema invalid: %s", e)
        return None


def _atomic_write(state: State) -> None:
    _ensure_parent()
    path = _state_path()
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".autopilot_state-", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError as e:
        _incr("autopilot_state_io_errors_total", op="write")
        logger.warning("autopilot state write failed: %s", e)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def read_state() -> State:
    """Return current state. If missing or corrupt → write default and return it.

    Corruption-tolerant: a half-written JSON file does not crash the loop. It is
    logged + counted (`autopilot_state_corruption_total`) and treated as missing.
    """
    with _locked():
        raw = _read_raw()
        if raw is None:
            state = _default_state()
            _atomic_write(state)
            return state
        validated = _validate_raw(raw)
        if validated is None:
            # Don't blow away the bad file — back it up so a human can inspect.
            try:
                bad = _state_path()
                bak = bad.with_suffix(bad.suffix + ".corrupt")
                with contextlib.suppress(OSError):
                    os.replace(bad, bak)
            except OSError:
                _incr("autopilot_state_io_errors_total", op="backup_corrupt")
            state = _default_state()
            _atomic_write(state)
            return state
        return validated


def is_active() -> bool:
    """Return True iff autopilot is currently running (mode != 'off')."""
    return read_state().mode != "off"


def is_user_locked() -> bool:
    """Return True iff state is `on_permanent` — only the user can clear it."""
    return read_state().mode == "on_permanent"


def _check_transition(
    current: State, new_mode: str, actor: str
) -> tuple[bool, str]:
    """Pure invariant check. Returns (ok, reason_if_not_ok)."""
    if new_mode not in VALID_MODES:
        return False, f"invariant:invalid_mode:{new_mode}"
    if actor not in VALID_ACTORS:
        return False, f"invariant:invalid_actor:{actor}"

    # No-op (same mode) is allowed but recorded.
    if current.mode == new_mode:
        return True, ""

    # Invariant 4: Atlas CANNOT set on_permanent.
    if actor == "atlas" and new_mode == "on_permanent":
        return False, "invariant:atlas_cannot_set_on_permanent"

    # Invariant 2 + 5: ONLY user can clear on_permanent.
    if current.mode == "on_permanent" and new_mode == "off" and actor != "user":
        return False, "invariant:user_lock:only_user_can_disable_permanent"

    # Invariant 5 extension: Atlas cannot transition `on_permanent → on_temporary`
    # either, because that would be a sneaky way to clear the permanent lock
    # (next Atlas action would flip temp → off). Aaron's spec: "Only user can
    # turn it off". An on_permanent state is sticky for the user.
    if current.mode == "on_permanent" and actor != "user":
        return False, "invariant:user_lock:permanent_is_user_only"

    return True, ""


def transition(
    new_mode: str,
    actor: str,
    reason: str = "",
    temporary_until: Optional[str] = None,
) -> tuple[bool, str]:
    """Attempt to transition to `new_mode` on behalf of `actor`.

    Returns (success, message). On failure, message starts with 'invariant:' or
    'io:'. On success, message is empty. State counters are always advanced.
    """
    with _locked():
        try:
            current = _read_current_or_default()
        except OSError as e:
            return False, f"io:read:{e}"

        ok, err = _check_transition(current, new_mode, actor)
        _incr(
            "autopilot_transitions_total",
            actor=actor,
            from_mode=current.mode,
            to_mode=new_mode,
            outcome="allowed" if ok else "blocked",
        )
        if not ok:
            logger.info(
                "autopilot transition blocked: %s -> %s by %s: %s",
                current.mode,
                new_mode,
                actor,
                err,
            )
            return False, err

        now = _now_iso()
        new_history = list(current.transition_history)
        new_history.append(
            {
                "ts": now,
                "monotonic_ns": _monotonic_ns(),
                "from": current.mode,
                "to": new_mode,
                "actor": actor,
                "reason": reason,
                "temporary_until": temporary_until,
            }
        )
        if len(new_history) > HISTORY_CAP:
            new_history = new_history[-HISTORY_CAP:]

        new_state = State(
            version=SCHEMA_VERSION,
            mode=new_mode,
            set_by=actor,
            set_at=now,
            temporary_until=(
                temporary_until if new_mode == "on_temporary" else None
            ),
            temporary_reason=(
                reason if new_mode == "on_temporary" and reason else None
            ),
            user_lock=(new_mode == "on_permanent"),
            transition_history=tuple(new_history),
        )

        try:
            _atomic_write(new_state)
        except OSError as e:
            return False, f"io:write:{e}"

        logger.info(
            "autopilot transition: %s -> %s by %s (reason=%r)",
            current.mode,
            new_mode,
            actor,
            reason,
        )
        return True, ""


def _read_current_or_default() -> State:
    raw = _read_raw()
    if raw is None:
        state = _default_state()
        _atomic_write(state)
        return state
    validated = _validate_raw(raw)
    if validated is None:
        state = _default_state()
        _atomic_write(state)
        return state
    return validated


def history_tail(n: int = 10) -> list[dict[str, Any]]:
    """Return the last n transitions (newest last)."""
    state = read_state()
    return list(state.transition_history[-n:])
