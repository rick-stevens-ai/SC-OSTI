# Multi-source PDF recovery fan-out

Worked example, 2026-06-10. Sequel to `oa-recovery-ceiling-2026-06-06.md` and
`unpaywall-overnight-recovery-2026-06-09.md`.

**Context:** OSTI corpus refresh, Phase D / Phase E. Unpaywall recovery (Phase
C) was running at 11% hit rate against 110K targets. The remaining 89K rows
were stratified into failure buckets (`skip_no_url`, `http_4xx`,
`returned_html`, `too_small`, etc.) and the question was: how do we close the
gap toward the ~60% ceiling in 24h?

This reference documents the **fan-out pattern**: launch 5-7 lever-specific
workers in parallel, each targeting one bucket × one external source, with
a **30-sample smoke test before every launch** to kill futile levers early.

## When this applies

- You have a corpus with N thousand fetchable artifacts (PDFs, datasets,
  records) and an Unpaywall/S2/Crossref recovery pass has already run
  (Phase 1).
- The residual is now classified into failure buckets — `4xx`, `html`,
  `too_small`, `no_url`, `wrong_magic`, etc.
- You have multiple hosts available (m1 + cherryrd + cels-lab-host + maybe
  uicgpu) and the bottleneck is your IP getting rate-limited by external
  services, not your local CPU.
- The user's framing is "wrap up by tomorrow" / "run everything in parallel"
  — a 24-hour wall budget with hard upper-bound on what's recoverable.

If the corpus is small (<1k missing) OR you haven't done a single-lever recovery
pass yet, this is overkill. Run Unpaywall first; come back here for the residual.

## The pattern

### 1. Stratify the residual into buckets first

Before designing workers, query the state DB for failure-bucket distribution:

```sql
SELECT
  CASE WHEN fetch_status = 'ok' THEN 'a_ok'
       WHEN fetch_status LIKE 'too_small_%' THEN 'too_small'
       WHEN fetch_status LIKE 'http_4%' THEN 'http_4xx'
       WHEN fetch_status LIKE '%html%' THEN 'returned_html'
       WHEN fetch_status = 'skip_no_url' THEN 'skip_no_url'
       ELSE 'other:' || fetch_status END AS bucket,
  COUNT(*)
FROM recovery GROUP BY bucket ORDER BY 2 DESC;
```

This drives target-list size, candidate worker design, and the
"what's-the-biggest-bucket" prioritization. Without this, you'll over-invest in
a small bucket OR miss a 5K row gap entirely.

### 2. For every candidate lever, run a 30-sample smoke FIRST

The single most valuable rule from this session: **never launch a multi-hour
worker without a 20-30 sample smoke test of its hit rate**. Project your hit
rate from the smoke, not from intuition. Concrete numbers from this session:

| Lever | Intuition | Smoke result | Decision |
|---|---|---|---|
| arXiv DOI search (APS/AIP/IOP DOIs) | 30-50% | **1% (2/200)** | KILLED |
| Stage 2 Unpaywall on biblio-discovered DOIs | 20-30% | **0/30** | NOT LAUNCHED |
| Free-OA publisher templates (Frontiers/Nature/PLoS/etc) | 40% | **5/25 = 20%** | LAUNCHED → 45% final |
| OSTI-biblio HTML parse for `returned_html` rows | 5-10% | **22/100** | LAUNCHED |

The arxiv intuition was off by 30×. The smoke costs ~2 minutes and would
have caught it. **I had this rule in memory and skipped it on the arxiv worker
because "DOI-based search seemed obviously solid for physics papers."** That
exact rationalization is what the rule exists to prevent. Same family as the
"structural extraction is fine per HARD RULE" trap on the main page.

### 3. Bucket × lever matrix — design before coding

Map each residual bucket to the lever(s) that can recover it. Skip combos
where smoke says <5% hit rate. Concrete matrix from this session:

| Bucket | Size | Lever 1 | Lever 2 | Notes |
|---|---|---|---|---|
| `returned_html` (osti.gov) | 4,438 | biblio_fetcher_v2 (re-parse for PURL/DOI) | — | 22% hit |
| `returned_html` (publisher) | 882 | html_parser (citation_pdf_url meta) | — | 5-10% hit |
| `skip_no_url` | 7,441 | biblio_fetcher (osti biblio parse) | — | 0.3% direct + 99% DOI discovery |
| `biblio_doi_found` (no PDF) | 9,966 | freeoa templates IF prefix matches | unpaywall (SKIPPED, 0/30 smoke) | recurse: now have DOI |
| `too_small` | 1,851 → 1,107 after Phase C | too_small_retry (re-fetch w/ stronger UA) | — | 62% hit final |
| `http_4xx` | 7,167 | mostly unrecoverable (Cloudflare/Bot Manager) | — | ~3% via freeoa templates |
| `arxiv-friendly DOIs` (10.1103, 10.1063, ...) | 2,170 | arxiv DOI search | — | **1% — KILLED** |
| Free-OA publisher DOIs | 949 | freeoa direct PDF | — | 45% hit |

The matrix surfaces obvious gaps (PMC = NIH biomedical, but PMC bot-walls all
our IPs with 1817-byte newline page — not a lever for us).

### 4. Launch workers in parallel — match worker count to lever shape

Three host types, three different worker-launch patterns:

- **cels-rbdgx2** (lab IP, OSTI/Unpaywall-friendly, file storage co-located
  with state DB): launch 4-6 workers via `nohup ... </dev/null &` with shared
  SQLite (`PRAGMA journal_mode=WAL`). Lock-per-row commits via Python
  `threading.Lock()`. Each worker is 6 ThreadPoolExecutor threads.
- **m1-mac-mini** (home IP, non-ANL): launch ONE worker per external service
  per IP via Hermes background-process. arXiv, ChemRxiv, anything that
  rate-limits per-IP.
- **cherryrd** (different non-ANL IP via Tailscale to my home network): same
  shape as m1 but via SSH+nohup+disown.

The shared SQLite on cels survives 4+ writers fine with WAL mode and per-row
locking. The state DB lives on the host's local SSD (`/rbstor/stevens/`), NOT
on a shared NFS mount — NFS locking + WAL is a known landmine.

### 5. The output-staging-then-commit pattern for non-DB hosts

Workers running on m1/cherryrd can't write to the cels SQLite directly (no
shared filesystem). The pattern:

1. Worker writes `<osti_id>.pdf` + `<osti_id>.meta` (TSV: osti_id, doi, year,
   lab, arxiv_id, bytes) to a local staging dir.
2. At end-of-run, rsync staging dir to the cels-side `osti_fulltext_unpay/`
   destination.
3. Run a single SQLite UPDATE pass on cels that reads the `.meta` sidecars,
   stats the actual `.pdf` files, and commits `(fetch_status, bytes, path, ts)`.

This separates fetch (parallel, on cheap hosts) from commit (single, on the
DB host). No DB-lock contention across hosts. No race conditions.

```bash
ssh cels-rbdgx2 '
for meta in /rbstor/stevens/osti_fulltext_unpay/arxiv_recovered/*.meta; do
  IFS=$"\t" read -r osti doi year lab arxiv_id sz < "$meta"
  pdf=$(dirname "$meta")/${osti}.pdf
  if [ -f "$pdf" ]; then
    actual=$(stat -c%s "$pdf")
    sqlite3 /rbstor/stevens/unpaywall_overnight.db "
      UPDATE recovery SET fetch_status=\"ok\", bytes=$actual, path=\"$pdf\",
      ts=$(date +%s) WHERE osti_id=\"$osti\""
  fi
done'
```

### 6. Sweep coordinator for chain-fired follow-ups

