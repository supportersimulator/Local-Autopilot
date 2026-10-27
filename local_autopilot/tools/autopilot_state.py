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
VALID_ACTORS: frozenset[str] = frozenset({"user", "atlas", "system"})
# Actors a human/CLI may name. "system" is internal: it may ONLY perform the
# automatic `on_temporary -> off` expiry (reason prefix "expired:").
CLI_ACTORS: frozenset[str] = frozenset({"user", "atlas"})
SYSTEM_EXPIRY_PREFIX = "expired:"
# Temporary mode must be bounded: deadline in (now+60s, now+MAX_TEMP_HOURS].
MIN_TEMP_LEAD_S = 60
MAX_TEMP_HOURS = 8
# Atlas elevation without an explicit deadline gets this, never unbounded.
DEFAULT_TEMP_MINUTES = 30
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
    # Rollback guard for temporary expiry (same boot -> monotonic elapsed).
    temporary_set_monotonic_ns: Optional[int] = None
    temporary_set_boot_id: Optional[str] = None

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
            "temporary_set_monotonic_ns": self.temporary_set_monotonic_ns,
            "temporary_set_boot_id": self.temporary_set_boot_id,
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


def _now_dt() -> dt.datetime:
    """Wall clock (UTC, tz-aware). Indirection so tests can move time."""
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_dt().isoformat(timespec="microseconds")


def _monotonic_ns() -> int:
    return time.monotonic_ns()


_BOOT_ID_CACHE: list = []


def _boot_id() -> Optional[str]:
    """Identifier of the current boot (None if unknown -> wall clock only)."""
    if _BOOT_ID_CACHE:
        return _BOOT_ID_CACHE[0]
    bid: Optional[str] = None
    try:
        bid = Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        try:
            import subprocess
            out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                 capture_output=True, text=True, timeout=2)
            bid = out.stdout.strip() or None
        except Exception:  # noqa: BLE001
            bid = None
    _BOOT_ID_CACHE.append(bid)
    return bid


