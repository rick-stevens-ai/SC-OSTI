# Persistent corpus catalog — schema + weekly refresh design (2026-06-13)

When Rick frames a corpus task with **"this needs to be updated weekly going forward,"** **"we'll want to extend backwards/forwards in time,"** or **"keep track of the decisions you make at this scale"** — that is the signal to stop building a one-shot consolidation script and start building a persistent SQLite-backed catalog system. This doc captures the canonical schema, the OSTI metadata API capabilities, and the evidence-chain decision pattern.

## When to load

- Rick asks for "comprehensive list with metadata that lets us update over time"
- The task is "consolidate sources" AND "refresh weekly" AND "extend time range"
- You catch yourself writing a one-shot script for something that will need to run again next month
- Cross-source dedup with N>1 instances per logical ID, where decisions need to be auditable later
- ANY corpus-management task at scale (>10K records) where Rick says "we'll need to update this over time"

## Architectural pivot trigger

The signal you should *not* build a one-shot is when the requirements include any TWO of:
- "weekly refresh" / "update going forward"
- "extend the time range" (forward or backward)
- "keep track of decisions"
- "structured" / "manage" / "scale"
- Multiple sources with overlap and tiebreaker logic

When 2+ of these fire, build the persistent system FIRST and write the one-shot operations as scripts that read/write the catalog DB.

**Pivot during a session is fine.** If you started with a one-shot Stage 1 OSTI API year lookup (10K IDs, sequential, ~85min) and then Rick asks for "downloaded comprehensive metadata catalog" — kill the one-shot, fold its work into the bigger catalog pull (which gets year as a free side-effect of pulling everything). Don't preserve sunk-cost-fallacy work.

## Layout (Cherry6TB)

```
osti_corpus/
  pdfs/<year>/<osti_id>.pdf            # canonical, hardlinked from source dirs
  md/<year>/<osti_id>.md               # Marker outputs (OCR pipeline)
  mmd/<year>/<osti_id>.mmd             # Nougat outputs, math subset
  cards/<year>/<osti_id>.{data,model,agent}.md  # xCards (extraction outputs)
  _state/
    catalog.sqlite                     # source of truth — all tables below
    *.py                               # all consolidation/refresh scripts
  _archive/<source>/...                # originals never deleted
  _audit/inventory.sqlite              # transient FS scan results (rebuildable)
  logs/                                # one log per run
  manifest/                            # exported CSV/JSON for sharing
```

## Schema (catalog.sqlite — five tables)

### `papers` — one row per OSTI record (authoritative)

Columns capture: `osti_id` (PK), `doi`, `title`, `publication_date`, `year`, `product_type`, `journal_*`, `research_orgs_json`, `primary_lab` (normalized short code), `sponsor_orgs_json`, `authors_json` (ORCID-tagged strings), `subjects_json`, `description` (abstract), `doe_contract_number`, `osti_links_json`, `catalog_first_seen_ts`, `catalog_last_seen_ts`, `metadata_source`, `has_pdf`, `canonical_pdf_path`, `canonical_source`, `canonical_size`, `canonical_sha256`, `needs_pdf_fetch`, `needs_ocr`, `md_path`, `mmd_path`, `notes`.

Indexes on `year`, `primary_lab`, `has_pdf`, `needs_pdf_fetch`, `doi`.

### `file_instances` — one row per PDF on disk (may be N per osti_id)

`instance_id` (PK), `osti_id`, `source`, `path` (UNIQUE), `size`, `sha256`, `extracted_title`, `title_match_score`, `first_seen_ts`, `last_verified_ts`, `is_canonical`, `canonical_decision_id` (FK to decisions).

### `decisions` — audit log of every canonical-pick

`decision_id` (PK), `ts`, `osti_id`, `decision_type` (`single_instance` | `duplicate_byte_identical` | `title_extract_match` | `size_largest` | `source_priority`), `chosen_instance_id`, `rejected_instance_ids_json`, `rationale` (human-readable), `method`, `confidence` (0-1), `inputs_json` (sizes, sha256s, scores).

Rationale must be human-readable. Don't write `"chose A"` — write `"All 2 instances byte-identical (sha256=abc12345...); picked by source priority"`. Rick reviews these.

### `refresh_runs` — provenance for each run

