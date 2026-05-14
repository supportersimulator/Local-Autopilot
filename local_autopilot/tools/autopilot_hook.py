"""Integration hooks for the runner loop (F2) and Atlas.

These functions are pure read/write wrappers around autopilot_state. They are
imported by the runner loop and by Atlas-side glue when Atlas wants to invoke
a temporary autopilot elevation.

This module deliberately does NOT execute any loop logic. It only exposes
yes/no decisions and a single transition entrypoint.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

try:  # package-style import when loaded as autopilot.tools.autopilot_hook
    from . import autopilot_state as st
except ImportError:  # script-style import (sys.path includes tools/)
    import sys

    _HERE = Path(__file__).resolve().parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import autopilot_state as st  # type: ignore

logger = logging.getLogger("autopilot.hook")

ATLAS_REQUEST_LOG = Path("/tmp/autopilot-atlas-requests.jsonl")


def should_run_next_cycle() -> tuple[bool, str]:
    """The runner calls this before each cycle.

    Returns (should_run, reason).
        - (False, "user_disabled") when mode == "off"
        - (True, "on_permanent")   when mode == "on_permanent"
        - (True, "on_temporary")   when mode == "on_temporary"
    """
    state = st.read_state()
    if state.mode == "off":
        return False, "user_disabled"
    if state.mode == "on_permanent":
        return True, "on_permanent"
    if state.mode == "on_temporary":
        return True, "on_temporary"
    # Unreachable given the closed enum, but ZSF defensive.
    return False, f"unknown_mode:{state.mode}"


def on_atlas_temp_request(
    reason: str, temporary_until: Optional[str] = None
) -> tuple[bool, str]:
    """Atlas asks: please activate temporary autopilot.

    Logs the request, checks the user_lock invariant, and (if allowed)
    transitions `off → on_temporary`. Atlas may NOT call this to override an
    `on_permanent` lock; the state machine will reject it.

    Returns (granted, reason_or_error).
    """
    import json as _json

    request_event = {
        "ts": time.time(),
        "actor": "atlas",
        "reason": reason,
        "temporary_until": temporary_until,
    }
    try:
        ATLAS_REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ATLAS_REQUEST_LOG.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(request_event) + "\n")
    except OSError:
        # ZSF — request log is best-effort.
        pass

    current = st.read_state()

    # Atlas cannot break the user_lock. If user has it on_permanent, we leave
    # it alone — Atlas elevation is meaningless when the user already has it
    # permanently enabled.
    if current.mode == "on_permanent":
        return False, "user_lock:already_on_permanent"

    ok, err = st.transition(
        "on_temporary",
        actor="atlas",
        reason=reason or "atlas-temporary",
        temporary_until=temporary_until,
    )
    return ok, err if err else "granted"


def on_atlas_temp_release(reason: str = "atlas-release") -> tuple[bool, str]:
    """Atlas signals: my temporary elevation is done; please go back to off.

    This is the only way Atlas itself can clear autopilot — and only when the
    current state is `on_temporary` (state machine will block any attempt to
    clear `on_permanent`).
    """
    current = st.read_state()
    if current.mode != "on_temporary":
        return False, f"not_in_temporary:{current.mode}"
    ok, err = st.transition("off", actor="atlas", reason=reason)
    return ok, err if err else "released"
