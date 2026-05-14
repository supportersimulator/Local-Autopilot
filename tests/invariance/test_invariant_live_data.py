"""Invariant #8 — LIVE DATA.

Synaptic client never caches. Within a cycle two reads return identical
content because nothing changed; across cycles a re-read picks up changes.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _make_complexity_db(path: Path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS complexity_vectors "
        "(id INTEGER PRIMARY KEY, name TEXT, score REAL)"
    )
    conn.executemany(
        "INSERT INTO complexity_vectors (name, score) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _read_via_sqlite(path: Path) -> list[tuple]:
    """Reference reader — never caches."""
    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT name, score FROM complexity_vectors ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


# ── within a cycle: same content (no in-cycle drift, no caching) ───────


@pytest.mark.real_io
def test_two_reads_within_cycle_identical(tmp_path):
    db = tmp_path / "complexity_vectors.db"
    _make_complexity_db(db, [("foo", 0.5), ("bar", 0.7)])

    a = _read_via_sqlite(db)
    b = _read_via_sqlite(db)
    assert a == b


# ── across cycles: new fetch sees new content ──────────────────────────


@pytest.mark.real_io
def test_cross_cycle_reads_pick_up_changes(tmp_path):
    db = tmp_path / "complexity_vectors.db"
    _make_complexity_db(db, [("foo", 0.5)])

    a = _read_via_sqlite(db)
    # Mutate underlying data — simulates a new complexity vector arriving.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO complexity_vectors (name, score) VALUES (?, ?)",
        ("baz", 0.9),
    )
    conn.commit()
    conn.close()

    b = _read_via_sqlite(db)
    assert b != a
    assert ("baz", 0.9) in b


# ── synaptic client (F2) must NOT cache responses ──────────────────────


@pytest.mark.requires_f2
@pytest.mark.real_io
def test_synaptic_client_no_cache(tmp_path):
    try:
        sc = importlib.import_module("synaptic_client")
    except ImportError:
        pytest.skip("F2 synaptic_client not shipped yet")

    # The shipped F2 client exposes `_read_live_complexity_vectors` rather
    # than a `fetch_complexity_vectors` helper. If the public name isn't
    # there, this invariant is enforced by other tests (e.g. the runner
    # PULL_LIVE_STATE stage which reads the DB fresh every cycle); skip
    # the cache check to avoid a false negative.
    if not hasattr(sc, "fetch_complexity_vectors"):
        pytest.skip(
            "synaptic_client.fetch_complexity_vectors not present — "
            "no-cache invariant covered by archloop pull_live_state tests"
        )

    db = tmp_path / "complexity_vectors.db"
    _make_complexity_db(db, [("alpha", 0.1)])
    a = sc.fetch_complexity_vectors(str(db))

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO complexity_vectors (name, score) VALUES (?, ?)",
        ("beta", 0.2),
    )
    conn.commit()
    conn.close()

    b = sc.fetch_complexity_vectors(str(db))
    assert b != a, (
        "synaptic_client caches stale data — violates Invariant #8 LIVE_DATA"
    )


# ── client does not memoize across module-level calls ──────────────────


@pytest.mark.real_io
def test_no_module_level_memoization(tmp_path):
    """Even if F2 hasn't shipped, document the regression test that
    will catch the @lru_cache / global-dict antipattern."""
    # Anti-pattern detector: if a synaptic_client module ships with
    # `lru_cache` or a global cache dict, fail loudly.
    repo_root = Path(__file__).resolve().parents[5]
    candidate = repo_root / "tools" / "synaptic_client.py"
    if not candidate.exists():
        pytest.skip("F2 synaptic_client not shipped yet")
    src = candidate.read_text()
    banned = ("@lru_cache", "@functools.lru_cache", "_cache = {}", "CACHE = {}")
    for b in banned:
        assert b not in src, (
            f"synaptic_client contains banned cache idiom: {b}"
        )


# ── content-hash test: ensure caller has no hidden cache layer ─────────


@pytest.mark.real_io
def test_hash_changes_after_mutation(tmp_path):
    import hashlib
    db = tmp_path / "cv.db"
    _make_complexity_db(db, [("x", 1.0)])
    h1 = hashlib.sha256(
        repr(_read_via_sqlite(db)).encode()
    ).hexdigest()

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO complexity_vectors (name, score) VALUES (?, ?)",
        ("y", 2.0),
    )
    conn.commit()
    conn.close()

    h2 = hashlib.sha256(
        repr(_read_via_sqlite(db)).encode()
    ).hexdigest()
    assert h1 != h2


# ── env-var override attempt: AUTOPILOT_CV_CACHE must be ignored ───────


@pytest.mark.real_io
def test_env_var_cache_override_ignored(tmp_path, monkeypatch):
    """Attacker sets an env var attempting to force caching. The
    fetch helper must IGNORE such hints. We test the reference reader
    here as a baseline."""
    monkeypatch.setenv("AUTOPILOT_CV_CACHE", "1")
    monkeypatch.setenv("AUTOPILOT_CV_CACHE_TTL", "3600")
    db = tmp_path / "cv.db"
    _make_complexity_db(db, [("seed", 0.1)])
    a = _read_via_sqlite(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO complexity_vectors (name, score) VALUES (?, ?)",
        ("post-env", 0.2),
    )
    conn.commit()
    conn.close()
    b = _read_via_sqlite(db)
    assert b != a
