# Corpus coverage characterization — three-phase pattern

**When to use**: user asks "how much of corpus X do we have, what's broken,
what can we recover?" Differs from "screen N papers for property X" (which is
the metadata-only-screening variant) and from "extract structured fields"
(the full-text variant). This is the **inventory + gap + recovery-prospect**
pattern.

Worked example: OSTI corpus characterization 2026-06-09. Output:
`OSTI_STATUS_REPORT.md` with 3 tables across 10 labs × 21 years (210 cells).
End-to-end wall: ~5 min for Phase 1+2a+2b+3 (sample); Phase 2c full
classification adds ~30 min, gated on USB-SSD I/O.

## Three phases, run in parallel where possible

### Phase 1 — Build ground-truth master from upstream recon

If you already have a recon enumeration (per-cell jsonl files from a prior
API sweep), parse them ONCE into a SQLite master keyed by `osti_id` (or
equivalent primary key) with columns `(lab, year, doi, title, pdf_links,
has_pdf_link)`. Add indexes on `(lab, year)` and `(doi)`. Also write a
`cell_counts` table so the coverage matrix renders without a `GROUP BY`
hot-loop on the main table.

Output: `phase1_master.db`. ~30s for 400K rows on M1.

Pattern: handle duplicates with `INSERT OR IGNORE` and count them separately
— OSTI cross-credits the same paper to multiple labs; first-lab-wins is fine
but DO surface the dedup count so the user knows.

### Phase 2 — Inventory + classify what's on disk

**Phase 2a** (local presence): walk the local PDF mirror with `pathlib`,
emit TSV `(osti_id, year_dir, size_bytes, path)`. Filename stem is the
primary key. Don't `find -name '*.pdf'` via shell — `pathlib.iterdir()` on
each year dir is faster and gives you size in one stat call.

**Phase 2b** (remote presence): SSH to remote host, `find $root -name '*.pdf'
-type f -printf '%f\t%h\t%s\n'`. One SSH call, parsed locally. ~10s for 68K
files on rbdgx2 over Tailscale.

**Phase 2c** (classify): for each PDF, classify as `good` / `image` /
`truncated` / `zero`. Locally, use `pdfinfo` + `pdftotext -l 3` (sample
first 3 pages — chars-per-page < 100 = image-only). Remotely on CELS hosts
where poppler-utils is NOT installed, fall back to `pypdf` — it's pre-
installed on rbdgx2. Threshold: < 100 chars / page average = image-only.

**Pitfall**: USB-mounted SSDs (Cherry6TB) saturate at ~45 files/sec for
parallel `pdftotext`, regardless of thread count. Going from 8 to 24 workers
gave zero speedup on this run. Don't waste time tuning thread count for
USB-attached drives — the disk's random-access ceiling is the bottleneck.
For internal NVMe or remote NFS-backed storage, 16-32 workers helps.

**Pitfall** (already in main SKILL.md but worth re-stating): pre-flight any
remote classify by checking `command -v pdfinfo` on the remote host. If it
returns nothing, switch to pypdf-only mode before sending 68K jobs. Failure
case 2026-06-09 wasted a launch + restart on assuming poppler was there.

**Output**: `phase2c_classify.tsv` with `(osti_id, location, status,
n_pages, n_chars, size_bytes)`. Status priority for dedup when same osti_id
exists in multiple locations: `good > image > truncated > zero > error`.

### Phase 2d — Coverage join

Join `phase2c` to `phase1_master` by `osti_id`. Per `(lab, year)` cell:
```
osti_count, local_count, good, image, truncated, zero,
missing_with_doi, missing_no_doi
```

Optional fast-path: write a `phase2d_presence.py` that only needs
phase2a+phase2b (not phase2c), so the user gets the headline coverage table
in ~2 minutes while the classifier runs to fill in the failure breakdown.

### Phase 3 — Stratified recoverability sample

For papers in the "missing or broken" pool (`status in {None, zero,
truncated, image}` AND has DOI), sample N per (lab × year) cell. Probe each
sampled paper against:
- **Unpaywall v2** `https://api.unpaywall.org/v2/{doi}?email=...`. Field:
  `best_oa_location.url_for_pdf`. **No rate limit issues** at 8 parallel
  workers, ~25 req/sec aggregate, no errors observed across 3,873 probes.
  Send a real `email=` param — they require it.
- **Semantic Scholar graph API**
  `https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf,isOpenAccess`.
  **Rate-limits hard** — http_429 after ~400 calls on anonymous tier.
  Without throttling, you get clean data for the first 1-2 labs (alphabetic
  order) and zeros for everyone else.

### Critical pitfall (Phase 3, 2026-06-09)

**Semantic Scholar anonymous tier is hostile to bulk probing.**
First-pass run of 3,873 papers at 8 workers parallel: AMES + ANL got real
data, all 8 subsequent labs returned 100% http_429. The skew makes the
recoverability table actively misleading because it appears S2 covers
nothing outside the first two alphabetic labs.

