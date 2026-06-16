# Network-identity head-to-head probe (m1 vs CELS vs prokko)

When Rick suggests "try it from inside Argonne / from prokko / from a different IP" for a fetch task, the rigorous test is a **same-sample side-by-side**, not two independent random probes against different samples. Same seed, same N IDs, two hosts, diff the per-paper transitions.

## When to use

- A bulk fetch from one host is yielding low recovery
- Rick suggests a different network identity (CELS ANL subnet, UChicago prokko, Aurora UAN, etc.)
- Before committing 100K+ fetches to one host, you want hard evidence the IP swap is worth it

## Anti-pattern

Running probe A from host 1, separately running probe B from host 2 with a *different* random seed, then comparing aggregate recovery rates. This conflates sample variance with network effect and obscures per-paper unlocks. **Always seed-pin and reuse the exact sample list across hosts.**

## Recipe

1. **Stratified sample** with fixed seed (e.g. `random.seed(42)`). Stratify by the natural axis (year, lab, or backlog bucket), not random across the whole corpus. 50/stratum × 5-6 strata = 250-300 IDs is enough signal.

2. **Probe from host A** (usually m1 / home). Write a TSV with columns: `osti_id`, `year`, `primary_lab`, `doi`, `bucket`, `http_status`, `content_type`, `size_hint`, `final_url`, `title`. One row per sample ID, in deterministic order.

3. **Drive host B from the same TSV.** Don't re-sample. The host-B script reads the TSV from host A, walks it row-by-row, writes its own TSV with identical schema.

4. **Diff per-paper transitions.** Tabulate `(m1_bucket → anl_bucket): count`. Surface:
   - **CHANGED ★ANL unlocked**: `m1=anything_non_recovered → anl=recovered_pdf` (this is the headline number — how many papers does the new IP actually unlock?)
   - **CHANGED ⚠ANL lost**: `m1=recovered_pdf → anl=anything_else` (regressions, often transient — note count but don't over-weight)
   - **No-change transitions**: `same → same` (most rows; the boring confirmation)

5. **Decide on real signal.** If "ANL unlocked" minus "ANL lost" is <5% of sample, the IP swap is **noise** — most of the delta is transient 503/network jitter that retry would have caught. If it's >20%, the IP swap is structural — bulk from the new host. In between, look at the failure-bucket composition of the unlocks (publisher-wall escapes vs. retry recoveries).

## Verified finding 2026-06-13: OSTI PURL endpoint is NOT IP-gated

300-paper 2000-2005 stratified probe (50/year × 6 years, seed=42), m1 home vs. cels-rbdgx3.cels.anl.gov:

| bucket | m1 (home) | anl (rbdgx3) | delta |
|---|---|---|---|
| recovered_pdf | 74 | 77 | +3 |
| http_404 | 200 | 200 | 0 |
| http_403 | 3 | 0 | -3 |
| http_503 | 3 | 0 | -3 |
| redirect_off | 19 | 21 | +2 |

Per-paper transitions: only 4 papers changed from non-recovered → recovered when switching to ANL, and ALL 4 were transient 503/403 retries (not publisher walls that ANL-IP specifically unlocks). Conclusion: **`osti.gov/servlets/purl/<id>` serves content equally to home and CELS IPs.** Don't waste a head-to-head probe re-verifying this; run OSTI bulk fetches from wherever has the disk + bandwidth.

The "try a different IP" prior STILL applies to commercial publishers:
- **Nature/Springer/RSC/Cambridge**: prokko (UChicago CS subnet 128.135.123.x) unlocks ~6,800 papers vs home — IP-licensing publishers
- **ScienceDirect/Elsevier**: needs explicit API key, IP swap alone doesn't help
- **Wiley**: walls all IPs equally without library auth
- **S2**: global rate limit token bucket, IP swap doesn't change it

So the pattern is "always test with same-sample head-to-head before declaring IP swap useless OR essential" — but OSTI's own PURL endpoint specifically is exempt.

## Reference scripts

Both scripts produce the same TSV schema for diffability:

- `_state/probe_backfill_2000_2005.py` (m1, draws sample from local catalog.sqlite)
- `/tmp/probe_from_sample.py` (peer host, reads sample TSV from arg, queries OSTI, writes results TSV)

Diff with a small Python script loading both TSVs into `osti_id → row` dicts, then walking `(m1[oid].bucket, peer[oid].bucket)` Counter.

## Bucket vocabulary (re-use across all OSTI probes)

- `recovered_pdf` — HTTP 200 + `%PDF` magic OR `application/pdf` Content-Type + size > 1KB
- `http_404` — PURL not found (most common for pre-2006 papers)
- `http_403` — publisher wall or OSTI policy block
- `http_503` — OSTI overload (transient; retry recovers)
- `redirect_off` — 200 returned non-PDF after a redirect (off-site landing page)
- `wrong_type` — 200, no redirect, but Content-Type isn't PDF
- `empty` — 200 PDF magic but ≤1KB (truncated/failed redirect)
- `timeout` — connect/read timeout
- `exception` — Python error (usually SSL/DNS)
