"""User-facing CLI for the autopilot state machine.

Commands:
    autopilot status                              — print current state + history tail
    autopilot on                                  — set on_permanent (actor=user)
    autopilot off                                 — set off (actor=user); only way to clear on_permanent
    autopilot temp <reason> [--until <iso>]       — set on_temporary; default actor=user
                                                    (use --actor atlas to simulate an Atlas-triggered
                                                    temporary elevation)

Exit codes:
    0 — success
    1 — invalid transition (invariant blocked it)
    2 — invariant violation in arguments (bad mode/actor)
    3 — I/O error

All commands print human-readable text AND append a JSON line to
/tmp/autopilot-cli.log so external observers can tail it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Make this script runnable both as a module and as a standalone file.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import autopilot_state as st  # noqa: E402

CLI_LOG = Path("/tmp/autopilot-cli.log")


def _log_event(event: dict) -> None:
    try:
        CLI_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CLI_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **event}) + "\n")
    except OSError:
        # ZSF — don't lose the CLI command just because /tmp is full.
        pass


def cmd_status(_args) -> int:
    state = st.read_state()
    print(f"autopilot mode      : {state.mode}")
    print(f"set by              : {state.set_by}")
    print(f"set at              : {state.set_at}")
    print(f"user_lock           : {state.user_lock}")
    if state.mode == "on_temporary":
        print(f"temporary_until     : {state.temporary_until}")
        print(f"temporary_reason    : {state.temporary_reason}")
    print(f"history (last 5):")
    for entry in state.transition_history[-5:]:
        print(
            f"  - {entry.get('ts')} {entry.get('from')} -> {entry.get('to')} "
            f"by {entry.get('actor')} ({entry.get('reason','')})"
        )
    counters = st.get_counters()
    if counters:
        print("counters:")
        for k, v in sorted(counters.items()):
            print(f"  {k} = {v}")
    _log_event({"cmd": "status", "mode": state.mode})
    return 0


def _do_transition(new_mode: str, actor: str, reason: str, until: Optional[str]) -> int:
    if actor not in st.VALID_ACTORS:
        print(f"ERROR: invalid actor {actor!r}", file=sys.stderr)
        _log_event({"cmd": "transition", "error": "invalid_actor", "actor": actor})
        return 2
    if new_mode not in st.VALID_MODES:
        print(f"ERROR: invalid mode {new_mode!r}", file=sys.stderr)
        _log_event({"cmd": "transition", "error": "invalid_mode", "mode": new_mode})
        return 2
    ok, msg = st.transition(new_mode, actor, reason=reason, temporary_until=until)
    if ok:
        state = st.read_state()
        print(f"OK: autopilot mode = {state.mode} (by {state.set_by})")
        _log_event(
            {
                "cmd": "transition",
                "ok": True,
                "to": new_mode,
                "actor": actor,
                "reason": reason,
            }
        )
        return 0
    print(f"BLOCKED: {msg}", file=sys.stderr)
    _log_event(
        {
            "cmd": "transition",
            "ok": False,
            "to": new_mode,
            "actor": actor,
            "reason": reason,
            "error": msg,
        }
    )
    if msg.startswith("io:"):
        return 3
    return 1


def cmd_on(args) -> int:
    return _do_transition("on_permanent", "user", reason=args.reason or "", until=None)


def cmd_off(args) -> int:
    return _do_transition("off", "user", reason=args.reason or "", until=None)


def cmd_temp(args) -> int:
    actor = args.actor or "user"
    return _do_transition(
        "on_temporary", actor, reason=args.reason, until=args.until
    )


def _resolve_state_paths(args) -> None:
    state_file = args.state_file or os.environ.get("AUTOPILOT_STATE_FILE")
    if state_file:
        st.set_state_paths(Path(state_file))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description="Autopilot state CLI")
    p.add_argument(
        "--state-file",
        help="Override the state file path (default: ~/.context-dna/autopilot_state.json).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Show current autopilot state.")
    p_status.set_defaults(func=cmd_status)

    p_on = sub.add_parser("on", help="Turn autopilot on permanently (user only).")
    p_on.add_argument("--reason", default="")
    p_on.set_defaults(func=cmd_on)

    p_off = sub.add_parser(
        "off",
        help="Turn autopilot off. Only the user can clear on_permanent.",
    )
    p_off.add_argument("--reason", default="")
    p_off.set_defaults(func=cmd_off)

    p_temp = sub.add_parser(
        "temp",
        help="Set on_temporary. Default actor=user; --actor atlas simulates an "
        "Atlas-elevated temporary autopilot.",
    )
    p_temp.add_argument("reason", help="Required reason for temporary elevation.")
    p_temp.add_argument(
        "--until",
        default=None,
        help=(
            "ISO8601 timestamp marking the deadline. REQUIRED unless reason "
            "contains 'atlas-decision' (Atlas-managed self-bound case). "
            "Invariant #3 TEMPORARY_BOUNDED — see tests/invariance/CONTRACT.md."
        ),
    )
    p_temp.add_argument(
        "--actor",
        choices=sorted(st.VALID_ACTORS),
        default="user",
        help="Who is requesting the temporary elevation (default: user).",
    )
    p_temp.set_defaults(func=cmd_temp)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _resolve_state_paths(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
