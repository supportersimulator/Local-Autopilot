"""Persistent rolling cost ledger (FIX-DESIGNS §1).

The runner used to start every invocation at `cost_so_far = 0.0`, so a
launchd/systemd tick every 30 min multiplied the "cap" by 48/day. This
ledger persists spend across invocations in a user-only directory and
enforces ONE cap per rolling window.

Files (dir 0700, files 0600, never /tmp):
    ledger.jsonl   append-only {"ts","run_id","kind","usd"} lines
    ledger.lock    flock target
    ledger.genesis written once at creation; if ledger.jsonl later goes
                   missing while genesis exists, that is tamper -> LedgerError
                   (deleting the ledger must not reset spend to $0)

Accounting: per run_id, spend = max(Σreserve, Σsettle) + Σcharge. A crash
after reserve() therefore still counts the reservation (fail closed).

Any corrupt line, unsafe permission, or I/O error raises LedgerError; the
runner treats that as cap hit (exit 6, counter `cost_ledger_unreadable`).
Recover by inspecting/fixing the file named in the error (never by deleting
it — that is detected as tamper).

CAVEAT: same-uid agents running with --dangerously-skip-permissions can still
edit these files; FIX-DESIGNS item 3 is the prerequisite for this being a
hard wall against a misbehaving agent.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    from . import _safe_dir as sd
except ImportError:  # script-style import
    import _safe_dir as sd  # type: ignore

WINDOW_ROLLING_24H = "rolling_24h"
WINDOW_CALENDAR_DAY = "calendar_day_local"
VALID_WINDOWS = (WINDOW_ROLLING_24H, WINDOW_CALENDAR_DAY)
CLOCK_SKEW_TOLERANCE_S = 300.0
DEFAULT_CAP_USD = 5.0  # used only if config lacks the key (conservative)


class LedgerError(Exception):
    """Ledger unreadable/corrupt/tampered. Treat as cap hit."""


class LedgerUnsafe(LedgerError):
    """Ledger dir/file has unsafe ownership or permissions."""


_OVERRIDE: Optional[Path] = None


def default_ledger_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Local-Autopilot"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "local-autopilot"


def set_ledger_dir(path: Path) -> None:
    """Test hook (in-process only)."""
    global _OVERRIDE
    _OVERRIDE = Path(path)


def clear_ledger_dir() -> None:
    global _OVERRIDE
    _OVERRIDE = None


def ledger_dir() -> Path:
    return _OVERRIDE if _OVERRIDE is not None else default_ledger_dir()


# --------------------------------------------------------------------- policy


@dataclass(frozen=True)
class CostPolicy:
    cap_usd: float
    window: str


def load_cost_policy(config_path: Path) -> CostPolicy:
    """Read `cost_cap_usd_per_window` / `cost_window` from config.yaml.

    Tiny line parser so the runner keeps working without PyYAML. Missing
    key -> DEFAULT_CAP_USD; unreadable/invalid -> LedgerError (fail closed).
    """
    cap = DEFAULT_CAP_USD
    window = WINDOW_ROLLING_24H
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return CostPolicy(cap, window)
    except OSError as e:
        raise LedgerError(f"cost policy unreadable: {config_path}: {e}") from e
    for line in text.splitlines():
        m = re.match(r"^\s*(cost_cap_usd_per_window|cost_window)\s*:\s*([^#]+)", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip("'\"")
        if key == "cost_cap_usd_per_window":
            try:
                cap = float(val)
            except ValueError as e:
                raise LedgerError(f"config {config_path}: bad cost_cap_usd_per_window {val!r}") from e
            if not cap >= 0:
                raise LedgerError(f"config {config_path}: negative cost cap")
        else:
            if val not in VALID_WINDOWS:
                raise LedgerError(f"config {config_path}: cost_window must be one of {VALID_WINDOWS}")
            window = val
    return CostPolicy(cap, window)


def effective_cap(config_cap: float, *overrides: Optional[float]) -> float:
    """CLI/env may only LOWER the configured cap: min(config, overrides...)."""
    vals = [float(config_cap)] + [float(o) for o in overrides if o is not None]
    return min(vals)


# --------------------------------------------------------------------- ledger


class CostLedger:
    def __init__(
        self,
        directory: Path,
        cap_usd: float,
        window: str = WINDOW_ROLLING_24H,
        clock: Optional[Callable[[], float]] = None,
    ):
        if window not in VALID_WINDOWS:
            raise LedgerError(f"unknown cost window {window!r}")
        self.dir = Path(directory)
        self.cap_usd = float(cap_usd)
        self.window = window
        self._clock = clock or time.time
        self.path = self.dir / "ledger.jsonl"
        self.lock_path = self.dir / "ledger.lock"
        self.genesis_path = self.dir / "ledger.genesis"
        with self._locked():
            pass  # validates / initialises

    # -- fs safety -------------------------------------------------------
    def _init_or_check(self) -> None:
        try:
            if not self.dir.exists() and not self.dir.is_symlink():
                sd.make_private_dir(self.dir)
            sd.check_private(self.dir, is_dir=True)
            sd.check_parents_in_home(self.dir)
            if not self.genesis_path.exists():
                if self.path.exists():
                    raise LedgerError(
                        f"{self.genesis_path} missing but ledger exists: tamper suspected"
                    )
                self._create_private(self.genesis_path, json.dumps({"ts": self._clock()}) + "\n")
                self._create_private(self.path, "")
            if not self.path.exists():
                raise LedgerError(
                    f"{self.path} missing but {self.genesis_path} exists: ledger was "
                    "deleted (tamper). Spend cannot be reset by deleting the ledger; "
                    "restore it or have the user run a deliberate reset."
                )
            sd.check_private(self.path, is_dir=False)
            sd.check_private(self.genesis_path, is_dir=False)
        except sd.UnsafePath as e:
            raise LedgerUnsafe(str(e)) from e
        except OSError as e:
            raise LedgerError(f"ledger dir {self.dir}: {e}") from e

    @staticmethod
    def _create_private(path: Path, text: str) -> None:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

    @contextlib.contextmanager
    def _locked(self):
        self._init_or_check()
        try:
            fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            raise LedgerError(f"ledger lock {self.lock_path}: {e}") from e
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- read ------------------------------------------------------------
    def _entries(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise LedgerError(f"ledger unreadable {self.path}: {e}") from e
        out = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                e["ts"] = float(e["ts"])
                e["usd"] = float(e["usd"])
                if e["kind"] not in ("reserve", "settle", "charge") or e["usd"] < 0:
                    raise ValueError(e["kind"])
                str(e["run_id"])
            except (ValueError, KeyError, TypeError) as exc:
                raise LedgerError(f"ledger corrupt at {self.path}:{n}: {exc}") from exc
            out.append(e)
        return out

    def _window_start(self, now: float) -> float:
        if self.window == WINDOW_ROLLING_24H:
            return now - 24 * 3600
        local = dt.datetime.fromtimestamp(now)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _total(self, entries: list[dict], now: float) -> float:
        if entries and max(e["ts"] for e in entries) > now + CLOCK_SKEW_TOLERANCE_S:
            start = float("-inf")  # clock went backwards: count everything
        else:
            start = self._window_start(now)
        per: dict[str, list[float]] = {}
        for e in entries:
            if e["ts"] < start:
                continue
            r = per.setdefault(str(e["run_id"]), [0.0, 0.0, 0.0])
            r[("reserve", "settle", "charge").index(e["kind"])] += e["usd"]
        return sum(max(r, s) + c for r, s, c in per.values())

    def window_total(self) -> float:
        with self._locked():
            return self._total(self._entries(), self._clock())

    def remaining(self) -> float:
        return max(0.0, self.cap_usd - self.window_total())

    # -- write -----------------------------------------------------------
    def _append(self, run_id: str, kind: str, usd: float) -> None:
        line = json.dumps({"ts": self._clock(), "run_id": str(run_id),
                           "kind": kind, "usd": round(float(usd), 6)})
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, (line + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            dfd = os.open(str(self.dir), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            raise LedgerError(f"ledger write failed {self.path}: {e}") from e

    def reserve(self, run_id: str, usd: float) -> bool:
        """Atomically refuse if window_total + usd > cap, else record it."""
        if usd < 0:
            raise ValueError("negative reservation")
        with self._locked():
            total = self._total(self._entries(), self._clock())
            if total + usd > self.cap_usd + 1e-9:
                return False
            self._append(run_id, "reserve", usd)
            return True

    def settle(self, run_id: str, actual_usd: float) -> None:
        with self._locked():
            self._append(run_id, "settle", max(0.0, actual_usd))

    def charge(self, run_id: str, usd: float) -> None:
        """Record money already spent (never refused: it is gone)."""
        if usd <= 0:
            return
        with self._locked():
            self._append(run_id, "charge", usd)


def open_ledger(cap_usd: float, window: str = WINDOW_ROLLING_24H,
                clock: Optional[Callable[[], float]] = None) -> CostLedger:
    return CostLedger(ledger_dir(), cap_usd=cap_usd, window=window, clock=clock)
