"""Invariant #3 — on_temporary is always bounded."""
from __future__ import annotations

import datetime as _dt

import pytest


def _future(seconds: int = 3600) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat() + "Z"


def _past(seconds: int = 60) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat() + "Z"


# ── on_temporary requires a termination condition ───────────────────────


@pytest.mark.real_io
def test_on_temporary_requires_until_or_atlas_decision(state_module, state_path):
    s = state_module.State(str(state_path))
    with pytest.raises(state_module.PermissionDenied):
        s.transition(to="on_temporary", actor="user", reason="just because")
    assert s.read()["mode"] == "off"


@pytest.mark.real_io
def test_on_temporary_accepts_until(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="bounded",
        temporary_until=_future(),
    )
    assert s.read()["mode"] == "on_temporary"
    assert s.read()["temporary_until"] is not None


@pytest.mark.real_io
def test_on_temporary_accepts_atlas_decision_reason(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="atlas-decision: cardio-gate green",
    )
    assert s.read()["mode"] == "on_temporary"


@pytest.mark.real_io
def test_blank_reason_blocked(state_module, state_path):
    s = state_module.State(str(state_path))
    with pytest.raises((state_module.PermissionDenied, ValueError)):
        s.transition(to="on_temporary", actor="user", reason="")


# ── auto-expiry — runner respects it ────────────────────────────────────


@pytest.mark.real_io
def test_temporary_until_in_past_is_expired_at_read(state_module, state_path):
    """When `temporary_until` is in the past, callers MUST treat the
    autopilot as expired. The state machine itself may keep the
    'on_temporary' literal so the audit trail stays honest, but any
    consumer can compute is_expired() trivially."""
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="will-expire",
        temporary_until=_past(),
    )
    cur = s.read()
    until = cur["temporary_until"]
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z"
    assert until < now, "stored until must be readable + comparable"


@pytest.mark.real_io
def test_runner_does_not_run_when_expired(state_module, state_path, tmp_path):
    """Pure unit-level shim: any runner implementation MUST gate on
    `now() < temporary_until` for on_temporary. We assert the contract
    on the state object — the actual runner test lives behind
    @requires_f2 (covered in test_invariant_resource_caps)."""
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="expired-on-arrival",
        temporary_until=_past(),
    )
    cur = s.read()
    # Helper consumers use: expired = until_iso < now_iso
    until_iso = cur["temporary_until"]
    now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z"
    assert until_iso < now_iso


# ── atlas can extend but cannot remove the bound ─────────────────────────


@pytest.mark.real_io
def test_atlas_can_extend_temporary(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="initial",
        temporary_until=_future(60),
    )
    s.transition(
        to="on_temporary",
        actor="atlas",
        reason="extend",
        temporary_until=_future(3600),
    )
    cur = s.read()
    assert cur["mode"] == "on_temporary"
    assert cur["temporary_until"] >= _future(3500)  # still bounded


@pytest.mark.real_io
def test_atlas_cannot_remove_bound_during_extend(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="initial",
        temporary_until=_future(60),
    )
    with pytest.raises(state_module.PermissionDenied):
        s.transition(
            to="on_temporary",
            actor="atlas",
            reason="strip",
            temporary_until=None,
        )
    # Original bound preserved
    cur = s.read()
    assert cur["temporary_until"] is not None
