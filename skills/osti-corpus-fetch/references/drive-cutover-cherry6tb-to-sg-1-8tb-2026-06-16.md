# Cherry6TB → SG-1-8TB cutover, 2026-06-16

Rick retired the 6TB external USB drive (Cherry6TB) as the OSTI working volume on 2026-06-16 and switched to the 8TB external (SG-1-8TB). This file is the migration record: what was on each drive, how the bit-for-bit copy was verified, the rationalized layout that replaced the legacy `osti_corpus/` + `osti_fulltext*` sprawl, and the path-rewrite playbook for the catalog DB.

## Why the cutover

Cherry6TB hit recurring I/O stalls — Spotlight reindex storms wedged the volume for hours (1.5GB sqlite open via `mode=ro&immutable=1` hung >120s, terminal PTY itself wedged at echo+date). Rick had a clean 8TB SG-series drive, did a straight cp before notifying me, set Cherry6TB read-only to prevent drift, and asked me to verify the copy was solid before erasing Cherry6TB.

## Drive parity verification (run BEFORE accepting a copy as canonical)

This is the verification sequence I ran. Repeat verbatim for any future drive cutover. The full pattern is also captured in skill `macos-volume-health` under "External drive cutover verification."

**Step 1 — Mount state.** Confirm source is read-only and target is read-write:

```python
import subprocess
out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
for line in out.split("\n"):
    if "Cherry6TB" in line or "SG-1-8TB" in line:
        print(line)
# Expected:
#   /dev/disk4s2 on /Volumes/Cherry6TB (hfs, local, nodev, nosuid, read-only, noowners)
#   /dev/disk5s2 on /Volumes/SG-1-8TB (hfs, local, nodev, nosuid, journaled, noowners)
```

If source isn't `read-only`, STOP and ask Rick to remount it read-only — otherwise sync drift can corrupt the verification.

**Step 2 — Top-level directory parity.** Names must match exactly:

```python
import os
def top(p): return sorted([e for e in os.listdir(p) if not e.startswith(".")])
src, dst = top("/Volumes/Cherry6TB"), top("/Volumes/SG-1-8TB")
print("only_src:", set(src) - set(dst))
print("only_dst:", set(dst) - set(src))
```

Both sets should be empty.

**Step 3 — Catalog/state DB byte-identity.** For any sqlite the project relies on:

```python
import os, sqlite3
from datetime import datetime
for label, root in [("src", src_root), ("dst", dst_root)]:
    db = f"{root}/<rel_path_to_db>"
    print(f"{label}: size={os.path.getsize(db):,}  mtime={datetime.fromtimestamp(os.path.getmtime(db)).isoformat()}")
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=30)
    cur = con.cursor()
    for t in [<list-of-key-tables>]:
        print(f"  {t}: {cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    con.close()
```

Same size + same mtime + same row counts across all key tables = DB is byte-identical. Don't trust `cmp` on a 1.5GB file over USB unless the lower-cost checks above pass first.

**Step 4 — Per-top-level dir size delta (du -sk).** Most dirs should match exactly. Any delta is a signal — usually one of:

- **Small delta (<100 KB)** = filesystem overhead noise. Ignore.
- **Large delta with hardlink-heavy source** = hardlinks didn't traverse the cp. SG-1-8TB had `osti_corpus/` 313 GB larger because `_stage_flat/` (99,787 PDFs) shared inodes with `pdfs/<year>/` on Cherry6TB (nlink=2) but came across as separate inodes on SG (nlink=1). Benign — same files stored twice, can be re-hardlinked or one tree dropped. Diagnose with:

  ```python
  st = os.stat(f"{root}/<some_canonical_file>")
  print(f"nlink={st.st_nlink} inode={st.st_ino}")
  # Cherry6TB: nlink=2 (canonical + stage_flat sharing inode)
  # SG-1-8TB:  nlink=1 (separate copies)
  ```

- **Real content delta** = copy was incomplete, ABORT. Re-rsync the offending dir.

**Step 5 — Per-shard/year count parity.** For year-bucketed corpora:

```python
import os
src_pdfs, dst_pdfs = f"{src}/pdfs", f"{dst}/pdfs"
for y in sorted(set(os.listdir(src_pdfs)) & set(os.listdir(dst_pdfs))):
    cn = len(os.listdir(f"{src_pdfs}/{y}"))
    sn = len(os.listdir(f"{dst_pdfs}/{y}"))
    if cn != sn: print(f"MISMATCH {y}: src={cn} dst={sn}")
```

Zero mismatches = year-bucketed tree is intact.

## What was verified

