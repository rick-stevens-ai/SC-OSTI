#!/usr/bin/env python3
"""
OSTI recon: enumerate papers for the 10 DOE Office of Science labs across a year range.

- Idempotent: skips cells already on disk.
- Portable: writes under Path(__file__).parent / "recon" — runs on m1 + CELS hosts unchanged.
- Polite: 0.5s sleep between API calls, single-threaded.

Output: recon/<Lab_With_Underscores>__<year>.jsonl (one JSON record per line).
Record schema: {osti_id, doi, title, links, research_orgs, publication_date, product_type, _query_lab, _query_year}

Usage:
    python3 recon_sc_labs.py [--years 2016-2026]
"""
from __future__ import annotations
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LABS = [
    "Ames Laboratory",
    "Argonne National Laboratory",
    "Brookhaven National Laboratory",
    "Fermi National Accelerator Laboratory",
    "Lawrence Berkeley National Laboratory",
    "Oak Ridge National Laboratory",
    "Pacific Northwest National Laboratory",
    "Princeton Plasma Physics Laboratory",
    "SLAC National Accelerator Laboratory",
    "Thomas Jefferson National Accelerator Facility",
]

YEARS = list(range(2016, 2027))  # 2016 through 2026 inclusive
ROWS = 100
API = "https://www.osti.gov/api/v1/records"
OUT = Path(__file__).parent / "recon"
OUT.mkdir(exist_ok=True)


def cell_path(lab: str, year: int) -> Path:
    safe = lab.replace(" ", "_")
    return OUT / f"{safe}__{year}.jsonl"


def fetch_cell(lab: str, year: int) -> int:
    """Fetch all pages for one lab×year. Returns record count. Skips if cell exists."""
    out_path = cell_path(lab, year)
    if out_path.exists() and out_path.stat().st_size > 0:
        return -1  # already done

    written = 0
    page = 1
    with out_path.open("w") as f:
        while True:
            params = {
                "research_org": lab,
                "publication_date_start": f"01/01/{year}",
                "publication_date_end": f"12/31/{year}",
                "rows": ROWS,
                "page": page,
            }
            url = f"{API}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    total = int(resp.headers.get("x-total-count", "0"))
                    records = json.loads(resp.read())
            except Exception as e:
                print(f"  ERR {lab} {year} page {page}: {e}", file=sys.stderr, flush=True)
                time.sleep(5)
                continue

            if not records:
                break

            for rec in records:
                rec["_query_lab"] = lab
                rec["_query_year"] = year
                f.write(json.dumps(rec) + "\n")
                written += 1

            if page * ROWS >= total or len(records) < ROWS:
                break

            page += 1
            time.sleep(0.5)  # polite

    return written


def main():
    total_cells = len(LABS) * len(YEARS)
    done = 0
    total_records = 0
    t0 = time.time()
    for lab in LABS:
        for year in YEARS:
            done += 1
            n = fetch_cell(lab, year)
            if n == -1:
                print(f"[{done}/{total_cells}] SKIP {lab} {year} (exists)", flush=True)
            else:
                total_records += n
                elapsed = time.time() - t0
                print(
                    f"[{done}/{total_cells}] {lab} {year}: {n} records "
                    f"(total {total_records}, {elapsed:.0f}s elapsed)",
                    flush=True,
                )
    print(f"\nDONE: {total_records} records across {total_cells} cells in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
