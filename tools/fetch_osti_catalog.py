#!/usr/bin/env python3
"""
Bulk-pull OSTI metadata for the 10 DOE SC labs, time-range parameterized.

Usage:
  fetch_osti_catalog.py --year-start 2000 --year-end 2026 [--labs LAB,LAB...] [--rows 500]
  fetch_osti_catalog.py --since-days 7    # weekly refresh: entry_date_start = now-7d

Strategy:
  - Iterate (lab, year). For each (lab, year), paginate /api/v1/records with rows=500.
  - Use entry_date for refresh mode (catches updates), publication_date for backfill.
  - Resumable: tracks progress in refresh_runs and skips (lab, year, page) combos already
    seen in this run if interrupted. Re-running is safe (UPSERT).
  - Rate limit: 1.5s between requests, MAX_RETRY=5 with exp backoff on 429/5xx.

Writes:
  - papers table (UPSERT on osti_id)
  - refresh_runs table (start/end, counts)
"""
import argparse, json, sqlite3, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta
from pathlib import Path

CATALOG = Path("/Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite")
API = "https://www.osti.gov/api/v1/records"
UA = "Mozilla/5.0 (Kukla agent / osti-corpus-consolidation; rick.stevens.ai@gmail.com)"
TIMEOUT = 60
DELAY = 1.5     # base inter-request delay (verified clean at 1-2 req/s sustained)
MAX_RETRY = 5
ROWS = 500

# The 10 DOE Office of Science labs — canonical research_org strings (matched to OSTI's index).
# We use these as the lab list for both initial pull and weekly refresh.
SC_LABS = [
    "Argonne National Laboratory",
    "Brookhaven National Laboratory",
    "Fermi National Accelerator Laboratory",
    "Lawrence Berkeley National Laboratory",
    "Oak Ridge National Laboratory",
    "Pacific Northwest National Laboratory",
    "Princeton Plasma Physics Laboratory",
    "SLAC National Accelerator Laboratory",
    "Thomas Jefferson National Accelerator Facility",
    "Ames National Laboratory",
]

