#!/usr/bin/env python3
"""metadata_reconcile.py — fill gaps in catalog.papers from Unpaywall + Crossref + OSTI.

Adds these new columns (idempotent):
  - oa_status      (closed/green/gold/hybrid/bronze) from Unpaywall
  - oa_url         best OA URL
  - oa_license     license string
  - oa_version     publishedVersion / acceptedVersion / submittedVersion
  - oa_evidence    free-text from Unpaywall
  - oa_last_check  ISO timestamp of Unpaywall query
  - crossref_type  (journal-article, book-chapter, ...)
  - crossref_publisher
  - crossref_issn_json
  - crossref_subjects_json
  - crossref_last_check
  - osti_recheck_ts
  - osti_recheck_status (still_listed / not_found / has_pdf / no_pdf)
  - reconcile_state  (pending / unpaywall_done / crossref_done / osti_done / all_done / no_doi)

Worker mode = one of: unpaywall, crossref, osti
Each worker leases batches via SQL claim and processes via API.

Usage:
  python metadata_reconcile.py --db /Volumes/SG-1-8TB/osti/catalog/catalog.sqlite --mode unpaywall --batch 100 --max-batches 10
  python metadata_reconcile.py --db ... --mode crossref ...
  python metadata_reconcile.py --db ... --mode osti ...
"""
import argparse, sqlite3, json, time, sys, os
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

UNPAYWALL_EMAIL = "kukla@kd9nwa.org"
OSTI_API = "https://www.osti.gov/api/v1/records"
CROSSREF_API = "https://api.crossref.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"

NEW_COLS = [
    ("oa_status",         "TEXT"),
    ("oa_url",            "TEXT"),
    ("oa_license",        "TEXT"),
    ("oa_version",        "TEXT"),
    ("oa_evidence",       "TEXT"),
    ("oa_last_check",     "TEXT"),
    ("crossref_type",     "TEXT"),
    ("crossref_publisher","TEXT"),
    ("crossref_issn_json","TEXT"),
    ("crossref_subjects_json","TEXT"),
    ("crossref_last_check","TEXT"),
    ("osti_recheck_ts",   "TEXT"),
    ("osti_recheck_status","TEXT"),
    ("reconcile_state",   "TEXT"),
]

def ensure_schema(con):
    cur = con.cursor()
    existing = [r[1] for r in cur.execute("PRAGMA table_info(papers)")]
    added = []
    for col, typ in NEW_COLS:
        if col not in existing:
            cur.execute(f"ALTER TABLE papers ADD COLUMN {col} {typ}")
            added.append(col)
    if added:
        print(f"Added columns: {added}", flush=True)
    # Index on reconcile_state for fast claim
    cur.execute("CREATE INDEX IF NOT EXISTS idx_papers_oa_check ON papers(oa_last_check)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_papers_crossref_check ON papers(crossref_last_check)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_papers_osti_recheck ON papers(osti_recheck_ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi)")
    con.commit()

def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "kukla-osti-reconcile/1.0 (kukla@kd9nwa.org)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionResetError) as e:
        return 0, str(e)

def reconcile_unpaywall(con, doi, osti_id):
    url = f"{UNPAYWALL_API}/{urllib.parse.quote(doi, safe='')}?email={UNPAYWALL_EMAIL}"
    status, body = http_get_json(url)
    now = datetime.now(timezone.utc).isoformat()
    fields = {"oa_last_check": now}
    if status == 200 and isinstance(body, dict):
        fields["oa_status"] = body.get("oa_status")
        loc = body.get("best_oa_location") or {}
        fields["oa_url"] = loc.get("url_for_pdf") or loc.get("url")
        fields["oa_license"] = loc.get("license")
        fields["oa_version"] = loc.get("version")
        fields["oa_evidence"] = loc.get("evidence")
    elif status == 404:
        fields["oa_status"] = "not_in_unpaywall"
    else:
        fields["oa_status"] = f"error_{status}"
    con.execute(
        "UPDATE papers SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE osti_id=?",
        list(fields.values()) + [osti_id]
    )

def reconcile_crossref(con, doi, osti_id):
    url = f"{CROSSREF_API}/{urllib.parse.quote(doi, safe='')}"
    status, body = http_get_json(url)
    now = datetime.now(timezone.utc).isoformat()
    fields = {"crossref_last_check": now}
    if status == 200 and isinstance(body, dict) and "message" in body:
        m = body["message"]
        fields["crossref_type"] = m.get("type")
        fields["crossref_publisher"] = m.get("publisher")
        fields["crossref_issn_json"] = json.dumps(m.get("ISSN", []))
        fields["crossref_subjects_json"] = json.dumps(m.get("subject", []))
    elif status == 404:
        fields["crossref_type"] = "not_in_crossref"
    else:
        fields["crossref_type"] = f"error_{status}"
    con.execute(
        "UPDATE papers SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE osti_id=?",
        list(fields.values()) + [osti_id]
    )

