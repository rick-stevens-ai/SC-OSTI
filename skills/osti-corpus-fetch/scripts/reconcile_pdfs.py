#!/usr/bin/env python3
"""Reconcile orphan PDFs under <pdf_root>/<year>/<id>.pdf that lack a
file_instances row.

Usage scenario: bulk_fetch_purl.py (or any fetcher) wrote PDFs to disk but
the DB INSERTs failed mid-run (schema mismatch, lock contention, crash).
The fetcher's target_path.exists() check will skip these on resume, leaving
papers.has_pdf=0 and file_instances empty. This script walks the actual
PDF directory tree, computes size+sha256, and inserts the missing catalog
rows. Idempotent — re-running adds nothing if everything already matches.

Args:
  --db        : catalog.sqlite path
  --pdf-root  : directory tree of <year>/<id>.pdf files
  --source    : value for file_instances.source (default 'backfill_purl')

Pattern proved out 2026-06-13 on 16 orphan PDFs from the smoke run.
"""
import sqlite3, hashlib, argparse, sys
from pathlib import Path
from datetime import datetime, timezone


def sha256_of(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--pdf-root", required=True)
    ap.add_argument("--source", default="backfill_purl")
    args = ap.parse_args()

    pdf_root = Path(args.pdf_root)
    if not pdf_root.is_dir():
        print(f"FATAL: {pdf_root} not a directory", file=sys.stderr); sys.exit(2)

    conn = sqlite3.connect(args.db, timeout=600)
    cur = conn.cursor()

    # Confirm tables exist with expected columns
    fi_cols = {r[0] for r in cur.execute("SELECT name FROM pragma_table_info('file_instances')")}
    p_cols = {r[0] for r in cur.execute("SELECT name FROM pragma_table_info('papers')")}
    for col in ("osti_id", "source", "path", "size", "sha256", "is_canonical",
                "first_seen_ts", "last_verified_ts"):
        if col not in fi_cols:
            print(f"FATAL: file_instances missing column '{col}'", file=sys.stderr); sys.exit(2)
    for col in ("osti_id", "has_pdf", "canonical_pdf_path", "canonical_source",
                "canonical_size", "canonical_sha256"):
        if col not in p_cols:
            print(f"FATAL: papers missing column '{col}'", file=sys.stderr); sys.exit(2)

    added = skipped = 0
    for year_dir in sorted(pdf_root.iterdir()):
        if not year_dir.is_dir():
            continue
        for pdf in sorted(year_dir.glob("*.pdf")):
            osti_id = pdf.stem
            row = cur.execute("SELECT instance_id FROM file_instances WHERE path=?",
                              (str(pdf),)).fetchone()
            if row:
                skipped += 1
                continue
            size = pdf.stat().st_size
            sha = sha256_of(pdf)
            ts = datetime.now(timezone.utc).isoformat()
            cur.execute("""
                INSERT OR IGNORE INTO file_instances
                  (osti_id, source, path, size, sha256, is_canonical, first_seen_ts, last_verified_ts)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (osti_id, args.source, str(pdf), size, sha, ts, ts))
            cur.execute("""
                UPDATE papers SET has_pdf=1, canonical_pdf_path=?, canonical_source=?,
                                  canonical_size=?, canonical_sha256=?
                WHERE osti_id=?
            """, (str(pdf), args.source, size, sha, osti_id))
            added += 1
            if added % 25 == 0:
                conn.commit()
                print(f"  reconciled {added} ({pdf.name})")
    conn.commit()
    conn.close()
    print(f"Done. added={added} skipped={skipped}")


if __name__ == "__main__":
    main()
