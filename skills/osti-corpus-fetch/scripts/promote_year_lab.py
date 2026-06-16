#!/usr/bin/env python3
"""Additively promote inventory.files.{year_from_api,lab_from_api} into papers.{year,primary_lab}.

Pattern: stage1_year_lookup.py and similar OSTI-API enrichment scripts write to a separate
inventory/audit DB (typically `_audit/inventory.sqlite`), not directly to the canonical
catalog. This script promotes those API resolutions into the catalog as an ADDITIVE-ONLY
update — only fills NULL/UNKNOWN values, never overwrites existing catalog values, even when
the API says something different.

Why additive-only? Because the OSTI `research_orgs[0]` field that becomes `lab_from_api`
doesn't always agree with the catalog's `primary_lab` — collaboration papers, multi-lab
grants, and second-author-affiliation papers produce legitimate divergence. The DIFFERS set
(~1,000+ on a typical pass) needs LLM classification on the full research_orgs array, NOT a
regex-driven flip.

The dry-run prints set-vs-changes-vs-differs counts SEPARATELY so the operator can see
exactly what would change before committing. Always dry-run first.

Usage:
    # Dry-run against a /tmp snapshot of the catalog (recommended first step)
    python3 promote_year_lab.py --inv /tmp/inv.sqlite --target /tmp/cat5.sqlite --dry-run

    # Promote to the live catalog (additive only, no overwrites)
    python3 promote_year_lab.py --inv /tmp/inv.sqlite \\
        --target /Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite

Adapt the LAB_PATTERNS list for any other corpus with a known canonical-code set.
Note the `\\bAMES\\b` anchor: bare `r'ames|AMES'` matches "NASA Ames" and is wrong.
For acronyms that are also common English words, always use word boundaries.
"""
import argparse
import re
import sqlite3
import sys
import time

# Order matters: more-specific patterns first to avoid AMES catching NASA Ames.
LAB_PATTERNS = [
    (r'argonne', 'ANL'),
    (r'lawrence berkeley|LBNL', 'LBNL'),
    (r'oak ridge|ORNL', 'ORNL'),
    (r'pacific northwest|PNNL', 'PNNL'),
    (r'brookhaven|BNL', 'BNL'),
    (r'fermi|FNAL', 'FNAL'),
    (r'jefferson|JLab|Thomas Jefferson', 'JLab'),
    (r'SLAC|stanford linear', 'SLAC'),
    (r'princeton plasma|PPPL', 'PPPL'),
    (r'ames laboratory|\bAMES\b', 'AMES'),  # NOT NASA Ames
    (r'idaho national|INL', 'INL'),
    (r'lawrence livermore|LLNL', 'LLNL'),
    (r'los alamos|LANL', 'LANL'),
    (r'sandia|SNL', 'SNL'),
    (r'national renewable|NREL', 'NREL'),
    (r'savannah river|SRNL', 'SRNL'),
    (r'national energy tech|NETL', 'NETL'),
]


def normalize_lab(s):
    if not s:
        return None
    for pat, code in LAB_PATTERNS:
        if re.search(pat, s, re.IGNORECASE):
            return code
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inv", default="/tmp/inv.sqlite",
                    help="Inventory DB with files.year_from_api, files.lab_from_api, files.api_status")
    ap.add_argument("--target", default="/tmp/cat5.sqlite",
                    help="Catalog DB to update (papers table)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan without writing")
    args = ap.parse_args()

    inv = sqlite3.connect(f"file:{args.inv}?mode=ro", uri=True, timeout=60)
    cat = sqlite3.connect(args.target, timeout=120)

    # Build {osti_id -> (year, lab_code)} from inv, taking first ok row per osti_id
    print(f"Reading inventory from {args.inv}...")
    by_id = {}
    for r in inv.execute("""
        SELECT osti_id, year_from_api, lab_from_api
          FROM files
         WHERE api_status='ok' AND year_from_api IS NOT NULL
    """):
        oid, yr, lab_str = r
        lab = normalize_lab(lab_str)
        if oid not in by_id:
            by_id[oid] = (yr, lab)
    print(f"  {len(by_id):,} OSTI IDs with API year (and possibly lab)")

    print("Reading current papers state...")
    cur_state = {}
    for r in cat.execute("SELECT osti_id, year, primary_lab FROM papers"):
        cur_state[r[0]] = (r[1], r[2])
    print(f"  {len(cur_state):,} papers total")

    will_set_year = 0
    will_set_lab = 0
    will_change_year = 0
    will_change_lab = 0
    updates = []
    for oid, (new_year, new_lab) in by_id.items():
        if oid not in cur_state:
            continue
        old_year, old_lab = cur_state[oid]
        set_year = old_year is None and new_year is not None
        change_year = (old_year is not None and new_year is not None
                       and old_year != new_year)
        set_lab = (not old_lab or old_lab in ('', 'UNKNOWN')) and new_lab
        change_lab = (old_lab and old_lab not in ('', 'UNKNOWN')
                      and new_lab and new_lab != old_lab)
        if set_year:
            will_set_year += 1
        if change_year:
            will_change_year += 1
        if set_lab:
            will_set_lab += 1
        if change_lab:
            will_change_lab += 1
        if set_year or set_lab:
            # Additive only — preserve existing values, never overwrite.
            updates.append((
                new_year if set_year else old_year,
                new_lab if set_lab else old_lab,
                oid
            ))

    print(f"\n--- Promotion plan (ADDITIVE ONLY, no overwrites of existing values) ---")
    print(f"  year SET (was NULL):              {will_set_year:,}")
    print(f"  lab  SET (was NULL/UNKNOWN):      {will_set_lab:,}")
    print(f"  -- diagnostics (NOT applied) --")
    print(f"  year DIFFERS (NOT changed):       {will_change_year:,}")
    print(f"  lab  DIFFERS (NOT changed):       {will_change_lab:,}")
    print(f"  rows to update:                   {len(updates):,}")
    print(f"\nNote on DIFFERS: these need LLM classification, not regex flip.")
    print(f"  See osti-corpus-fetch SKILL pitfalls for the rationale.")

    if args.dry_run:
        print("\nDRY RUN — no writes")
        return 0

    print(f"\nWriting to {args.target}...")
    t0 = time.time()
    cat.executemany(
        "UPDATE papers SET year=?, primary_lab=? WHERE osti_id=?",
        updates
    )
    cat.commit()
    print(f"  committed {len(updates):,} updates in {time.time()-t0:.1f}s")
    inv.close()
    cat.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
