# Coverage accounting discipline — papers vs non-papers, download-vs-lookup yield

Captured 2026-06-10 after Rick asked for a re-segmentation of the OSTI 2006-2026 refresh that excluded non-paper DOIs. The pre-correction numbers ("407K universe", "110K filtered Unpaywall target", "40.6% Unpaywall hit rate") all encoded the same class of mistake: mixing scope levels or conflating two different yield metrics. This card is the recipe for not making any of those again.

## Rule 1 — Always segment `product_type` BEFORE quoting coverage

The OSTI Phase 1 DB universe count (`SELECT COUNT(*) FROM papers`) is **NOT** the paper count. As of 2026-06-10 on the 2006-2026 pull:

| product_type | Count | Pct | Paper? |
|---|---|---|---|
| Journal Article | 176,139 | 43.2% | yes |
| Conference | 39,123 | 9.6% | yes |
| Technical Report | 19,443 | 4.8% | yes |
| Book / Thesis | 3,661 | 0.9% | yes |
| **Papers subtotal** | **238,366** | **58.5%** | — |
| Dataset | 151,204 | 37.1% | no |
| Program Document | 10,098 | 2.5% | no |
| Patent | 4,298 | 1.1% | no |
| Software | 2,591 | 0.6% | no |
| Multimedia | 1,129 | 0.3% | no |
| Other / Missing | 16 | 0.0% | no |
| **Non-paper subtotal** | **169,338** | **41.5%** | — |

41.5% of the universe is non-paper records. **Reporting "X% of 407K covered" was wrong by 70% relative magnitude** — the denominator was inflated by the dataset entries that have no PDF to fetch and never will.

Source of `product_type`: parsed out of `raw_json` per-record (not its own column in the current schema). One-shot:

```python
import sqlite3, json
from collections import Counter
conn = sqlite3.connect("phase1_master.db")
ptypes = Counter()
for (raw,) in conn.execute("SELECT raw_json FROM papers WHERE raw_json IS NOT NULL"):
    j = json.loads(raw)
    ptypes[j.get("product_type") or "MISSING"] += 1
```

Persist the segmentation: emit `papers_manifest.tsv` (osti_id, doi, doi_prefix, product_type) + `paper_ids.txt` early in any analysis and reuse them downstream rather than re-parsing. Anyone (subagent, future session, Rick reviewing) can sanity-check the universe count instantly.

## Rule 2 — Drop OSTI-internal/dataset DOI prefixes from any DOI-keyed recovery target

Even within "Journal Article" `product_type`, DOI prefix `10.17188` is Materials Project dataset entries (not papers). Other known non-paper prefixes that crept in during 2026 ingest:

- `10.17188` — Materials Project (144,844 records — single largest poison source)
- `10.11578`, `10.25984`, `10.18141`, `10.46936`, `10.5072`, `10.18434` — other OSTI / dataset / NIST registries (smaller volumes)

Filter these before any Unpaywall / Crossref / publisher recovery loop. They're not findable in those databases, they 404 every API, and they front-load 50%+ of the failure log if you don't exclude them.

```python
OSTI_DATA_PREFIXES = {"10.17188", "10.11578", "10.25984", "10.18141",
                      "10.46936", "10.5072", "10.18434"}
target = [r for r in rows
          if r["doi"].split("/")[0] not in OSTI_DATA_PREFIXES]
```

On the 2026-06-10 refresh, this drop went from 257,495 raw DOI-having records to 110,291 real-paper candidates — **147,204 records (57%) were dataset DOIs masquerading as paper recovery targets**.

## Rule 3 — Distinguish lookup-success from download-success in recovery yield

Phase 3 v2 reported "Unpaywall 40.6%" — that number counted records where Unpaywall returned a `best_oa_location.url_for_pdf` (lookup success). The actual download-success rate when we honored that URL was **9.8%** in the smoke test:

| Failure mode in the gap | Count per 500 | Pct |
|---|---|---|
| Unpaywall returned no record (`http_404`) | 53 → 24 after DOI filter | ~5% |
| Unpaywall says `is_oa=false` (not OA) | 158 | ~32% |
| `is_oa=true` but no `url_for_pdf` | 34 | ~7% |
| Got URL, publisher 403 (paywall behind Unpaywall's OA flag) | 119 | ~24% |
| Got URL, HTML landing page (not PDF) | 95 | ~19% |
| Got URL, "too small" generic block page | 26 | ~5% |
| **Real PDF download succeeded** | **49** | **~10%** |

When planning capacity for a recovery pass, **always quote download-success, never lookup-success.** A "40% Unpaywall hit" planning input over-promises actual coverage lift by 4x.

Always emit a per-bucket breakdown in the smoke report. The 24% publisher-403 bucket (`pubs.acs.org/doi/pdf`, `link.aps.org/pdf`, `onlinelibrary.wiley.com/doi/pdf`, etc.) is a fixed structural ceiling, not a transient error worth retrying.

## Rule 4 — Cross-source dedup BEFORE declaring a transfer plan

When two hosts both hold fetched PDFs (Cherry6TB on m1 + `/rbstor/stevens/osti_fulltext_v2/` on cels-rbdgx2), Phase 2c already merges them — `phase2c_classify.tsv` is keyed by `osti_id` with `status ∈ {good, image, truncated, zero}` and represents the **union**, not a single source.

A fresh `find /rbstor/stevens/osti_fulltext_v2 -name "*.pdf"` on cels gave 68,471 files / 54,464 paper-scoped. Naive read: "we have 54K papers on cels to pull back!" Actual cross-tab against Phase 2c:

- 53,650 already on Cherry as `good` (no-op pull, double-fetch)
- 786 on Cherry as `image` only (cels may have text version — worth pulling for upgrade)
- 28 on Cherry as `truncated` (worth pulling for upgrade)
- 0 not in Phase 2c at all (no truly new content)
- **814 net-new (~5 GB)**

Before quoting "pull X GB from cels to Cherry," do this join. Skipping it leads to a transfer plan that's 60x larger than the real delta.

## Rule 5 — Image-PDF fraction needs the same union accounting

"How many of our PDFs are image-only and need OCR?" — count `status='image'` in the deduped union, not the raw on-disk count. On the 2026-06-10 snapshot: **1,837 image papers across both hosts**, of which 268 are already through Marker (`/rbstor/stevens/osti_fulltext_v2_md/`), leaving **1,569 still needing OCR**. The raw cels-rbdgx2 inventory has more image PDFs than that, but most are duplicates of Cherry-side good PDFs (cels was the staging mirror, not a separate source of unique content).

## Rule 6 — Marker (OCR) worker host vs output host

When assembling an OCR queue from the union accounting, remember the workers and outputs live on different hosts:

- **Worker host:** `uicgpu`, scripts at `/data/stevens/launch_full_marker.sh` + `/data/stevens/marker_batch.py`, queue staged at `/data/stevens/ocr_inbox/full/<osti_id>.pdf`, output written to `/data/stevens/ocr_output/full/<osti_id>.{md,json}`.
- **Output mirror:** `cels-rbdgx2:/rbstor/stevens/osti_fulltext_v2_md/<osti_id>.{md,json}` (path appears via a separate sync — exact mechanism not fully traced as of 2026-06-10, but the `.json` sidecar's `"src"` field confirms the worker path).
- **Cherry mirror:** `/Volumes/Cherry6TB/osti_fulltext_v2_md/<osti_id>.{md,json}`, kept fresh by `~/.hermes/scripts/marker_mirror.sh` cron (daily 04:00 CT).

To queue new OCR work:
1. Identify image-papers needing OCR via the union accounting (`status='image'` minus already-Marker'd IDs).
2. Find each PDF's actual on-disk path. Phase 2c's `location` column is only `cherry6tb` or `rbdgx2` — you must rebuild the path index from disk by walking the relevant tree.
3. tar-pipe the PDFs to `uicgpu:/data/stevens/ocr_inbox/full/`. Use `--strip-components` to flatten — the launcher expects flat `<osti_id>.pdf`. Cherry-side files need `--strip-components=4` (`/Volumes/Cherry6TB/osti_fulltext/<year>/<year>/<id>.pdf`); cels-side need `--strip-components=4` (`/rbstor/stevens/osti_fulltext_v2/<bucket>/<id>.pdf` from `/` root).
4. Restart the launcher: `ssh uicgpu 'bash /data/stevens/launch_full_marker.sh'`.
5. Monitor via `ssh uicgpu 'tail /data/stevens/ocr_logs/full.gpu*.log'`.

GPU health pre-flight is mandatory before relaunching — uicgpu often has stuck GPUs (1, 5, 7 dead on 2026-06-07). The launcher currently hardcodes GPUs (0, 2, 3, 4, 6); edit before run if health snapshot differs.

## Phase shape for an iterative recovery loop (Phase E)

Once the deduped accounting is in hand, the gap-closing work fits a state-machine of strategies tried in priority order per paper. Strategy hit-rate ceiling per Phase 3 v2 measurements:

| Strategy | Applies when | Approx hit on fresh residual |
|---|---|---|
| osti_purl | always | ~28% (re-probe from DOE IP fixes prior 0-byte fetches) |
| unpaywall | has DOI, not OSTI-internal prefix | ~10% download-success |
| crossref | has DOI | ~2% |
| s2 | has DOI or title | ~5% |
| arxiv | title-search, physics/CS labs | ~8% |
| publisher_html | landing-page URL only | ~3% |
| google_scholar | last-resort | <1% |

Cumulative ceiling on a residual gap: ~50% recoverable, lifting total coverage from ~47% to ~67%. State persistence (per-attempt log) is mandatory — without it you can't compute marginal yield when adding strategy N+1.

Build phase-by-phase, not all-at-once. Strategies 1+2 (osti_purl + unpaywall) are already proven this session. 3-5 (crossref + s2 + arxiv) next. 6-7 only if marginal yield justifies effort. See `PHASE_E_DESIGN.md` in the working dir for the full spec.

## Canonical artifacts produced by a clean coverage pass

After running the segmentation + cross-tab, the working directory should hold:

- `papers_manifest.tsv` — `osti_id, doi, doi_prefix, product_type` for the paper-only universe
- `paper_ids.txt` — flat list of paper osti_ids for quick `grep -f` joins
- `cels_v2_inventory.tsv` — `filename, size` from `find /rbstor/stevens/osti_fulltext_v2 -name '*.pdf' -printf '%f\t%s\n'` (or whichever staging host applies)
- `cels_md_inventory.tsv` — Marker output inventory (`.md` files)
- `pull_to_cherry_papers_v2.txt` — net-new IDs after cross-source dedup
- `OSTI_PAPERS_COVERAGE_<DATE>.md` — the human-readable summary report

These names compose with the existing `phase2c_classify.tsv` and `phase1_master.db` and shouldn't be renamed casually (per Rick's anti-cosmetic-churn rule).
