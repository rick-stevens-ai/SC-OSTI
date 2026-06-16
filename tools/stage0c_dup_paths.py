#!/usr/bin/env python3
"""
Stage 0c: capture duplicate path rows that stage0_audit.py collapsed via PRIMARY KEY.
Builds a second table: duplicate_paths(osti_id, source, path, size) for every extra path
beyond the first one seen for a given (osti_id, source). Stage 4 will archive these.
"""
import os, re, sqlite3, sys, time
from pathlib import Path

BASE = Path("/Volumes/Cherry6TB")
AUDIT = BASE / "osti_corpus/_audit/inventory.sqlite"

ID_RE = re.compile(r"(\d+)\.pdf$", re.IGNORECASE)

conn = sqlite3.connect(AUDIT)
conn.executescript("""
DROP TABLE IF EXISTS duplicate_paths;
CREATE TABLE duplicate_paths (
    osti_id TEXT,
    source TEXT,
    path TEXT,
    size INTEGER,
    PRIMARY KEY (osti_id, source, path)
);
CREATE INDEX idx_dup_osti ON duplicate_paths(osti_id);
""")

# Load known canonical paths from files table
canon = {}
for osti_id, source, path in conn.execute("SELECT osti_id, source, path FROM files"):
    canon[(osti_id, source)] = path

dup_count = 0
for src in ["osti_fulltext", "osti_fulltext_unpay", "osti_fulltext_v2"]:
    root = BASE / src
    if not root.exists():
        continue
    print(f"Scanning {src} for extra paths...", flush=True)
    t0 = time.time()
    seen_in_src = {}  # osti_id -> first path seen
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"):
                continue
            m = ID_RE.search(fn)
            if not m:
                continue
            osti_id = m.group(1)
            full = os.path.join(dirpath, fn)
            canon_path = canon.get((osti_id, src))
            if canon_path and full != canon_path:
                # This is a duplicate path for the same (id, source)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                conn.execute("INSERT OR REPLACE INTO duplicate_paths VALUES (?,?,?,?)",
                             (osti_id, src, full, size))
                dup_count += 1
    conn.commit()
    print(f"  {src}: scanned in {time.time()-t0:.1f}s", flush=True)

print(f"\nTotal extra paths captured: {dup_count}")

print("\n=== Duplicate paths by source ===")
for src, n in conn.execute("SELECT source, COUNT(*) FROM duplicate_paths GROUP BY source"):
    print(f"  {src:25s} {n:>5d} extra paths")

print("\n=== Sample 5 duplicates ===")
for row in conn.execute("SELECT osti_id, source, path, size FROM duplicate_paths LIMIT 5"):
    print(f"  {row}")

conn.close()