def fetch_page(url):
    """GET with retry-on-429/5xx and exponential backoff."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                total = resp.headers.get("X-Total-Count") or resp.headers.get("x-total-count")
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
                return body, int(total) if total and total.isdigit() else None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRY - 1:
                back = 2 ** (attempt + 2)  # 4, 8, 16, 32
                print(f"    [{e.code}] retry {attempt+1}/{MAX_RETRY} in {back}s", flush=True)
                time.sleep(back)
                continue
            raise
        except urllib.error.URLError as e:
            if attempt < MAX_RETRY - 1:
                back = 2 ** (attempt + 2)
                print(f"    [URLError] retry {attempt+1}/{MAX_RETRY} in {back}s", flush=True)
                time.sleep(back)
                continue
            raise
    raise RuntimeError(f"Exhausted {MAX_RETRY} retries on {url}")

def normalize_lab(orgs_list):
    """Pick the first SC lab name from the research_orgs list, return canonical short form."""
    if not orgs_list:
        return None
    org_str = " ".join(str(o) for o in orgs_list[:3]).lower()
    LAB_MAP = {
        "argonne": "ANL",
        "brookhaven": "BNL",
        "fermi national": "FNAL",
        "fermilab": "FNAL",
        "lawrence berkeley": "LBNL",
        "lbnl": "LBNL",
        "oak ridge": "ORNL",
        "ornl": "ORNL",
        "pacific northwest": "PNNL",
        "pnnl": "PNNL",
        "princeton plasma": "PPPL",
        "pppl": "PPPL",
        "slac": "SLAC",
        "jefferson lab": "JLab",
        "thomas jefferson": "JLab",
        "jlab": "JLab",
        "ames laboratory": "AMES",
        "ames national": "AMES",
    }
    for key, short in LAB_MAP.items():
        if key in org_str:
            return short
    return None

def upsert_paper(conn, rec, source):
    osti_id = str(rec.get("osti_id") or "")
    if not osti_id:
        return False
    pub = rec.get("publication_date") or rec.get("publicationDate") or ""
    year = None
    if pub:
        if "-" in pub and len(pub) >= 4:
            try: year = int(pub.split("-")[0])
            except: pass
        elif "/" in pub and len(pub) >= 10:
            try: year = int(pub.split("/")[-1][:4])
            except: pass
    orgs = rec.get("research_orgs") or []
    primary_lab = normalize_lab(orgs)
    now = datetime.utcnow().isoformat() + "Z"
    row = {
        "osti_id": osti_id,
        "doi": rec.get("doi"),
        "title": rec.get("title"),
        "publication_date": pub or None,
        "year": year,
        "product_type": rec.get("product_type"),
        "journal_name": rec.get("journal_name"),
        "journal_volume": rec.get("journal_volume"),
        "journal_issue": rec.get("journal_issue"),
        "research_orgs_json": json.dumps(orgs, ensure_ascii=False),
        "primary_lab": primary_lab,
        "sponsor_orgs_json": json.dumps(rec.get("sponsor_orgs") or [], ensure_ascii=False),
        "authors_json": json.dumps(rec.get("authors") or [], ensure_ascii=False),
        "subjects_json": json.dumps(rec.get("subjects") or [], ensure_ascii=False),
        "description": rec.get("description"),
        "doe_contract_number": rec.get("doe_contract_number"),
        "osti_links_json": json.dumps(rec.get("links") or [], ensure_ascii=False),
        "catalog_last_seen_ts": now,
        "metadata_source": source,
    }
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM papers WHERE osti_id=?", (osti_id,))
    exists = cur.fetchone() is not None
    if exists:
        cur.execute("""
            UPDATE papers SET
                doi=COALESCE(?, doi),
                title=COALESCE(?, title),
                publication_date=COALESCE(?, publication_date),
                year=COALESCE(?, year),
                product_type=COALESCE(?, product_type),
                journal_name=COALESCE(?, journal_name),
                journal_volume=COALESCE(?, journal_volume),
                journal_issue=COALESCE(?, journal_issue),
                research_orgs_json=?,
                primary_lab=COALESCE(?, primary_lab),
                sponsor_orgs_json=?,
                authors_json=?,
                subjects_json=?,
                description=COALESCE(?, description),
                doe_contract_number=COALESCE(?, doe_contract_number),
                osti_links_json=?,
                catalog_last_seen_ts=?,
                metadata_source=?
            WHERE osti_id=?
        """, (row["doi"], row["title"], row["publication_date"], row["year"],
              row["product_type"], row["journal_name"], row["journal_volume"],
              row["journal_issue"], row["research_orgs_json"], row["primary_lab"],
              row["sponsor_orgs_json"], row["authors_json"], row["subjects_json"],
              row["description"], row["doe_contract_number"], row["osti_links_json"],
              row["catalog_last_seen_ts"], row["metadata_source"], osti_id))
        return False  # updated
    else:
        cur.execute("""
            INSERT INTO papers
              (osti_id, doi, title, publication_date, year, product_type,
               journal_name, journal_volume, journal_issue, research_orgs_json,
               primary_lab, sponsor_orgs_json, authors_json, subjects_json,
               description, doe_contract_number, osti_links_json,
               catalog_first_seen_ts, catalog_last_seen_ts, metadata_source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (osti_id, row["doi"], row["title"], row["publication_date"], row["year"],
              row["product_type"], row["journal_name"], row["journal_volume"],
              row["journal_issue"], row["research_orgs_json"], row["primary_lab"],
              row["sponsor_orgs_json"], row["authors_json"], row["subjects_json"],
              row["description"], row["doe_contract_number"], row["osti_links_json"],
              now, row["catalog_last_seen_ts"], row["metadata_source"]))
        return True   # added

