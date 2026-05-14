"""In-tree reference implementation that satisfies the F1 contract.

Used by the invariance harness ONLY when tools/autopilot_state.py is absent.
This is intentionally minimal — its purpose is to let the invariance tests
exercise *real* file I/O, flock, atomic rename, JSON-corruption recovery,
etc., even before F1 ships. When the real module appears, conftest.py
prefers it.

Tests that monkey-patch internal helpers (e.g. State._lock_path) target
the implementation that's actually being used — there is no second code
path. That keeps the harness honest.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path


class StateCorruption(Exception):
    """Raised when on-disk JSON is unparseable or schema invalid."""


class PermissionDenied(Exception):
    """Raised when an actor attempts a forbidden transition."""


def _utcnow_iso() -> str:
    # Use UTC-aware datetime then drop tzinfo for stable ISO output with 'Z'.
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


_DEFAULT = {
    "mode": "off",
    "temporary_until": None,
    "temporary_reason": None,
    "transition_history": [],
    "counters": {"autopilot_transitions_total": 0, "cycles_completed": 0},
    "schema_version": 1,
}

VALID_MODES = {"off", "on_temporary", "on_permanent"}
VALID_ACTORS = {"user", "atlas", "system"}


def _schema_ok(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    for k in ("mode", "transition_history", "counters", "schema_version"):
        if k not in d:
            return False
    if d["mode"] not in VALID_MODES:
        return False
    if not isinstance(d["transition_history"], list):
        return False
    if not isinstance(d["counters"], dict):
        return False
    return True


class State:
    VALID_MODES = VALID_MODES
    VALID_ACTORS = VALID_ACTORS

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._atomic_write(_DEFAULT)

    # ── locking helpers ────────────────────────────────────────────────
    def _acquire(self):
        f = open(self._lock_path, "w")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    def _release(self, f):
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()

    # ── on-disk i/o ────────────────────────────────────────────────────
    def _atomic_write(self, data: dict) -> None:
        # write to sibling tmp then rename — POSIX atomic on same FS
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def read(self) -> dict:
        if not self.path.exists():
            return dict(_DEFAULT)
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
            if not raw:
                raise StateCorruption("file is empty")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise StateCorruption(f"non-utf-8 bytes: {e}") from e
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise StateCorruption(f"JSON parse: {e}") from e
        if not _schema_ok(data):
            raise StateCorruption("schema invalid")
        return data

    # ── transition ─────────────────────────────────────────────────────
    def transition(
        self,
        *,
        to: str,
        actor: str,
        reason: str,
        temporary_until: str | None = None,
    ) -> dict:
        if to not in VALID_MODES:
            raise ValueError(f"bad target mode: {to}")
        if actor not in VALID_ACTORS:
            raise ValueError(f"bad actor: {actor}")

        f = self._acquire()
        try:
            cur = self.read()
            frm = cur["mode"]

            # Invariant #1: only user may downgrade on_permanent. Atlas/system
            # cannot move out of on_permanent by ANY path (off OR on_temporary).
            if frm == "on_permanent" and to != "on_permanent" and actor != "user":
                raise PermissionDenied(
                    "USER_ONLY_DEACTIVATE: actor=%s cannot leave on_permanent "
                    "(attempted to=%s)" % (actor, to)
                )

            # Invariant #1 (race-hardened): atlas can only emit `to=off` when
            # ending its own on_temporary elevation. Without this, a TOCTOU race
            # (user-off + atlas-off both target on_permanent → off; user wins
            # flock first) lets atlas observe frm=off and pass an off→off no-op
            # past the actor gate. Aaron's spec: "Only user can turn it off" —
            # so atlas-off from anything other than on_temporary is denied
            # regardless of what atlas's racy read saw.
            if actor == "atlas" and to == "off" and frm != "on_temporary":
                raise PermissionDenied(
                    "USER_ONLY_DEACTIVATE: atlas cannot emit to=off from %s "
                    "(only on_temporary→off is permitted for atlas)" % frm
                )

            # Invariant #2: only user may set on_permanent (atlas + system blocked).
            if to == "on_permanent" and actor != "user":
                raise PermissionDenied(
                    "NO_ATLAS_PROMOTION: actor=%s cannot set on_permanent"
                    % actor
                )

            # Invariant #3: on_temporary must carry a termination condition
            if to == "on_temporary":
                has_bound = bool(temporary_until) or (
                    reason and "atlas-decision" in reason
                )
                if not has_bound:
                    raise PermissionDenied(
                        "TEMPORARY_UNBOUNDED: on_temporary requires "
                        "temporary_until or reason containing 'atlas-decision'"
                    )

            # Atlas may extend on_temporary but cannot remove the bound
            if (
                frm == "on_temporary"
                and to == "on_temporary"
                and actor == "atlas"
            ):
                if temporary_until is None and not (
                    reason and "atlas-decision" in reason
                ):
                    raise PermissionDenied(
                        "ATLAS_BOUND_REMOVAL: atlas cannot strip "
                        "temporary bound during extend"
                    )

            new = dict(cur)
            new["mode"] = to
            if to == "on_temporary":
                new["temporary_until"] = temporary_until
                new["temporary_reason"] = reason
            else:
                new["temporary_until"] = None
                new["temporary_reason"] = None
            entry = {
                "ts": _utcnow_iso(),
                "actor": actor,
                "from": frm,
                "to": to,
                "reason": reason,
            }
            # monotonic ts guarantee — never go backwards
            history = list(new.get("transition_history", []))
            if history:
                last_ts = history[-1].get("ts", "")
                # if clock jitter would produce equal/earlier ts, bump by ms
                while entry["ts"] <= last_ts:
                    time.sleep(0.001)
                    entry["ts"] = _utcnow_iso()
            history.append(entry)
            new["transition_history"] = history
            counters = dict(new.get("counters", {}))
            counters["autopilot_transitions_total"] = (
                counters.get("autopilot_transitions_total", 0) + 1
            )
            new["counters"] = counters

            self._atomic_write(new)

            # Append-only audit log
            log = Path("/tmp/autopilot-cli.log")
            with open(log, "a") as lf:
                lf.write(
                    json.dumps(
                        {
                            "ts": entry["ts"],
                            "actor": actor,
                            "from": frm,
                            "to": to,
                            "reason": reason,
                        }
                    )
                    + "\n"
                )
            try:
                os.chmod(log, 0o644)
            except OSError:
                pass

            return new
        finally:
            self._release(f)