When a long-running primary worker (Phase C unpaywall, 5h ETA) is going to
produce more rows in the `too_small` bucket as it processes, schedule a sweep
coordinator to re-fire the bucket-specific recovery worker on the new rows
once the primary exits. The coordinator polls every 5min for the primary's PID;
on disappearance, it snapshots state and launches the round-2 worker on
newly-discovered rows. This auto-extends the parallel fan-out into the
post-primary window without requiring you to babysit.

## Lever-by-lever notes

### arXiv DOI search — beware low hit on APS/IOP

Smoke: 30 DOIs from APS/AIP/IOP/etc → 2 arXiv hits. Reasoning: arXiv's metadata
search by exact DOI is unreliable for physics papers. The arXiv preprint and
the journal publication often have **different titles, different abstracts**,
and the DOI of the published paper isn't reliably written into the arXiv
record metadata. Title-based fuzzy search would work better but requires
fetching titles first AND another LLM judgment to verify match.

**If you do want arXiv recovery for a physics corpus**, fetch titles for the
target DOIs first (Crossref `/works/<doi>`), then arXiv-search by title with
fuzzy match + author check. That's a separate multi-stage pipeline; not worth
it for <2K targets.

ANL IPs hit a structural HTTP/2 429 on arXiv API regardless of UA/backoff;
must run from non-ANL IP (m1, cherryrd). I confirmed this twice now — see the
existing memory entry.

### PMC bot-wall signature

`pmc.ncbi.nlm.nih.gov/articles/<PMCID>/pdf/` returns HTTP 200 with body
**exactly 1817 bytes of `0x0a` (newlines)** on bot detection. The cookie/UA
flow doesn't unblock this — appears to be recent NIH policy across both
cels and m1 IP ranges. Same on `www.ncbi.nlm.nih.gov` legacy redirect.

**Fallback that works: EuropePMC** at `europepmc.org`. The PDF endpoint also
fails, but the **fullTextXML** REST endpoint serves JATS XML cleanly:

```
https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML
```

JATS XML is structured text — better than PDF for downstream RAG/extraction.
For an XML-accepting downstream pipeline (chunker, indexer), use this instead
of PDF. For PDF-only consumers, EuropePMC is a dead lever.

### Free-OA publisher templates — the proven recipes

These URL patterns each returned a valid PDF in the 2026-06-10 smoke:

| Publisher | DOI prefix | Direct PDF URL template | Verified |
|---|---|---|---|
| Frontiers | `10.3389/` | `https://www.frontiersin.org/articles/<doi>/pdf` | ✓ |
| Nature OA (NComms, SciRep, etc) | `10.1038/<accession>` | `https://www.nature.com/articles/<accession>.pdf` | ✓ |
| eLife | `10.7554/eLife.<id>` | `https://elifesciences.org/articles/<id>.pdf` | ✓ |
| PLoS | `10.1371/journal.<jcode>.<id>` | `https://journals.plos.org/<jsub>/article/file?id=<doi>&type=printable` | ✓ |
| BMC | `10.1186/<accession>` | `https://link.springer.com/content/pdf/<doi>.pdf` | ✓ |
| bioRxiv/medRxiv | `10.1101/<id>` | `https://www.biorxiv.org/content/10.1101/<id>.full.pdf` (also medrxiv.org) | ✓ |
| Copernicus | `10.5194/<jrn>-<vol>-<page>-<year>` | `https://<jrn>.copernicus.org/articles/<vol>/<page>/<year>/<full-doi-tail>.pdf` | ✓ |
| JOSS | `10.21105/joss.<id>` | (varies — try Crossref link first) | not in smoke |

**Cloudflare-walled (skip these for direct GET):**
- PNAS `10.1073/` — 403 with Cloudflare ray-id, same on cels and m1
- ACS `10.1021/` — 403 paywall
- Wiley `10.1002/` — 403 paywall
- Elsevier `10.1016/` — 403 paywall

