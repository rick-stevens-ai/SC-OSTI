#!/usr/bin/env python3
"""Stage 0b: summary queries against the inventory DB (after stage0_audit.py crashed on column ref)."""
import sqlite3
from pathlib import Path

AUDIT = Path("/Volumes/Cherry6TB/osti_corpus/_audit/inventory.sqlite")
conn = sqlite3.connect(AUDIT)

print("=== Total rows ===")
n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
print(f"  {n} file rows")

print("\n=== Per-source × bucket ===")
for src, bucket, cnt in conn.execute("""
    SELECT source, bucket, COUNT(*) FROM files GROUP BY source, bucket ORDER BY source, bucket
"""):
    print(f"  {src:25s} {bucket:20s} {cnt:>8d}")

print("\n=== Canonical-pick preview (lowest priority wins per osti_id) ===")
for src, cnt in conn.execute("""
    SELECT f.source, COUNT(*) FROM files f
    JOIN (SELECT osti_id, MIN(priority) AS best FROM files GROUP BY osti_id) b
      ON f.osti_id = b.osti_id AND f.priority = b.best
    GROUP BY f.source ORDER BY f.source
"""):
    print(f"  {src:25s} would contribute {cnt:>8d} canonical files")

uniq = conn.execute("SELECT COUNT(DISTINCT osti_id) FROM files").fetchone()[0]
print(f"\nTotal unique OSTI IDs across all sources: {uniq}")

print("\n=== Year distribution (DISTINCT osti_id after path+unpay resolution) ===")
for year, cnt in conn.execute("""
    SELECT year, COUNT(DISTINCT osti_id) FROM files GROUP BY year ORDER BY year
"""):
    ylabel = str(year) if year else "UNKNOWN"
    print(f"  {ylabel:>8s} {cnt:>7d}")

print("\n=== IDs still year_unknown after unpay join (need OSTI API in Stage 1) ===")
unk = conn.execute("""
    SELECT COUNT(DISTINCT osti_id) FROM files WHERE year IS NULL
""").fetchone()[0]
print(f"  {unk} OSTI IDs need API lookup")

print("\n=== Source overlap (how many sources hold each osti_id) ===")
for n_src, cnt in conn.execute("""
    SELECT n_src, COUNT(*) FROM (
        SELECT osti_id, COUNT(DISTINCT source) AS n_src FROM files GROUP BY osti_id
    ) GROUP BY n_src ORDER BY n_src
"""):
    print(f"  appears in {n_src} source(s): {cnt:>7d} OSTI IDs")

print("\n=== Duplicate path rows (same osti_id+source, multiple files — fulltext 2016 nesting?) ===")
dup_path = conn.execute("""
    SELECT source, COUNT(*) - COUNT(DISTINCT osti_id) AS dups FROM files GROUP BY source
""").fetchall()
for src, dups in dup_path:
    print(f"  {src:25s} {dups:>5d} extra path rows (multi-path same id)")

conn.close()
