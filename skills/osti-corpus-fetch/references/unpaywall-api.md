# Unpaywall API reference — OSTI fallback path

Authoritative endpoint shape, schema, and quirks for the **Unpaywall REST API v2**, the second-stage fallback when OSTI `/servlets/purl/{id}` returns 404 / 403 / HTML. Verified live 2026-06-07.

Docs source-of-truth: https://unpaywall.org/api/v2 (SPA — `curl` returns shell, use the rendered page if you need to re-verify).

## TL;DR

- **Free.** Auth = `?email=<contact>` parameter on every request. No API key, no token.
- **Rate limit: 100,000 calls / day** soft limit. Beyond that, download the full snapshot instead.
- **Two endpoints only**: DOI lookup (rock-solid) and title search (flaky, was 500ing 2026-06-07).
- **For OSTI fallback we only need the DOI endpoint** — every OSTI record carries a DOI in the metadata, so search is irrelevant for this pipeline.

## Endpoints

### `GET /v2/{doi}` — DOI lookup (the workhorse)

```
GET https://api.unpaywall.org/v2/10.1038/nature12373?email=rick.stevens@uchicago.edu
```

Returns a **DOI Object** describing OA status, every known OA location, license, journal metadata.

Key fields for the OSTI fallback pipeline:

| Field | Use |
|-------|-----|
| `is_oa` (bool) | Gate: skip everything if `false` → goes on unfetchable list. |
| `best_oa_location.url_for_pdf` | **Direct PDF URL — fetch this.** |
| `best_oa_location.host_type` | `publisher` or `repository`. Repositories (arXiv, PMC, institutional) usually deliver clean PDFs; publisher links sometimes hit paywalls behind the OA flag. |
| `best_oa_location.version` | `publishedVersion` (preferred) / `acceptedVersion` (postprint) / `submittedVersion` (preprint). |
| `best_oa_location.license` | `cc-by`, `cc-by-nc`, `null`, etc. Worth recording for downstream redistribution decisions. |
| `oa_locations[]` | All known locations. If `best_oa_location.url_for_pdf` 404s, walk this array. |
| `doi`, `title`, `journal_name`, `journal_issns`, `published_date` | Reconciliation metadata for the unfetchable report. |
| `genre` | `journal-article` / `proceedings-article` / `book-chapter` / etc. |

Top-level booleans worth reading:
- `is_oa` — has any OA copy anywhere
- `journal_is_oa` — gold OA journal
- `journal_is_in_doaj` — DOAJ-listed
- `has_repository_copy` — green OA available

### `GET /v2/search?query=<text>[&is_oa=bool][&page=N]` — title search (avoid for OSTI use)

```
GET https://api.unpaywall.org/v2/search?query=quantum+entanglement&is_oa=true&email=...
```

- **Title-only.** Body / abstract / author search NOT supported.
- Multiple terms whitespace-separated, AND by default.
- Operators: `"quoted phrase"`, `OR` (replaces AND), `-term` (negation).
- Returns 50 results/page; paginate with `page=2,3,...`.
- Each result = `{response: <DOI Object>, score: <float>, snippet: <HTML>}`.
- **WAS 500ING 2026-06-07** across all queries. Status unclear — if you need it, retry with backoff; if it stays down, fall back to CrossRef or OpenAlex search and resolve via Unpaywall by DOI.

We don't need this for the OSTI pipeline because OSTI records carry DOIs.

## Auth, headers, error modes

