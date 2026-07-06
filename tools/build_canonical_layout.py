#!/usr/bin/env python3
"""
Build canonical pdfs/<year>/<id>.pdf layout via hardlinks.

For each is_canonical=1 file_instance whose paper has a known year:
  target = /Volumes/SG-1-8TB/osti/pdfs/<year>/<osti_id>.pdf
  if target doesn't exist: os.link(source_path, target)
  if target exists and is hardlink-identical to source: skip (idempotent)
  if target exists but different inode: log conflict, leave alone

Idempotent. Records per-paper outcome in build_canonical_log table.

Year source: papers.publication_date (extract YYYY). For orphans / NULL dates,
falls back to file_instances.year if column exists, else skips and logs.

Usage:
  build_canonical_layout.py [--limit N] [--run-tag TAG] [--dry-run]
"""
import argparse, os, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path

CATALOG = "/Volumes/SG-1-8TB/osti/catalog/catalog.sqlite"
PDFS_ROOT = Path("/Volumes/SG-1-8TB/osti/pdfs")

def init_log_table(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS build_canonical_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_tag TEXT,
        ts TEXT,
        osti_id TEXT,
        source_path TEXT,
        target_path TEXT,
        outcome TEXT,   -- linked / already_linked / conflict_diff_inode / src_missing / no_year / skipped_in_layout
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_bcl_runtag ON build_canonical_log(run_tag);
    CREATE INDEX IF NOT EXISTS ix_bcl_outcome ON build_canonical_log(outcome);
    """)
    con.commit()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--run-tag", type=str, default=f"build_canonical_{datetime.now().strftime('%Y%m%d_%H%M')}")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(CATALOG, timeout=600)
    cur = con.cursor()
    init_log_table(con)

    # Pull all canonical files with year, joined to paper
    q = """
    SELECT fi.instance_id, fi.osti_id, fi.path, fi.source,
           CASE
             WHEN p.publication_date IS NOT NULL AND substr(p.publication_date,1,4) GLOB '[12][0-9][0-9][0-9]'
                THEN substr(p.publication_date,1,4)
             ELSE NULL
           END AS year
    FROM file_instances fi
    LEFT JOIN papers p ON p.osti_id = fi.osti_id
    WHERE fi.is_canonical = 1
    ORDER BY year IS NULL, year, fi.osti_id
    """
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = list(cur.execute(q))
    total = len(rows)
    print(f"Canonical files to process: {total}")
    if args.dry_run:
        print(f"DRY RUN — would create hardlinks under {PDFS_ROOT}/")
        sample = rows[:5] + rows[-5:] if total > 10 else rows
        for r in sample:
            print(f"  {r[1]:>10s}  year={r[4]}  src={r[2][-50:]}")
        return

    ts0 = time.time()
    counts = {"linked":0, "already_linked":0, "conflict_diff_inode":0,
              "src_missing":0, "no_year":0, "skipped_in_layout":0, "error":0}
    log_batch = []
    BATCH = 500

    for idx, (instance_id, osti_id, src_path, source, year) in enumerate(rows, 1):
        ts = datetime.now(timezone.utc).isoformat()
        outcome = None
        note = None
        target_path = None

        if year is None:
            outcome = "no_year"
            note = "publication_date missing or not YYYY-prefixed"
        else:
            year_dir = PDFS_ROOT / year
            target_path = str(year_dir / f"{osti_id}.pdf")

            # If source IS already under pdfs/<year>/, skip
            if src_path == target_path:
                outcome = "skipped_in_layout"
                note = "source already at canonical path"
            elif not os.path.exists(src_path):
                outcome = "src_missing"
                note = f"source file does not exist on disk"
            else:
                try:
                    year_dir.mkdir(parents=True, exist_ok=True)
                    if os.path.exists(target_path):
                        # Already a target — check inode
                        if os.stat(target_path).st_ino == os.stat(src_path).st_ino:
                            outcome = "already_linked"
                        else:
                            outcome = "conflict_diff_inode"
                            note = f"target exists with different inode (src_ino={os.stat(src_path).st_ino}, tgt_ino={os.stat(target_path).st_ino})"
                    else:
                        os.link(src_path, target_path)
                        outcome = "linked"
                except OSError as e:
                    outcome = "error"
                    note = f"{type(e).__name__}: {e}"

        counts[outcome] = counts.get(outcome, 0) + 1
        log_batch.append((args.run_tag, ts, osti_id, src_path, target_path, outcome, note))

        if len(log_batch) >= BATCH:
            cur.executemany("""
                INSERT INTO build_canonical_log(run_tag, ts, osti_id, source_path, target_path, outcome, note)
                VALUES (?,?,?,?,?,?,?)
            """, log_batch)
            con.commit()
            log_batch.clear()

        if idx % 1000 == 0 or idx == total:
            dt = time.time() - ts0
            rate = idx / dt if dt > 0 else 0
            eta_min = (total - idx) / rate / 60 if rate > 0 else 0
            cstr = " ".join(f"{k}={v}" for k,v in counts.items() if v)
            print(f"  {idx:>6d}/{total} rate={rate:.0f}/s eta={eta_min:.1f}min  {cstr}", flush=True)

    if log_batch:
        cur.executemany("""
            INSERT INTO build_canonical_log(run_tag, ts, osti_id, source_path, target_path, outcome, note)
            VALUES (?,?,?,?,?,?,?)
        """, log_batch)
        con.commit()

    dt_min = (time.time() - ts0) / 60
    print(f"\n=== Done in {dt_min:.1f}min ===")
    for k,v in sorted(counts.items(), key=lambda x:-x[1]):
        if v: print(f"  {k:25s} {v:>7d}")

if __name__ == "__main__":
    main()