For the OSTI residual, freeoa templates landed **427/949 = 45% hit rate**.
The misses are mostly old PNAS articles + a few template-pattern outliers
(Copernicus per-journal URL shape varies).

PLoS journal-code map (`10.1371/journal.<jcode>.<id>`):
- `pone` → `plosone`
- `pbio` → `plosbiology`
- `pcbi` → `ploscompbiol`
- `pgen` → `plosgenetics`
- `ppat` → `plospathogens`
- `pmed` → `plosmedicine`
- `pntd` → `plosntds`

### Crossref TDM — dead without API key

Crossref `/works/<doi>` returns `link[].URL` entries with content-type
`application/pdf`. Looks promising. **But** those URLs are the publisher's
PDF endpoints we already 403 on — same Cloudflare/paywall wall. Without a
Crossref TDM token (institutional), Crossref adds nothing on top of
Unpaywall for closed-access content. Verified 4/10 returned a PDF URL,
0/10 fetched successfully.

### biblio_fetcher (OSTI biblio HTML → DOI + PURL)

OSTI's biblio HTML for `osti.gov/biblio/<id>` reliably contains the
publisher DOI in a `<meta name="DC.identifier">` or visible link. Two
distinct buckets to target:

1. **`skip_no_url` bucket** (Phase C had no Unpaywall URL): biblio parse
   discovers the DOI — 99% DOI hit, but the discovered DOIs are mostly
   closed-access journals (ACS/Elsevier/Wiley/Springer/IOP) that 403 every
   subsequent fetch. Direct PDF hit ~0.3%.

2. **`returned_html` bucket where pdf_url LIKE '%osti.gov/biblio%'**: Phase
   C fetched the OSTI biblio page and got HTML back, but didn't extract the
   DOI or try PURL. Re-running biblio parse on these yields ~22% direct PDF
   hit (much higher than bucket 1) because these OSTI records often have a
   PURL link that points at a valid Argonne/lab-hosted PDF that Phase C
   skipped over.

**Lesson:** the bucket × lever matrix matters. Same lever (biblio parse)
against two different bucket sources gave 0.3% vs 22% hit rates.

### Don't re-run Unpaywall on biblio-discovered DOIs

Smoke 2026-06-10: 30 random biblio-discovered DOIs → **0/30 OA**. Reasoning:
the `skip_no_url` bucket means Unpaywall *already verdicted* the underlying
paper as "no OA PDF" via OSTI's metadata. Discovering the publisher DOI from
biblio HTML doesn't change Unpaywall's verdict on the SAME paper — Unpaywall
keys on title+author+identifier and reaches the same conclusion regardless
of which DOI you fed it. **Save the ~50 minutes** that running this Stage 2
would burn.

This is the second time the "wait — same paper, same OA index" trap landed.
The first was the original `skip_no_url` bucket itself (we re-ran Unpaywall
on rows the first pass had marked `skip_no_url`, got 0/20). The general rule:
**OA-source memoization is per-paper, not per-DOI** — if the paper is closed
through one DOI, it's closed through every DOI that resolves to the same
paper.

## Numbers from the 2026-06-10 fan-out session

Starting state: `ok = 4,187`, Phase C in flight at 11.2% on 110K.

After parallel fan-out:

| Worker | Outcome | New OK | Hit rate |
|---|---|---|---|
| freeoa_fetcher (cels) | DONE | +427 | 45% (427/949) |
| biblio_fetcher (cels, skip_no_url) | DONE | +28 direct + 9966 DOIs | 0.3% direct, 99% DOI |
| biblio_fetcher_v2 (cels, returned_html OSTI) | RUNNING | +22 in first 100 | ~22% |
| html_parser (cels, publisher HTML) | RUNNING | +1 in first 25 | ~4% |
| arxiv worker (m1, APS DOIs) | KILLED | 2/200 | 1% |
| arxiv worker (cherryrd, APS DOIs) | KILLED | 2/225 | 0.9% |
| too_small_retry round 1 (cels) | DONE | +1,157 | 62.5% (1157/1851) |
| sweep_coordinator (will fire round 2) | WAITING | TBD | TBD |
| Phase C in-progress (cels) | RUNNING (29% done) | +3,524 | 11.2% (sustained) |

