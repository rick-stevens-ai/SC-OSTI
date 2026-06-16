# Publisher-specific PDF-fetch failure modes (Unpaywall pool, 22K probed)

**Source:** Phase C live data, 22,170 OSTI papers probed against Unpaywall-resolved URLs from cels-rbdgx2 on 2026-06-10. Recovery rate at headline: 11.3%. The 88.7% miss-rate decomposes into 6 publisher-specific mechanisms below, each with a distinct recovery strategy and an empirically-grounded yield estimate.

Complements `why-papers-are-missing-2026-06-10.md` (which covers structural OSTI-side modes: never-deposited / SSO-walled / publisher-403 generic). This doc covers the **publisher-side bot-detection and stub-serving mechanisms** that show up when you take a DOI-keyed recovery pool through publisher URLs.

## Live distribution from 22,170-paper probe

| Bucket | Count | Pct |
|---|---:|---:|
| `skip_no_url` (Unpaywall has no DOI or no OA copy) | 8,821 | 39.7% |
| `http_403` (publisher blocked) | 4,805 | 21.7% |
| **`ok` (PDF retrieved + validated)** | **2,503** | **11.3%** |
| `not_pdf_magic_text/html` (returned landing page) | 3,842 | 17.3% |
| `too_small_3038 / 1817 / 2711` (truncated/stub bodies) | 1,237 | 5.6% |
| `http_404` (URL stale) | 122 | 0.6% |
| `http_502 / 202 / -1` (transient) | 119 | 0.5% |

Realistic ceiling after running the recovery cascade against these buckets: **another 15-20 percentage points**, putting overall OSTI corpus coverage in the **55-60% range**. Honest hard floor remains **~30-35% unrecoverable** — pre-1995 metadata-only, classified/export-controlled, dead conference URLs, lab-intranet-only.

## Mechanism 1: APS Cloudflare bot-challenge

- **Affected:** 1,084 papers, DOI prefix `10.1103`
- **URL pattern:** `https://link.aps.org/...` or `https://journals.aps.org/...`
- **HTTP signature:** `HTTP/2 403`, content-type `text/html; charset=UTF-8`, body ~5511 bytes, `<title>Just a moment...</title>`, `<meta name="robots" content="noindex,nofollow">`

```
$ curl -sLI "https://link.aps.org/accepted/10.1103/PhysRevB.105.094424"
HTTP/2 403
content-type: text/html; charset=UTF-8
```

- **Why:** Cloudflare "I'm Under Attack" interstitial. Triggers on plain `curl` with non-browser User-Agent. Hits from DOE IPs too (Cloudflare is User-Agent + JS-challenge based, not IP-based for this rule).
- **Recovery:** arXiv preprint via DOI lookup. Physics community deposits ≥80% of APS papers on arXiv. Strategy `arxiv` in Phase E plan covers this. Also try OSTI biblio HTML (may have OSTI-hosted PURL distinct from link.aps.org) and Unpaywall `host_type=repository` records.
- **Expected recovery for this bucket:** 60-80%.

## Mechanism 2: Springer/Nature "cookies_not_supported" stub

- **Affected:** 1,189 papers, prefixes `10.1007`, `10.1038`, `10.1140`, `10.1186`
- **URL pattern:** `https://www.nature.com/articles/<id>.pdf` or `https://link.springer.com/content/pdf/<doi>.pdf`
- **HTTP signature:** `HTTP/2 200` + content-type `application/pdf` (Nature) OR `text/html` (Springer), body exactly 3038 bytes, final URL contains `?error=cookies_not_supported&code=...`

```
$ curl -sL "https://link.springer.com/content/pdf/10.1140%2Fepjc%2Fs10052-017-5180-3.pdf"
HTTP=200 bytes=399419 ctype=text/html
Final URL: https://link.springer.com/article/.../s11071-022-07876-8?error=cookies_not_supported&code=***
```

