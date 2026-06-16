#!/usr/bin/env python3
"""Smoke probe: 2000-2005 OSTI PURL recovery rate.

Stratified sample (50 per year × 6 years = 300) from papers with no local PDF.
Records per-paper outcome to a TSV. No retention of bodies > 1KB.

Buckets:
  recovered_pdf  — HTTP 200 + magic bytes match %PDF + size > 1KB
  http_404       — PURL 404
  http_403       — PURL 403 (publisher wall)
  http_other     — other 4xx/5xx
  redirect_off   — followed redirect but result not PDF
  wrong_type     — 200 but Content-Type not application/pdf
  empty          — 200 but size <= 1KB
  timeout        — connect/read timeout
  exception      — other Python error
"""
import sqlite3, json, os, random, time, urllib.request, urllib.error, ssl
from pathlib import Path
from datetime import datetime, timezone

DB = "/Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite"
OUT_DIR = Path(f"/Volumes/Cherry6TB/osti_corpus/probes/backfill_2000-2005_{datetime.now().strftime('%Y%m%d-%H%M')}")
PER_YEAR = 50
YEARS = list(range(2000, 2006))
DELAY = 1.0  # gentle
TIMEOUT = 30
UA = "Mozilla/5.0 (Kukla agent / osti-corpus-backfill-probe; rick.stevens.ai@gmail.com)"

def classify(osti_id):
    url = f"https://www.osti.gov/servlets/purl/{osti_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            final_url = r.geturl()
            ctype = r.headers.get("Content-Type", "")
            body = r.read(8192)  # just sniff
            size_hint = r.headers.get("Content-Length")
            try:
                size_hint = int(size_hint) if size_hint else len(body)
            except ValueError:
                size_hint = len(body)
            if not body.startswith(b"%PDF") and "pdf" not in ctype.lower():
                if final_url != url:
                    return ("redirect_off", 200, final_url, ctype, size_hint)
                return ("wrong_type", 200, final_url, ctype, size_hint)
            if size_hint <= 1024:
                return ("empty", 200, final_url, ctype, size_hint)
            return ("recovered_pdf", 200, final_url, ctype, size_hint)
    except urllib.error.HTTPError as e:
        return (f"http_{e.code}", e.code, url, "", 0)
    except urllib.error.URLError as e:
        reason = str(e.reason).lower()
        if "timed out" in reason or "timeout" in reason:
            return ("timeout", 0, url, "", 0)
        return ("exception", 0, url, str(e)[:100], 0)
    except Exception as e:
        return ("exception", 0, url, str(e)[:100], 0)

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    random.seed(42)
    conn = sqlite3.connect(DB, timeout=300)
    conn.row_factory = sqlite3.Row

    # Stratified sample
    samples = []
    for y in YEARS:
        rows = conn.execute("""
            SELECT p.osti_id, p.year, p.primary_lab, p.doi, p.title
            FROM papers p
            LEFT JOIN file_instances fi ON p.osti_id = fi.osti_id
            WHERE p.year = ? AND fi.osti_id IS NULL
        """, (y,)).fetchall()
        rows = list(rows)
        random.shuffle(rows)
        picked = rows[:PER_YEAR]
        print(f"  year={y}: {len(rows)} no-PDF papers, picked {len(picked)}")
        samples.extend(picked)
    conn.close()

    print(f"\nProbing {len(samples)} OSTI PURLs with {DELAY}s delay (ETA ~{int(len(samples) * (DELAY + 2) / 60)} min)\n")
    tsv_path = OUT_DIR / "results.tsv"
    with open(tsv_path, "w") as f:
        f.write("osti_id\tyear\tprimary_lab\tdoi\tbucket\thttp_status\tcontent_type\tsize_hint\tfinal_url\ttitle\n")
        buckets = {}
        for i, r in enumerate(samples, 1):
            bucket, status, final_url, ctype, size = classify(r["osti_id"])
            buckets[bucket] = buckets.get(bucket, 0) + 1
            f.write(f"{r['osti_id']}\t{r['year']}\t{r['primary_lab']}\t{r['doi'] or ''}\t{bucket}\t{status}\t{ctype}\t{size}\t{final_url}\t{(r['title'] or '')[:120]}\n")
            f.flush()
            if i % 25 == 0:
                print(f"  {i}/{len(samples)} buckets={buckets}")
            time.sleep(DELAY)

    # Summary
    summary_path = OUT_DIR / "SUMMARY.md"
    total = len(samples)
    with open(summary_path, "w") as f:
        f.write(f"# OSTI PURL recovery probe 2000-2005\n\n")
        f.write(f"Sampled: {total} ({PER_YEAR}/yr × {len(YEARS)} yrs)\n")
        f.write(f"Started: {datetime.now(timezone.utc).isoformat()}\n\n")
        f.write("## Buckets\n\n")
        for b, c in sorted(buckets.items(), key=lambda x: -x[1]):
            pct = 100.0 * c / total
            f.write(f"- **{b}**: {c} ({pct:.1f}%)\n")
        f.write("\n## Per-year breakdown\n\n")
        # Re-read TSV for per-year
        from collections import defaultdict
        per_year = defaultdict(lambda: defaultdict(int))
        with open(tsv_path) as g:
            next(g)
            for line in g:
                parts = line.rstrip("\n").split("\t")
                per_year[parts[1]][parts[4]] += 1
        for y in sorted(per_year):
            row = per_year[y]
            n = sum(row.values())
            rec = row.get("recovered_pdf", 0)
            f.write(f"- {y}: {rec}/{n} recovered ({100.0*rec/n:.0f}%); buckets={dict(row)}\n")
    print(f"\nDone. Summary: {summary_path}")
    print(f"TSV: {tsv_path}")
    print(f"Final buckets: {buckets}")

if __name__ == "__main__":
    main()
