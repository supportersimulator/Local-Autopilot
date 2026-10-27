"""Item 1 — persistent rolling cost ledger (FIX-DESIGNS.md §1).

NEG tests fail on 44f7fc9: the runner resets cost_so_far=0.0 every
invocation, CLI can raise the cap, unknown cost is booked as $0.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest

from tests._fix_helpers import REPO_ROOT, isolate_runner, need, opt, teardown_overrides

import archloop_runner  # noqa: E402

cost_ledger = opt("cost_ledger")


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = isolate_runner(tmp_path, monkeypatch)
    yield paths
    teardown_overrides()


def _fake_cycle(charge_usd, calls):
    def fake(cycle_idx, **kw):
        calls.append(kw.get("cost_so_far"))
        d = Path(os.environ["AUTOPILOT_LOG_ROOT"]) / f"c{len(calls)}"
        d.mkdir(parents=True, exist_ok=True)
        r = archloop_runner.CycleResult(cycle=cycle_idx, cycle_dir=d)
        r.cost_usd = charge_usd
        r.satisfied = True
        return r
    return fake


# ---------------------------------------------------------------- NEG


def test_cap_persists_across_invocations(env, monkeypatch):
    """Two separate 30-min ticks share one budget: tick 2 must refuse."""
    calls: list = []
    monkeypatch.setattr(archloop_runner, "run_cycle", _fake_cycle(0.60, calls))
    rc1 = archloop_runner.main(["--cycles", "1", "--cost-cap-usd", "0.50"])
    rc2 = archloop_runner.main(["--cycles", "1", "--cost-cap-usd", "0.50"])
    assert len(calls) == 1, f"second invocation ran a cycle (calls={calls})"
    assert rc2 == 6, f"expected exit 6 (cost_cap) on 2nd tick, got rc1={rc1} rc2={rc2}"


def test_cli_cannot_raise_cap(env, monkeypatch, tmp_path):
    """config says $5/window; --cost-cap-usd 50 must not raise it."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("cost_cap_usd_per_window: 5.0\ncost_window: rolling_24h\n")
    monkeypatch.setattr(archloop_runner, "CONFIG_PATH", cfg, raising=False)
    if cost_ledger is not None:
        led = cost_ledger.open_ledger(cap_usd=100.0)
        led.charge("prior-tick", 5.0)
    calls: list = []
    monkeypatch.setattr(archloop_runner, "run_cycle", _fake_cycle(1.0, calls))
    rc = archloop_runner.main(["--cycles", "1", "--cost-cap-usd", "50"])
    assert calls == [], "cycle ran although $5 window cap was already spent"
    assert rc == 6
    need(cost_ledger, "cost_ledger")
    assert cost_ledger.effective_cap(5.0, 50.0, None) == 5.0
    assert cost_ledger.effective_cap(5.0, 1.0, None) == 1.0


def test_unparseable_cost_line_fails_closed():
    v = archloop_runner.parse_3s_verdict("HOLD\ncost_usd: N/A\n")
    assert v["cost_usd"] is None, f"unparseable cost booked as {v['cost_usd']!r}"
    assert v.get("cost_parse_error") is True


def test_cross_exam_timeout_is_not_free(tmp_path, monkeypatch):
    cli = tmp_path / "3s"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o700)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="3s", timeout=1)

    monkeypatch.setattr(archloop_runner.subprocess, "run", boom)
    v = archloop_runner.run_cross_exam("agg", cli_path=str(cli), timeout_s=1)
    assert v["cost_usd"] is None, "timed-out cross-exam booked as $0"

    acct = getattr(archloop_runner, "_account_cross_exam_cost", None)
    assert acct is not None, "_account_cross_exam_cost missing (fix not applied)"
    counters: dict = {}
    r = archloop_runner.CycleResult(cycle=1, cycle_dir=tmp_path)
    charged = acct(r, v, counters)
    assert charged == archloop_runner.COST_UNPARSED_PENALTY_USD > 0
    assert r.aborted == "cost_unknown"
    assert counters.get("cost_unparsed") == 1