- Auth: `?email=<contact>` on URL. **Use `rick.stevens@uchicago.edu`** (Rick's standing contact).
- No special headers required. Standard `User-Agent` is fine; consider a project tag (`User-Agent: osti-fallback/rick.stevens@uchicago.edu`).
- 404 on DOI lookup = DOI not in Unpaywall's index. Don't retry.
- 422 = malformed DOI. Strip leading `doi:` prefix and any trailing whitespace before sending.
- 500 = upstream issue (search endpoint was doing this 2026-06-07). Single retry with 5s backoff; if it persists, surface upstream rather than burning the daily quota.

## Pacing for the 110K OSTI gap

At 100K/day cap, the projected ~60K Unpaywall lookups (~60% PURL miss rate) fit comfortably in one day. Realistic sustained rate:

```python
# 8 workers @ ~0.5s/call = 16 req/s = 57,600/hr
# Daily cap = 100K → ~1.7 hours of wall time before hitting the cap
# Pace at 1.15 req/s if you want to stay under cap on a multi-day run
```

For one-shot OSTI gap fill: 8 workers, no throttle, finishes in ~1-2 hrs, well under cap. For ongoing pipelines, throttle.

## Alternative: full database snapshot

If you ever need >100K lookups/day or want offline access:

- ~150 GB compressed, ~50M records (DOI → OA metadata)
- https://unpaywall.org/products/snapshot
- Updates monthly. Free for non-commercial use; registration required.

Overkill for the OSTI gap fallback — stick with the API.

## Cascade pattern for OSTI bulk fetch (revised 2026-06-07 from live smoke results)

```
PURL /servlets/purl/{id}
   │
   ├── 200 application/pdf, >10KB → OK, write to disk
   │
   └── 404 / 403 / HTML / <10KB
            │
            ▼
   Have DOI from OSTI metadata? (87.3% yes, measured on the 174K candidate set)
            │
            ├── YES → Unpaywall /v2/{doi}
            │           │
            │           ├── is_oa=true → walk oa_locations[], REPOSITORY URLs FIRST, publisher last
            │           │                 (sciencedirect/wiley/springer publisher PDFs scraper-403 even when marked OA)
            │           │
            │           ├── is_oa=false → unfetchable
            │           │
            │           └── 404 from Unpaywall = DOI not in Crossref index (DOE-internal DOI)
            │                                     → fall through to S2 path below
            │
            └── NO DOI (or Unpaywall 404'd the DOI) → S2 title search → new DOI? → Unpaywall(new_doi)
                                                                                      │
                                                                                      └── then same publisher/repo logic
            │
            └── all paths exhausted → record on unfetchable list (Ollie → comments@osti.gov)
```

**The critical revision from the naive cascade**: only invoke S2 when there is no DOI to look up OR Unpaywall didn't recognize the OSTI-provided DOI. Calling S2 after every Unpaywall miss burns the anonymous-tier quota for nothing — Unpaywall already had the answer ("not OA"), and S2 returning a different DOI for the same paper won't change Unpaywall's verdict.

## Hybrid S2+Unpaywall cascade — implementation notes

Verified live 2026-06-07 on cels-rbdgx2 against 15 bulk-fetch failures. The naive "always try S2 after Unpaywall" cascade gives ~7% recovery and bottlenecks on S2 429s. The DOI-aware cascade above is the right shape.

### Semantic Scholar tier reality

**We have an API key.** 40-char S2 key stashed in three canonical locations:
- m1 login keychain: `security find-generic-password -a rick-stevens-ai -s semantic-scholar-api-key -w`
- cherryrd: `~/.openclaw/.env` line `S2_API_KEY=...`
- cels-rbdgx2: `~/.env` line `S2_API_KEY=...`

**Keyed tier**: ~7-10 req/s sustained per token. With 8 threads at `S2_DELAY=0.15` per-thread, comfortable headroom. The published anonymous-tier numbers below are historical reference — **do not write new code that targets the anon tier**.

**Anonymous tier (historical, avoid)**: `~1 req/sec sustained, bursts hard-429 immediately`. Single worker, 3-5s sleep between calls, exponential backoff. 11/15 smoke-test calls 429'd even with 1.5s spacing. If you find yourself writing this throttling, you forgot to wire the key — go fix that instead.

### Boilerplate: load S2 key at probe-script init

```python
S2_API_KEY = ""
env_path = Path.home() / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if line.startswith("S2_API_KEY="):
            S2_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
print(f"[init] S2 API key present: {bool(S2_API_KEY)} (len={len(S2_API_KEY)})", flush=True)

def _fetch(url, source, max_retries=3):
    headers = {"User-Agent": UA}
    if source == "s2" and S2_API_KEY:
        headers["x-api-key"] = S2_API_KEY
    req = urllib.request.Request(url, headers=headers)
    # ... rest of fetch logic
```

The `[init]` print is load-bearing — if you don't see `True (len=40)` in the first 2 lines of stdout when launching a probe, kill immediately and fix the key path. Symptom of a missing key on a multi-thousand-paper probe: rate collapses from N/s to <1/s within ~5 minutes as S2 starts 429'ing every keyed call, and the script silently exponential-backoffs to a halt.

### Rate collapse = wiring bug, not throughput limit

If you see this pattern in a probe log:
```
  300/3,873  rate=1.5/s  eta=39.8min
  500/3,873  rate=0.7/s  eta=76.7min
  750/3,873  rate=0.6/s  eta=89.1min
```
The script is either (a) anon-tier and hitting the global token bucket, or (b) keyed but not actually sending the header. Either way: **kill, don't wait**. Verify with `grep "http_429" <log>` — if 429s dominate, it's the key. Don't trust an exponential-backoff machine to recover from a wiring bug.

### Publisher-URL 403 trap (the failure mode that makes "Unpaywall says OA" misleading)

Unpaywall flags a paper `is_oa=true` and returns a publisher URL like:
- `https://www.sciencedirect.com/science/article/pii/S0306261921010382`
- `https://nph.onlinelibrary.wiley.com/doi/pdfdirect/10.1111/nph.16826`

These are **legally OA** but the publisher blocks anonymous/scripted fetches with 403. The fetcher walks `oa_locations[]` and every entry from the same publisher 403s.

**Fix: sort `oa_locations` by `host_type` — repositories first (arXiv, PMC, institutional, lab .gov, OSTI itself), publishers last.** Repository copies almost always serve PDFs without auth checks.

```python
def sort_oa_locations(oa_locations):
    """Repository before publisher; preserve original order within each bucket."""
    repos    = [loc for loc in oa_locations if loc.get("host_type") == "repository" and loc.get("url_for_pdf")]
    publishers = [loc for loc in oa_locations if loc.get("host_type") != "repository" and loc.get("url_for_pdf")]
    return repos + publishers
```

Even with browser-like headers (Accept-Language, full Chrome UA string), Sciencedirect/Wiley/Springer publisher PDF endpoints stay 403. Don't bother with the UA arms race for this class — go to repository or accept defeat.

### Unpaywall `oa_locations[]` deduplication

Unpaywall returns duplicate URLs in `oa_locations[]` (same URL twice when `best_oa_location` and `first_oa_location` and an `oa_locations[]` entry all point at the same record). Dedupe before fetching or you'll send the same failing request twice.

### Realistic recovery yield on OSTI failures

On the smoke (15 failures, all with DOI):
- **3/15 already on disk** (`skip_exists` — fetched between bulk run and fallback run)
- **1/15 recovered via s2+unpaywall** (~7%)
- **4/15 Unpaywall 404 on the OSTI DOI** (DOI not in Crossref — likely DOE-internal)
- **4/15 Unpaywall said OA but URLs 403'd or weren't PDFs** (publisher wall problem above)
- **3/15 Unpaywall said `not_oa`** (genuinely paywalled — these go to the comments@osti.gov letter)

After the host_type-sorting fix, the publisher-403 bucket should mostly resolve, so realistic recovery on the failed-bulk pool is probably **25-40%**, not 60-70% as the naive math suggests. **Plan the unfetchable list accordingly** — Rick's expecting a real residual to send to OSTI, not zero.

### Pre-flight: how much of the OSTI candidate pool has DOI?

```bash
python3 -c "
import json
n=d=0
with open('/tmp/candidates_papers_only.jsonl') as f:
    for line in f:
        try:
            r=json.loads(line); n+=1
            if r.get('doi'): d+=1
        except: pass
print(f'{d:,}/{n:,} = {d/n*100:.1f}% have DOI')
"
```

Measured 2026-06-07 on 174,329 candidates: **152,208 with DOI = 87.3%**. The ~13% no-DOI tail is where S2 actually earns its rate-limit pain. If your candidate pool has dramatically different DOI coverage, re-plan the S2 budget accordingly.

## Smoke probe

```bash
curl -s "https://api.unpaywall.org/v2/10.1038/nature12373?email=rick.stevens@uchicago.edu" \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['is_oa'], r['best_oa_location']['url_for_pdf'] if r['best_oa_location'] else None)"
```

Expected: `True https://www.nature.com/articles/nature12373.pdf` in ~200ms.