`run_id` (PK), `started_ts`, `ended_ts`, `run_type` (`initial_catalog` | `weekly_refresh` | `backfill_year` | `pdf_fetch` | `manual`), `params_json`, `records_added`, `records_updated`, `pdfs_added`, `errors`, `notes`.

### `pdf_fetch_log` — every PDF fetch attempt

`fetch_id` (PK), `osti_id`, `run_id` (FK), `ts`, `url`, `http_status`, `bytes`, `sha256`, `saved_path`, `error`.

## OSTI metadata API — verified capability (2026-06-13)

```
GET https://www.osti.gov/api/v1/records
```

Parameters:
- `research_org=<lab+name+url+encoded>` (use spaces as `+`)
- `publication_date_start=MM/DD/YYYY`, `publication_date_end=MM/DD/YYYY`
- `entry_date_start=MM/DD/YYYY` — use this for weekly refresh (catches updates)
- `rows=500` (verified MAX — `rows=200` and `rows=500` both work; `rows=1000` returns 500)
- `page=1` (1-indexed)

Response:
- Header `X-Total-Count` (case-insensitive) — total records matching query, for pagination math
- Body is a JSON array of records, NOT wrapped in `{results: [...]}`
- Length 0 = end of pages

Per-record schema (verified fields, all directly usable):
```
osti_id, doi, title, publication_date (ISO), product_type,
journal_name, journal_volume, journal_issue,
research_orgs (list of full org strings),
sponsor_orgs (list),
authors (list of "Lastname, First (ORCID:xxx)" strings),
subjects (list),
description (abstract),
doe_contract_number,
links (list of {rel, href} — find rel containing 'purl' or 'fulltext')
```

### Rate limit empirics

- **DELAY=1.5s sustained** is the floor for single-threaded sequential pulls. Some pages will still 429 — must have retry-with-backoff.
- **Backoff schedule**: 4s → 8s → 16s → 32s, up to MAX_RETRY=5.
- **TIMEOUT=60s** per page (large pages with rich metadata can take 20s+).
- **Burst behavior**: 0.5s between requests *probably* works but produces ~5% 429 rate. Not worth the speed gain.
- **Concurrent jobs hitting the API fight each other** for the rate budget. If you have a metadata-pull running, don't also run a per-ID lookup script in parallel. Kill one and let the other finish.
- **Per lab-year**: typically 1 page (most years <500 records); the big years (ANL/ORNL/LBNL 2010-2024) take 2-7 pages, ~5-25s. Full pull 10 labs × 27 years ≈ 22-60 min walltime.

### Lab normalization map

OSTI's `research_orgs` strings are inconsistent. Map substring → short code:

```python
LAB_MAP = {
    "argonne":          "ANL",
    "brookhaven":       "BNL",
    "fermi national":   "FNAL", "fermilab": "FNAL",
    "lawrence berkeley":"LBNL", "lbnl":     "LBNL",
    "oak ridge":        "ORNL", "ornl":     "ORNL",
    "pacific northwest":"PNNL", "pnnl":     "PNNL",
    "princeton plasma": "PPPL", "pppl":     "PPPL",
    "slac":             "SLAC",
    "jefferson lab":    "JLab", "thomas jefferson": "JLab", "jlab": "JLab",
    "ames laboratory":  "AMES", "ames national":    "AMES",  # BOTH forms appear in the API
}
```

**Pitfall I hit:** I used `"ames national"` only, missed all the `"Ames Laboratory (AMES), Ames, IA (United States)"` records — 31 rows landed with `primary_lab=NULL` before I caught it. Always include BOTH the "Laboratory" and "National Laboratory" variants for each lab, plus the short-form acronym, because OSTI sometimes returns just `"LBNL, Berkeley"` etc.

### Cross-lab affiliation reality

Querying `research_org=Ames National Laboratory&year=2024` returns 64 records, of which only 6 normalize to AMES — the rest are cross-affiliated papers (Ames + ANL, Ames + LBNL, Ames + LANL, etc.) where Ames is *a* collaborator but the *first* research_org happens to be elsewhere. **This is fine** — UPSERT on osti_id collapses cross-lab duplicates automatically. Don't try to de-conflict per-lab queries.

### Weekly refresh shape

```bash
fetch_osti_catalog.py --since-days 7 --run-type weekly_refresh
```

