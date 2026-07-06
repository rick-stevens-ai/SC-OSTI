#!/usr/bin/env python3
"""
Import Stage 0 inventory (osti_corpus/_audit/inventory.sqlite) into the canonical
catalog.sqlite file_instances table. Idempotent (ON CONFLICT(path) DO UPDATE).

Also pulls the duplicate_paths rows from Stage 0c so every disk file is represented.
"""
import sqlite3, os, sys
from datetime import datetime
from pathlib import Path

AUDIT = "/Volumes/SG-1-8TB/osti/catalog/inventory.sqlite"
CATALOG = "/Volumes/SG-1-8TB/osti/catalog/catalog.sqlite"

now = datetime.utcnow().isoformat() + "Z"

src = sqlite3.connect(AUDIT)
dst = sqlite3.connect(CATALOG)
dst.execute("PRAGMA journal_mode=WAL")

added = 0
existed = 0
batch = []
BATCH_SIZE = 5000

# 1. Canonical files from the files table
print("Importing files table from inventory.sqlite...")
rows = src.execute("SELECT osti_id, source, path, size FROM files").fetchall()
print(f"  {len(rows):,} canonical file rows")
for osti_id, source, path, size in rows:
    batch.append((osti_id, source, path, size, now, now))
    if len(batch) >= BATCH_SIZE:
        dst.executemany("""
            INSERT INTO file_instances (osti_id, source, path, size, first_seen_ts, last_verified_ts)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              osti_id=excluded.osti_id,
              source=excluded.source,
              size=excluded.size,
              last_verified_ts=excluded.last_verified_ts
        """, batch)
        dst.commit()
        added += len(batch)
        batch = []
        if added % 25000 == 0:
            print(f"  ... {added:,} rows")
if batch:
    dst.executemany("""
        INSERT INTO file_instances (osti_id, source, path, size, first_seen_ts, last_verified_ts)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
          osti_id=excluded.osti_id, source=excluded.source, size=excluded.size, last_verified_ts=excluded.last_verified_ts
    """, batch)
    dst.commit()
    added += len(batch)
print(f"  imported {added:,} canonical rows")

# 2. Duplicate paths from duplicate_paths table
print("Importing duplicate_paths table...")
dup_rows = src.execute("SELECT osti_id, source, path, size FROM duplicate_paths").fetchall()
print(f"  {len(dup_rows):,} duplicate file rows")
batch = []
dup_added = 0
for osti_id, source, path, size in dup_rows:
    batch.append((osti_id, source, path, size, now, now))
    if len(batch) >= BATCH_SIZE:
        dst.executemany("""
            INSERT INTO file_instances (osti_id, source, path, size, first_seen_ts, last_verified_ts)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              osti_id=excluded.osti_id, source=excluded.source, size=excluded.size, last_verified_ts=excluded.last_verified_ts
        """, batch)
        dst.commit()
        dup_added += len(batch)
        batch = []
if batch:
    dst.executemany("""
        INSERT INTO file_instances (osti_id, source, path, size, first_seen_ts, last_verified_ts)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
          osti_id=excluded.osti_id, source=excluded.source, size=excluded.size, last_verified_ts=excluded.last_verified_ts
    """, batch)
    dst.commit()
    dup_added += len(batch)
print(f"  imported {dup_added:,} duplicate rows")

# 3. Summary
total = dst.execute("SELECT COUNT(*) FROM file_instances").fetchone()[0]
unique_ids = dst.execute("SELECT COUNT(DISTINCT osti_id) FROM file_instances").fetchone()[0]
per_source = dst.execute("SELECT source, COUNT(*) FROM file_instances GROUP BY source ORDER BY 2 DESC").fetchall()
overlap_2 = dst.execute("""
    SELECT COUNT(*) FROM (
        SELECT osti_id FROM file_instances GROUP BY osti_id HAVING COUNT(*) >= 2
    )
""").fetchone()[0]
print(f"\n=== file_instances summary ===")
print(f"  Total rows: {total:,}")
print(f"  Unique osti_ids: {unique_ids:,}")
print(f"  Per source:")
for s, c in per_source:
    print(f"    {s:30s} {c:>8,}")
print(f"  IDs with 2+ instances (overlap candidates): {overlap_2:,}")

src.close()
dst.close()