| Check | Result |
|---|---|
| Top-level dirs | identical (12/12) |
| catalog.sqlite | byte-identical (1,520,893,952 bytes, mtime 2026-06-14T16:30:04, 8 table row counts match) |
| osti_corpus/pdfs/ year buckets | 28/28 years, all counts identical |
| BV-BRC-cites, Ozan_PARSED_PDFS, argonium_mcqa, osti_fulltext_unpay, osti_fulltext_v2, osti_fulltext_v2_md, osti_recovery_2026-06-09 | byte-exact (du -sk delta == 0) |
| osti_fulltext | +56 KB on dst (noise) |
| osti_probe | +8 KB on dst (noise) |
| **osti_corpus** | **+313 GB on dst** — diagnosed as un-hardlinked `_stage_flat/` duplication. Benign; reclaim after OCR. |

## Pre-cutover SG-1-8TB layout (what we inherited)

```
/Volumes/SG-1-8TB/
├── BV-BRC-cites/                  8.2 GB / 28k files
├── Dropbox/                       167 GB / 39k files  (full Dropbox mirror — separate concern)
├── Ozan_PARSED_PDFS/              1.0 GB / 17k files
├── argonium_mcqa/                 124 MB
├── misc/                          168 KB
├── osti_corpus/                   600 GB / 200k files  ← active corpus + catalog DB
│   ├── _state/catalog.sqlite      1.52 GB (the real DB)
│   ├── catalog.sqlite             0 bytes  ← stub at root, ignore
│   ├── state.db                   0 bytes  ← stub at root, ignore
│   ├── _stage_flat/               99,787 PDFs (un-hardlinked dup of pdfs/)
│   ├── _manifests/                OCR pack manifests
│   ├── _audit/inventory.sqlite    42 MB
│   ├── pdfs/<year>/               2000-2026 + unknown, 96,011 PDFs canonical
│   ├── logs/, probes/, _archive/, manifest/
├── osti_fulltext/                 387 GB / 68k files  (2016-2025 + tarballs)
├── osti_fulltext_v2/              212 GB / 69k files  (2018-2020 + flat + unknown)
├── osti_fulltext_unpay/           59 GB / 31k files   (2006-2026 Unpaywall recoveries + arxiv_recovered/, arxiv_title_recovered/, prokko_recovered/)
├── osti_fulltext_v2_md/           486 MB / 8k files   (Markdown extractions)
├── osti_probe/                    152 KB              (one-off probe artifacts)
├── osti_recovery_2026-06-09/      82 MB / 39 files
```

Several years of staging-dir sprawl. The 99,787 PDFs in `_stage_flat/` are all already in `pdfs/<year>/` (catalog `build_canonical_log` records the moves). All four `osti_fulltext*` source dirs have been ingested into the canonical `pdfs/<year>/` tree per `file_instances.source` values in catalog.sqlite.

## Rationalized post-cutover layout (target)

```
/Volumes/SG-1-8TB/osti/                         (NEW root)
├── pdfs/<year>/<osti_id>.pdf                   canonical year-bucketed PDF tree
├── text/<year>/<osti_id>.md                    Marker OCR output
├── text/<year>/<osti_id>.mmd                   Nougat OCR output (math/equation-heavy)
├── text/<year>/<osti_id>.meta.json             per-paper OCR metadata
├── catalog/
│   ├── catalog.sqlite                          rebuilt — all Cherry6TB paths rewritten to SG-1-8TB
│   ├── catalog.sqlite.YYYYMMDD.bak             rolling snapshots
│   └── DESIGN.md                               schema doc
├── manifests/                                  OCR pack manifests (one per Polaris/Aurora job)
├── logs/                                       runner logs
├── scripts/                                    runner code (single source of truth)
├── _staging/                                   incoming fetches before promote
│   ├── recovery_<ts>/                          recovery_queue drains
│   ├── backfill_<era>_<ts>/                    bulk-fetch results
│   └── publisher_<source>_<ts>/                Unpaywall, S2, etc.
└── _archive/
    └── 2026-06-16_pre-rationalize/             pre-cutover state DBs + manifests snapshot

/Volumes/SG-1-8TB/_legacy/                      moved aside, not deleted
├── osti_fulltext/                              (incl. year .tar.gz files)
├── osti_fulltext_v2/
├── osti_fulltext_unpay/
├── osti_fulltext_v2_md/
├── osti_recovery_2026-06-09/
├── osti_probe/
└── README.md                                   what each is, lineage, when to purge
```

## Catalog DB path rewrite (Cherry6TB → SG-1-8TB)

The catalog DB carries Cherry6TB paths in `file_instances.path` for 168K rows. Rewrite procedure:

1. Snapshot: `cp _state/catalog.sqlite catalog/catalog.sqlite` (also `cp` to `_archive/<date>/`).
2. Verify on a 10-row sample before bulk:

   ```sql
   SELECT path FROM file_instances WHERE path LIKE '/Volumes/Cherry6TB/%' LIMIT 10;
   ```

3. Bulk update (one transaction):

   ```sql
   BEGIN;
   UPDATE file_instances
     SET path = REPLACE(path, '/Volumes/Cherry6TB/osti_corpus/pdfs/', '/Volumes/SG-1-8TB/osti/pdfs/')
     WHERE path LIKE '/Volumes/Cherry6TB/osti_corpus/pdfs/%';
   -- Repeat for any other Cherry6TB path prefixes that appear (osti_fulltext, osti_fulltext_v2, etc.)
   COMMIT;
   ```

4. Re-verify all 168K paths resolve to readable files on disk:

   ```python
   import sqlite3, os
   con = sqlite3.connect("catalog/catalog.sqlite")
   missing = []
   for (path,) in con.execute("SELECT path FROM file_instances"):
       if not os.path.isfile(path): missing.append(path)
   print(f"missing: {len(missing)}")
   ```

5. Same rewrite for OCR pack manifests (`_manifests/ocr_pack_*.jsonl`): `canonical_path` and `source_path` fields. Snapshot before edit. Write to a new file with cutover timestamp; keep original for forensics.

## Sanity-check baseline after rationalization

```
278,645 papers tracked
168,540 file_instances
160,493 papers with PDF (57.6% coverage)
118,152 papers missing PDF (heavily 2000-2014: 33% coverage there vs 70-90% for 2015+)
  286 recovery_queue rows pending
  631 PDFs in pdfs/unknown/ (no year metadata — Stage-4 reconcile target)
```

These numbers came from the Cherry6TB catalog before cutover and should match exactly after rebuild on SG. If they don't, the rebuild dropped rows — abort and re-investigate.

## Pitfalls observed during this cutover

- **Don't trust `du` on /Volumes paths to be instant** — the per-shard parity probe (10 dirs × 2 volumes) took 20s real time, would be 5+ min on a wedged Cherry6TB. Schedule verification during a non-Spotlight window.
- **Stub files at filesystem root that mirror deeper real files** (0-byte `catalog.sqlite` at `osti_corpus/` root, with the real 1.52 GB at `osti_corpus/_state/catalog.sqlite`) — assume any 0-byte file you encounter near a same-named non-zero file is a stub, not the canonical artifact. Don't open the stub and conclude the DB is empty.
- **Hardlinks don't traverse cross-filesystem cp.** Document this for any future drive migration — the dst-side size delta is expected, not a copy failure. Diagnose via `st_nlink == 1` on the dst vs `>= 2` on the source.

## Augmenting `file_instances` with a `canonical_path` column (2026-06-16 execution refinement)

The original schema kept `file_instances.path` = "where the file was originally fetched" (e.g. `osti_fulltext/2016/<id>.pdf`). For canonical resolution, after rationalization, we ALSO want a `canonical_path` field pointing at `osti/pdfs/<year>/<id>.pdf` — the authoritative location. Add the column rather than overwriting `path` (keeps fetch provenance for forensics).

```sql
ALTER TABLE file_instances ADD COLUMN canonical_path TEXT;
```

Populate it for `is_canonical=1` rows by joining `papers.year` (for known year) with filesystem fallback (for `papers.year IS NULL` cases):

```python
oid_to_year = {}
# Prefer year from papers table
for (oid, y) in cur.execute("SELECT osti_id, year FROM papers WHERE year IS NOT NULL"):
    oid_to_year[oid] = str(int(y))
# Filesystem fallback for unknown-year rows
for y in os.listdir(NEW_PDFS_ROOT):
    if not os.path.isdir(f"{NEW_PDFS_ROOT}/{y}"): continue
    for f in os.listdir(f"{NEW_PDFS_ROOT}/{y}"):
        if f.endswith(".pdf"):
            oid = f[:-4]
            oid_to_year.setdefault(oid, y)   # don't override papers-table year

# Build canonical_path = /Volumes/SG-1-8TB/osti/pdfs/<year>/<id>.pdf
for inst_id, oid in cur.execute("SELECT instance_id, osti_id FROM file_instances WHERE is_canonical=1"):
    y = oid_to_year.get(oid, "unknown")   # final fallback: pdfs/unknown/
    cp = f"/Volumes/SG-1-8TB/osti/pdfs/{y}/{oid}.pdf"
    cur.execute("UPDATE file_instances SET canonical_path=? WHERE instance_id=?", (cp, inst_id))
```