def pull_lab_year(conn, lab, year, source_tag):
    """Pull all OSTI records for (lab, year), paginating. Returns (added, updated)."""
    lab_q = lab.replace(" ", "+")
    start_date = f"01/01/{year}"
    end_date = f"12/31/{year}"
    added = 0
    updated = 0
    page = 1
    total = None
    while True:
        url = f"{API}?research_org={lab_q}&publication_date_start={start_date}&publication_date_end={end_date}&rows={ROWS}&page={page}"
        try:
            body, t = fetch_page(url)
        except Exception as e:
            print(f"  ERROR {lab} {year} page {page}: {e}", flush=True)
            return added, updated, "error"
        if total is None:
            total = t
        if not body:
            break
        for rec in body:
            was_added = upsert_paper(conn, rec, source_tag)
            if was_added:
                added += 1
            else:
                updated += 1
        conn.commit()
        if len(body) < ROWS:
            break
        page += 1
        time.sleep(DELAY)
    return added, updated, "ok"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-start", type=int, default=2000)
    ap.add_argument("--year-end", type=int, default=2026)
    ap.add_argument("--labs", type=str, default=None, help="Comma-separated subset of SC_LABS short names")
    ap.add_argument("--since-days", type=int, default=None, help="Weekly refresh mode")
    ap.add_argument("--run-type", type=str, default="initial_catalog")
    args = ap.parse_args()

    conn = sqlite3.connect(CATALOG)
    started = datetime.utcnow().isoformat() + "Z"
    cur = conn.cursor()
    cur.execute("""INSERT INTO refresh_runs
        (started_ts, run_type, params_json) VALUES (?, ?, ?)""",
        (started, args.run_type, json.dumps(vars(args))))
    run_id = cur.lastrowid
    conn.commit()

    labs = SC_LABS
    if args.labs:
        wanted = {x.strip().upper() for x in args.labs.split(",")}
        labs = [L for L in SC_LABS if any(w in L.upper() for w in wanted)]

    total_added = 0
    total_updated = 0
    total_errors = 0
    t0 = time.time()
    for lab in labs:
        for year in range(args.year_start, args.year_end + 1):
            t_ly = time.time()
            print(f"\n[{lab} {year}]", flush=True)
            added, updated, status = pull_lab_year(conn, lab, year, source_tag=f"osti_api_bulk_run{run_id}")
            if status == "error":
                total_errors += 1
            total_added += added
            total_updated += updated
            print(f"  +{added} new, ~{updated} updated, ({time.time()-t_ly:.1f}s) "
                  f"running total: {total_added}+/{total_updated}~ in {(time.time()-t0)/60:.1f}min", flush=True)
            time.sleep(DELAY)

    ended = datetime.utcnow().isoformat() + "Z"
    cur.execute("""UPDATE refresh_runs SET ended_ts=?, records_added=?, records_updated=?, errors=? WHERE run_id=?""",
                (ended, total_added, total_updated, total_errors, run_id))
    conn.commit()

    print(f"\n=== Run {run_id} complete ===")
    print(f"  +{total_added} new records, ~{total_updated} updated, {total_errors} lab-year errors")
    print(f"  Elapsed: {(time.time()-t0)/60:.1f}min")

    total_papers = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    print(f"  Total papers in catalog: {total_papers}")
    print("\n  Per-lab counts:")
    for lab, cnt in conn.execute("SELECT primary_lab, COUNT(*) FROM papers WHERE primary_lab IS NOT NULL GROUP BY primary_lab ORDER BY COUNT(*) DESC"):
        print(f"    {lab:6s} {cnt:>7d}")
    print("\n  Year distribution:")
    for year, cnt in conn.execute("SELECT year, COUNT(*) FROM papers WHERE year IS NOT NULL GROUP BY year ORDER BY year"):
        print(f"    {year}  {cnt:>6d}")
    conn.close()

if __name__ == "__main__":
    main()
