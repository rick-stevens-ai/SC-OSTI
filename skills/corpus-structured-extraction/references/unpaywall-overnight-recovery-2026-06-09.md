# Unpaywall overnight recovery (worked example, 2026-06-09)

Companion to the "OA ceiling" and "DOI-prefix-filtering" pitfalls in the main
SKILL.md. Concrete numbers + recipe for an overnight Unpaywall pass against
an OSTI-shape corpus.

## The setup

After Phase 3 v2 measured 40.6% "Unpaywall PDF hit rate" on a stratified probe
(3,873 papers, DOE IP, S2 key), the user said "I want to push the unpaywall
over night. Can you set that up." Plausible expected yield: 257K target × 40%
= ~100K new PDFs.

What actually happened:

| Stage | Hit rate | Cause |
|---|---|---|
| Probe (Phase 3 v2) lookup-only | 40.6% | Counts every Unpaywall record with a `url_for_pdf` field as success |
| First smoke (100 papers, unfiltered) | **2%** | Most papers were OSTI-internal DOIs Unpaywall has never seen |
| Smoke after DOI-prefix filter (200 papers) | 12% | Real journal-DOI subset only |
| Smoke after URL rewrites (500 papers) | **9.8%** | Stable estimate after URL fixes |

## Why probe and bulk gave different answers

The probe's 40.6% was a **lookup success** — Unpaywall returned a record with
an `url_for_pdf`. The bulk's 9.8% was **PDF-on-disk success** — the URL
actually served bytes that pass `%PDF` magic + 10KB min size. The gap was the
~30 percentage points of "Unpaywall thinks it's OA but the publisher serves
HTML / 403 / a 3KB block-page".

**Rule: any "ceiling measurement" probe should follow through to a real
download check** — magic byte verification, min-size threshold — not stop at
metadata. The probe-vs-bulk asymmetry will lie to you about expected yield by
3-4× otherwise.

## DOI-prefix filter (the dominant fix)

Top DOI registrant prefixes in the 257K target set:

```
  144,843  10.17188  (LBNL Materials Project — dataset DOIs, Unpaywall 404s)
   18,893  10.1016   Elsevier
   15,776  10.1021   ACS
   11,557  10.1103   APS
    5,256  10.1038   Nature
    4,989  10.1002   Wiley
    4,133  10.1007   Springer
    4,100  10.1063   AIP
    3,869  10.1039   RSC
    3,243  10.1109   IEEE
    3,207  10.1088   IOP
    3,018  10.2172   OSTI Technical Report
    2,347  10.11578  CHESS / institutional
```

OSTI-internal / dataset prefixes to drop:
- `10.17188` — LBNL Materials Project (dominant, 56% of the unfiltered set)
- `10.11578` — institutional facility deposit
- `10.25984` — DOE OSTI dataset
- `10.18141` — DOE OSTI
- `10.46936` — DOE OSTI
- `10.5072` — DataCite test prefix (rare)
- `10.18434` — NIST data

Filter snippet (from `unpaywall_overnight.py`):

```python
OSTI_INTERNAL = {"10.17188", "10.11578", "10.25984", "10.18141",
                 "10.46936", "10.5072", "10.18434"}

keep = [r for r in rows
        if r[3].split('/')[0] not in OSTI_INTERNAL]
```

257,495 raw → 110,291 filtered. The dropped 147K rows would all have produced
fast Unpaywall 404s — useless wall time.

## URL rewrites (small but free boost)

Unpaywall's `best_oa_location.url_for_pdf` often points at an HTML landing
page, not a real PDF. Deterministic rewrites that work without parsing
landing pages:

```python
def rewrite_url(url):
    # arxiv.org/abs/<id> -> arxiv.org/pdf/<id>.pdf
    if "arxiv.org/abs/" in url:
        return url.replace("/abs/", "/pdf/").rstrip("/") + ".pdf"
    if "arxiv.org/html/" in url:
        return url.replace("/html/", "/pdf/").rstrip("/") + ".pdf"
    # bioRxiv/medRxiv content URLs -> .full.pdf
    if ("biorxiv.org/content/" in url or "medrxiv.org/content/" in url) \
            and not url.endswith(".pdf"):
        return url.rstrip("/") + ".full.pdf"
    return url
```

Tiny — three preprint hosts — but they cover most of the "repository
host_type, HTML body" failure bucket.

## Failure-mode breakdown (post-filter, post-rewrite, 500 papers)

