# Multi-source OSTI consolidation onto Cherry6TB (2026-06-13)

When the Cherry6TB OSTI store has accumulated **multiple parallel source dirs** from different
fetch generations (legacy + v2 + Unpaywall fallback + manual recoveries), the pre-OCR step is
**consolidate metadata + flatten to a single canonical layout** before any large compute pass.

This reference captures the **4-stage gated consolidation pattern** that works when:

- Sources have different year-encoding shapes (some encode year in the path, some in a sidecar DB, some unknown)
- Sources overlap (same OSTI ID present in 2+ sources at different fetch quality)
- You need a single `osti_corpus/pdfs/<year>/<id>.pdf` flat-by-year layout as input to downstream OCR

The pattern is **reversible** (originals archived, never deleted) and **safe to re-run** (read-only audit + idempotent hardlink build).

## Hard rule — workflow ordering (Rick, 2026-06-13)

> "Fix the metadata and consolidate before doing anything else"

Consolidation comes BEFORE any large-scale downstream pass (OCR, classification, extraction).

The temptation when an OCR pipeline (Polaris, uicgpu, Aurora) is staged and ready is to launch it against the existing fragmented sources. **Don't.** Each source has different year-encoding quality, different fetch-failure profiles, and overlapping IDs at different quality levels. An OCR pass against the fragmented state produces:

