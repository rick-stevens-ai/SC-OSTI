# Stage-by-stage failure-class probe — 2026-06-08

Smoke gate before bulk-launching a re-fetch pass. Built when Ollie reported
8,707 papers in `failed_recovery.txt` and asked whether a CELS-host bulk
re-fetch would meaningfully recover the gap.

## The pattern

A bulk-fetch decision needs more than a headline success rate. Two probes
look "the same" at 26% recovery but are completely different decisions:

- 26% = transient network noise → retry path will lift to 50-70%.
- 26% = OSTI access-policy 403 + RST cluster → no amount of retry helps,
  needs an email to `comments@osti.gov`.

The probe distinguishes them by classifying failures *per stage* (metadata
fetch, landing-page fetch, PURL fetch, PDF body inspection, optional
text-extraction) and *per lab*, so the structural pattern is visible.

## Scripts

- `scripts/build_failure_class_sample.py` — builds the stratified-by-lab
  TSV sample from `failed_recovery.txt` + `recon_v2/` shards. Run on m1.
- `scripts/probe_cels_failure_classes.py` — runs the 5-stage probe per ID
  from the sample TSV. Run on cels-rbdgx2 (or any CELS-network host).
  Prints a per-stage `Counter` summary and the recovery-rate gate verdict.

## Deployment

```bash
# 1. Build sample on m1
cd ~/code/osti-replication-candidates
python3 ~/.hermes/skills/research/osti-corpus-fetch/scripts/build_failure_class_sample.py

# 2. Stage to CELS
ssh cels-rbdgx2 'mkdir -p /rbstor/stevens/osti_probe'
scp sample_50_for_cels_probe.tsv \
    ~/.hermes/skills/research/osti-corpus-fetch/scripts/probe_cels_failure_classes.py \
    cels-rbdgx2:/rbstor/stevens/osti_probe/

# 3. Run detached (ssh wrapper has ~60s timeout; probe takes 8-12 min)
ssh cels-rbdgx2 'cd /rbstor/stevens/osti_probe && nohup python3 probe_cels_failure_classes.py > probe.log 2>&1 & disown; echo "pid=$!"'

# 4. Poll
ssh cels-rbdgx2 'tail -25 /rbstor/stevens/osti_probe/probe.log'

# 5. Pull results back
scp cels-rbdgx2:/rbstor/stevens/osti_probe/sample_50_probe_results.tsv \
    ~/code/osti-replication-candidates/
```

## Empirical baseline (38 IDs, 9 labs, 2026-06-08, cels-rbdgx2)

Stage 3 (PURL fetch) outcome distribution:

| Outcome | Count | % |
|---|---|---|
| 200 (PDF returned) | 14 | 36.8% |
| 403 (access-policy forbidden) | 13 | 34.2% |
| err:RemoteDisconnected (TCP reset) | 11 | 28.9% |

Final s4 outcome (was the body actually a PDF?):

| Outcome | Count | % |
|---|---|---|
| pdf_ok | 10 | 26% |
| not_pdf (HTML/error body served as 200) | 4 | 11% |
| no_file (request failed before body) | 24 | 63% |

Per-lab cluster (this is the real decision input):

| Lab | n | pdf_ok | Dominant failure |
|---|---|---|---|
| Argonne | 5 | 3 | ✓ 60% recovery |
| SLAC | 5 | 3 | ✓ 60% |
| Princeton PPPL | 3 | 2 | ✓ 67% |
| Brookhaven | 1 | 1 | ✓ 100% (n=1) |
| Oak Ridge | 5 | 1 | 60% are 403 |
| Lawrence Berkeley | 5 | 0 | mix of 403 + reset |
| Pacific Northwest | 5 | 0 | **100% 403** |
| Fermi | 5 | 0 | 60% reset, 40% not_pdf |
| Jefferson Lab | 4 | 0 | 50% 403, 50% HTML |

Two clear regimes:

- **Recoverable** (Argonne / SLAC / PPPL / BNL): 60-100% pdf_ok via PURL
- **OSTI-walled-off** (PNNL / LBNL / Fermi / JLab / ORNL): 0-20% pdf_ok,
  dominated by 403 and RST

## Decision gate

If sample recovery from CELS is:

- **< 30%** → DO NOT bulk fetch. Investigate dominant failure class first.
  - If `403` cluster dominates → email `comments@osti.gov` about lab-specific
    access policy before any retry.
  - If `RemoteDisconnected` cluster dominates → re-run probe with retries
    (3 attempts, exponential backoff). If retries recover most of them,
    rate-limiting is the issue; throttle bulk fetch to ≤4 workers.
- **30-50%** → bulk fetch the recoverable-lab subset only (filter the failed
  list by lab match). Don't waste slots on the walled-off labs.
- **> 50%** → bulk fetch the whole pool from CELS is justified.

## Output-dir hygiene convention

Per Rick (2026-06-08): probe outputs should land in a timestamped run dir,
not loose in the cwd, so multiple probe runs accumulate cleanly and the
gate decision can be re-derived later. Layout:

