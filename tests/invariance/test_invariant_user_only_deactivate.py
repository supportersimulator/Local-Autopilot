"""Invariants #1 + #2 — USER-ONLY-DEACTIVATE and NO ATLAS PROMOTION.

Matrix: actor × from-mode × to-mode. Plus three explicit bypass attempts:
  - monkey-patch the gating internals
  - direct JSON edit on disk
  - race condition between two actors
"""
from __future__ import annotations

import json
import threading

import pytest


# ── 1) Matrix of allowed / forbidden transitions ────────────────────────

# (actor, from, to, allowed?)
TRANSITION_MATRIX = [
    # USER may do anything
    ("user", "off",          "on_temporary",  True),
    ("user", "off",          "on_permanent",  True),
    ("user", "on_temporary", "on_permanent",  True),
    ("user", "on_temporary", "off",           True),
    ("user", "on_permanent", "off",           True),  # invariant #1: only user
    ("user", "on_permanent", "on_temporary",  True),

    # ATLAS may not clear on_permanent (invariant #1)
    ("atlas", "on_permanent", "off",          False),
    ("atlas", "on_permanent", "on_temporary", False),  # also a downgrade-attack

    # ATLAS may not promote to on_permanent (invariant #2)
    ("atlas", "off",          "on_permanent", False),
    ("atlas", "on_temporary", "on_permanent", False),

    # ATLAS may toggle within bounds (still must carry bound — see test below)
    ("atlas", "off",          "on_temporary", True),
    ("atlas", "on_temporary", "off",          True),

    # SYSTEM is treated like atlas re: permanent — never auto-promote
    ("system", "off",          "on_permanent", False),
    ("system", "on_permanent", "off",          False),
]


@pytest.mark.parametrize("actor,frm,to,allowed", TRANSITION_MATRIX)
@pytest.mark.real_io
def test_transition_matrix(state_module, state_path, actor, frm, to, allowed):
    s = state_module.State(str(state_path))

    # Seed: drive into `frm` using a USER actor (always permitted).
    if frm != "off":
        kw = {}
        if frm == "on_temporary":
            kw["temporary_until"] = "2099-01-01T00:00:00Z"
        s.transition(to=frm, actor="user", reason="seed", **kw)

    kwargs = {}
    if to == "on_temporary":
        kwargs["temporary_until"] = "2099-01-01T00:00:00Z"

    if allowed:
        s.transition(to=to, actor=actor, reason="m", **kwargs)
        assert s.read()["mode"] == to
    else:
        with pytest.raises(state_module.PermissionDenied):
            s.transition(to=to, actor=actor, reason="m", **kwargs)
        # State unchanged
        assert s.read()["mode"] == frm


# ── 2) Bypass #1 — monkey-patch attack ───────────────────────────────────


@pytest.mark.real_io
def test_bypass_monkeypatch_actor_normaliser(state_module, state_path):
    """Even if an attacker rewrites VALID_ACTORS at runtime to add a
    'super-atlas' bypass alias, on_permanent → off must still require
    actor=='user' (literal string check, not allow-list lookup)."""
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")

    # Inject fake actor by widening the allow-list
    try:
        state_module.VALID_ACTORS.add("super-atlas")
        state_module.State.VALID_ACTORS.add("super-atlas")
    except Exception:
        pass

    with pytest.raises(state_module.PermissionDenied):
        s.transition(to="off", actor="super-atlas", reason="bypass attempt")
    assert s.read()["mode"] == "on_permanent"


# ── 3) Bypass #2 — direct JSON tamper ────────────────────────────────────


@pytest.mark.real_io
def test_bypass_direct_json_tamper_detected(state_module, state_path):
    """Tampering the file directly with `mode: off` does NOT count as
    a user-issued deactivate. Read returns the tampered value but the
    transition_history reveals tampering (no matching entry)."""
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")

    raw = json.loads(state_path.read_text())
    raw["mode"] = "off"
    state_path.write_text(json.dumps(raw))

    cur = s.read()
    assert cur["mode"] == "off"
    # The tampering signature: most recent transition entry doesn't end at 'off'
    last = cur["transition_history"][-1]
    assert last["to"] == "on_permanent", (
        "mode==off but no transition entry recorded → tamper detected"
    )


# ── 4) Bypass #3 — race condition (user vs atlas) ────────────────────────


@pytest.mark.real_io
def test_race_user_off_vs_atlas_off(state_module, state_path):
    """Two threads attempt off transitions from on_permanent simultaneously:
    user is allowed, atlas is not. Final state must be 'off' (user wins)
    and atlas's attempt must raise."""
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")

    results = {"user": None, "atlas": None}
    barrier = threading.Barrier(2)

    def user_thread():
        barrier.wait()
        try:
            s.transition(to="off", actor="user", reason="user-deactivate")
            results["user"] = "ok"
        except Exception as e:
            results["user"] = type(e).__name__

    def atlas_thread():
        barrier.wait()
        try:
            s.transition(to="off", actor="atlas", reason="atlas-attempt")
            results["atlas"] = "ok"
        except state_module.PermissionDenied:
            results["atlas"] = "denied"
        except Exception as e:
            results["atlas"] = type(e).__name__

    t1 = threading.Thread(target=user_thread)
    t2 = threading.Thread(target=atlas_thread)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # At least one ordering is valid:
    #   user-first: user='ok', atlas='denied' (came from on_permanent → off)
    #              OR atlas='denied' (now from 'off' → 'off' which still is OK
    #              but doesn't unlock on_permanent path)
    #   atlas-first: atlas='denied' (still on_permanent), user='ok'
    assert results["user"] == "ok"
    assert results["atlas"] == "denied"
    assert s.read()["mode"] == "off"


# ── 5) Reading on_permanent does NOT auto-clear ──────────────────────────


@pytest.mark.real_io
def test_on_permanent_survives_many_reads(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")
    for _ in range(50):
        assert s.read()["mode"] == "on_permanent"


# ── 6) atlas promotion blocked even from on_temporary with a valid bound ─


@pytest.mark.real_io
def test_atlas_cannot_promote_even_with_bound(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(
        to="on_temporary",
        actor="user",
        reason="seed",
        temporary_until="2099-01-01T00:00:00Z",
    )
    with pytest.raises(state_module.PermissionDenied):
        s.transition(
            to="on_permanent",
            actor="atlas",
            reason="please please please",
            temporary_until=None,
        )
    assert s.read()["mode"] == "on_temporary"


# ── 7) PermissionDenied error message identifies invariant ───────────────


@pytest.mark.real_io
def test_permission_denied_message_traceability(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")
    with pytest.raises(state_module.PermissionDenied) as exc:
        s.transition(to="off", actor="atlas", reason="x")
    msg = str(exc.value).upper()
    assert "USER" in msg or "PERMANENT" in msg, (
        "denial message must trace back to the invariant being enforced"
    )


# ── 8) Audit log shows the denied attempt? (silent failures forbidden) ───
# Note: implementations MAY skip writing denied attempts; what they MUST do
# is leave state unchanged AND surface the denial via exception. We test
# the exception path here; observability tests cover successful trail.


@pytest.mark.real_io
def test_denied_transition_leaves_history_untouched(state_module, state_path):
    s = state_module.State(str(state_path))
    s.transition(to="on_permanent", actor="user", reason="seed")
    before = len(s.read()["transition_history"])
    for _ in range(5):
        with pytest.raises(state_module.PermissionDenied):
            s.transition(to="off", actor="atlas", reason="x")
    after = len(s.read()["transition_history"])
    assert before == after, "denied transitions must not append to history"
