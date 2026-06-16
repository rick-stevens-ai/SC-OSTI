#!/usr/bin/env python3
"""Daily SC-OSTI refresh.

Runs idempotently:
  1. Re-dump catalog.sqlite schema -> schemas/sql/catalog.sql
  2. Mirror current skills -> skills/ (via refresh_skills.py)
  3. Write today's state snapshot -> docs/state/<YYYY-MM-DD>.md
  4. Append one line to docs/state/INDEX.md

Safe to run repeatedly; overwrites today's snapshot, appends index once.
Designed to be invoked by launchd / cron on m1-mac-mini.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SC = Path(__file__).resolve().parent.parent
DB = Path("/Volumes/SG-1-8TB/osti/catalog/catalog.sqlite")
PDFS_ROOT = Path("/Volumes/SG-1-8TB/osti/pdfs")


def dump_schema() -> None:
    out = SC / "schemas/sql/catalog.sql"
    if not DB.exists():
        print(f"[skip] catalog DB not mounted at {DB}")
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "WHEN 'view' THEN 2 WHEN 'trigger' THEN 3 ELSE 4 END, name"
    ).fetchall()
    con.close()
    body = (
        f"-- catalog.sqlite schema dump\n"
        f"-- generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"-- source: {DB}\n"
        f"-- objects: {len(rows)}\n\n"
    )
    for t, n, sql in rows:
        body += f"-- {t}: {n}\n{sql.strip()};\n\n"
    out.write_text(body)
    print(f"[ok] schema dump -> {out} ({out.stat().st_size} B, {len(rows)} objects)")


def refresh_skills() -> None:
    script = SC / "tools/refresh_skills.py"
    if not script.exists():
        print(f"[skip] {script} missing")
        return
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    print(r.stdout.rstrip() or "[ok] skills refreshed")
    if r.returncode != 0:
        print(r.stderr.rstrip(), file=sys.stderr)


def snapshot_state() -> Path | None:
    if not DB.exists():
        print(f"[skip] catalog DB not mounted at {DB}")
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c = con.cursor()
    total = c.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    with_doi = c.execute("SELECT COUNT(*) FROM papers WHERE doi IS NOT NULL AND doi != ''").fetchone()[0]
    no_doi = total - with_doi
    fi = c.execute("SELECT COUNT(*) FROM file_instances").fetchone()[0]
    unique_pdfs = c.execute("SELECT COUNT(DISTINCT canonical_path) FROM file_instances WHERE canonical_path IS NOT NULL").fetchone()[0]
    with_pdf = c.execute("SELECT COUNT(*) FROM papers WHERE has_pdf = 1 OR canonical_pdf_path IS NOT NULL").fetchone()[0]
    missing = total - with_pdf
    coverage = round(100 * with_pdf / total, 2) if total else 0.0

    prod = c.execute(
        "SELECT product_type, COUNT(*) FROM papers GROUP BY product_type ORDER BY 2 DESC LIMIT 12"
    ).fetchall()
    prod_nodoi = c.execute(
        "SELECT product_type, COUNT(*) FROM papers WHERE doi IS NULL OR doi = '' "
        "GROUP BY product_type ORDER BY 2 DESC LIMIT 12"
    ).fetchall()
    lab = c.execute(
        "SELECT primary_lab, COUNT(*) FROM papers GROUP BY primary_lab ORDER BY 2 DESC LIMIT 15"
    ).fetchall()
    oa = c.execute(
        "SELECT oa_status, COUNT(*) FROM papers WHERE oa_status IS NOT NULL GROUP BY oa_status ORDER BY 2 DESC"
    ).fetchall()
    md_done = c.execute("SELECT COUNT(*) FROM papers WHERE md_path IS NOT NULL").fetchone()[0]
    mmd_done = c.execute("SELECT COUNT(*) FROM papers WHERE mmd_path IS NOT NULL").fetchone()[0]
    con.close()

    year_dist = {}
    if PDFS_ROOT.exists():
        for yd in sorted(PDFS_ROOT.iterdir()):
            if yd.is_dir():
                year_dist[yd.name] = sum(
                    1 for p in yd.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
                )

    df_out = subprocess.run(
        ["df", "-h", "/Volumes/SG-1-8TB"], capture_output=True, text=True
    ).stdout.strip()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap = SC / f"docs/state/{today}.md"
    ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows_prod = "\n".join(f"| {p or '(null)'} | {n:,} |" for p, n in prod)
    rows_prod_nd = "\n".join(f"| {p or '(null)'} | {n:,} |" for p, n in prod_nodoi)
    rows_lab = "\n".join(f"| {p or '(null)'} | {n:,} |" for p, n in lab)
    rows_oa = (
        "\n".join(f"| {s or '(null)'} | {n:,} |" for s, n in oa)
        if oa
        else "_(Unpaywall reconcile not yet populated)_"
    )
    rows_yr = "\n".join(f"| {y} | {n:,} |" for y, n in sorted(year_dist.items()))
    pdfs_on_disk = sum(year_dist.values())

    body = f"""# Corpus state — {today}

Snapshot at {ts_iso} on m1-mac-mini.

## Catalog DB (`{DB}`)
- **Total papers:** {total:,}
- **With DOI:** {with_doi:,} ({100*with_doi/total:.1f}%)
- **Without DOI:** {no_doi:,} ({100*no_doi/total:.1f}%)
- **File instances:** {fi:,}
- **Unique canonical PDFs on disk:** {unique_pdfs:,}
- **Papers with PDF:** {with_pdf:,} ({coverage}%)
- **Papers missing PDF:** {missing:,}
- **Papers with .md (Marker):** {md_done:,}
- **Papers with .mmd (Nougat):** {mmd_done:,}

## Product type — full corpus
| product_type | n |
|---|---|
{rows_prod}

## Product type — NO-DOI papers only
| product_type | n |
|---|---|
{rows_prod_nd}

## Primary lab (top 15)
| primary_lab | n |
|---|---|
{rows_lab}

## OA status (Unpaywall responses)
| oa_status | n |
|---|---|
{rows_oa}

## Disk
```
{df_out}
```

PDFs on disk total: {pdfs_on_disk:,}

| year | count |
|------|-------|
{rows_yr}
"""
    snap.write_text(body)
    print(f"[ok] state snapshot -> {snap} ({snap.stat().st_size} B)")
    return snap


def update_index(snap: Path | None) -> None:
    if snap is None:
        return
    idx = SC / "docs/state/INDEX.md"
    today = snap.stem
    line = f"- [{today}](./{snap.name})\n"
    if not idx.exists():
        idx.write_text(
            "# State snapshots\n\nDaily corpus state, generated by `tools/update_daily.py`.\n\n"
            + line
        )
        print(f"[ok] index created -> {idx}")
        return
    existing = idx.read_text()
    if line.strip() in existing:
        print(f"[ok] index already contains {today}, no change")
        return
    # Insert at top of list section (after the first blank line after header)
    idx.write_text(existing.rstrip() + "\n" + line)
    print(f"[ok] index appended {today} -> {idx}")


def main() -> int:
    dump_schema()
    refresh_skills()
    snap = snapshot_state()
    update_index(snap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