**Total: +5,160 PDFs in ~75 minutes of fan-out wall time** (counting only
completed workers), with Phase C still running for another ~4h projected to
add another ~10K.

## Pre-flight checklist

Before launching the fan-out:

1. **Stratify the residual** — query bucket distribution from state DB.
2. **For each lever candidate, smoke 20-30 samples** — refuse to skip this.
   Project hit rate from data, not intuition.
3. **Build the bucket × lever matrix** — surface dead combos before coding.
4. **Decide host assignment** — cels for DB-co-located workers, m1/cherryrd
   for rate-limited external services, never uicgpu for fetch (no public
   egress from compute nodes).
5. **Set up staging-then-commit for non-DB hosts** — local staging dirs +
   `.meta` sidecar TSVs + rsync + sqlite UPDATE pass.
6. **Schedule sweep coordinator** for round-2 follow-ups on buckets that
   the primary worker grows.
7. **Verify all workers alive after 5min**: `pgrep -af python3 | grep
   <keyword>` on each host. Single-host launches that silently die
   (`Inappropriate ioctl for device` from stdin-not-redirected) are the
   #1 fan-out failure mode — see the existing `< /dev/null` pitfall.

## Anti-patterns

- **Launching arxiv/PMC/Crossref-TDM workers without a smoke.** The
  smoke takes 2 minutes; the wasted multi-hour run takes 2 hours. Always
  smoke. I did skip smoke on arxiv this session and burned ~30 minutes
  of wall time on workers that ran at 1% hit.
- **Restarting the primary recovery (Phase C) to "include the new targets."**
  Don't — launch a separate worker on the delta (parallel-worker-delta
  pattern). The primary has resume semantics that won't survive an input
  swap.
- **Letting two workers fight over the same row.** Bucket × lever matrix
  + WHERE clauses scoped to disjoint statuses prevents this. The freeoa
  worker targets `http_4xx AND doi LIKE '10.3389/%'` while the html_parser
  targets `LIKE '%html%' AND pdf_url NOT LIKE '%osti.gov%'`. They cannot
  collide.
- **Treating "freeoa published 1620 rows committed" as 1620 NEW rows.** The
  DB query for `path LIKE '%/osti_fulltext_unpay/%'` returns all rows
  committed via that path mechanism across the lifetime of the project,
  not just today's. Always compare `SELECT COUNT(*) WHERE fetch_status='ok'`
  before-and-after for true delta.
- **Re-running Unpaywall on biblio-discovered DOIs.** Same paper, same
  verdict. 0/30 smoke proved it.
- **Building a PMC fetcher without a smoke against the 1817-byte newline
  signature.** The PMC bot wall is HTTP 200 — your fetcher will report
  "success" while writing junk to disk. Validate by bytes (size >= 4096 +
  magic = `%PDF`), not by status code.

## Linked artifacts

- `scripts/bulk_fetch_launcher_template.py` — primary fetcher template,
  used as Phase C base.
- `references/unpaywall-overnight-recovery-2026-06-09.md` — Phase 1
  recovery pattern. This document is its sequel.
- `references/oa-recovery-ceiling-2026-06-06.md` — the "measure the
  ceiling before adopting a tool" rule. This document operationalizes
  the fan-out after the ceiling is known.
- `references/diagnostic-probe-shape-2026-06-09.md` — the probe-must-
  match-bulk-shape rule. Same family as the smoke-before-launch rule
  here.
- `references/parallel-worker-delta-pattern-2026-06-07.md` — the
  separate-worker-on-the-delta rule. Used here for round-2 too_small
  recovery via sweep_coordinator.