def test_templates_have_no_cost_flag():
    for name in ("autopilot-tick.plist.template", "autopilot-tick.service.template"):
        text = (REPO_ROOT / "daemons" / name).read_text()
        assert "--cost-cap-usd" not in text, f"{name} still passes --cost-cap-usd"


def test_runner_refuses_when_ledger_unsafe(env, monkeypatch):
    need(cost_ledger, "cost_ledger")
    cost_ledger.open_ledger(cap_usd=10.0)  # creates dir
    os.chmod(env["ledger"], 0o755)
    calls: list = []
    monkeypatch.setattr(archloop_runner, "run_cycle", _fake_cycle(0.0, calls))
    rc = archloop_runner.main(["--cycles", "1"])
    assert rc == 6 and calls == []
    c = json.loads(env["counters"].read_text())
    assert c.get("cost_ledger_unreadable", 0) >= 1


# ---------------------------------------------------------------- POS (module)


class _Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _ledger(tmp_path, cap=5.0, clock=None):
    need(cost_ledger, "cost_ledger")
    return cost_ledger.CostLedger(tmp_path / "L", cap_usd=cap,
                                  clock=clock or _Clock())


def test_rolling_window_expires(tmp_path):
    clk = _Clock()
    led = _ledger(tmp_path, cap=5.0, clock=clk)
    led.charge("a", 5.0)
    assert led.reserve("b", 0.01) is False
    clk.t += 25 * 3600
    assert led.window_total() == 0.0
    assert led.reserve("b", 1.0) is True


def test_crash_after_reserve_counts(tmp_path):
    led = _ledger(tmp_path)
    assert led.reserve("r1", 2.0)
    # "crash": new instance, never settled
    led2 = _ledger(tmp_path)
    assert led2.window_total() == pytest.approx(2.0)
    led2.settle("r1", 0.5)  # settle lower than reserve still counts reserve
    assert led2.window_total() == pytest.approx(2.0)
    assert led2.reserve("r2", 1.0)
    led2.settle("r2", 1.5)  # actual above reservation counts actual
    assert led2.window_total() == pytest.approx(3.5)


def _reserve_worker(args):
    d, i = args
    import sys
    from tests._fix_helpers import TOOLS  # noqa: F401
    import cost_ledger as cl
    return cl.CostLedger(Path(d), cap_usd=5.0).reserve(f"w{i}", 1.0)


def test_concurrent_reserve_never_exceeds(tmp_path):
    need(cost_ledger, "cost_ledger")
    d = tmp_path / "L"
    cost_ledger.CostLedger(d, cap_usd=5.0)  # init
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(8) as pool:
        results = pool.map(_reserve_worker, [(str(d), i) for i in range(8)])
    assert sum(results) == 5
    assert cost_ledger.CostLedger(d, cap_usd=5.0).window_total() == pytest.approx(5.0)


def test_bad_perms_refuse(tmp_path):
    led = _ledger(tmp_path)
    os.chmod(led.path, 0o644)
    with pytest.raises(cost_ledger.LedgerUnsafe):
        _ledger(tmp_path).window_total()


def test_deleted_ledger_is_tamper_not_reset(tmp_path):
    led = _ledger(tmp_path)
    led.charge("x", 4.0)
    led.path.unlink()
    with pytest.raises(cost_ledger.LedgerError):
        _ledger(tmp_path).window_total()


def test_corrupt_line_fails_closed(tmp_path):
    led = _ledger(tmp_path)
    with open(led.path, "a") as f:
        f.write("{not json\n")
    with pytest.raises(cost_ledger.LedgerError):
        led.window_total()


def test_clock_backwards_conservative(tmp_path):
    clk = _Clock()
    led = _ledger(tmp_path, clock=clk)
    led.charge("old", 3.0)
    clk.t -= 3600  # clock went back an hour: newest entry is in the future
    assert led.window_total() == pytest.approx(3.0)
    clk.t = 1_800_000_000.0 - 48 * 3600  # far back: still conservative
    assert led.window_total() == pytest.approx(3.0)


def test_ledger_never_in_tmp():
    need(cost_ledger, "cost_ledger")
    d = str(cost_ledger.default_ledger_dir())
    assert not d.startswith("/tmp") and not d.startswith("/private/tmp")