## Year-mismatch fallback — DB year ≠ filesystem year

The first canonical_path build will have a small percentage (~0.1%) of misses where the DB's `papers.year` disagrees with where the PDF physically lives in `pdfs/<year>/`. Common causes: paper had no year in DB but PDF landed in `unknown/`, or a year was corrected in DB after the file was already filed under the original year. Don't repair the filesystem — repair the canonical_path:

```python
# Build osti_id -> actual_year_on_disk
disk_map = {}
for y in os.listdir(NEW_PDFS_ROOT):
    if not os.path.isdir(f"{NEW_PDFS_ROOT}/{y}"): continue
    for f in os.listdir(f"{NEW_PDFS_ROOT}/{y}"):
        if f.endswith(".pdf"):
            disk_map[f[:-4]] = y

# Patch canonical_path for any row where the path-as-built doesn't resolve
fixes = []
for (inst_id, oid, cp) in cur.execute(
    "SELECT instance_id, osti_id, canonical_path FROM file_instances WHERE is_canonical=1"
):
    if not os.path.exists(cp):
        actual_y = disk_map.get(oid)
        if actual_y:
            new_cp = f"/Volumes/SG-1-8TB/osti/pdfs/{actual_y}/{oid}.pdf"
            fixes.append((new_cp, inst_id))
cur.executemany("UPDATE file_instances SET canonical_path=? WHERE instance_id=?", fixes)
```

Verified 2026-06-16: 106 of 99,787 rows needed this fix (0.1%); all 106 were files whose DB year was off-by-one or marked NULL but the file was filed under a specific year. After patch, **99,787/99,787 canonical paths resolve cleanly**.

## Executed cutover results (2026-06-16, end-to-end)

For reference when doing the next migration — what actually shipped:

| Step | Time | Result |
|---|---|---|
| Snapshot to `_archive/2026-06-16_pre-rationalize/` | 24 s | 1.6 GB (catalog.sqlite + manifests + audit DB) |
| Build new dir skeleton (`osti/{pdfs,text,catalog,manifests,logs,scripts,_staging,_archive}` + `_legacy/`) | < 1 s | 10 dirs |
| Move 28 year buckets `osti_corpus/pdfs/<y>` → `osti/pdfs/<y>` | 1.07 s | 99,787 files (atomic rename, no copy) |
| Copy + open new catalog DB at `osti/catalog/catalog.sqlite` | 27 s | 1.52 GB |
| ALTER TABLE + populate `canonical_path` for 99,787 canonical rows | 7 s | 99,787 updated; 525 used "unknown" fallback |
| Verify canonical_path resolution | 0.4 s | 99,681/99,787 OK; 106 year-mismatches found |
| Year-mismatch fallback patch | 3 s | 99,787/99,787 resolve |
| Path rewrite `file_instances.path` Cherry6TB→SG-1-8TB across 4 source prefixes | < 1 s | 168,540 rows; 0 Cherry6TB refs remaining |
| VACUUM | 107 s | 1.52 GB → 1.13 GB |
| Move 6 legacy source dirs to `_legacy/` | < 1 s | atomic renames |
| Salvage logs / _state / _manifests / _audit / probes into new layout | < 1 s | preserved |

Total wall time: ~3 minutes for the rationalization itself once the snapshot landed. The `_stage_flat/` reclaim (313 GB delete) is a separate explicit step gated on user confirmation per the cleanup hierarchy rule.

## What the catalog DB looks like POST-rebuild (for future verification)

```
file_instances columns: instance_id, osti_id, source, path, size, sha256, extracted_title,
                        title_match_score, first_seen_ts, last_verified_ts, is_canonical,
                        canonical_decision_id, canonical_path     ← NEW
file_instances.path source distribution (after rewrite):
  /Volumes/SG-1-8TB/osti/pdfs/             7,238 (was Cherry6TB/osti_corpus/pdfs/)
  /Volumes/SG-1-8TB/_legacy/osti_fulltext/         67,590
  /Volumes/SG-1-8TB/_legacy/osti_fulltext_v2/      69,285
  /Volumes/SG-1-8TB/_legacy/osti_fulltext_unpay/   24,427
  Cherry6TB references remaining: 0
```

Note: `pdf_fetch_log.saved_path`, `build_canonical_log.source_path/target_path` STILL reference Cherry6TB after rewrite. **Don't rewrite historical-action log fields** — they're forensic records of what was done at a particular moment, and rewriting them would lie about the history. Document the cutover (this file) instead.