Use `entry_date_start` not `publication_date_start` — catches metadata updates to existing records (corrections, doi backfills, full-text adds) as well as new entries.

For backfill of historical range:
```bash
fetch_osti_catalog.py --year-start 1990 --year-end 1999 --run-type backfill_year
```

Both are idempotent against `papers` via UPSERT (UPDATE if exists, INSERT if not).

## Canonical-pick evidence chain

For each osti_id with 2+ file_instances, the resolver gathers evidence in this priority order:

```
1. SHA-256 byte-identity
   ├─ if ALL non-null hashes match → pick by source priority, log 'duplicate_byte_identical', conf=1.0
   └─ store the sha256 on every instance (no wasted hashing later)

2. Title-extract similarity (only if papers.title available from catalog)
   ├─ pdftotext -l 1 -q <path> -  (first page only, ~10ms per PDF)
   ├─ normalize: lowercase + collapse whitespace
   ├─ score: substring=1.0, first-60-chars-substring=0.95, else SequenceMatcher fuzzy
   ├─ if best score ≥ 0.5 → pick highest-scoring, log 'title_extract_match', conf=score
   └─ if no scores reach 0.5 → fall through to step 3

3. Largest-size wins
   ├─ smaller files are usually error pages / paywalls / truncated
   ├─ if unique max → pick it, log 'size_largest', conf=0.8

4. Source priority (final tiebreak when sizes tied)
   ├─ osti_fulltext > osti_fulltext_v2 > osti_fulltext_unpay
   └─ log 'source_priority', conf=0.6
```

**Empirical observation (2026-06-13 inventory, 97,883 unique OSTI IDs):**
- 94,148 (96%) single-source — no decision needed, mark canonical
- 5,027 (4%) overlap IDs needing decisions
- Of the overlaps, the dominant pattern is **byte-identical copies across sources** (re-runs of unpay logic written to v2 dir). First 15 overlaps tested all SHA-collapsed in <1s.

**Full empirical results from 2026-06-13 end-to-end pass** (after the cels-rbdgx2 sweep added 67K v2 files, bumping totals):
- 162,376 distinct osti_ids across 168,540 file_instances
- 156,730 single-source (97%) — auto-canonical
- **5,646 multi-instance candidates** entered `resolve_overlaps.py`
- Resolution breakdown:
  - **2,725 (48.3%)** byte_identical — SHA-256 matched across sources
  - **2,638 (46.7%)** single — overlap candidate had only 1 real file after within-source path-dup cleanup
  - **228 (4.0%)** title_match — pdftotext + SequenceMatcher resolved
  - **48 (0.9%)** size_largest
  - **7 (0.1%)** source_priority fallback (SHA differed, title extraction failed all, sizes equal — extremely rare)
  - **0 errors** across the entire 5,646-id run
- Walltime **9.1 min @ 10.5 candidates/s** (resolve_overlaps on m1 against catalog.sqlite, no daemon contention)
- Final state: 249,664 decision rows total (incl. 151,362 dataset-exclusion + 92,790 single-instance + 5,512 multi-instance + recovery), 99,786 canonical PDFs (35.8% coverage of 278,645 catalog papers)
- **Per-year coverage curve** (have/total/pct): 2000-2005 PURL backfill caps at 20-35%; 2006-2010 only 11-19% (next recovery target); 2015 anomaly at 11%; 2016-2017 healthy 46-49%; 2018 dip 30% (publication-spike year, 23,606 papers vs ~13K neighbors); 2019-2025 healthy 50-61%
- **2000-2005 backfill via bulk_fetch_purl.py**: 7,202 PDFs recovered of 24,945 attempts (28.9%) in 13.04h walltime @ 0.53/s. Failure mix: 14,648 http_404 (papers pre-date PURL-served era, hard floor), 2,630 redirect_off (publisher off-site dead/inaccessible), 121 http_403, 180 exception, 106 http_503, 56 wrong_type. Hard floor: pre-2006 OSTI records are largely metadata-only with no fulltext PURL attached.
- **SHA-256 coverage on file_instances before this pass**: backfill_purl=100% (hashed at fetch time), legacy sources 0.3-1.5% (osti_fulltext: 179/67,590 = 0.3%, osti_fulltext_unpay: 365/24,427 = 1.5%, osti_fulltext_v2: 35/11,180 = 0.3%). **Don't gate dedup on full hash backfill** — resolve_overlaps.py only needs to hash multi-instance candidates (~12K files), not the entire 168K corpus. SHA gets backfilled lazily as overlaps are resolved.
- **Stage 1 OSTI per-ID year lookup**: 67,827 v2 unknown-year files at 2.0 req/s sustained, ETA ~9.5h. Non-blocking for dedup (runs against inventory.sqlite separately) but required before final `pdfs/<year>/<id>.pdf` filesystem reorganization for the v2 subset.