def parse_deadline(value: Any, now: Optional[dt.datetime] = None) -> dt.datetime:
    """Parse + bound-check a temporary_until value. Raises ValueError.

    Requires ISO-8601 with a timezone (a trailing 'Z' is accepted), strictly
    later than now + MIN_TEMP_LEAD_S and no later than now + MAX_TEMP_HOURS.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("temporary_until is required (ISO-8601 with timezone)")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"temporary_until {value!r} is not ISO-8601") from None
    if d.tzinfo is None:
        raise ValueError(f"temporary_until {value!r} has no timezone (add Z or +HH:MM)")
    now = now or _now_dt()
    if d <= now + dt.timedelta(seconds=MIN_TEMP_LEAD_S):
        raise ValueError(f"temporary_until {value!r} is in the past or < {MIN_TEMP_LEAD_S}s away")
    if d > now + dt.timedelta(hours=MAX_TEMP_HOURS):
        raise ValueError(
            f"temporary_until {value!r} is more than {MAX_TEMP_HOURS}h away; "
            "use `autopilot on` (user-only on_permanent) for unbounded runs"
        )
    return d


def _expired(state: "State") -> bool:
    """True if an on_temporary state is past its deadline. Fail closed:
    a missing/unparseable deadline counts as expired."""
    if state.mode != "on_temporary":
        return False
    raw = state.temporary_until
    try:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        deadline = dt.datetime.fromisoformat(text)
        if deadline.tzinfo is None:
            return True
    except (TypeError, ValueError):
        return True
    if _now_dt() >= deadline:
        return True
    # Wall clock rolled back? On the same boot, monotonic elapsed since the
    # elevation was set is authoritative.
    if (state.temporary_set_monotonic_ns is not None
            and state.temporary_set_boot_id is not None
            and state.temporary_set_boot_id == _boot_id()):
        try:
            set_at = dt.datetime.fromisoformat(state.set_at)
            budget_ns = (deadline - set_at).total_seconds() * 1e9
            if _monotonic_ns() - int(state.temporary_set_monotonic_ns) >= budget_ns:
                return True
        except (TypeError, ValueError):
            return True
    return False


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
            temporary_set_monotonic_ns=raw.get("temporary_set_monotonic_ns"),
            temporary_set_boot_id=raw.get("temporary_set_boot_id"),
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


def read_state(enforce_expiry: bool = True) -> State:
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
        if enforce_expiry:
            validated, _ = _expire_locked(validated)
        return validated


def _expire_locked(state: State) -> tuple[State, bool]:
    """Caller holds _locked(). Revert an expired on_temporary to off as the
    internal `system` actor. Returns (state, did_expire)."""
    if not _expired(state):
        return state, False
    reason = f"{SYSTEM_EXPIRY_PREFIX}{state.temporary_until}"
    now = _now_iso()
    hist = list(state.transition_history)
    hist.append({"ts": now, "monotonic_ns": _monotonic_ns(), "from": state.mode,
                 "to": "off", "actor": "system", "reason": reason,
                 "temporary_until": None})
    new_state = State(version=SCHEMA_VERSION, mode="off", set_by="system",
                      set_at=now, temporary_until=None, temporary_reason=None,
                      user_lock=False, transition_history=tuple(hist[-HISTORY_CAP:]))
    _incr("autopilot_transitions_total", actor="system", from_mode="on_temporary",
          to_mode="off", outcome="allowed")
    _incr("autopilot_temporary_expired_total")
    logger.warning("autopilot temporary mode expired (%s); reverted to off", reason)
    _atomic_write(new_state)
    return new_state, True


def expire_if_due() -> bool:
    """Enforce the temporary deadline now. True iff this call reverted it."""
    with _locked():
        raw = _read_raw()
        if raw is None:
            return False
        validated = _validate_raw(raw)
        if validated is None:
            return False
        _, did = _expire_locked(validated)
        return did


def is_active() -> bool:
    """Return True iff autopilot is currently running (mode != 'off')."""
    return read_state().mode != "off"


def is_user_locked() -> bool:
    """Return True iff state is `on_permanent` — only the user can clear it."""
    return read_state().mode == "on_permanent"


def _check_transition(
    current: State, new_mode: str, actor: str,
    *, reason: str = "", temporary_until: Optional[str] = None,
) -> tuple[bool, str]:
    """Pure invariant check. Returns (ok, reason_if_not_ok).

    Invariant 3 — TEMPORARY_BOUNDED — added 2026-05-14 after Aaron caught the
    CLI letting `autopilot temp "..."` succeed with temporary_until=None and a
    reason that did not contain 'atlas-decision'. The CONTRACT.md required
    this rule; the in-tree mock enforced it; this real module did not. The
    invariance test passed only because the conftest forced HAS_F1=False so
    every invariance test ran against the mock — textbook test theater. The
    rule is now enforced HERE, in the real module, with NO actor-based
    carve-out. Invariance does not have exceptions.
    """
    if new_mode not in VALID_MODES:
        return False, f"invariant:invalid_mode:{new_mode}"
    if actor not in VALID_ACTORS:
        return False, f"invariant:invalid_actor:{actor}"

    # Actor invariants — evaluated BEFORE the no-op short-circuit. A TOCTOU race
    # (mac3 2026-05-14) showed that letting `off → off` be a free no-op lets atlas
    # smuggle a "successful" deactivation past the actor gate when user-off lands
    # first under flock contention.

    # Invariant 6: `system` may ONLY auto-expire on_temporary -> off.
    if actor == "system":
        if not (current.mode == "on_temporary" and new_mode == "off"
                and (reason or "").startswith(SYSTEM_EXPIRY_PREFIX)):
            return False, "invariant:system_actor_only_expires_temporary"
        return True, ""

    # Invariant 4: Atlas CANNOT set on_permanent.
    if actor == "atlas" and new_mode == "on_permanent":
        return False, "invariant:atlas_cannot_set_on_permanent"

    # Invariant 1 (hardened, race-safe): atlas can only emit `to=off` when ending
    # its own on_temporary elevation. Off→off is denied because semantically that
    # is "atlas attempted to turn the system off" — Aaron's spec: only user can.
    if actor == "atlas" and new_mode == "off" and current.mode != "on_temporary":
        return False, "invariant:user_lock:atlas_off_only_from_temporary"

    # Invariant 2 + 5: ONLY user can clear on_permanent.
    if current.mode == "on_permanent" and new_mode == "off" and actor != "user":
        return False, "invariant:user_lock:only_user_can_disable_permanent"

    # Invariant 5 extension: Atlas cannot transition `on_permanent → on_temporary`
    # either, because that would be a sneaky way to clear the permanent lock
    # (next Atlas action would flip temp → off). Aaron's spec: "Only user can
    # turn it off". An on_permanent state is sticky for the user.
    if current.mode == "on_permanent" and actor != "user":
        return False, "invariant:user_lock:permanent_is_user_only"

    # Invariant 3: on_temporary must be bounded.
    # Either a temporary_until deadline OR a reason containing 'atlas-decision'
    # (the Atlas-managed self-bound case where the runner enforces the bound
    # internally). NO actor-based carve-out: user must supply --until.
    # Note: this fires even for no-op (current == new_mode) transitions to
    # `on_temporary`, so a stuck bad state CANNOT be re-entered without a
    # proper bound. The same-mode shortcut now sits AFTER this check.
    # The deadline is PARSED and bounded (MIN_TEMP_LEAD_S..MAX_TEMP_HOURS).
    # The old "atlas-decision in reason" bypass is gone: any actor could type
    # it. Atlas without --until gets DEFAULT_TEMP_MINUTES stamped by
    # transition() before this check.
    if new_mode == "on_temporary":
        try:
            parse_deadline(temporary_until)
        except ValueError as e:
            return False, f"invariant:temporary_bounded:{e}"

    # No-op (same mode) is allowed but recorded — only AFTER all actor + bounded
    # invariants above have been re-validated. Prevents stuck bad states from
    # being perpetuated via repeated same-mode "transitions" AND closes the
    # TOCTOU race where atlas could smuggle a successful off→off through.
    if current.mode == new_mode:
        return True, ""

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
    if (new_mode == "on_temporary" and actor == "atlas"
            and (temporary_until is None or str(temporary_until).strip() == "")):
        temporary_until = (
            _now_dt() + dt.timedelta(minutes=DEFAULT_TEMP_MINUTES)
        ).isoformat()
    with _locked():
        try:
            current = _read_current_or_default()
            current, _ = _expire_locked(current)
        except OSError as e:
            return False, f"io:read:{e}"

        ok, err = _check_transition(
            current, new_mode, actor,
            reason=reason, temporary_until=temporary_until,
        )
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
            temporary_set_monotonic_ns=(
                _monotonic_ns() if new_mode == "on_temporary" else None
            ),
            temporary_set_boot_id=(
                _boot_id() if new_mode == "on_temporary" else None
            ),
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