**Workaround options**:
1. **Apply for an S2 API key** (free, fast turnaround) — bumps rate to
   1 req/sec stable, 100 req/5min burst. Set `x-api-key` header.
2. **Throttle anon to ~0.5 req/sec** with a sleep + single worker. Triples
   the wall time but every cell gets fair coverage.
3. **Use S2 only as a fallback** when Unpaywall misses — drops the call
   volume by ~40% and stays well under the anonymous rate ceiling for most
   corpora.

**Unpaywall is the reliable workhorse** for bulk recoverability probes.
Use it as the primary, S2 as supplement if rate-limit handled.

### Extrapolation

For each lab, project the sample rate back onto the full
`missing_with_doi` pool:
```
est_recoverable = missing_with_doi * (sample_any_pdf / sample_size)
```

Surface this as a separate "extrapolated to full missing set" table —
sample numbers and projected numbers in different tables, not mixed.

## Report shape — 3 tables Rick expects

1. **Coverage matrix** in three parts (1a / 1b / 1c): OSTI count grid, have
   grid, coverage % grid. Each is a labs × years matrix with row + column
   totals. Lab columns ordered by size (ANL, BNL, FNAL, LBNL, ORNL, PNNL,
   SLAC, JLAB, PPPL, AMES) for visual consistency. Years on rows.

2. **Failure-mode breakdown per lab**: `Have | Good | Image-only | Truncated
   | Zero-byte` with raw counts and percentages of-have. One row per lab +
   TOTAL row.

3. **Recoverability per lab**: `Sampled | Unpaywall hit | S2 hit | Any source
   | Rate`. Plus a separate "extrapolated to full missing set" table with
   `Missing | Est. recoverable | Est. still gone`.

Markdown tables with right-aligned numeric columns. Format integers with
thousands separators (`f"{n:,}"`). Percentages as `f"{p*100:.0f}%"` for the
coverage matrix, `f"{p*100:.1f}%"` for headline rates.

## Anomaly flags to surface in the headline

**Metadata-inflation watch**: if a single (lab, year) cell has an OSTI count
that's 5-10× the lab's median year count, the metadata is likely poisoned
(dataset bulk-deposits filed as papers). Worked case 2026-06-09: LBNL 2020
had 115,917 records vs LBNL median ~4,500/year — almost certainly not
"papers" in any conventional sense. **Always do a sanity scan** of the
OSTI-count grid before treating the coverage percentages as actionable;
flag any cell > 5× lab median and recommend a `kind=paper` filter pass on
recon before downstream work.

**Outlier coverage**: rank labs by coverage % at the end. The lowest-coverage
lab without metadata inflation is the best ROI target for the recovery
pipeline. Worked case: JLAB at 9% coverage genuine + 59% Unpaywall recoverable
= 3,000 immediate wins for the first sprint.

## Source files (this run, 2026-06-09)

Under `~/code/`:
- `phase1_build_master.py` — recon → SQLite master
- `phase2a_local_inventory.py` — Cherry6TB walker
- `phase2b_remote_inventory.py` — rbdgx2 SSH walker
- `phase2c_classify.py` — pdfinfo/pdftotext + pypdf classifier
- `phase2d_presence.py` — fast headline coverage (no classification)
- `phase2d_coverage.py` — full quality-broken-down coverage
- `phase3_recoverability.py` — Unpaywall + S2 stratified probe
- `build_report.py` — 3-table markdown report generator

Output dir: `~/code/osti-replication-candidates/`:
- `phase1_master.db` (407K rows, indexed on lab+year, doi)
- `phase2[abc]*.tsv` (per-PDF detail)
- `phase2d_presence.tsv`, `phase2d_coverage.tsv` (per-cell aggregates)
- `phase3_papers.tsv`, `phase3_cells.tsv` (recoverability detail + cell agg)
- `OSTI_STATUS_REPORT.md` (the final 3-table report)

## When to reuse this pattern vs the existing variants

- Use **this pattern** when the question is "what fraction of corpus X do we
  have, what's the failure breakdown, what can we recover" — i.e. a
  characterization across one stable id-space with multiple intersecting
  states (present/missing × good/broken × recoverable/not).
- Use the **metadata-only-screening variant** when the question is "which
  papers in this corpus have property Y" — pure judgment on metadata,
  no inventory needed.
- Use the **full-text variant** when the question is "extract field Z from
  each paper" — text-extraction pipeline, not coverage.
- Use the **augmentation variant** when the question is "add field W to
  these N existing structured records" — has-the-record case, not
  has-the-source-pdf case.

These four variants are all under one umbrella because they share the
"corpus → structured output" frame and the same operating rules (HARD RULE
on regex-for-judgment, smoke-before-scale, broad-except network loops). The
data-flow shape differs.
