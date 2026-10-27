"""User-only control directory + fail-closed kill switch (FIX-DESIGNS §4).

Replaces the old `/tmp/autopilot.stop` (non-durable, deletable by anyone,
redirectable via AUTOPILOT_KILL_FILE). Semantics of `kill_state()`:

    STOP file present                 -> ("stop", "stop_file")
    control dir missing               -> ("stop", "control_dir_missing")
    symlink / wrong owner / mode>0700 -> ("stop", "unsafe:<detail>")
    RUN marker missing                -> ("stop", "run_marker_missing")
    any OSError while checking        -> ("stop", "unreadable:<err>")
    otherwise                         -> ("run", "ok")

Recover with:  python -m local_autopilot.tools.control_dir init|resume|status

CAVEAT: agents still run as the same uid with --dangerously-skip-permissions
(headless_executor.py). Until that is removed (FIX-DESIGNS item 3) an agent
can still delete/create files here; this module only makes the switch
durable, non-redirectable, and fail-closed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from . import _safe_dir as sd
except ImportError:  # script-style import
    import _safe_dir as sd  # type: ignore

STOP_NAME = "STOP"
RUN_NAME = "RUN"

_OVERRIDE: Optional[Path] = None


def default_control_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Local-Autopilot" / "control"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "local-autopilot" / "control"


def set_control_dir(path: Path) -> None:
    """Test hook (in-process only; deliberately NOT an env var)."""
    global _OVERRIDE
    _OVERRIDE = Path(path)


def clear_control_dir() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def control_dir() -> Path:
    return _OVERRIDE if _OVERRIDE is not None else default_control_dir()


def kill_state() -> tuple[str, str]:
    d = control_dir()
    try:
        try:
            os.lstat(d)
        except FileNotFoundError:
            return "stop", "control_dir_missing"
        try:
            sd.check_private(d, is_dir=True)
            sd.check_parents_in_home(d)
        except sd.UnsafePath as e:
            return "stop", f"unsafe:{e}"
        try:
            os.lstat(d / STOP_NAME)
            return "stop", "stop_file"
        except FileNotFoundError:
            pass
        try:
            sd.check_private(d / RUN_NAME, is_dir=False)
        except FileNotFoundError:
            return "stop", "run_marker_missing"
        except sd.UnsafePath as e:
            return "stop", f"unsafe:{e}"
        return "run", "ok"
    except OSError as e:
        return "stop", f"unreadable:{e}"


def _write_private(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o600)


def init_control_dir() -> Path:
    d = control_dir()
    sd.make_private_dir(d)
    run = d / RUN_NAME
    if not run.exists():
        _write_private(run, json.dumps({"created": time.time()}) + "\n")
    return d


def request_stop(reason: str = "") -> Path:
    d = control_dir()
    sd.make_private_dir(d)
    p = d / STOP_NAME
    _write_private(p, json.dumps({"ts": time.time(), "reason": reason}) + "\n")
    return p


def clear_stop() -> None:
    try:
        (control_dir() / STOP_NAME).unlink()
    except FileNotFoundError:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="autopilot-control",
                                description="Local Autopilot kill switch")
    p.add_argument("cmd", choices=("status", "init", "stop", "resume"))
    p.add_argument("--reason", default="")
    a = p.parse_args(argv)
    if a.cmd == "init":
        init_control_dir()
    elif a.cmd == "stop":
        request_stop(a.reason)
    elif a.cmd == "resume":
        init_control_dir()
        clear_stop()
    state, why = kill_state()
    print(json.dumps({"control_dir": str(control_dir()), "state": state, "reason": why}))
    return 0 if state == "run" or a.cmd == "stop" else 1


if __name__ == "__main__":
    raise SystemExit(main())
