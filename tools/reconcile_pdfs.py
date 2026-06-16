#!/usr/bin/env python3
"""Reconcile orphan PDFs in osti_corpus/pdfs/ that have no file_instances row.

Walks pdfs/<year>/<id>.pdf, checks each against file_instances + papers, and
inserts missing rows (computes size+sha256). Idempotent.
"""
import sqlite3, hashlib, sys
from pathlib import Path
from datetime import datetime, timezone

DB = "/Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite"
PDFS = Path("/Volumes/Cherry6TB/osti_corpus/pdfs")

def sha256_of(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def main():
    conn = sqlite3.connect(DB, timeout=600)
    cur = conn.cursor()
    added = updated = skipped = 0
    for year_dir in sorted(PDFS.iterdir()):
        if not year_dir.is_dir():
            continue
        for pdf in sorted(year_dir.glob("*.pdf")):
            osti_id = pdf.stem
            # already in file_instances?
            row = cur.execute("SELECT instance_id FROM file_instances WHERE path=?", (str(pdf),)).fetchone()
            if row:
                skipped += 1
                continue
            size = pdf.stat().st_size
            sha = sha256_of(pdf)
            ts = datetime.now(timezone.utc).isoformat()
            cur.execute("""
                INSERT OR IGNORE INTO file_instances
                  (osti_id, source, path, size, sha256, is_canonical, first_seen_ts, last_verified_ts)
                VALUES (?, 'backfill_purl', ?, ?, ?, 1, ?, ?)
            """, (osti_id, str(pdf), size, sha, ts, ts))
            cur.execute("""
                UPDATE papers SET has_pdf=1, canonical_pdf_path=?, canonical_source='backfill_purl',
                                  canonical_size=?, canonical_sha256=?
                WHERE osti_id=?
            """, (str(pdf), size, sha, osti_id))
            added += 1
            if added % 25 == 0:
                conn.commit()
                print(f"  reconciled {added} ({pdf.name})")
    conn.commit()
    conn.close()
    print(f"Done. added={added} skipped={skipped}")

if __name__ == "__main__":
    main()