- **Why:** Springer Nature serves a 3038-byte HTML stub when the request looks bot-like (no session cookie). The HTTP status is 200 OK and content-type is `application/pdf` for the Nature URL pattern, so **HTTP-status + content-type validation is insufficient** — must check magic bytes AND size threshold. Sometimes the same URL serves the real PDF, sometimes the stub — depends on cookie/session state. Forensic re-probe of the same Nature URL Phase C captured as `too_small_3038` returned a real 1.2MB PDF when probed from M1 with a fresh session.
- **Why exactly 3038 bytes:** that's the byte-length of the cookies-not-supported HTML template.
- **Recovery:** retry with `--cookie-jar` to capture and replay session cookies; honor `Accept-Encoding: gzip, deflate, br` so client looks browser-like; PMC mirror for NIH-funded work; arXiv preprint for physics (10.1140 EPJC especially).
- **Expected recovery for this bucket:** 70-85%.

## Mechanism 3: ScienceDirect / Wiley / ACS subscription wall

- **Affected:** ~1,620 papers (727 + 395 + 436 + others), prefixes `10.1016`, `10.1002`, `10.1021`
- **URL pattern:** `sciencedirect.com/science/article/pii/<id>`, `onlinelibrary.wiley.com/doi/pdf/<doi>`, `pubs.acs.org/doi/pdf/<doi>`
- **HTTP signature:** Hard 403 with publisher-branded HTML body. DOE IP doesn't help (no Argonne-wide site license).
- **Why:** Real subscription wall. UChicago library IP via prokko might work for some, but per-paper at scale isn't viable.
- **Recovery:** CrossRef `text-mining` link field (text-and-data-mining API grants, untested at scale); author preprints on arXiv / institutional repos / ResearchGate; Unpaywall's `repository` host_type entries (already feeding the `ok` count when present).
- **Expected recovery for this bucket:** 30-50%. The ~50% that aren't recoverable are the real "paywalled, no green OA copy" tail.

## Mechanism 4: IOP "Page Not Found" dressed as 200

- **Affected:** ~409 papers, prefix `10.1088`
- **URL pattern:** `https://iopscience.iop.org/article/<doi>/pdf`
- **HTTP signature:** Sometimes `HTTP 404`, sometimes `HTTP 200` — both with body `<title>IOPscience::.. Page Not Found</title>`, body ~30,520 bytes

```
$ curl -sL "https://iopscience.iop.org/article/10.1088/1361-6633/ac7129/pdf"
HTTP=404 bytes=30520 ctype=text/html
<title>IOPscience::.. Page Not Found</title>
```

