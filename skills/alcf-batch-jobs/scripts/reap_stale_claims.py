#!/usr/bin/env python3
"""Reap stale 'claimed' (Nougat) or 'running' (Marker) rows in OCR manifests.

Resets rows that have been in-flight for longer than --stale-minutes back to
pending state, clearing worker assignment so the next job can re-claim them.

The classic trigger is PBS walltime SIGKILL — when `=>> PBS: job killed:
walltime N exceeded limit M` fires, mid-claim rows are abandoned with no
error logged and no partial output written. The row sits in `claimed` or
`running` state forever unless reaped. This script is the reaper.

Idempotent. Safe to run multiple times. Use --dry-run first.

Usage examples:
    # Show what would be reaped (nougat)
    python reap_stale_claims.py --db smoke_nougat.sqlite --type nougat \\
        --stale-minutes 15 --dry-run

    # Actually reap (after walltime-killed marker job)
    python reap_stale_claims.py --db prod_marker.sqlite --type marker \\
        --stale-minutes 15

    # Periodic sweeper cron (every 10min during long runs)
    */10 * * * * /eagle/.../reap_stale_claims.py --db /eagle/.../prod.sqlite \\
        --type nougat --stale-minutes 20

Verified working 2026-06-16 against a walltime-killed Polaris Nougat smoke
(reaped 4 rows that had been 'claimed' for 38 hours).
"""
import sqlite3
import argparse
import time
import datetime
import sys


def reap_nougat(db_path, stale_minutes, dry_run):
    """Reset 'claimed' jobs older than stale_minutes back to pending."""
    cutoff = time.time() - stale_minutes * 60
    c = sqlite3.connect(db_path)
    cur = c.cursor()
    cur.execute("""
        SELECT id, pdf_path, nougat_worker, nougat_claimed_at
        FROM jobs
        WHERE nougat_status='claimed'
          AND nougat_claimed_at IS NOT NULL
          AND nougat_claimed_at < ?
        ORDER BY id
    """, (cutoff,))
    rows = cur.fetchall()
    print("Nougat stale-claimed rows ({}+ min): {}".format(stale_minutes, len(rows)))
    for r in rows:
        age_min = (time.time() - r[3]) / 60
        print("  id={} worker={} age_min={:.1f} pdf={}".format(
            r[0], r[2], age_min, r[1]))
    if rows and not dry_run:
        # Reset state + re-mark needs_nougat so they get re-picked
        cur.executemany("""
            UPDATE jobs
            SET nougat_status=NULL, nougat_worker=NULL, nougat_claimed_at=NULL,
                nougat_error='reaped_walltime_kill', needs_nougat=1
            WHERE id=?
        """, [(r[0],) for r in rows])
        c.commit()
        print("Reset {} rows to pending state.".format(len(rows)))
    elif rows:
        print("(dry-run; no changes written)")
    c.close()
    return len(rows)


def reap_marker(db_path, stale_minutes, dry_run):
    """Reset 'running' manifest rows older than stale_minutes back to pending."""
    cutoff_epoch = time.time() - stale_minutes * 60
    c = sqlite3.connect(db_path)
    cur = c.cursor()
    cur.execute("""
        SELECT id, pdf, worker, started_at
        FROM manifest
        WHERE status='running' AND started_at IS NOT NULL
    """)
    all_rows = cur.fetchall()
    rows = []
    for r in all_rows:
        try:
            # started_at is ISO timestamp string
            ts = datetime.datetime.fromisoformat(
                r[3].replace("Z", "+00:00")
            ).timestamp()
            if ts < cutoff_epoch:
                rows.append(r + (ts,))
        except (ValueError, AttributeError):
            # Non-parseable timestamp = treat as stale
            rows.append(r + (0.0,))
    print("Marker stale-running rows ({}+ min): {}".format(stale_minutes, len(rows)))
    for r in rows:
        age_min = (time.time() - r[4]) / 60 if r[4] else None
        print("  id={} worker={} age_min={} pdf={}".format(r[0], r[2], age_min, r[1]))
    if rows and not dry_run:
        cur.executemany("""
            UPDATE manifest
            SET status='pending', worker=NULL, started_at=NULL,
                error='reaped_walltime_kill'
            WHERE id=?
        """, [(r[0],) for r in rows])
        c.commit()
        print("Reset {} rows to pending.".format(len(rows)))
    elif rows:
        print("(dry-run; no changes written)")
    c.close()
    return len(rows)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="Path to manifest sqlite")
    p.add_argument("--type", required=True, choices=["marker", "nougat"],
                   help="Which manifest schema to operate on")
    p.add_argument("--stale-minutes", type=int, default=15,
                   help="Min age (minutes) to consider claim stale (default: 15)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be reaped without modifying DB")
    args = p.parse_args()
    if args.type == "nougat":
        n = reap_nougat(args.db, args.stale_minutes, args.dry_run)
    else:
        n = reap_marker(args.db, args.stale_minutes, args.dry_run)
    sys.exit(0 if n >= 0 else 1)
