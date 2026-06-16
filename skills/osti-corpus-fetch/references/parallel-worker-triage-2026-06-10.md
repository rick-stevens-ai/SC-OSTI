# Parallel-worker triage — Phase C fan-out session, 2026-06-10

## Context

Phase C (Unpaywall recovery against ~110K-paper OSTI failed-recovery pool) was running
at ~4.9 records/s with 11.3% PDF-on-disk yield. Rick: "Code this up and get moving and
do things in parallel. I want to wrap up the 200K downloads in 24 hours or less."

Built 5 parallel fallback workers in one push, smoke-tested each within 2-5 minutes of
launch, killed 3 that were structurally dead, kept 2 winners.

## Workers + decision table

| Worker | Bucket / Strategy | Smoke result | Decision | Why |
|---|---|---|---|---|
| `too_small_retry_v2.py` | curl --compressed retry of Phase C's `too_small_*` rows (Springer/Nature/BMC stubs) | **1157/1851 ok = 62.5%** | **KEEP** — finished in ~30 min | TLS-fingerprint fix from `urllib-vs-curl-tls-fingerprint-2026-06-10.md` |
| `biblio_fetcher.py` | scrape OSTI biblio HTML for PURL link and publisher DOI; fetch PURL if present | direct PDF: 8/3100 = 0.26% — but **2,500+ DOIs extracted in side-channel** | **KEEP for the DOI side-channel** — direct hit rate too low to count on | PURLs mostly redirect to JLab/lab-SSO walls (see why-papers-are-missing-2026-06-10.md mode 2) |
| `arxiv_fetcher.py` (v1 multi-thread) | arXiv API DOI search → arXiv PDF for APS/HEP papers (DOI prefix 10.1103, 10.1140, etc.) | **3/350 ok = 0.9%, all subsequent calls 429** | **KILL** | ANL IP block hard-rate-limited by arXiv Varnish cache — see new pitfall |
| `arxiv_v2.py` (single-thread 3.5s sleep) | same, polite single-thread | Not launched — smoke `curl` still 429 from cels after 30s cooldown | **DON'T LAUNCH from cels** | Same root cause; needs different egress IP |
| `landing_scraper.py` | publisher landing page → `<meta name="citation_pdf_url">` → PDF, for IOP/RSC/AIP/Frontiers/OSA/IUCr/ECS | **3/150 ok = 2%** | **KILL** | IOP returns 14KB Radware CAPTCHA HTML, no meta tag; same pattern on other targeted publishers |
| `stage2_unpaywall.py` | re-query Unpaywall on the DOIs that `biblio_fetcher` extracted from biblio HTML | **0/20 OA hit rate on smoke** (all `is_oa: false`) | **DON'T LAUNCH** | Anti-pattern: `skip_no_url` already means Unpaywall returned no OA URL for that DOI; re-querying same source with same key returns same answer |
| `sweep_coordinator.py` | watches Phase C PID, auto-launches a second `too_small_retry_v2` sweep when Phase C finishes (to catch newly-discovered too_small_* rows from Phase C's later batches) | running | **KEEP** | Cheap, idempotent, captures the long-tail too_small bucket without re-running Phase C |

## Triage discipline (the lesson)

When fan-ing out N parallel fallback workers:

1. **Launch all of them with progress logging every 25-50 records.** Don't batch-launch
   in serial; the goal is to learn fastest, not to run politely.
2. **Within 2-3 minutes, each worker has processed 100-500 records.** That's enough
   signal: if ok-rate is >10%, you have a winner; if it's <5%, the worker is
   structurally dead, not slow.
3. **Kill dead workers immediately.** Don't let them run "in case it improves" —
   they're either rate-limited (will get worse, not better) or hitting a CAPTCHA wall
   (will never recover). Either way they:
   - Burn DB write contention (SQLite WAL lock under N writers)
   - Burn SSH bandwidth (curl subprocess output flooding the channel)
   - Pollute the log dir with thousands of `fail` lines
   - Create false sense of progress when total `done` counter rises
4. **For the dead workers, root-cause the failure once.** Don't fix it twice. Examples
   this session:
   - arxiv 429 — diagnosed with `curl -v` showing HTTP/2 429 + `cache-control: private`
     from Varnish cache. Same IP, same UA, single call, no traffic in flight = structural
     IP block.
   - IOP CAPTCHA — diagnosed with `head -c 3000` showing `<title>Radware Bot Manager
     Captcha</title>` and obfuscated JS. Same pattern on AIP, likely RSC.
5. **For the survivor(s), let them finish and capture the recovery delta.** Don't pile
   more workers on top to "speed it up" — too_small_retry_v2 was already running at
   the curl subprocess rate ceiling; adding more workers just contends on DB writes.
6. **Stage round 2 of the winner via a coordinator process** that watches the upstream
   driver and re-fires the winning worker on the new bucket additions when the
   driver exits. Cheap; idempotent; captures the long tail without manual intervention.

## Yield accounting

- Phase C steady state before fan-out: 2,503 ok / 22,170 probed (11.3%)
- Fan-out delta from too_small_retry_v2: **+1,157 PDFs** (single 30-min worker)
- Net after fan-out: 4,187 ok / 25,000 probed = **16.7%** (up 5.4 absolute points)
- Phase C ETA at 4.9/s on 110K total: ~5 hours remaining
- Sweep coordinator round-2 estimate: ~5K-7K new too_small_* rows × 62% recovery
  = **~3,000-4,300 additional PDFs** when Phase C finishes
- 24h total projection: **~31K recovered out of 110K failed pool** (~28% of failed
  pool, up from 11.3%) — combined with existing 113K already on Cherry6TB = **~144K /
  238K = ~60% paper-universe coverage**

## What the dead workers cost in retrospect

Negligible — kill decisions happened within 5 minutes of launch. The arxiv worker
posted 3 successes before being killed (kept those, didn't matter to the integrity
of the state DB). landing_scraper had 3 ok out of 150 = 2 net wins after kill.

The bigger cost was on stage2_unpaywall: I almost launched 2,500-DOI Unpaywall query
batch before the smoke caught the anti-pattern. **The 20-sample smoke saved roughly
8-12 minutes of API time, ~5MB log noise, and (more importantly) a false-positive
"DOI side-channel found" entry in the state DB that would have polluted later
analysis.** Always smoke a "stage 2" worker before launch — especially when the
input was generated by stage 1.

## Reusable smoke recipe

```bash
# 1. Pick 20 random rows from the target bucket
sqlite3 $STATE_DB \
  "SELECT osti_id, doi FROM recovery WHERE fetch_status='<bucket>' \
   ORDER BY RANDOM() LIMIT 20" | \
  while IFS='|' read oid doi; do
    # 2. Run the strategy's core lookup (Unpaywall, S2, arxiv, landing)
    #    and print one-line: OSTI_ID | OA/closed | pdf_url_or_NONE
    ...
  done | tee /tmp/smoke_<worker>.txt

# 3. Count wins
grep -c "OA" /tmp/smoke_<worker>.txt    # expect >=2 of 20 for a viable worker
grep -c "pdf=http" /tmp/smoke_<worker>.txt
```

Decision gate: if `pdf=http` count is <2 of 20 (10%), kill the planned worker. If
2-4 of 20, run as low-priority and re-evaluate after 500 records. If >=5 of 20,
green-light at full parallel.

## Evening session addendum (same day)

After the initial fan-out, ran four more workers against narrower target buckets.
Same triage discipline applied; recorded here for reference.

| Worker | Bucket / Strategy | Smoke result | Decision | Why |
|---|---|---|---|---|
| `biblio_fetcher_v2.py` | re-target the 2,998 `returned_html` OSTI rows that biblio_fetcher v1 missed (different WHERE clause) | **333/2998 ok = 11%** + 2,657 new DOIs extracted | **KEEP — full run** | Confirms `returned_html` bucket has recoverable PURL after re-parse; DOI side-channel still the bigger win |
| `html_parser.py` | non-OSTI HTML landing pages (2,408 rows) → `<meta citation_pdf_url>` extraction | **103/2408 ok = 4%** | **KEEP — full run** | Marginal but real; complements rather than overlaps with EZproxy bucket |
| `freeoa_fetcher.py` | DOI-template direct fetch on Frontiers/Nature OA/PLoS/eLife/BMC/Copernicus/bioRxiv DOIs (949 rows) | **427/949 ok = 45%** | **KEEP — full run** | Single best non-Phase-C worker by hit rate; template fetcher beats Unpaywall lookup for OA prefixes |
| `arxiv_multi_ip.py` (M1 + cherryrd, DOI search) | `arxiv.org/api/query?search_query=doi:"<doi>"` from non-ANL hosts | **2/200 ok at hour 1 = 1%** | **KILL both partitions** | arXiv DOI index is sparse for publisher DOIs — root cause is upstream coverage, not rate limiting. Salvaged 4 PDFs, killed both partitions, pivoted to title+author search (see arxiv-title-author-search-2026-06-10.md) which smoked at 50% on the same target pool |

## Evening session — second-order lessons

1. **Hit-rate disparity between strategy variants is the gold signal for a pivot.**
   arxiv DOI search at 1% vs arxiv title+author at 50% on the *same target pool* is
   a 50x gap — the upstream coverage is fine, the query shape is wrong. Don't keep
   tuning a 1%-hit worker; rebuild around the upstream's actual primary key.

2. **"Re-target with different WHERE clause" is a cheap second pass.** biblio_v2
   reused 95% of v1's code, swapping only the SELECT filter to cover rows v1 missed.
   333 PDFs + 2,657 DOIs in one ~45-minute run with no new infrastructure. Worth
   doing whenever your driver script left rows unprocessed because of an overly-narrow
   WHERE.

3. **DOI side-channels keep generating value even when direct fetch fails.** Both
   biblio_v1 and biblio_v2 had low direct-PDF hit rates (8/3100 and 333/2998) but
   each extracted thousands of fresh DOIs that fed downstream strategies (Unpaywall,
   arxiv_title, freeoa_fetcher). Don't judge a metadata-scrape worker by its direct-PDF
   number; the side-channel often dwarfs the headline.

4. **A killed worker that's pivoted-from is a win, not a loss.** arxiv_multi_ip
   produced 4 salvaged PDFs across both partitions over ~3h of runtime. Cheap
   investment to learn the DOI-search path is dead AND that the pivot to title+author
   search works. Frame the kill as "investment in upstream characterization,"
   not as "wasted compute."

5. **`stage2_unpaywall` anti-pattern, restated.** Two independent worker designs
   this session (the initial fan-out's stage2_unpaywall on biblio-extracted DOIs,
   and a separate plan to re-query Unpaywall after biblio_v2 extracted its 2,657
   DOIs) both got caught by the 0-of-20 smoke. The rule generalizes: **before
   piping discovered DOIs from a metadata scrape back into a lookup API, smoke 20
   first.** If the source has already returned "no OA" for the *same* DOI keys
   (which is what `skip_no_url` means in Phase C state), the second pass returns
   the same answer. Same applies to S2 — if S2 told you "no openAccessPdf" for a
   DOI in stage 1, scraping that DOI elsewhere and re-querying S2 won't change the
   answer.

## End-of-day totals (with evening fan-out)

- Phase C FINAL: 11,943 ok / 110,291 probed = 10.8%
- too_small_retry_v2 round-1: +1,157
- too_small_retry_v2 round-2 (auto-fired by sweep_coordinator): in flight
- biblio_v2: +333 PDFs + 2,657 DOIs into the queue
- html_parser: +103
- freeoa_fetcher: +427
- arxiv_multi_ip: +4 (then killed, pivoted to arxiv_title)
- arxiv_title (M1 + cherryrd, 11,219 targets at 50% smoke): in flight, ~16h ETA
- **State DB before fan-out: 5,188 ok**
- **State DB end of session: 15,377 ok** (+10,189 in one day)
- **Realistic landing ~24h from start without EZproxy: ~25-28K ok = 62-70% of paper universe**
- **With EZproxy worker on 35K walled: +10-12K more = 40K+ ok = 80%+ coverage**