```
/rbstor/stevens/osti_probe/probe_cels_<host>_<YYYYMMDD>-<HHMM>/
  SUMMARY.md       # one-paragraph human gate result
  summary.txt      # machine-readable bucket counts + per-lab + gate verdict
  summarize.py     # re-runnable summarizer (snapshot)
  results.tsv      # per-ID stage outcomes
  input_sample.tsv # exact IDs probed (reproducible)
  probe_cels.py    # the probe script that ran (snapshot, not the root copy
                   # which can drift)
  run.log          # full stdout/stderr
```

No PDF bodies retained — `/tmp/_osti_probe.pdf` is overwritten per ID and
deleted at the end. Only status/error metadata per stage is kept; that's
all the gate needs and it keeps the run dir <50KB.

If you tighten the probe (retries, hardened timeouts, etc.), generate a
fresh timestamped dir for each new run rather than overwriting the prior
one. The diff across runs is itself useful data when characterizing whether
OSTI's behavior shifted between probes.

## Retry-worthiness by failure class

When characterizing the failed_recovery pool, separate the failures into
retryable vs not-retryable BEFORE deciding what to do:

| Stage-3 outcome | Retry? | Why |
|---|---|---|
| `200 + pdf_ok` | (success) | — |
| `200 + not_pdf` | **No** | HTML body served as 200 = publisher-only abstract. PURL has no PDF to return; retrying gets the same HTML. |
| `403 Forbidden` | **No** | OSTI access policy on that lab/era. Retrying same path = same 403. Email `comments@osti.gov`. |
| `404 Not Found` | **No** | OSTI has no record of that PURL. Try Unpaywall fallback if DOI known; otherwise add to unfetchable list. |
| `5xx server error` | Yes (1 retry) | OSTI infrastructure hiccup. Single retry with 5s delay; don't loop. |
| `RemoteDisconnected` | **Yes** | TCP reset mid-fetch. Almost always rate-limiting or transient. Retry with exponential backoff. |
| `timeout` | **Yes** | Same as RemoteDisconnected — bounded retry with backoff. |
| `tls`/`dnserr` | Yes (1 retry) | Network blip on the CELS side. If a single retry fails, the host or DNS is genuinely broken; escalate. |

Rule: never silently retry the `200+not_pdf` and `403` classes — those need
escalation, not more requests. The probe's bucket distribution tells you
how big each escalation pile is.

## Hardened-timeout recipe (for any probe variant or bulk fetch)

The first-pass probe used a single 30s `urlopen` timeout for every stage,
which is too loose for bulk work — one slow request can stall the whole
worker thread. For any follow-up probe with retries, or for the bulk
fetcher itself, use per-stage caps:

- metadata GET: ≤ 10s (small JSON, should be sub-second)
- landing GET: ≤ 10s (HTML, should be 1-3s)
- PURL+PDF GET: ≤ 20s, plus a `Content-Length` cap — abort if the response
  header indicates >5MB (most OSTI PDFs are <2MB; a multi-GB body is a
  redirect to a video or dataset and not what we want)
- 3 retries with exponential backoff (1s, 3s, 9s) ONLY for the retry-worthy
  classes from the table above
- never retry `403` / `404` / `200+not_pdf` even once

Concrete pattern (httpx or urllib3 with a per-call `timeout=` and a small
`tenacity` decorator restricted by exception class) — implementation detail,
but the discipline matters more than the library choice.

## Pitfalls observed

- **SSH wrapper has ~60s timeout** on the Hermes terminal tool; the probe
  takes 8-12 min. Always launch detached (`nohup ... & disown; echo $!`)
  and poll the log file separately. Same trap as the uicgpu multi-worker
  launcher pattern; both are the same root cause.
- **Python 3.10 rejects backslashes inside f-string expressions** — common
  trap when generating a quick `summary.txt` via inline `python3 -c "..."`.
  If you need conditionals with quoted strings inside an f-string, write
  the summarizer to a file first (see `summarize.py` in the run dir) and
  scp it over rather than embedding via heredoc. Python 3.12+ removes this
  restriction but rbdgx2 has 3.10.
- **`pdftotext` is not installed** on cels-rbdgx2 by default. The probe
  marks s5 as `no_pdftotext` and continues; s1-s4 are the decision-grade
  stages, s5 is a nice-to-have. Don't block on installing poppler-utils.
  Treat `no_pdftotext` as an environment note, NOT as an OSTI failure —
  the 10 pdf_ok rows still count as recovered.
- **Don't assume "failed_recovery.txt" maps 1:1 to recon_v2 records.** In
  2026-06-08 the sample build found 8,696 of 8,707 had recon matches —
  11 were orphaned (probably from a prior recon pass with a different
  query schema). The build script flags these so you can decide whether
  to probe them separately.

## What was wrong with Ollie's headline brief

The "8,707 missing PDFs, fetch from CELS" framing implied a uniform
network-vantage problem. The probe revealed a *lab-structural access policy*
problem. Going straight to bulk would have:
- consumed CELS bandwidth on 5,500+ requests that were never going to succeed
- generated a fresh `failed_recovery.txt` with the same structure
- delayed the right next action (email OSTI about PNNL/LBNL/Fermi/JLab access)

The probe takes ~12 min and the script is reusable.
