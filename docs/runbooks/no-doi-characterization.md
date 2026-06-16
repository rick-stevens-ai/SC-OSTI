# NO-DOI characterization runbook

**Status:** queued, not started
**Scope:** the 63,476 papers in `papers` where `doi IS NULL OR doi = ''`
**Why this matters:** DOI is the primary key for most fetch/reconcile strategies (Unpaywall, Crossref, S2, OpenAlex). Papers without a DOI need a different gap-filling pipeline. We need to know what they ARE before we can pick that pipeline.

## What the snapshot already tells us (2026-06-16)

NO-DOI breakdown by `product_type`:

| product_type | n |
|---|---|
| Conference | 28,549 |
| Journal Article | 15,095 |
| Program Document | 10,088 |
| Patent | 4,941 |
| Book | 1,899 |
| Multimedia | 1,077 |
| (null) | 630 |
| Software | 538 |
| Thesis/Dissertation | (smaller) |
| Technical Report | (smaller) |

**First-pass interpretation:**

1. **Conference (28,549)** — likely a mix of proceedings papers, talks, posters. Many DOE conference outputs have OSTI-only PURLs and no DOI. Strategy: OSTI PURL fetch + OSTI metadata is usually enough; no external reconcile needed. The PDF may already be on disk under a different `osti_id`.

2. **Journal Article (15,095)** — surprising; journal articles SHOULD have a DOI. Two hypotheses to test:
   - (a) Metadata was incomplete at first ingest and DOI is recoverable by Crossref title+author search;
   - (b) These are pre-DOI-era articles (pre-2000, or fields that were slow to adopt DOI).
   Strategy: run Crossref title+author lookup → if DOI found, backfill; if not, classify as genuinely pre-DOI.

3. **Program Document (10,088)** — DOE program reports, project deliverables, milestone docs. Almost never have a DOI. OSTI PURL is the only path; many will be already on disk.

4. **Patent (4,941)** — patents have their own ID system (USPTO numbers), not DOI. Out of scope for DOI-based reconcile; treat as a separate corpus slice.

5. **Book (1,899)** — books have ISBN, not DOI (mostly). May resolve via OpenLibrary or Google Books for metadata enrichment, but not for full-text.

6. **Multimedia (1,077)** — videos, presentations, audio. No reasonable text-extraction path; out of scope for OCR pipeline.

7. **(null) (630)** — product_type missing entirely. Investigate sample; likely metadata bug.

8. **Software (538)** — code releases (DOE codes registered with OSTI). May have a Zenodo DOI; check `osti_links_json`.

## Plan (when started)

**Step 1 — distribution analysis** (cheap, no external calls)
- Cross-tab `product_type` × `year` × `primary_lab` for the 63,476.
- Bucket by decade. The pre-2000 cohort gets a "pre-DOI-era" tag.
- Save to `/Volumes/SG-1-8TB/osti/analyses/no_doi_distribution.csv` + write a `docs/analyses/no-doi-distribution-<date>.md` to SC-OSTI.

**Step 2 — sample inspection** (LLM, ~200 papers)
- For 25 papers each from the top 8 product_type buckets, fetch metadata + (if available) PDF first page.
- LLM judge (Argo Sonnet 4.6) classifies: is this recoverable via Crossref? OSTI-only? Genuinely no full-text? Patent? Multimedia?
- Output: per-bucket recoverability rate.

**Step 3 — strategy table** (write to `docs/runbooks/no-doi-strategy.md`)
- One row per product_type with assigned fetch/recovery strategy and expected yield.

**Step 4 — pilot reconciles** (small)
- Run the assigned strategy on 500 papers per bucket; measure actual yield vs. predicted.

**Step 5 — production pass** (gated on pilot)
- Full run per bucket, only if pilot yield justifies it.

## Pitfalls / rules
- **HARD RULE:** no regex for judgment fields (recoverability classification etc.) — LLM on every sample.
- Many NO-DOI papers MAY already have a PDF on disk under their `osti_id`; check disk before fetching.
- Don't email any external service (Crossref, OSTI) without rate-limiting (1 req/s default).
- Patents and Multimedia are explicit drop-outs from the OCR / xCARDS pipeline; flag with `notes` column rather than silently skipping.

## Owner
Kukla, opportunistic. Not a parallel-launch priority while metadata reconcile workers are running.