- Duplicate work on overlapping IDs (same paper OCR'd from 2+ sources)
- Output organized by source-of-origin instead of by year (downstream xCard joining breaks)
- Stale year tags from source-of-truth confusion (unpay DB year vs path-encoded year vs unknown)

Consolidate first, then OCR against the single canonical store. The pause is hours; the do-over is days.

## Source landscape (canonical, as of 2026-06-13)

Cherry6TB `/Volumes/Cherry6TB/` typically holds 4 osti_* dirs simultaneously:

| Dir | Count | Year encoding | Notes |
|-----|-------|---------------|-------|
| `osti_fulltext/` | 67,590 PDFs | path: `YYYY/YYYY/ID.pdf` (doubly nested for 2017-2025) or `YYYY/ID.pdf` (2016 mixed) | Canonical, year-organized; 470 byte-identical dups in 2016 flat-vs-nested. **Source priority 1.** |
| `osti_fulltext_v2/` | ~10K-67K PDFs (varies during sweep) | path: mostly `:00Z/ID.pdf` or flat `ID.pdf`; few `YYYY/ID.pdf` | Newer fetch from cels-rbdgx2. **No year in path** for ~95% of files. **Source priority 2.** |
| `osti_fulltext_unpay/` | 24,427 PDFs | path: `YYYY/ID.pdf` (clean) + special subdirs `prokko_recovered/`, `arxiv_title_recovered/`, `arxiv_recovered/` | **Has `_state/unpaywall_overnight.db` with year+lab+doi metadata** (canonical for join-back). Covers 2006-2015 (those years are absent from `osti_fulltext/`). **Source priority 3.** |
| `osti_fulltext_v2_md/` | 0 PDFs | n/a — holds `<id>.md` + `<id>.json` sidecars from prior Marker OCR pass | Joins by `osti_id`. Not consolidated into PDFs/; mirrored separately by `marker_mirror.sh`. |

Source priority rule: **fulltext > v2 > unpay**. Reasoning: `osti_fulltext/` is canonical year-organized; `v2` is newer fetch (different recovery path, may have re-fetched 0-byte fulltext entries); `unpay` last because of its `recovered/` subdirs (less clean provenance).

## Target layout

```
/Volumes/Cherry6TB/osti_corpus/
├── pdfs/<year>/<id>.pdf      ← hardlinks pointing at the canonical source file per OSTI ID
├── manifest/                  ← derivative manifests (per-year, per-lab, etc.)
├── logs/                      ← stage logs
├── _archive/                  ← original source dirs moved here after Stage 4 (NEVER deleted)
├── _state/                    ← stage scripts + intermediate state
│   └── stage0_audit.py
└── _audit/                    ← read-only audit artifacts
    └── inventory.sqlite       ← Stage 0 output, gates Stages 1-4
```

## Stage 0: read-only inventory (gates everything else)

Walk all 4 source dirs, build a SQLite inventory with one row per (osti_id, source). For each file, resolve year by:

1. **year_from_path** — regex `(?:^|/)(20\d{2})(?:/|$)` against the relative path
2. **year_from_unpay** — join against `osti_fulltext_unpay/_state/unpaywall_overnight.db` recovery table (`{osti_id: (year, lab, doi)}`)
3. **year** = path-year if present else unpay-year else `NULL` (UNKNOWN bucket — feeds Stage 1)

Schema:

```sql
CREATE TABLE files (
    osti_id TEXT,
    source TEXT,                  -- 'osti_fulltext' | 'osti_fulltext_v2' | 'osti_fulltext_unpay'
    priority INTEGER,             -- 1 | 2 | 3
    path TEXT,                    -- absolute path on Cherry6TB
    size INTEGER,
    year_from_path INTEGER,
    year_from_unpay INTEGER,
    lab_from_unpay TEXT,
    doi_from_unpay TEXT,
    year INTEGER,                 -- resolved (year_from_path or year_from_unpay)
    bucket TEXT,                  -- 'year_from_path' | 'year_from_unpay' | 'year_unknown'
    PRIMARY KEY (osti_id, source)
);
CREATE INDEX idx_osti_id ON files(osti_id);
CREATE INDEX idx_year ON files(year);
CREATE INDEX idx_source ON files(source);
```

Canonical-pick preview SQL (which source would Stage 2 hardlink from, per OSTI ID):

```sql
SELECT osti_id, source, path, year
FROM (
    SELECT osti_id, source, path, year, priority,
           MIN(priority) OVER (PARTITION BY osti_id) AS best
    FROM files
)
WHERE priority = best;
```

Stage 0 outputs:
- `_audit/inventory.sqlite` — full table
- `logs/stage0_audit.log` — per-source counts, per-year distribution, year-bucket summary

Per-source year-bucket summary tells you the size of the Stage 1 lookup queue (`year_unknown` rows).

Production runtime: ~5-10 min on Cherry6TB HFS for ~103K PDFs across 3 trees. Single-threaded `os.walk` — don't parallelize, HFS catalog locks make it slower.

**Run script:** `/Volumes/Cherry6TB/osti_corpus/_state/stage0_audit.py` (template at end of this doc).

## Stage 1: OSTI API year lookup (only for IDs that survived Stage 0 with year=NULL)

For osti_fulltext_v2's ~10,000+ IDs whose path doesn't encode year AND aren't in the unpay DB, query OSTI per-ID metadata API at `https://www.osti.gov/api/v1/records/<id>`. Extract `publication_date`'s year.

Throttle: **the per-ID `/api/v1/records/<id>` endpoint is more aggressively rate-limited than the search endpoint**. Empirical smoke 2026-06-13 from m1 home:

- 1st request → HTTP 200, 0.17s
- 2nd consecutive (no delay) → **HTTP 429**
- 30 reqs at 0.5s sleep → 30/30 HTTP 200, no 429s

Use a 0.3s sleep (≈2 req/s effective with network latency) — **~85 min for 10K IDs**, not the "36 min at 5 req/s" earlier versions of this doc claimed. Always include exponential backoff on 429 (2/4/8/16s, 4 attempts) for the inevitable transient hits during a multi-hour run.

Run from m1 home (API works fine from home; this is metadata not PDF). Output rows go into the same `files` table (UPDATE WHERE year IS NULL). Idempotent via `WHERE api_status IS NULL OR api_status IN ('error','timeout')` — safe to interrupt and resume.

**Don't** run Stage 1 before Stage 0 completes — you'd be hitting the API for IDs that the unpay DB already knows about (~3K of them), wasting both your budget and OSTI's.

## Stage 2: hardlink build (idempotent, reversible)

For each unique OSTI ID, pick the canonical source per priority, link to `pdfs/<year>/<id>.pdf`:

```python
os.makedirs(f"pdfs/{year}", exist_ok=True)
target = f"pdfs/{year}/{osti_id}.pdf"
if not os.path.exists(target):
    os.link(source_path, target)   # hardlink — zero copy, same inode
```

Hardlinks chosen over symlinks/copies because:
- **Zero space overhead** (same inode, no new bytes)
- **Survive source-dir rename** (Stage 4 moves originals to `_archive/`; symlinks would break, hardlinks don't)
- **Same-volume only** — requires source + target on same filesystem (Cherry6TB ✓)
- **Atomic per file** (no half-state on interrupt)
- **Reversible** — unlink the hardlink, original is untouched

Stage 2 output: `pdfs/<year>/<id>.pdf` populated for every (resolved-year, OSTI-ID) pair.

UNKNOWN year handling: if Stage 1 fails to resolve year for some IDs (404 from OSTI API, e.g. retracted records), bucket them under `pdfs/UNKNOWN/<id>.pdf`. Don't drop them.

## Stage 3: SQLite manifest (single source of truth for downstream pipelines)

`osti_corpus/manifest/osti_corpus.sqlite` — the canonical store DB downstream pipelines query. Schema:

```sql
CREATE TABLE papers (
    osti_id TEXT PRIMARY KEY,
    year INTEGER,
    lab TEXT,
    doi TEXT,
    source TEXT,                  -- which dir provided the canonical PDF
    canonical_path TEXT,          -- absolute path under pdfs/<year>/<id>.pdf
    original_path TEXT,           -- absolute path in source dir (post-Stage 4: under _archive/)
    pdf_size INTEGER,
    pdf_sha256 TEXT,              -- optional, expensive to compute
    md_path TEXT,                 -- if Marker .md exists in osti_fulltext_v2_md/
    mmd_path TEXT,                -- if Nougat .mmd exists
    xcards_data INTEGER DEFAULT 0,  -- count of data-card xCards extracted
    xcards_model INTEGER DEFAULT 0,
    xcards_agent INTEGER DEFAULT 0,
    status TEXT,                  -- 'ready' | 'ocr_pending' | 'extraction_pending' | 'done'
    last_seen TIMESTAMP
);
CREATE INDEX idx_year ON papers(year);
CREATE INDEX idx_lab ON papers(lab);
CREATE INDEX idx_status ON papers(status);
```

Populate from inventory.sqlite (canonical pick) + join unpay DB for lab/doi + join `osti_fulltext_v2_md/` for sidecar paths.

## Stage 4: archive originals (keep, don't delete)

```
mv /Volumes/Cherry6TB/osti_fulltext           /Volumes/Cherry6TB/osti_corpus/_archive/
mv /Volumes/Cherry6TB/osti_fulltext_v2        /Volumes/Cherry6TB/osti_corpus/_archive/
mv /Volumes/Cherry6TB/osti_fulltext_unpay     /Volumes/Cherry6TB/osti_corpus/_archive/
mv /Volumes/Cherry6TB/osti_fulltext_v2_md     /Volumes/Cherry6TB/osti_corpus/_archive/
```

Hardlinks survive `mv` within the same volume because inodes don't move. After Stage 4:

- `osti_corpus/pdfs/<year>/<id>.pdf` is the canonical view
- `osti_corpus/_archive/<source>/...` is the original tree, intact
- Both point at the same inodes — no extra bytes used
- If anything goes wrong, `mv _archive/<src> ..` reverses it

**Don't `rm -rf` the originals.** Per Rick's cleanup hierarchy (vault `workflows/`), canonical artifacts always go to `_archive/`, not the trash, until an explicit replacement is locked in for at least a few days.

## Parallelism gate against in-progress fetches

If a cels→Cherry sweep (`rsync` from `cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2/`) is still running, the safe order is:

| Stage | Safe during sweep? | Why |
|-------|--------------------|-----|
| Stage 0 (audit) | ✓ idempotent — re-run after sweep completes | Read-only, no mutation. Walk sees whatever PDFs are present at scan time. |
| Stage 1 (year lookup) | ✓ but scope only what Stage 0 saw | API queries are wasted budget if Stage 0 missed half the v2 corpus. |
| Stage 2 (hardlink build) | ✗ defer until sweep done | Building hardlinks against a partial v2 means a re-run after sweep completes; wasted work. |
| Stage 3 (manifest) | ✗ defer until Stage 2 done | Manifest joins inventory.sqlite ⊕ canonical-pick — same partial-data problem. |
| Stage 4 (archive originals) | ✗ defer until 2+3 done | Moving the source dirs mid-sweep breaks the rsync target. |

Common pattern: launch Stage 0 in parallel with the sweep, post the audit numbers as a progress signal, hold Stages 2-4 for explicit Rick approval after sweep completes + audit is reviewed.

## Pitfalls

- **DON'T enumerate Cherry6TB root with `ls` / `find -maxdepth 1`** — HFS catalog lock hangs 60s+. Direct deep paths work fine. For Stage 0, use `os.walk` from each source root individually; never list `/Volumes/Cherry6TB/` itself.

- **Spotlight reindex stalls the WHOLE volume — including read-only immutable sqlite (Cherry6TB I/O wedge, 2026-06-14/15).** When multiple `mdworker_shared` PIDs spawn within the last few minutes (visible via `ps auxw | grep mdworker`), even `sqlite3 "file:...catalog.sqlite?mode=ro&immutable=1" ".tables"` hangs >120s on a 1.5GB DB. This is NOT a DB writer lock — there's no other process holding the file. It's pure volume I/O latency under Spotlight pressure. The Hermes terminal session itself wedges when its parent shell is stuck on a prior Cherry6TB op (`echo && date` will time out at 10s); switch to `execute_code` Python subprocess to bypass — host is alive, only the PTY is wedged. Standard triage ladder for any volume I/O stall:

  1. **Immediate small probe via Python subprocess**, NOT shell: `subprocess.run(["echo","hello"], timeout=5)` + `subprocess.run("ps auxw | grep mdworker | grep -v grep", shell=True, timeout=10)`. Confirms host alive and identifies Spotlight as wedger.
  2. **If terminal also wedged**, ONE bounded longer probe at 60-90s wall timeout against the target DB/file: `subprocess.run(['sqlite3', 'file:...?mode=ro&immutable=1', '.tables'], timeout=90)`.
  3. **If step 2 times out, mark Stage 0 `blocked/provisional`** with explicit structured flags. Don't stack more probes — every additional probe compounds the pressure:
     ```
     catalog_writer_active: false       # ps confirmed no active OSTI worker
     catalog_unreadable_timeout: true   # >120s immutable sqlite open
     volume_probe_degraded: true        # other Cherry6TB ops also timing out
     likely_cause: spotlight_reindex    # ps shows recent mdworker_shared PIDs
     source_changing: unknown           # can't audit without working sqlite
     ```
     `blocked/provisional with structured flags` is a valid Stage 0 output. Document the gate result, do NOT try to "push through" by killing processes or escalating to forced reads.
  4. **Back off 15-30 minutes** before retry. Spotlight reindex of a multi-TB volume can run minutes to hours; there's no progress signal short of `mdutil -s /Volumes/Cherry6TB`.
  5. **9+ minutes on a single sqlite/find probe = wedged, not slow.** Kill the probe with SIGTERM; don't wait for "progress." A real query against an unstressed catalog.sqlite at this size returns in <2s.

- **Don't disable Spotlight on Cherry6TB silently.** `sudo mdutil -i off /Volumes/Cherry6TB` is reversible and is the right answer if the volume is consistently wedged by reindex, but it's an OS-config change on a mounted volume — needs Rick's explicit go and his sudo. Don't propose it as a "while we're at it" cleanup; it's a deliberate decision.

- **Never delete WAL/SHM files by hand to "force-recover" an interrupted SQLite writer.** SQLite recovers on the owner process's next normal open. Manual WAL deletion can corrupt the DB. If a writer was interrupted (Ctrl-C, kill, host reboot), the next legitimate open will replay WAL and roll back uncommitted state automatically. The cost of waiting for that natural recovery is seconds; the cost of a corrupted catalog.sqlite is rebuilding from scratch over hours.

- **Reverse-lookup year from path requires the doubly-nested regex.** `osti_fulltext/2024/2024/1234567.pdf` has `2024` appear twice. Use `(?:^|/)(20\d{2})(?:/|$)` — captures either match. A loose `\d{4}` would catch the OSTI ID's first 4 digits and label every paper as year 1234.

- **Unpay DB schema is `recovery`, not `papers` or `osti_corpus`.** Columns: `osti_id, doi, year, lab, unpay_status, pdf_url, host_type, is_oa, fetch_status, bytes, path, ts`. `year` is INT, `osti_id` is TEXT. Index on `fetch_status`. The DB is at `/Volumes/Cherry6TB/osti_fulltext_unpay/_state/unpaywall_overnight.db`.

- **v2 sidecar JSON in `osti_fulltext_v2_md/<id>.json` does NOT carry year.** Schema is `{"id":"1000347","src":"/data/stevens/ocr_inbox/full/1000347.pdf","status":"ok","chars":21318,"images":9,"elapsed_s":194.06}`. The `src` field reveals the OCR worker host (uicgpu in this case), useful for "where did this OCR come from" — but year must come from the PDF's source dir lookup, not the .json.

- **Hardlinks require same filesystem.** Cherry6TB is one APFS volume so this works. Cross-volume hardlinks fail with `EXDEV: Cross-device link`. If consolidating across volumes (rare), fall back to symlinks + a path-canonicalizer in downstream consumers — at the cost of `mv`-fragility (Stage 4 would break symlinks).

- **Per-source priority can flip on a fetch generation.** Today fulltext>v2>unpay; if a future re-fetch lands much-cleaner PDFs in v2 (e.g. all 0-byte fulltext entries replaced), v2 should outrank fulltext. **Per OSTI ID, prefer the largest non-zero file when sources tie on quality** — the dedup audit on osti_fulltext 2016 found 470 byte-identical pairs but no quality-difference pairs. If a pair differs in size, prefer the larger.

- **Source-priority pick must also filter 0-byte files.** Some sources contain 0-byte sentinels from broken fetches. Stage 2 should pick the highest-priority **non-zero** file, not blindly the highest-priority match. SQL: `WHERE size > 0 ORDER BY priority ASC LIMIT 1` in the canonical-pick CTE.

- **Don't run Stage 1 before Stage 0.** Naive instinct is to start the slow API-bound lookup in parallel with the audit. But Stage 1's scope (which OSTI IDs need API year lookup) is **defined by Stage 0** (the year_unknown bucket). Without Stage 0, Stage 1 either queries every ID (wasteful: ~70% have year already from path) or guesses the scope wrong.

- **Stage 0 must include `size` column.** Even though it's slower (one extra `stat` per file), this is the only inventory pass — capture size now so Stage 2's pick-non-zero rule has the data. `os.path.getsize` is cheap on HFS; the cost is the directory walk itself, not the stat per file.

- **`os.walk` on Cherry6TB blocks single-threaded on the directory entries.** Stage 0 takes ~5-10 min for ~103K PDFs across 3 trees. Don't `multiprocessing.Pool` it — concurrent walks of the same volume cause HFS catalog lock contention and the parallel version is SLOWER. Trust the single-threaded walk; print progress every 20K files so the operator can see it's alive. **Empirical 2026-06-13**: 67,590 files in 0.8s + 24,427 in 0.8s + 11,178 in 0.3s = under 2 seconds total. Much faster than feared — HFS catalog is fine for `walk`, the slow-down is only `ls`/`find -maxdepth 1` at the volume root.

- **`INSERT OR REPLACE` on `(osti_id, source)` PRIMARY KEY silently drops duplicate paths within the same source.** The 470 byte-identical dups in `osti_fulltext` 2016 (`/2016/ID.pdf` flat vs `/2016/2016/ID.pdf` nested) and similar dups in unpay (811) and v2 (298) get collapsed to ONE inventory row, so Stage 4 archive won't know about the second path. **Fix: run a Stage 0c second pass** that re-walks each source and records every path NOT equal to the canonical one already in `files` into a separate `duplicate_paths(osti_id, source, path, size)` table. Then Stage 4 archives both the canonical and the dup paths. Empirical 2026-06-13: stage0c took ~0.1s per source (the os.walk results are still in OS cache from Stage 0), captured 1,579 extra paths that would otherwise be lost. The 2-pass cost is trivial; the alternative (changing Stage 0's PK to `(osti_id, source, path)`) bloats the file-instance count and breaks the canonical-pick query shape. Stage 0c template at end of this doc.

- **Reconcile counts after Stage 0 + 0c against on-disk file count BEFORE proceeding to Stage 1.** Stage 0 gives canonical-file rows; Stage 0c gives dup-path rows; their sum must equal `find <source> -name '*.pdf' | wc -l` exactly. Mismatch = silent dropped rows somewhere in the walk (broken symlink, permission error swallowed by try/except). Empirical 2026-06-13: 67,120 + 470 = 67,590 ✓ / 23,616 + 811 = 24,427 ✓ / 10,882 + 298 = 11,180 ✓ — all reconciled against `find` counts. **Don't run Stage 1 against an unreconciled inventory** — you'd be paying API budget on rows that may have phantom misses.

## Template: stage0_audit.py

See `_state/stage0_audit.py` in any project that uses this pattern. Key shape:

```python
#!/usr/bin/env python3
"""Stage 0: Read-only inventory of all 4 Cherry6TB osti_* sources."""
import os, re, sqlite3, sys, time
from pathlib import Path

BASE = Path("/Volumes/Cherry6TB")
AUDIT = BASE / "osti_corpus/_audit/inventory.sqlite"
UNPAY_DB = BASE / "osti_fulltext_unpay/_state/unpaywall_overnight.db"

SOURCE_PRIORITY = {
    "osti_fulltext": 1,
    "osti_fulltext_v2": 2,
    "osti_fulltext_unpay": 3,
}

YEAR_RE = re.compile(r"(?:^|/)(20\d{2})(?:/|$)")
ID_RE = re.compile(r"(\d+)\.pdf$", re.IGNORECASE)

# init_db() creates the schema above
# load_unpay_map() returns {osti_id: (year, lab, doi)} from recovery table
# scan_source(src_name, root, unpay_map, conn) walks one tree, INSERTs rows
# main() loops the 3 sources, then prints per-source/per-bucket/per-year summary
```

## Template: stage0c_dup_paths.py — capture duplicate paths PRIMARY KEY collapsed

Run AFTER `stage0_audit.py` completes. Re-walks each source and records every path NOT equal to the canonical one already in `files` into a `duplicate_paths` table. Stage 4 archives both canonical and dup paths.

```python
#!/usr/bin/env python3
"""Stage 0c: capture duplicate path rows that stage0_audit.py collapsed via PRIMARY KEY."""
import os, re, sqlite3, time
from pathlib import Path

BASE = Path("/Volumes/Cherry6TB")
AUDIT = BASE / "osti_corpus/_audit/inventory.sqlite"
ID_RE = re.compile(r"(\d+)\.pdf$", re.IGNORECASE)

conn = sqlite3.connect(AUDIT)
conn.executescript("""
DROP TABLE IF EXISTS duplicate_paths;
CREATE TABLE duplicate_paths (
    osti_id TEXT, source TEXT, path TEXT, size INTEGER,
    PRIMARY KEY (osti_id, source, path)
);
CREATE INDEX idx_dup_osti ON duplicate_paths(osti_id);
""")

# Load canonical paths from files table
canon = {(o, s): p for o, s, p in conn.execute("SELECT osti_id, source, path FROM files")}

for src in ["osti_fulltext", "osti_fulltext_unpay", "osti_fulltext_v2"]:
    root = BASE / src
    if not root.exists(): continue
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if not fn.lower().endswith(".pdf"): continue
            m = ID_RE.search(fn)
            if not m: continue
            osti_id = m.group(1)
            full = os.path.join(dirpath, fn)
            canon_path = canon.get((osti_id, src))
            if canon_path and full != canon_path:
                try: size = os.path.getsize(full)
                except OSError: continue
                conn.execute("INSERT OR REPLACE INTO duplicate_paths VALUES (?,?,?,?)",
                             (osti_id, src, full, size))
    conn.commit()

# Reconcile against on-disk count
for src in ["osti_fulltext", "osti_fulltext_unpay", "osti_fulltext_v2"]:
    c = conn.execute("SELECT COUNT(*) FROM files WHERE source=?", (src,)).fetchone()[0]
    d = conn.execute("SELECT COUNT(*) FROM duplicate_paths WHERE source=?", (src,)).fetchone()[0]
    print(f"{src}: canonical={c} dup={d} total={c+d}  (must match find <src> -name '*.pdf' | wc -l)")
conn.close()
```

## Generalizes to

- Any "N parallel fetch generations need consolidation before downstream compute" pattern
- arXiv corpus consolidation when you've got `arxiv_2024_q1/`, `arxiv_2024_q2_retry/`, `arxiv_replicate_recovered/`
- HF Hub model harvest consolidation across multiple snapshot dates
- Patent corpus consolidation when USPTO bulk + Google Patents scrape + UID-recovery converge

The shape: **gated stages (audit → resolve-metadata → build-canonical-layout → manifest → archive-originals), idempotent + reversible at every step, hardlinks-not-copies-not-symlinks, originals to `_archive/` not `rm`.**

## Cross-reference

- `references/cross-host-consolidation-audit-2026-06-12.md` — sibling pattern, for cross-host (m1 + cels + aurora) consolidation. This doc is the **same-host, multi-source-dir** consolidation. Run cross-host first (sweep finishes), then run this (consolidate the now-complete local set).
- `references/coverage-accounting-2026-06-10.md` Rule 4 — **always cross-tab against Phase 2c before declaring a transfer plan**. Same instinct here: cross-tab inventory.sqlite against the existing canonical store before any source-priority pick.

## Late-session additions (2026-06-13, post-clean-sweep + 2000-2005 backfill)

These three pitfalls surfaced after Stages 0/0b/0c had already been built and run once. Apply on any subsequent re-run.

### DB-vs-disk drift after rsync — always re-run Stage 0 first

After the clean-sweep landed (osti_fulltext_v2 went 159G → 212G, +57,105 new PDFs), `file_instances` in `catalog.sqlite` was stale by ~58K rows: DB knew about 11,180 v2 PDFs, disk had 69,285. Running `resolve_overlaps.py` (canonical-pick + SHA-dedup) against the stale inventory would have:

- Undercounted multi-instance osti_ids by ~5× (saw 5,027 in stale state; actual would be much higher post-import)
- Missed ~58K canonical-pick decisions
- Produced a manifest that doesn't match disk

**Standard sequence after ANY rsync that adds files to a source dir:**

1. Re-run `_state/stage0_audit.py` — read-only walk, safe while recovery daemon is hammering catalog (different DB), ~5-10 min for 100K+ PDFs.
2. Re-run `_state/stage0c_dup_paths.py` for the refreshed dup-path picture.
3. Re-run `_state/stage0b_summary.py` to see per-source/per-year deltas.
4. **Pause the recovery daemon** before Stage 1+ (`pgrep -af recovery_worker | xargs kill -TERM`).
5. `_state/stage1_year_lookup.py` for `:00Z/` files with `year_unknown` (OSTI API, ~85 min for 10K IDs at 2 req/s polite).
6. `_state/import_inventory_to_catalog.py` to bring catalog.sqlite up to date.
7. `_state/resolve_overlaps.py` to pick canonical per multi-instance osti_id.
8. Resume daemon: `nohup python3 _state/recovery_worker.py --daemon --interval 600 &`.

**Pre-flight drift test (cheap, run any time):**

```bash
for src in osti_fulltext osti_fulltext_v2 osti_fulltext_unpay; do
  disk=$(find /Volumes/Cherry6TB/$src -name "*.pdf" 2>/dev/null | wc -l | tr -d ' ')
  db=$(sqlite3 /Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite \
       "SELECT COUNT(*) FROM file_instances WHERE source='$src'")
  echo "$src: disk=$disk db=$db drift=$((disk - db))"
done
```

Disagreement >5% means re-inventory before any dedup or canonical work.

### `:00Z/` literal subdirectory in osti_fulltext_v2 is real, not a path bug

`/Volumes/Cherry6TB/osti_fulltext_v2/:00Z/` exists as a literal directory with **29,534 PDFs** (verified `ls -la`, `stat` on samples). Inode-confirmed distinct files (different inodes, different sizes) from the root-flat `/Volumes/Cherry6TB/osti_fulltext_v2/<id>.pdf` (814 files) and year-encoded `/2018,/2019,/2020/<id>.pdf` paths. The dir was created by an earlier rsync where the source path ended in an ISO timestamp fragment (`...T00:00:00Z`) and trailing-slash semantics planted `:00Z` as a literal subdir name in the target.

**Disposition:**
- The `:00Z/` PDFs are real fetches with content; not redundant copies to delete.
- They don't carry year info in their path → `stage1_year_lookup.py` resolves year per osti_id via the OSTI per-ID metadata API.
- `resolve_overlaps.py` handles cross-source overlap (some `:00Z/<id>.pdf` may have a matching `osti_fulltext/<year>/<year>/<id>.pdf` — SHA tiebreak picks canonical).
- The hardlink build (Stage 2) produces the clean canonical `osti_corpus/pdfs/<year>/<id>.pdf` layout from whichever source wins the evidence ladder — the messy original layout doesn't propagate.
- Leave `:00Z/` originals in place until Stage 4 `_archive/` move. **Do NOT `mv :00Z/* .`** as a "cleanup" — it breaks `osti_fulltext_v2` as a stable source root mid-consolidation and rsync from cels would re-create the dir on next sweep anyway.

### SHA-256 coverage is heterogeneous — target the hash backfill, don't full-corpus it

Verified 2026-06-13 `file_instances` SHA state across all sources:

| source | total | hashed | % | unhashed |
|---|---|---|---|---|
| `backfill_purl` | 7,228 | 7,228 | 100.0% | 0 |
| `recovery_unpaywall` / `_s2` / `_crossref` | 10 | 10 | 100.0% | 0 |
| `osti_fulltext` | 67,590 | 179 | 0.3% | 67,411 |
| `osti_fulltext_unpay` | 24,427 | 365 | 1.5% | 24,062 |
| `osti_fulltext_v2` | 11,180 | 35 | 0.3% | 11,145 |

Pre-PURL sources skipped SHA-256 at fetch time (cost: ~500GB of read I/O at ~80 MB/s on slow HFS = many hours). `resolve_overlaps.py` only NEEDS SHA for the **multi-instance subset** (~5K osti_ids in current stale state; up to ~30-40K after v2 import lands). The other ~99% of single-instance files can defer hashing.

**Targeted backfill query (run before resolve_overlaps if it's hash-gated):**

```sql
SELECT path FROM file_instances
WHERE sha256 IS NULL
  AND osti_id IN (
    SELECT osti_id FROM file_instances
    GROUP BY osti_id HAVING COUNT(*) >= 2
  );
```

Typically yields 10-30K paths (vs 168K full corpus). Hash these only.

**Generalizes**: any SHA-or-content-dedup pass over a multi-source corpus should target the multi-instance subset first, not the full universe. The single-instance majority can hash lazily (compute on demand when a paper enters OCR or extraction queue and the file path needs verification).

### Cherry6TB I/O wedge from Spotlight reindex — diagnose before retrying

Symptom escalator (any of these = wedge, NOT a writer lock or stale DB):

1. `ls /Volumes/Cherry6TB/` or `find -maxdepth 1` hangs >60s (known HFS catalog quirk — long-standing)
2. `find /Volumes/Cherry6TB/<src> -name '*.pdf' | wc -l` hangs >60s (drift pre-flight stuck)
3. **Even `sqlite3 "file:...?mode=ro&immutable=1" ".tables"` on a small or large catalog.sqlite hangs >120s** (this is the new one — read-only immutable opens shouldn't block on any writer, so it's not lock contention; it's the volume's read I/O itself)
4. `bash -c 'echo hello'` from the Hermes terminal tool hangs at the configured timeout (the shell session inherited a stalled FD on Cherry6TB and is wedged at the process level, not the command level)

When (3) or (4) fires, **stop hitting the terminal tool** — the session is wedged on a `readdir`/`read` syscall against the volume and retrying won't unstick it. The diagnostic isn't "is the DB locked," it's "what's pinning the volume."

**Bypass for diagnosis (works while terminal session is wedged):** use `execute_code` with `subprocess.run(..., timeout=N)`. The Python sandbox spawns a fresh process tree that isn't blocked by the original terminal's stuck FD. Confirmed 2026-06-14 — `subprocess.run(["echo", "hello"])` returned in 0.23s while the Hermes terminal was hanging on `echo hello && date` at 10s.

**Prime suspect when (3) is true: Spotlight (`mdworker_shared`) is reindexing the volume.** Check:

```bash
ps auxw | grep -E 'mds|mdworker' | grep -v grep | head -5
```

If you see 3+ `mdworker_shared` processes with recent start times (within the last few minutes), Spotlight is the wedger. Secondary suspects: Time Machine snapshot (`backupd`), `fsck_apfs`, a stuck rsync, virus scanner.

**Don't disable Spotlight without asking Rick.** `sudo mdutil -i off /Volumes/Cherry6TB` is reversible and low-stakes, but it touches OS-level config and needs his sudo. Surface the wedge as a 3-option ask:

1. Disable Spotlight on the volume (needs Rick sudo, fast, reversible with `-i on`)
2. Wait it out — no progress signal, could be minutes to hours
3. Defer Cherry6TB work, pivot to CherryRd-side tasks, revisit when the volume quiets

Default lean = (3) when there's no fresh rsync to consolidate against (i.e., no recovery_worker active, no fetch in flight). Catalog.sqlite is ground truth between writes; it isn't drifting if nothing is writing.

**Safe disable procedure when Rick says go (verified path, 2026-06-15):**

Critical pitfall first — **even `mdutil -s /Volumes/Cherry6TB` (read-only status check) hangs at 15s when the volume is sufficiently wedged.** Don't assume status will return; it won't. The status check itself crosses the same I/O path that's jammed, so skip step 1 below if a prior shorter probe already timed out, and proceed straight to the disable.

```
# Step 1 (optional, skip if status calls already hang):
mdutil -s /Volumes/Cherry6TB             # read current state — may itself hang at 15s

# Step 2 — turn off indexing for the volume:
sudo mdutil -i off /Volumes/Cherry6TB

# Step 3 — erase the existing index so workers exit promptly:
sudo mdutil -E /Volumes/Cherry6TB

# Step 4 — make it permanent for future re-mounts / re-creations:
sudo touch /Volumes/Cherry6TB/.metadata_never_index

# Step 5 — immediately kill in-flight workers (don't wait for them to notice):
sudo killall mdworker_shared
```

Steps 2-5 ALL need sudo. The Hermes sandbox **blocks `sudo -S` pipes**, so Kukla cannot run any of this non-interactively — these must be executed by Rick at a local terminal. Don't try `expect`/`echo PASS | sudo -S` workarounds; they're guarded specifically. Draft the exact commands in chat and hand off.

**Reverse procedure (if disabling caused unexpected issues — Finder search, Mail.app indexing, etc. depend on Spotlight):**

```
sudo rm /Volumes/Cherry6TB/.metadata_never_index
sudo mdutil -i on /Volumes/Cherry6TB
sudo mdutil -E /Volumes/Cherry6TB         # forces fresh reindex
```

Be aware: re-enabling triggers a multi-hour reindex of a multi-TB volume that will re-wedge the volume for the duration. Don't re-enable casually.

**Post-disable verification ladder** (run after Rick confirms the sudo commands landed):

1. `subprocess.run(["ps", "auxw"], ...)` filtered for `mdworker_shared` — should show ZERO matches within ~5s of killall
2. `subprocess.run(["/bin/date"], ...)` — should return instantly, confirming host PTY recovered
3. `subprocess.run(["stat", "/Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite"], ...)` — should return file metadata in <1s
4. `sqlite3 "file:.../catalog.sqlite?mode=ro&immutable=1" ".tables"` — should return in <2s on a healthy volume

If any step in 1-4 still hangs, Spotlight wasn't the only wedger — fall back to the broader suspects list (Time Machine, fsck_apfs, stuck rsync, antivirus) before assuming the disable failed.

**Don't confuse this with "DB-vs-disk drift after rsync"** (earlier subsection). That one wants you to re-audit because the catalog is stale relative to disk. This one wants you to *not* try to read the catalog at all until the volume quiets — the catalog is fine, the read path is jammed.

**Don't confuse with "another process holds a writer lock."** Test: open with `?mode=ro&immutable=1` URI. If that still hangs, it's volume-level. If it returns instantly, it was a writer lock and the original query was waiting on the busy-timeout.

### "Check `_state/` BEFORE composing a new pipeline script"

Near-miss this session: I started planning a fresh `stage0_audit.py`-equivalent before checking what was already in `_state/`. Found the complete pipeline already built — `stage0_audit.py`, `stage0b_summary.py`, `stage0c_dup_paths.py`, `stage1_year_lookup.py`, `import_inventory_to_catalog.py`, `resolve_overlaps.py`, plus `reconcile_pdfs.py`, `bulk_fetch_purl.py`, `recovery_worker.py`, `remove_datasets.py`, `init_catalog.py`, `fetch_osti_catalog.py`, `probe_backfill_2000_2005.py`, `schema_recovery.sql`.

**Drop-in pre-flight: `ls /Volumes/Cherry6TB/osti_corpus/_state/*.py` + `head -40` on every unfamiliar script.** ~30 seconds saves ~500 lines of duplicate code and a real risk of producing an inferior reimplementation that misses pitfalls already encoded in the existing script (e.g. the unpay-DB join schema, the year regex, the size>0 filter).