**Decisions table is your audit trail.** Rick can ask "why did you pick the v2 version of 1234567?" and you answer by reading the row.

## Pitfalls

- **Don't run a per-ID API lookup script in parallel with a bulk metadata pull.** They fight for the same rate budget; both throw 429s, both stall. Pick one, kill the other.

- **`pdftotext` is `/opt/homebrew/bin/pdftotext`** (poppler-utils). Verify before relying. First-page extraction is ~10-50ms per PDF for typical sizes.

- **The resolver's UPDATE on `papers` silently affects 0 rows** if the osti_id isn't in `papers` yet (e.g. when running resolver before catalog pull completes, or for non-SC-lab PDFs). Add a stub-insert fallback so file_instances IDs don't get lost:
  ```python
  if conn.execute("SELECT 1 FROM papers WHERE osti_id=?", (osti_id,)).fetchone() is None:
      conn.execute("INSERT INTO papers (osti_id, ..., metadata_source='stub_from_file_only', ...) VALUES (...)")
  ```

- **f-string + backslash trap (Python 3.13):** `f"... {[r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type=\"table\"')]}"` — invalid because backslash inside `{}`. Workaround: bind to a local first.
  ```python
  tables = [r[0] for r in conn.execute("SELECT ...")]
  print(f"  Tables: {tables}")
  ```

- **`datetime.utcnow()` is deprecated in Python 3.12+.** Use `datetime.now(timezone.utc).isoformat()` instead. Won't break, just noisy DeprecationWarning. Patch when you see it.

- **Schema reads are 4x faster after `PRAGMA journal_mode=WAL`** — set it once on the connection that does heavy writes. The inventory import (103K rows) takes 5s with WAL vs 20s without.

- **Sample bias when smoke-testing overlap resolver:** the first N IDs (sorted ascending) are heavily biased toward old `unpay` recoveries that are byte-identical to their v2 copies. Smoke on those will look 100% byte-identical and underestimate the harder cases. To stress the title-extract path, query specifically for cross-source overlaps with size diff and ORDER BY year DESC.

- **The `papers` table can have rows where `has_pdf=0`** — these are catalog entries for which we don't yet have a PDF (publisher abstract-only records, papers we haven't fetched yet, etc.). The PDF fetch worker reads `WHERE has_pdf=0 AND needs_pdf_fetch=1`.

## Re-running the resolver

Resolver is idempotent against the `decisions` table via the `run_tag` field. To re-run with new logic (e.g. you bumped the title-similarity threshold), pass `--run-tag resolve_v2` — it will skip osti_ids that already have a decision tagged `resolve_v2` but will produce fresh decisions for everything else.

To completely re-resolve, delete decisions for the prior tag first:
```sql
DELETE FROM decisions WHERE inputs_json LIKE '%"run_tag": "resolve_v1"%';
UPDATE file_instances SET is_canonical=0, canonical_decision_id=NULL;
UPDATE papers SET has_pdf=0, canonical_pdf_path=NULL, canonical_source=NULL, canonical_size=NULL, canonical_sha256=NULL WHERE metadata_source != 'osti_api_bulk%';
```

## Reusable beyond OSTI

The schema generalizes to any "many-sources, one-canonical-target, weekly-refresh" corpus task:
- arXiv refresh (papers + sources + decisions, refresh via `submittedDate`)
- HuggingFace Hub harvest (models + files + decisions, refresh via `last_modified`)
- Patent corpus (Patents + family-members + decisions)
- Genesis Mission proposal corpus (Proposals + revisions + decisions, refresh on new submissions)

The five-table pattern (`<thing>`, `<thing>_instances`, `decisions`, `refresh_runs`, `<thing>_fetch_log`) is the bones; column lists differ per domain but the relationships hold.
