#!/usr/bin/env python3
"""Audit-trail-first corpus cleanup: remove papers rows matching product_type filter.

Pattern (reusable for any corpus catalog with a `decisions` + `refresh_runs` table):
  1. SELECT COUNT to confirm scope
  2. Check overlap with file_instances (PDFs on disk — info-only, file rows kept)
  3. **Run `.schema refresh_runs` and `.schema decisions` first** to verify column names — schemas
     drift between context summaries and reality. The column names below match the schema
     init_catalog.py creates as of 2026-06-13 (started_ts/ended_ts, params_json, no run_tag).
     If your DB was built by a different init script, ADJUST the INSERTs before running.
  4. INSERT refresh_runs row (run_type='exclusion_pass', started_ts, params_json)
  5. INSERT one decisions row per osti_id (pre-deletion snapshot in inputs_json;
     run_tag embedded in rationale string since decisions has no run_tag column)
  6. DELETE papers rows
  7. UPDATE refresh_runs (ended_ts, records_updated)
  8. Verify with SELECT COUNT

Usage:
  python3 remove_by_product_type.py --db /Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite \\
    --types Dataset,Patent,Multimedia --run-tag exclude_non_papers_v1 [--dry-run]

Defaults to dry-run False — script does the deletion. Use --dry-run for a preview.
"""
import argparse, sqlite3, json, sys
from datetime import datetime, timezone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="Path to catalog.sqlite")
    ap.add_argument("--types", required=True, help="Comma-separated product_type values to remove")
    ap.add_argument("--run-tag", required=True, help="Tag for decisions.run_tag column")
    ap.add_argument("--dry-run", action="store_true", help="Preview counts only, no DELETE")
    args = ap.parse_args()

    types = [t.strip() for t in args.types.split(",")]
    placeholders = ",".join(["?"] * len(types))

    conn = sqlite3.connect(args.db, timeout=600)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    # 1. Count
    n = cur.execute(
        f"SELECT COUNT(*) FROM papers WHERE product_type IN ({placeholders})", types
    ).fetchone()[0]
    print(f"Found {n:,} rows matching product_type IN {types}")
    if n == 0:
        return

    # 2. Check PDFs on disk (info only — file_instances rows NOT deleted)
    on_disk = cur.execute(
        f"""SELECT COUNT(DISTINCT fi.osti_id) FROM file_instances fi
            INNER JOIN papers p ON fi.osti_id=p.osti_id
            WHERE p.product_type IN ({placeholders})""",
        types,
    ).fetchone()[0]
    print(f"  ...of which {on_disk} have a PDF on disk (file_instances row kept)")

    if args.dry_run:
        # Show a sample
        print("\nSample (5 rows):")
        for row in cur.execute(
            f"""SELECT osti_id, product_type, primary_lab, year, substr(title,1,60)
                FROM papers WHERE product_type IN ({placeholders}) LIMIT 5""",
            types,
        ):
            print(f"  {row}")
        print("\n(dry-run; no changes made)")
        return

    # 3. Refresh run row
    # NOTE: column names here match init_catalog.py as of 2026-06-13
    # (started_ts/ended_ts/params_json, NOT started_at/ended_at/year_start/labs_json).
    # Run `.schema refresh_runs` against your DB if unsure.
    started = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """INSERT INTO refresh_runs (run_type, started_ts, params_json,
                                     records_added, records_updated, errors)
           VALUES ('exclusion_pass', ?, ?, 0, 0, 0)""",
        (started, json.dumps({"filter": f"product_type IN {types}", "run_tag": args.run_tag})),
    )
    run_id = cur.lastrowid
    print(f"  refresh_runs run_id={run_id}")

    # 4. Decisions rows (one per osti_id).
    # NOTE: decisions has NO run_tag column — embed run_tag in rationale string.
    ts = datetime.now(timezone.utc).isoformat()
    rationale = f"product_type IN ({','.join(types)}); not a research paper; removed from active corpus [run_tag={args.run_tag}]"
    print("Logging decisions...")
    cur.execute(
        f"""INSERT INTO decisions (osti_id, decision_type, method, confidence, rationale, inputs_json, ts)
            SELECT osti_id, 'excluded_non_paper', 'product_type_filter', 1.0, ?,
                   json_object('product_type', product_type, 'primary_lab', primary_lab,
                               'year', year, 'title', title),
                   ?
            FROM papers WHERE product_type IN ({placeholders})""",
        (rationale, ts, *types),
    )
    print(f"  logged {cur.rowcount:,} decisions")
    conn.commit()

    # 5. Delete
    print("Deleting papers rows...")
    cur.execute(f"DELETE FROM papers WHERE product_type IN ({placeholders})", types)
    deleted = cur.rowcount
    print(f"  deleted {deleted:,} papers rows")

    # 6. Close refresh_runs row
    ended = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "UPDATE refresh_runs SET ended_ts=?, records_updated=? WHERE run_id=?",
        (ended, deleted, run_id),
    )
    conn.commit()

    # 7. Verify
    remaining = cur.execute(
        f"SELECT COUNT(*) FROM papers WHERE product_type IN ({placeholders})", types
    ).fetchone()[0]
    total = cur.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    print(f"\nVerify: matching rows remaining={remaining}, total papers={total:,}")
    conn.close()


if __name__ == "__main__":
    main()
