#!/usr/bin/env python3
"""
Stage 0: Read-only inventory of all 4 Cherry6TB osti_* sources.
Builds: osti_corpus/_audit/inventory.sqlite with one row per (osti_id, source).
Schema lets Stage 2 pick the canonical file per OSTI ID by priority.

Sources & how we infer year:
  - osti_fulltext: path encodes year (YYYY/ID.pdf or YYYY/YYYY/ID.pdf)
  - osti_fulltext_unpay: path encodes year (YYYY/ID.pdf) + has _state/unpaywall_overnight.db
  - osti_fulltext_v2: mostly :00Z/ID.pdf or flat ID.pdf -> year UNKNOWN; will join from unpay DB or OSTI API in Stage 1
"""
import os, re, sqlite3, sys, time
from pathlib import Path

BASE = Path("/Volumes/Cherry6TB")
AUDIT = BASE / "osti_corpus/_audit/inventory.sqlite"
UNPAY_DB = BASE / "osti_fulltext_unpay/_state/unpaywall_overnight.db"

# Source priority (lower wins for canonical pick): fulltext > v2 > unpay
SOURCE_PRIORITY = {
    "osti_fulltext": 1,
    "osti_fulltext_v2": 2,
    "osti_fulltext_unpay": 3,
}

YEAR_RE = re.compile(r"(?:^|/)(20\d{2})(?:/|$)")
ID_RE = re.compile(r"(\d+)\.pdf$", re.IGNORECASE)

def init_db():
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    if AUDIT.exists():
        AUDIT.unlink()
    conn = sqlite3.connect(AUDIT)
    conn.executescript("""
    CREATE TABLE files (
        osti_id TEXT,
        source TEXT,
        priority INTEGER,
        path TEXT,
        size INTEGER,
        year_from_path INTEGER,
        year_from_unpay INTEGER,
        lab_from_unpay TEXT,
        doi_from_unpay TEXT,
        year INTEGER,
        bucket TEXT,
        PRIMARY KEY (osti_id, source)
    );
    CREATE INDEX idx_osti_id ON files(osti_id);
    CREATE INDEX idx_year ON files(year);
    CREATE INDEX idx_source ON files(source);
    """)
    return conn

def load_unpay_map():
    """{osti_id: (year, lab, doi)} from unpay DB."""
    if not UNPAY_DB.exists():
        print("  [WARN] unpay DB missing", file=sys.stderr)
        return {}
    conn = sqlite3.connect(UNPAY_DB)
    rows = conn.execute("SELECT osti_id, year, lab, doi FROM recovery").fetchall()
    conn.close()
    m = {}
    for oid, year, lab, doi in rows:
        m[str(oid)] = (year, lab, doi)
    print(f"  Loaded {len(m)} unpay records")
    return m

def scan_source(src_name, root, unpay_map, conn):
    print(f"\n=== Scanning {src_name} ===", flush=True)
    t0 = time.time()
    pri = SOURCE_PRIORITY[src_name]
    cur = conn.cursor()
    batch = []
    n = 0
    n_year_path = 0
    n_year_unpay = 0
    n_no_year = 0
    n_no_id = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            m_id = ID_RE.search(fn)
            if not m_id:
                n_no_id += 1
                continue
            osti_id = m_id.group(1)
            rel = full[len(str(root))+1:] if full.startswith(str(root)) else full
            m_year = YEAR_RE.search(rel)
            year_from_path = int(m_year.group(1)) if m_year else None
            unpay_rec = unpay_map.get(osti_id)
            year_from_unpay = unpay_rec[0] if unpay_rec else None
            lab_from_unpay = unpay_rec[1] if unpay_rec else None
            doi_from_unpay = unpay_rec[2] if unpay_rec else None
            year = year_from_path or year_from_unpay
            bucket = "year_from_path" if year_from_path else ("year_from_unpay" if year_from_unpay else "year_unknown")
            if year_from_path: n_year_path += 1
            elif year_from_unpay: n_year_unpay += 1
            else: n_no_year += 1
            batch.append((osti_id, src_name, pri, full, size,
                          year_from_path, year_from_unpay, lab_from_unpay, doi_from_unpay,
                          year, bucket))
            n += 1
            if len(batch) >= 5000:
                cur.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
                conn.commit()
                batch.clear()
                if n % 20000 == 0:
                    print(f"    {n} files... ({time.time()-t0:.1f}s)", flush=True)
    if batch:
        cur.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
    print(f"  {src_name}: {n} files in {time.time()-t0:.1f}s")
    print(f"    year_from_path={n_year_path}  year_from_unpay={n_year_unpay}  year_unknown={n_no_year}  no_id={n_no_id}")
    return n

def main():
    print("Initializing audit DB:", AUDIT)
    conn = init_db()
    unpay_map = load_unpay_map()
    total = 0
    for src in ["osti_fulltext", "osti_fulltext_unpay", "osti_fulltext_v2"]:
        root = BASE / src
        if not root.exists():
            print(f"  [SKIP] {root} does not exist")
            continue
        total += scan_source(src, root, unpay_map, conn)
    print(f"\nTotal file rows: {total}")
    print("\n=== Per-source, per-year-bucket summary ===")
    for src, bucket, cnt in conn.execute("""
        SELECT source, bucket, COUNT(*) FROM files GROUP BY source, bucket ORDER BY source, bucket
    """):
        print(f"  {src:25s} {bucket:20s} {cnt:>8d}")
    print("\n=== Unique OSTI IDs (canonical-pick preview) ===")
    for src, cnt in conn.execute("""
        SELECT source, COUNT(*) FROM (
            SELECT osti_id, source, MIN(priority) OVER (PARTITION BY osti_id) AS best
            FROM files
        ) WHERE priority = best GROUP BY source ORDER BY source
    """):
        print(f"  {src:25s} would contribute {cnt:>8d} canonical files")
    total_unique = conn.execute("SELECT COUNT(DISTINCT osti_id) FROM files").fetchone()[0]
    print(f"\nTotal unique OSTI IDs across all sources: {total_unique}")
    print("\n=== Year distribution (after path+unpay resolution) ===")
    for year, cnt in conn.execute("""
        SELECT year, COUNT(DISTINCT osti_id) FROM files GROUP BY year ORDER BY year
    """):
        ylabel = str(year) if year else "UNKNOWN"
        print(f"  {ylabel:>8s} {cnt:>7d}")
    conn.close()
    print(f"\nWrote audit DB: {AUDIT}")

if __name__ == "__main__":
    main()
