#!/usr/bin/env python3
"""Re-rank `complexity_vectors.db` rows.

Aaron's framing (G1 brief): the drift_ranking_score must reflect *recent*
activity, not the rank you happened to seed the row with. Without
periodic re-ranking, Synaptic over-promotes whichever vector was scored
high months ago and ignores fresh drift.

This script reads the live DB and recomputes:

    drift_ranking_score = risk_score * 10
                          + min(trigger_count, 20) * 2
                          + decay_factor(last_triggered_at)

decay_factor:
    * triggered in last 24h    → +20
    * triggered in last  7d    → +10
    * triggered in last 30d    →  +5
    * triggered in last 90d    →  +2
    * older                    →   0
    * never triggered          → -5  (slowly demote stale rows)

This is intentionally simple — the goal is to make the ranking responsive
to *recency*, not to replace Synaptic's qualitative judgement.

Usage:
    python3 scripts/rerank_complexity_vectors.py
    python3 scripts/rerank_complexity_vectors.py --db ~/.context-dna/complexity_vectors.db --dry-run

The script is safe to run on a cron — it never deletes rows and only
mutates the `drift_ranking_score`, `current_alert_level`, and `updated_at`
columns. Run from launchd / systemd via the templates under daemons/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = Path(
    os.environ.get(
        "AUTOPILOT_COMPLEXITY_DB",
        str(Path.home() / ".context-dna" / "complexity_vectors.db"),
    )
)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _decay_factor(last_iso: str | None) -> float:
    if not last_iso:
        return -5.0
    try:
        # Accept both `2026-05-14T08:00:00+00:00` and naive forms.
        s = last_iso.replace("Z", "+00:00")
        last = dt.datetime.fromisoformat(s)
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return 0.0
    age = _now_utc() - last
    hours = age.total_seconds() / 3600
    if hours <= 24:
        return 20.0
    if hours <= 24 * 7:
        return 10.0
    if hours <= 24 * 30:
        return 5.0
    if hours <= 24 * 90:
        return 2.0
    return 0.0


def _alert_level(new_score: float) -> str:
    if new_score >= 80:
        return "critical"
    if new_score >= 50:
        return "elevated"
    if new_score >= 30:
        return "watch"
    return "none"


def rerank(db_path: Path, *, dry_run: bool = False) -> dict:
    """Return a summary dict; mutate the DB only when dry_run=False."""
    if not db_path.exists():
        return {"db": str(db_path), "error": "missing"}

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT vector_id, risk_score, trigger_count, last_triggered_at, "
        "       drift_ranking_score "
        "FROM complexity_vectors"
    ).fetchall()

    updates = []
    for r in rows:
        old = float(r["drift_ranking_score"] or 0.0)
        new = (
            float(r["risk_score"] or 5.0) * 10
            + min(int(r["trigger_count"] or 0), 20) * 2
            + _decay_factor(r["last_triggered_at"])
        )
        new = max(0.0, round(new, 2))
        updates.append({
            "vector_id": r["vector_id"],
            "old": old,
            "new": new,
            "alert": _alert_level(new),
        })

    if not dry_run:
        now_iso = _now_utc().isoformat()
        for u in updates:
            con.execute(
                "UPDATE complexity_vectors "
                "SET drift_ranking_score = ?, "
                "    current_alert_level = ?, "
                "    updated_at = ? "
                "WHERE vector_id = ?",
                (u["new"], u["alert"], now_iso, u["vector_id"]),
            )
        con.commit()
    con.close()

    return {
        "db": str(db_path),
        "rows_updated": len(updates) if not dry_run else 0,
        "rows_inspected": len(updates),
        "dry_run": dry_run,
        "top5": sorted(updates, key=lambda x: -x["new"])[:5],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Re-rank complexity_vectors.db")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    result = rerank(args.db, dry_run=args.dry_run)
    if "error" in result:
        print(f"[rerank] error: {result['error']} ({result['db']})", file=sys.stderr)
        return 1
    print(
        f"[rerank] db={result['db']} inspected={result['rows_inspected']} "
        f"updated={result['rows_updated']} dry_run={result['dry_run']}"
    )
    for r in result["top5"]:
        print(
            f"  {r['vector_id']:8} old={r['old']:7.2f} new={r['new']:7.2f} "
            f"alert={r['alert']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