| Bucket | Count | % | Notes |
|---|---|---|---|
| ok (PDF on disk) | 49 | 9.8% | Real success |
| not_oa | 158 | 31.6% | Unpaywall has the record but no open version exists |
| skip_no_url (incl. 404) | 34 | 6.8% | Unpaywall has no record at all |
| http_403 | 119 | 23.8% | Publisher paywall (APS, Wiley, ScienceDirect dominant) |
| not_pdf_magic text/html | 95 | 19.0% | URL resolved to HTML landing page |
| too_small_3038 | 24 | 4.8% | Identical-size HTML block page (publisher gateway) |
| Other (404, 410, 503, oversize) | 21 | 4.2% | Mixed |

The 24 `too_small_3038` rows are the signature of a single publisher gateway
serving a fixed-size HTML denial response that passes `Content-Type: pdf`
checks but fails magic bytes. Always include both checks.

## Publisher-403 distribution

| Publisher domain | 403 count | Notes |
|---|---|---|
| link.aps.org | 22 | APS journals — Unpaywall mislabels OA |
| onlinelibrary.wiley.com | 13 | Wiley TDM walls |
| sciencedirect.com | 6 | Elsevier TDM walls |
| pubs.acs.org | 4 | ACS |
| tandfonline.com | 3 | Taylor & Francis |
| jbc.org / cell.com / pnas.org | various | Cell Press, PNAS |

These 403s are NOT recoverable via Unpaywall retry — they need an
institutional-IP Elsevier TDM API key, Wiley's licensed text-mining endpoint,
or a different OA index (arXiv green-OA, PMC for biomedical). Don't waste
overnight cycles retrying them.

## Successful PDF hosts

| Host | Wins |
|---|---|
| pubs.rsc.org | 5 | RSC fully OA after 2019 |
| journals.plos.org | 4 | PLOS gold OA |
| misportal.jlab.org | 3 | JLab institutional repo |
| aanda.org | 2 | A&A gold OA |
| osti.gov/servlets/purl/* | 6 | OSTI PURL itself (residual papers not caught in Phase 2) |

Mostly fully-OA journals and institutional repos. The TDM-walled publishers
(Elsevier, Wiley, ACS) are absent from the win column entirely.

## Expected overnight yield

- Filtered target: 110,291 papers
- Observed throughput: 4.8 req/s at 8 workers (limited by per-request PDF download time, not Unpaywall ratelimit)
- Wall time for full pass: ~6.4 hours
- Expected new PDFs: ~10,800
- Coverage lift: 120,624 → ~131,432 (29.6% → 32.2%)

Modest. The honest message to the user: "10K new PDFs, not 100K — because
57% of the corpus is dataset DOIs Unpaywall doesn't index, and most of the
journal subset is Elsevier/Wiley/ACS who paywall everything."

## Resumable state DB shape

SQLite, one row per `osti_id`, columns:

```
osti_id TEXT PRIMARY KEY
doi, year INTEGER, lab TEXT
unpay_status TEXT       -- 'ok' | 'not_oa' | 'http_404' | 'parse_err' | 'no_pdf_url'
pdf_url TEXT            -- final URL after rewrite
host_type TEXT          -- 'publisher' | 'repository' (from Unpaywall)
is_oa INTEGER           -- 0 | 1
fetch_status TEXT       -- 'ok' | 'http_<code>' | 'not_pdf_magic_<ctype>' | 'too_small_<n>' | 'too_large_<n>' | 'skip_no_url'
bytes INTEGER
path TEXT
ts REAL
```

Resume = `SELECT osti_id FROM recovery` and filter the target list against
it. Re-runs against the same DB are safe.

## Recovery roadmap beyond Unpaywall

Once Unpaywall is exhausted, the next sources to layer on (in priority order):

1. **OSTI direct PURL** for papers Unpaywall doesn't know about — must run
   from a DOE IP. ~3% of target population.
2. **arXiv direct** for any DOI that has an arXiv twin (CrossRef can help
   resolve via reference linking, but more reliably: query S2 with the DOI
   and check `externalIds.arXiv`).
3. **CORE.ac.uk OAI** for green-OA copies in institutional repos.
4. **Direct lab-repository scrape** for PNNL/ORNL/ANL/BNL papers — these are
   the lowest-yield labs via Unpaywall (45-58%) because applied-energy work
   often isn't on preprint servers. Each lab has its own institutional repo
   API. Will recover the residual ~5-8% that nothing else hits.

The full chain ceiling is approximately Phase 3 v2's "any source 68.7%" —
which translates to ~280K of the 407K total OSTI universe, with the residual
30% being mostly pre-2010 applied-energy where the only path is direct lab
agreement.

## See also

- `references/oa-recovery-ceiling-2026-06-06.md` — the 50-paper pre-flight
  ceiling-measurement recipe (run this BEFORE the smoke)
- `references/corpus-coverage-characterization-2026-06-09.md` — Phase 3 v2
  worked example that produced the 40.6% probe number
- `scripts/bulk_fetch_launcher_template.py` — generalizable bulk-fetch
  framework with the locked retry/timeout/size-cap rules