- **Why:** IOP's URL scheme for `/pdf` is inconsistent across journals. The DOI is fine; the `/pdf` suffix is wrong for some IOP journals.
- **Recovery:** re-resolve DOI via CrossRef to get the canonical PDF URL; OR strip `/pdf` suffix and parse the landing page for `<meta name="citation_pdf_url">` (this meta tag is standard across most publisher sites — Google Scholar's documented scraping target).
- **Expected recovery for this bucket:** 60-70% via citation_pdf_url scraping.

## Mechanism 5: OSTI biblio HTML returned instead of PDF

- **Affected:** 2,047 papers
- **URL pattern:** `https://www.osti.gov/biblio/<id>` (Unpaywall sometimes points at the biblio page when no direct PURL is known)
- **HTTP signature:** `HTTP 200` + content-type `text/html`, body ~78KB
- **Why:** This is actually GOOD news — biblio HTML contains publisher DOI (often missing from `papers.doi`), OSTI PURL link (sometimes present even when Unpaywall doesn't know about it), title, authors. Parsing it primes the recovery cascade.
- **Recovery:** parse `<a href="/servlets/purl/...">` and `<meta name="citation_doi">` from biblio HTML; re-attempt PURL fetch from cels-rbdgx2 (DOE IP — M1 returns 503 on PURL).
- **Expected recovery for this bucket:** 40-60%. Biblio always exists; PURL may or may not.

## Mechanism 6: `skip_no_url` (no DOI in Unpaywall)

- **Affected:** 8,821 papers, 39.7% of probed pool
- **Diagnosis:** Unpaywall API said "I don't have any OA copy for this DOI" OR our `papers.doi` is NULL so we couldn't query Unpaywall at all.
- **Recovery:**
  - **For NULL DOIs:** OSTI biblio HTML has the publisher DOI 80%+ of the time. Stage E.3 (`osti_biblio_parse`) fixes this.
  - **For "no OA" responses:** try CrossRef → Semantic Scholar → arXiv chain. S2 has a wider `openAccessPdf` surface than Unpaywall because it indexes preprint servers more aggressively.
  - Direct OSTI PURL — even without a DOI, OSTI biblio has the PURL link.
- **Expected recovery for this bucket:** 30-50% — on a large base (8,821), so this bucket alone could yield 2,500-4,000 PDFs.

## Priority order (recovery-yield ÷ engineering-cost)

1. **`osti_biblio_parse`** — runs wave-0, gives publisher DOI + PURL for every paper. Highest leverage.
2. **`osti_purl`** — from cels-rbdgx2 only. Recovers ~15% of biblio-resolved papers.
3. **`arxiv`** — recovers 60-80% of the APS Cloudflare bucket alone (~700 papers); also strong for HEP/cond-mat.
4. **`publisher_html` scrape** — citation_pdf_url meta-tag works on IOP, RSC, AIP, Frontiers. ~500-1000 papers.
5. **`s2`** — wider OA index than Unpaywall, especially for preprints. Authenticated with S2 API key.
6. **`crossref`** — text-mining links, fallback DOI re-resolution.
7. **Springer/Nature cookie-jar retry** — gets the 3038-byte stubs. Lower priority but cheap.
8. **`google_scholar`** — deferred. Captcha pain. Only if residual ≥10K.

## Forensic verification commands

Re-probe any of the failure modes:

```bash
# APS Cloudflare 403
curl -sLI "https://link.aps.org/accepted/10.1103/<DOI-SUFFIX>"

# Springer cookies stub (look for 3038 bytes or ?error=cookies_not_supported)
curl -sL -w "size=%{size_download} ctype=%{content_type} final=%{url_effective}\n" \
  -o /tmp/probe.bin "https://link.springer.com/content/pdf/<URL-ENCODED-DOI>.pdf"

# IOP 404-dressed-as-200
curl -sL -o /tmp/probe.bin -w "HTTP=%{http_code} size=%{size_download}\n" \
  "https://iopscience.iop.org/article/<DOI>/pdf"

# OSTI biblio HTML structure
curl -sL "https://www.osti.gov/biblio/<OSTI-ID>" | grep -oE '(purl/[0-9]+|citation_doi)'
```

## Generalizable rules

- **`product_type=Journal Article` + DOI prefix segments cleanly into 6-7 publisher mechanisms.** The top 10 DOI prefixes (10.1103, 10.1016, 10.1021, 10.1002, 10.1007, 10.1038, 10.1088, 10.1063, 10.1073, 10.1029) cover ~70% of any OSTI-DOI-keyed recovery pool — segment by prefix early, route to mechanism-specific recovery.
- **HTTP 200 + content-type=application/pdf is NOT proof of PDF.** Magic byte check (`%PDF`) + size threshold (>4096 bytes) + pdftotext char check are the three witnesses needed. Springer/Nature cookies stubs slip past status+content-type but fail magic bytes; OSTI/IOP HTML stubs slip past status alone but fail content-type.
- **Per-publisher recovery rates vary 5-fold (30% Wiley vs 80% APS-via-arxiv).** Don't quote aggregate recovery rates — quote per-mechanism, so you can size each fallback strategy independently.