def reconcile_osti(con, osti_id):
    url = f"{OSTI_API}?osti_id={urllib.parse.quote(str(osti_id))}"
    status, body = http_get_json(url)
    now = datetime.now(timezone.utc).isoformat()
    fields = {"osti_recheck_ts": now}
    if status == 200 and isinstance(body, list) and len(body):
        rec = body[0]
        # Check for fulltext link
        links = rec.get("links", []) or []
        has_pdf = any(l.get("rel") == "fulltext" for l in links if isinstance(l, dict))
        fields["osti_recheck_status"] = "has_pdf" if has_pdf else "no_pdf"
        # Update missing fields opportunistically
        if rec.get("publication_date"):
            existing = con.execute("SELECT publication_date, year FROM papers WHERE osti_id=?", (osti_id,)).fetchone()
            if existing and not existing[0]:
                pd = rec.get("publication_date")
                yr = None
                try:
                    yr = int(pd.split("/")[-1]) if "/" in pd else int(pd[:4])
                except Exception:
                    pass
                con.execute("UPDATE papers SET publication_date=?, year=? WHERE osti_id=?", (pd, yr, osti_id))
        if rec.get("doi"):
            existing = con.execute("SELECT doi FROM papers WHERE osti_id=?", (osti_id,)).fetchone()
            if existing and not existing[0]:
                con.execute("UPDATE papers SET doi=? WHERE osti_id=?", (rec["doi"], osti_id))
    elif status == 404:
        fields["osti_recheck_status"] = "not_found"
    else:
        fields["osti_recheck_status"] = f"error_{status}"
    con.execute(
        "UPDATE papers SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE osti_id=?",
        list(fields.values()) + [osti_id]
    )

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--mode", required=True, choices=["unpaywall", "crossref", "osti"])
    p.add_argument("--batch", type=int, default=50, help="Rows per batch (commits after each)")
    p.add_argument("--max-batches", type=int, default=0, help="0 = unlimited")
    p.add_argument("--sleep", type=float, default=0.1, help="Sleep between requests (s)")
    args = p.parse_args()

    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(con)

    if args.mode == "unpaywall":
        select_sql = "SELECT osti_id, doi FROM papers WHERE doi IS NOT NULL AND doi != '' AND oa_last_check IS NULL LIMIT ?"
        worker = reconcile_unpaywall
    elif args.mode == "crossref":
        select_sql = "SELECT osti_id, doi FROM papers WHERE doi IS NOT NULL AND doi != '' AND crossref_last_check IS NULL LIMIT ?"
        worker = reconcile_crossref
    else:  # osti
        select_sql = "SELECT osti_id FROM papers WHERE osti_recheck_ts IS NULL LIMIT ?"
        worker = None  # special, handled below

    batch_n = 0
    total_ok, total_err = 0, 0
    t_start = time.time()
    while True:
        rows = con.execute(select_sql, (args.batch,)).fetchall()
        if not rows:
            print(f"[{args.mode}] No more rows. Exiting.", flush=True)
            break
        batch_t0 = time.time()
        for r in rows:
            try:
                if args.mode == "osti":
                    reconcile_osti(con, r[0])
                else:
                    reconcile_unpaywall(con, r[1], r[0]) if args.mode == "unpaywall" else reconcile_crossref(con, r[1], r[0])
                total_ok += 1
            except Exception as e:
                total_err += 1
                print(f"[{args.mode}] err {r[0]}: {e}", flush=True)
            time.sleep(args.sleep)
        con.commit()
        batch_n += 1
        rate = len(rows) / (time.time() - batch_t0)
        elapsed = time.time() - t_start
        print(f"[{args.mode}] batch {batch_n}: {len(rows)} rows in {time.time()-batch_t0:.1f}s ({rate:.1f}/s)  total ok={total_ok} err={total_err}  elapsed={elapsed:.0f}s", flush=True)
        if args.max_batches and batch_n >= args.max_batches:
            print(f"[{args.mode}] Hit max-batches limit.", flush=True)
            break

    con.close()
    print(f"[{args.mode}] DONE. ok={total_ok} err={total_err} batches={batch_n} elapsed={time.time()-t_start:.0f}s", flush=True)

if __name__ == "__main__":
    main()
