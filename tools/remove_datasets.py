#!/usr/bin/env python3
"""Remove product_type='Dataset' rows from papers, with audit trail.

Logs one decisions row PER deleted osti_id (decision_type='excluded_non_paper').
Also logs a refresh_runs row marking the cleanup pass.
"""
import sqlite3, json, sys
from datetime import datetime, timezone

DB = "/Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite"
RUN_TAG = "exclude_datasets_v1"

def main():
    conn = sqlite3.connect(DB, timeout=600)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    # Count first
    n = cur.execute("SELECT COUNT(*) FROM papers WHERE product_type='Dataset'").fetchone()[0]
    print(f"Found {n:,} Dataset rows")
    if n == 0:
        return

    # Check overlap with file_instances (rows that have PDFs)
    on_disk = cur.execute("""
        SELECT COUNT(DISTINCT fi.osti_id) FROM file_instances fi
        INNER JOIN papers p ON fi.osti_id=p.osti_id
        WHERE p.product_type='Dataset'
    """).fetchone()[0]
    print(f"  ...of which {on_disk} have a PDF on disk (file_instances row will be kept)")

    # Start refresh_runs entry
    started = datetime.now(timezone.utc).isoformat()
    cur.execute("""
        INSERT INTO refresh_runs (run_type, started_ts, params_json,
                                  records_added, records_updated, errors)
        VALUES ('exclusion_pass', ?, ?, 0, 0, 0)
    """, (started, json.dumps({"filter": "product_type='Dataset'", "run_tag": RUN_TAG})))
    run_id = cur.lastrowid
    print(f"  refresh_runs run_id={run_id}")

    # Log one decision per osti_id
    ts = datetime.now(timezone.utc).isoformat()
    print("Logging decisions...")
    cur.execute("""
        INSERT INTO decisions (osti_id, decision_type, method, confidence, rationale, inputs_json, ts)
        SELECT osti_id, 'excluded_non_paper', 'product_type_filter', 1.0,
               'product_type=Dataset; not a research paper; removed from active corpus [run_tag=' || ? || ']',
               json_object('product_type', product_type, 'primary_lab', primary_lab, 'year', year, 'title', title),
               ?
        FROM papers WHERE product_type='Dataset'
    """, (RUN_TAG, ts))
    print(f"  logged {cur.rowcount:,} decisions")
    conn.commit()

    # Now delete
    print("Deleting papers rows...")
    cur.execute("DELETE FROM papers WHERE product_type='Dataset'")
    deleted = cur.rowcount
    print(f"  deleted {deleted:,} papers rows")

    # Close refresh_runs
    ended = datetime.now(timezone.utc).isoformat()
    cur.execute("""
        UPDATE refresh_runs SET ended_ts=?, records_updated=? WHERE run_id=?
    """, (ended, deleted, run_id))
    conn.commit()

    # Verify
    remaining_dataset = cur.execute("SELECT COUNT(*) FROM papers WHERE product_type='Dataset'").fetchone()[0]
    total_papers = cur.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    print(f"\nVerify: Dataset rows remaining={remaining_dataset}, total papers={total_papers:,}")
    conn.close()

if __name__ == "__main__":
    main()
