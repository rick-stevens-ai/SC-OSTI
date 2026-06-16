# OSTI corpus expansion — recon-then-fetch-then-fallback

Pattern for **growing** an OSTI paper corpus (not screening one). Use when the
user says "expand the collection to cover X" / "fetch all papers from
{lab/year/topic}" / "refresh the corpus."

Distinguishes from the screening recipe (`osti-replication-screening-2026-06-05.md`)
which assumes the PDFs are already on disk.

## The three-phase shape

1. **Recon** — query OSTI API per (lab × year) cell, persist minimal metadata
   per cell as resumable JSONL. NO downloads. Build candidate ID set, diff
   against local store to identify the gap.
2. **Primary fetch** — OSTI PURL endpoint for gap IDs.
3. **Fallback fetch** — Unpaywall for the gap-after-primary, using DOI from
   the recon metadata.
4. **Failed list** — what's left after both fallbacks is the genuinely
   unfetchable set; email to `comments@osti.gov` for upstream resolution.

Why split recon from fetch: PDFs are 100-1000× the bytes of metadata. You want
the candidate set frozen and reviewable BEFORE committing to a bulk-download
run. Recon for 10 labs × 11 years takes ~10-20 min and ~250MB; fetching the
gap takes hours/days and tens of GB.

## CRITICAL — run from a CELS host, not from home

Rick reminded me (2026-06-07) and I verified empirically: **OSTI's PURL/PDF
endpoint discriminates against non-lab IPs.** From the home M1 mini, the
PURL endpoint returns 503/404 on almost every request — even for records
that have a working PDF. From CELS hosts (ANL public range, e.g. <cels-chicago-3>)
the same PURL returns the PDF in ~310ms.

Verified 2026-06-07 across hosts:
| Host             | OSTI API | PURL 1172426 (2016, has PDF) |
|------------------|----------|------------------------------|
| home M1 mini     | 200 (190ms) | **503** (timeout)         |
| cels-oss120 (<cels-chicago-3>, `ollama`) | 200 (165ms) | **200 application/pdf (266ms)** |
| cels-llama70 (`vllm31`)             | 200 (190ms) | **200 application/pdf (311ms)** |
| uicgpu           | NO INTERNET (000) | 000                  |

The API itself works fine from home (it's just metadata). What discriminates
is the PURL/PDF endpoint. So you CAN do recon from home, but any actual
PDF download MUST run from a CELS host.

### Operating pattern: scp-script → ssh-bg-run → scp-results

```bash
# 1. Ship the script to cels-oss120 (or cels-llama70)
scp recon_sc_labs.py cels-oss120:~/code/osti-replication-candidates/

# 2. Run in background on remote via Hermes background-process tool:
#    terminal(background=true, command='ssh cels-oss120 "cd ... && python3 recon_sc_labs.py 2>&1 | tee recon.log"')
#    notify_on_complete=true so you get pinged on finish.

# 3. Pull results back when done
scp -r cels-oss120:~/code/osti-replication-candidates/recon/ ./recon/
```

`uicgpu` has NO public internet egress (not even DNS) — it's compute-only.
Don't try to use it for fetches.

## Portable-path discipline for cross-host scripts

Scripts that you ship between m1 (home) and cels-oss120 (lab) MUST NOT
hardcode `/Users/stevens/...` paths — that directory doesn't exist on Linux
hosts and the script will crash with `PermissionError: '/Users'` on the
first `Path.mkdir()` call.

Use one of:
```python
OUT = Path(__file__).parent / "recon"      # relative to script location — preferred
OUT = Path("~/code/...").expanduser()      # relative to home dir
OUT = Path(os.environ.get("OSTI_OUT", "recon"))  # configurable via env
```

Bit me 2026-06-07 — first attempt at running `recon_sc_labs.py` on cels-oss120
crashed instantly because I'd written `OUT = Path("/Users/stevens/.../recon")`.
One-line fix, but one-restart cost. **Pre-flight any cross-host script for
hardcoded `/Users/...` or `/Volumes/...` paths before scp.**

## OSTI API parameter shape (verified 2026-06-07)

Endpoint: `https://www.osti.gov/api/v1/records`

Query params that work:
- `research_org=<lab name string>` — must match OSTI's exact spelling (see
  lab list below)
- `publication_date_start=MM/DD/YYYY` — US format, slash-delimited
- `publication_date_end=MM/DD/YYYY`
- `rows=100` — max per page
- `page=1` — 1-indexed

Response headers:
- `x-total-count: <int>` — total records matching query (use to plan pagination)

Per-record fields:
- `osti_id` — int, the join key
- `doi` — string, may be empty
- `title`, `publication_date`, `description` (abstract)
- `research_orgs[]` — list of canonical org strings
- `links[]` — `[{rel: 'citation', href: '...'}, ...]` — note that there is
  NOT always a `rel: 'fulltext'` link; PDF URL must be constructed
- `product_type` — Journal Article / Technical Report / Thesis / Conference / etc.

## 10 DOE Office of Science National Labs — exact API strings

These are the canonical strings the OSTI API matches against `research_org`.
Don't abbreviate — the API does substring matching but capitalization and
hyphenation matter for cleanliness:

```python
SC_LABS = [
    "Ames Laboratory",
    "Argonne National Laboratory",
    "Brookhaven National Laboratory",
    "Fermi National Accelerator Laboratory",
    "Lawrence Berkeley National Laboratory",
    "Oak Ridge National Laboratory",
    "Pacific Northwest National Laboratory",
    "Princeton Plasma Physics Laboratory",
    "SLAC National Accelerator Laboratory",
    "Thomas Jefferson National Accelerator Facility",
]
```

NOT in this list (NNSA / EERE / FE / other DOE programs, even though they are
DOE labs): LANL, LLNL, SNL, INL, NETL, NREL. If the user says "DOE labs"
without qualification, ASK whether they mean strict SC-10 or broader.

## Recon script template (resumable, polite, ~10 min for 10 × 11 cells)

```python
#!/opt/homebrew/bin/python3.13
"""Recon: for each (lab × year), fetch all OSTI metadata records to JSONL."""
import json, time, urllib.request, urllib.parse
from pathlib import Path

SC_LABS = [...]  # see above
YEARS = list(range(2016, 2027))
ROWS = 100
BASE = "https://www.osti.gov/api/v1/records"
OUT = Path(__file__).parent / "recon"; OUT.mkdir(exist_ok=True)  # PORTABLE: works on m1 home and cels-oss120

def fetch_lab_year(lab, year):
    safe = lab.replace(" ", "_")
    f = OUT / f"{safe}__{year}.jsonl"
    if f.exists() and f.stat().st_size > 0:
        return "cached", sum(1 for _ in open(f))
    all_recs, page, total = [], 1, None
    while True:
        url = BASE + "?" + urllib.parse.urlencode({
            "rows": ROWS, "page": page, "research_org": lab,
            "publication_date_start": f"01/01/{year}",
            "publication_date_end":   f"12/31/{year}",
        })
        req = urllib.request.Request(url, headers={"User-Agent": "kukla-osti/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if total is None:
                    total = int(r.headers.get("x-total-count", "0"))
                data = json.loads(r.read())
        except Exception as e:
            return "error", f"page{page}: {type(e).__name__}: {e}"
        if not data: break
        for rec in data:
            all_recs.append({
                "osti_id": str(rec.get("osti_id", "")),
                "doi": rec.get("doi") or "",
                "title": (rec.get("title") or "").strip(),
                "publication_date": rec.get("publication_date") or "",
                "research_orgs": rec.get("research_orgs") or [],
                "product_type": rec.get("product_type") or "",
                "_lab": lab, "_year": year,
            })
        if len(data) < ROWS or (total and page * ROWS >= total): break
        page += 1
        time.sleep(0.3)  # be polite
    tmp = f.with_suffix(".tmp")
    with open(tmp, "w") as out:
        for rec in all_recs:
            out.write(json.dumps(rec) + "\n")
    tmp.rename(f)
    return "fetched", len(all_recs)
```

Pitfalls:
- Resume on cache hit per (lab, year) file — if a cell errors you can rerun
  the whole script and only that cell will retry.
- `time.sleep(0.3)` between pages is enough; OSTI tolerates 1 req/s/IP from
  unauthenticated callers fine.
- Use `Optional[str]` typing OR shebang to python3.13 — the default macOS
  python3 is 3.9 which still supports `Optional` import but NOT PEP-604
  `str | None`.

## PDF fetch — PURL endpoint pattern and known flakiness

Primary PURL: `https://www.osti.gov/servlets/purl/{osti_id}`

Observed failure modes (2026-06-07):
- `503` during US business-hours load — retry off-hours OR back off
- `404` for older records (pre-2018) that lack PURL hosting on osti.gov
  itself; many redirect to publisher-hosted PDFs (escholarship, bnl.gov,
  lab .gov sites)
- Silent 0-byte PDFs when the publisher landing-page redirect fails (cookie
  walls, anti-bot)

Recovery cascade for unfetchable PURLs (from
`pdf-corpus-extraction-2026-06-06.md`):
1. Try PURL with `curl` (NOT urllib — urllib's HTTPRedirectHandler 403s on
   escholarship landing pages; curl with same User-Agent succeeds)
2. Parse OSTI biblio landing page for `citation_pdf_url` meta tag
3. Fallback to Unpaywall (next section)

## Unpaywall fallback

Free API, no key required, courtesy email in URL:

```
GET https://api.unpaywall.org/v2/{doi}?email=rick.stevens@uchicago.edu
```

Response shape (relevant fields):
- `is_oa: bool`
- `best_oa_location.url_for_pdf` — direct PDF link if available
- `best_oa_location.host_type` — publisher / repository
- `oa_locations[]` — all OA hosts (try multiple)

Pitfalls:
- Requires a `doi` field in the recon record. ~15-25% of OSTI records have
  no DOI — those skip the unpaywall step entirely.
- `is_oa=true` does not guarantee a working PDF — the URL may still 403 or
  point at an abstract. Verify content-length > 5KB and content-type matches
  `application/pdf` before declaring success.
- Email matters — they rate-limit per-email at 100k/day and may block calls
  with missing/fake emails.
- Same OA-ceiling rule from `oa-recovery-ceiling-2026-06-06.md` applies:
  measure ceiling on a 50-paper sample before committing to a full run.

## Failed-list email to OSTI support

OSTI's official help address: **`comments@osti.gov`**
- SLA: M-F 9am-4pm EST, response within 48hr
- Verified from osti.gov/contact (2026-06-07)

Email format that's helpful to OSTI:
```
Subject: PDF availability for N OSTI records (DOE-SC labs 2016-2026)

We're building a research dataset from OSTI records across the 10 DOE Office
of Science labs (2016-2026). After fetching from /servlets/purl/{id} and
falling back via Unpaywall for the DOI, N records remain inaccessible.

Attached: failed_list.csv with columns [osti_id, title, doi, attempted_urls,
last_error]. Could you advise on:
1. Are these records intentionally not OA?
2. Is there an authoritative download path we're missing?
3. Should we re-submit specific failed IDs via a different channel?

Contact: Rick Stevens, rick.stevens@uchicago.edu / rstevens@anl.gov
```

Cap the attachment at ~1000 records or break into batches — don't dump 8K
rows on a help desk.

## Cherry6TB-as-target gotcha

OSTI fulltext convention in this environment: `/Volumes/Cherry6TB/osti_fulltext/<year>/<id>.pdf`

Operational quirks:
- HFS root-catalog scans hang (60s+ timeouts on `ls /Volumes/Cherry6TB/` or
  `find -maxdepth 1`). Direct `stat <known-path>` works fine.
- 8K-12K of the existing PDFs are 0-byte (failed downloads from prior runs).
  Either skip if filesize > 0, OR re-fetch the 0-byte set as part of the
  expansion's "gap" definition.
- When detecting "already have this PDF", check `stat <path>` AND `size > 0`
  — don't just check file existence.

## Verification checklist before declaring done

1. `find recon/ -name '*.jsonl' | wc -l` == `len(SC_LABS) * len(YEARS)`
2. Sum of all JSONL lines ≈ sum of x-total-count values from API headers
3. Gap-vs-local count matches: `gap = candidates - existing_with_size_gt_0`
4. Smoke 20 PDFs from the gap before launching bulk fetch — confirm PURL
   works for at least a majority
5. Failed list CSV columns: osti_id, title, doi, primary_url_tried,
   primary_status, fallback_url_tried, fallback_status, final_disposition

## Orphan diagnostic — "have but not in current candidate set" is NOT "old"

When you compute `gap_in_other_direction = existing - candidates` (the IDs you
have on disk but weren't returned by the new recon), the instinct is to assume
those are pre-scope (older year range, different lab program, archived).
**Don't assume — probe the API.**

Pattern (2026-06-07): after expanding from a prior 67K corpus to a new
176K-candidate set, 1,566 IDs were "orphans" — on disk but not in new
candidates. I assumed pre-2016. Probed 80 via `GET /api/v1/records/{id}`:
**70 of 70 successful responses were 2020 LBNL Journal Articles** — exactly
in-scope. They were in fact the recon gap from a *truncated* LBNL 2020 cell
(IncompleteRead on page 412 silently lost ~1,000 records that ended up
distributed across publication-date-by-LBNL queries the prior corpus had run).

The lesson: **orphans don't mean "out of scope." They mean "your current
recon missed them."** Mandatory diagnostic before declaring orphans
acceptable-to-drop:

```python
# Sample 50-100 orphans, probe API, count by year + lab + product_type
# If >5% are in-scope-year + in-scope-lab + Journal Article,
# the recon has gaps. Refetch those cells (the script is idempotent).
```

Cells with `ERR ... IncompleteRead` or `ERR ... HTTP Error 429` in the recon
log are the usual suspects. The recon script's resume-on-cache-hit means
deleting the partial cell file and rerunning recovers the lost records.

## Email extraction from fulltext (use case: artifact-gap follow-up, replication contacts, Genesis Mission challenge-area mapping)

After PDFs are fetched + textified, a per-paper author + email extraction pass
unlocks three follow-on workflows: (1) write to corresponding authors of papers
whose xCards have missing artifact info, (2) build a contact list for the
replication-project pool, (3) map authors to Genesis Mission challenge areas
via their paper portfolio.

**Right model 2026-06-07:** `argo:claude-haiku-4.5` via Argo proxy. Free,
fast (~22 papers/s/12-workers), strong at structured JSON output. The
schema-bound prompt should constrain to first 4000 chars (covers title +
author block + first paragraph) — that's where the contact info lives, and
extracting from the whole paper pollutes with bibliography author emails.

**Schema:**
```json
{"authors": [{"name": "...", "email": "name@inst.edu",
              "affiliation": "Lab/University",
              "is_corresponding": true/false}],
 "corresponding_email": "primary@inst.edu or null"}
```

**Critical: set `max_tokens >= 4096`.** Default 1024 truncates the JSON
mid-author for papers with 10+ coauthors (common for DOE lab papers), and
the truncated response fails to parse, falling back to regex-only and losing
the structured author/affiliation/corresponding signal. Verified 2026-06-07:
with `max_tokens=1024` ~30% of papers fell back to regex-only; with `max_tokens=4096`
fallback dropped near zero.

**Regex as belt-and-suspenders, not primary:** always run a regex pass over
the same first-4000-char window for `[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`
and the obfuscated `name AT inst DOT edu` variant. Store as `regex_emails`
alongside the LLM's structured output. If the LLM fails (proxy 403, JSON
truncation, etc.), regex captures most papers' corresponding email anyway
because authors who mark themselves corresponding write the bare email out.

**Per-paper output (JSONL, resumable on osti_id):**
```json
{"osti_id": "1788205",
 "regex_emails": ["Ljiljana.PasaTolic@pnnl.gov"],
 "authors": [{"name": "Mowei Zhou", ...}, ... 17 more ...],
 "corresponding_email": "Ljiljana.PasaTolic@pnnl.gov",
 "extraction": "llm"}
```

Then a Python rollup pass produces per-author records keyed on email:
`{email, name, primary_affiliation, n_papers, paper_ids[], n_cards_data,
n_cards_model, n_cards_agent, n_replicable_no_lab, challenge_areas{},
primary_challenge}`.

## Genesis Mission challenge areas — canonical taxonomy location

When a Genesis Mission classification or alignment task surfaces, the
authoritative source is in `~/Dropbox/GENESIS-RFA/`:

- `GM-NOFO-RFA-AREAS.xlsx` — single-sheet table with columns Topic, Challenge
  Area, Subtopic, Office, Focus Area. Verified 2026-06-07: contains **21
  numbered Challenge Areas** (Topics 1-17 = National Science & Technology
  Challenges, Topics 18-21 = cross-cutting platform needs) with **99 focus
  areas total**, each tagged with the lead DOE office (BES, BER, FES, NE,
  ASCR, AMMTO, ITO, AFFO, CMEI, IESO, etc.).
- `NOFO-LHP-DE-FOA-0003612.pdf` — the full LHP NOFO; section "CHALLENGE
  AREAS FOR APPLICATION FORMATION" starts ~line 494 of `pdftotext` output
  and contains the prose Challenge/AI-Solution/Justification/National-Impact
  blocks plus focus-area definitions.
- `GM-NOFO-BER-ASCR-Areas.xlsx` — subset view filtered to BER + ASCR foci
  only. Useful for biology + ASCR-co-funded subset.
- `Genesis-Mission-Competitive-Proposal-Guide.md` + `.pdf` — Rick's prose
  guide on the four components of each topic (Challenge, AI Solution,
  Justification, National Impact) and proposal structure.
- `GM-NOFO-USE-THIS-FOR-AI.pdf` — focus-area-only excerpt suitable to feed
  an LLM classifier as context.

If a user mentions "26 Challenge Areas" but the LHP NOFO has 21, this is
the **Lighthouse pool count** they're remembering, not the post-rationalization
NOFO count. Genesis Mission's predecessor was the **AI Lighthouse Challenges**
process (late 2024/early 2025) — DOE/SC reviewed 134 LH submissions, surfaced
a working set, then rationalized into the 21-topic NOFO. The Lighthouse pool
breakdown from `~/Dropbox/DOE-LHP-DARIO/` filename inventory:

| Office | Count | Examples |
|---|---:|---|
| SC | 13 | Accelerators, Biotechnology, CMM, Fusion DCP, Manufacturing, Materials by Design, Microelectronics, Quantum Algorithms, Quantum Systems, Quarks2Cosmos, Subsurface Biogeochemistry, Water for Energy, AI Driven Laboratories |
| Applied Energy (LEAP) | 11 | Grid Scaling, Nuclear Energy, Critical Minerals, Subsurface Systems, Nuclear Remediation, Materials Development, Semiconductor Manufacturing, Advanced Manufacturing, Buildings, Data Center Innovation, Biotechnology |
| NNSA | 9 | 3 Thrusts × 2-4 Ideas (Apex Forge, Alchemy, Spark Catalyst, Watcher, etc.) |
| Raw total | 33 | |
| Deduped (SC↔LEAP overlap on Biotech, Crit Min, Materials, Microelec, Nuclear, Subsurface) | **~26-27** | This is the "26" |

The final 21 NOFO Challenge Areas = ~26 deduped Lighthouses + 4 cross-cutting
platform topics (HPC code curation, AI for Scientific Reasoning, Cybersecurity,
AI in Fluid Flow) - some absorbed/dropped (Buildings, several NNSA-specific
items). The Lighthouse name → NOFO topic mapping is mostly 1:1 (Manufacturing
LH → Topic 1, Biotechnology LH → Topic 2, etc.).

**For classification: use the 21-topic NOFO taxonomy** (post-rationalization,
authoritative) but **keep a "Lighthouse origin name" tag** since those names
are more memorable for socializing the work. The Lighthouse Framework deck
at `/Users/stevens/Dropbox/Lighthouse Framework (standalone) v2  -  Read-Only.pptx`
(real, 397KB, 4 slides) is the framework's origin story.

Lab-PI-Guide.pdf may have the broader list; check it if the user can't
point to the missing 5.

To extract the table programmatically:
```python
import openpyxl
wb = openpyxl.load_workbook(os.path.expanduser('~/Dropbox/GENESIS-RFA/GM-NOFO-RFA-AREAS.xlsx'))
ws = wb['Sheet1']
rows = list(ws.iter_rows(values_only=True))
header = rows[0]  # ('Topic', 'Challenge Area', 'Subtopic', 'Office', 'Focus Area')
data = [dict(zip(header, r)) for r in rows[1:] if r[0] is not None]
# Group by Topic # for the 21 Challenge Areas
areas = {}
for d in data:
    t = d['Topic']
    if t not in areas:
        areas[t] = {'num': t, 'name': d['Challenge Area'], 'focus_areas': []}
    areas[t]['focus_areas'].append({
        'code': d['Subtopic'], 'office': d['Office'], 'focus': d['Focus Area']
    })
```

For LLM classification: title + abstract + first paragraph → one or more
challenge area codes (e.g. `5A`, `19B`) with confidence + brief rationale.
Cascade: cheap llama70 / Haiku 4.5 first pass over all candidates → Sonnet 4.6
pass on the ambiguous ~10-15%.

## PDF extraction toolchain on the M1 mini

The default macOS python3 (3.9) does NOT have `pymupdf`/`fitz` installed by
default. Three working options ranked by speed:

1. **`pdftotext` from poppler** (`/opt/homebrew/bin/pdftotext`) — fast (~50ms
   per PDF), already installed via homebrew. Best for bulk extraction when
   layout-aware output isn't needed.
2. **`pymupdf` installed in python3.13** at `/opt/homebrew/bin/python3.13`
   — fastest if you need block-level access; verify `import pymupdf` first.
3. **`marker-pdf`** (project-local install) — best quality for scientific
   papers with equations/tables, but ~5× slower.

For per-paper email extraction (Phase 1 above), already-extracted .txt files
from prior `marker-pdf` or `pymupdf` runs are sufficient. Don't re-extract
PDFs that already have a `.txt` sidecar.

## Dropbox 0-byte sync corruption — common gotcha when reading taxonomy/spec docs

Rick's Dropbox has a recurring failure mode: filenames appear in `ls` but
the actual files are 0 bytes. The directory hierarchy is intact (folder
names, file names, mtimes are real), but the contents never synced down to
this machine. Affected paths I've hit:

- `/Users/stevens/Dropbox/DOE_AI_Lighthouse_Challenges.pdf` (0 bytes)
- All of `~/Dropbox/DOE-LHP-DARIO/SC AI Lighthouse Challenge - *.docx` (0 bytes each)
- `~/Dropbox/DOE-LHP-DARIO/AI Lighthouse Challenge Coordination.xlsx` (0 bytes)
- Many `~/Dropbox/DOE-LHP-DARIO/WORKING-*/REVISED.*/*.txt` (0 bytes)
- Many `~/Dropbox/DOE-LHP-DARIO/NNSA/*.docx` (0 bytes)

Symptoms when you try to use them:
- `openpyxl.load_workbook`: `BadZipFile: File is not a zip file`
- `pdftotext`: `Syntax Error: Document stream is empty`
- `zipfile.ZipFile` on docx: `BadZipFile`

**Pre-flight pattern when working from a Dropbox dir:**
```bash
find /path/to/dropbox/dir -type f -size +1c    # only files with content
file <path>                                     # confirms "empty" vs real
ls -la <path>                                   # confirms 0-byte
```

**Filenames are often informative even when contents are missing.** From the
0-byte `DOE-LHP-DARIO/` tree alone, I reconstructed the full Lighthouse pool
breakdown (13 SC + 11 LEAP + 9 NNSA) and the Lighthouse → Genesis topic
mapping just from the filename inventory. Don't assume a 0-byte dir is
useless — `ls -R` + filename pattern analysis often gives you 80% of what
you needed.

When the contents really do matter, ask Rick to force a Dropbox re-sync
(right-click → Smart Sync → Online Only, then Local) on the specific
subtree. Don't try to recover the missing files yourself.

## xCard generation pipeline — canonical scripts at ~/Dropbox/ARGONNE-PAPERS/GOOD/

When Rick says "generate more xCards" / "re-extract cards from these papers"
/ "the cards need another pass" / "the artifact-fetchability fraction is too
low," **don't reinvent the pipeline.** The existing scripts at
`~/Dropbox/ARGONNE-PAPERS/GOOD/` are the canonical xCard generators that
produced the existing 4628 DATA + 1231 MODEL + 86 AGENT cards:

Pipeline shape: **PDF → TXT → batch_extract (model/agent/data) → augment → enrich → validate**

Key scripts:
- `extract-o-matic.py` (127KB, the big one) — has three card-type prompts:
  `_get_model_card_prompt`, `_get_agent_card_prompt`, `_get_data_card_prompt`.
  Default model is `gpt-4.1`; for re-runs swap to `argo:claude-sonnet-4.6`
  for quality or `llama70` for free local throughput.
- `batch_extract.py` — parallel runner; mode flags `--mode model_card_extraction`,
  `agent_card_extraction`, `data_card_extraction`.
- `pdf2txt.py` — PDF → TXT preprocessor.
- `augment_cards.py` — adds discovered URLs + DOI metadata to extracted cards.
- `enrich_cards.py`, `validate_cards.py` — post-extraction passes.
- `full_card_pipeline.sh` — the orchestrator wrapper. Standard invocation:
  ```bash
  ./full_card_pipeline.sh /path/to/papers_dir --resolve-dois --workers 8
  ```

For re-extraction passes (the Thread 2 "improve artifact-fetchability"
ask): copy the pipeline scripts to `~/code/<project>/xcards_pipeline/`,
update prompts in `extract-o-matic.py` to enforce stricter URL-verification
requirements (refuse to claim an artifact unless a working URL is in the
paper), then run with `--mode model_card_extraction --model
argo:claude-sonnet-4.6` for quality.

The card-output dirs that the pipeline writes:
- `<BASE>_MODEL_CARDS/`, `<BASE>_AGENT_CARDS/`, `<BASE>_DATA_CARDS/`
- After distillation, the polished versions live at
  `~/Dropbox/ARGONNE-PAPERS/XCARDS/MARKDOWN-{DATA,MODEL,AGENT}-CARDS`
  (the 4628/1231/86 counts above).
- Raw extractions live at
  `~/Dropbox/ARGONNE-PAPERS/GOOD/ALL-PAPERS-{DATA,MODEL,AGENT}-CARDS`
  (18,902 each, includes NO_SIGNALS results).

## Model endpoint pitfall — verify model presence at proxy BEFORE writing the extraction script

Confirmed twice (2026-05-25 with Kimi-K2.6 tool-call failures, 2026-06-07
with hardcoded `llama70` against Argo): **model registries drift faster than
docs.** The pattern:

1. Before writing any extraction script, `curl -sS http://<proxy>/v1/models`
   and confirm the model id you plan to use is actually present.
2. CELS llama70 lives at `http://<cels-chicago-2>:80/v1` with `Bearer CELS`
   — NOT at the Argo proxy. Argo has Claude/GPT/Gemini/DeepSeek but no Llama
   as of 2026-06-07.
3. If you get HTTP 403 on the first call, it's wrong model name × wrong
   endpoint, not broken auth (auth fails would 401).
4. If the model registry doc (`~/.hermes/ollie-context/MODEL_REGISTRY.md`)
   lists Llama on Argo, that's stale — trust the live `/v1/models` over the
   doc.

Quick reference for this M1 mini's working endpoints (2026-06-07):
| Endpoint | URL | Auth | Best models |
|---|---|---|---|
| Argo proxy | `http://<tailnet-aggregator>:44497/v1` | `Bearer stevens` | `argo:claude-haiku-4.5` (fast structured), `argo:claude-sonnet-4.6` (workhorse), `argo:claude-opus-4.7` (overkill) |
| CELS chicago-2 | `http://<cels-chicago-2>:80/v1` | `Bearer CELS` | `llama70` (free, local, 131K context) |
| CELS chicago-3 | `http://<cels-chicago-3>:80/v1` | `Bearer CELS` | `oss120` |
| CELS chicago-1 | `http://<cels-chicago-1>:80/v1` | `Bearer CELS` | `kimi-k2.6` (reasoning, slow) |
| CELS hcdgx2 | `http://<tailnet-host>:9999/v1` | `Bearer CELS` | `gemma4` |

`~/Dropbox/AI-ENVIRONMENT/CELS_ENDPOINT_MAP.md` is the live-verified
authoritative map; check it when the table above seems stale.

## What this expanded over the prior 67k corpus

Prior corpus: 67,119 papers, mixed labs/years, ~88% downloaded.
After expansion (estimated): roughly 100-250K candidate IDs from the
10-lab × 11-year sweep. Net new after dedupe against the existing 67k:
unknown until recon completes, but expect ~50-150K new IDs given the prior
corpus was Argonne-heavy.

## Post-fetch PDF integrity audit — what the fetcher's inline check misses

The `bulk_fetch.py` inline validation (`Content-Type` contains "pdf" OR first
4 bytes are `%PDF`) catches obvious 404 HTML and bytewise junk, but the OSTI
corpus at scale reveals three bug classes the inline check **does not** catch:

1. **OCR-only / scanned PDFs**: parse cleanly with pypdf, but `extract_text()`
   returns empty. Verified rate on OSTI bulk fetch: **6.5%** in a 300-PDF
   random sample (rbdgx2 `/rbstor/stevens/osti_fulltext_v2/`, 2026-06-07).
   Projected at corpus completion (~140K PDFs): ~9,000 OCR-only papers,
   ~400K pages. These need a separate marker-pdf pipeline (see
   `ocr-and-documents` skill, "Batch OCR pipeline" section). They're invisible
   to the fetcher's `ok` status and silently degrade any downstream LLM
   extraction that reads `.txt` sidecars.

2. **HTML 302 landing pages with mis-set Content-Type**: ~1 in 25K, rare but
   real. One concrete OSTI example landed as
   `/rbstor/stevens/osti_fulltext_v2/smoke/1891990.pdf` — file starts with
   `<!DOCTYPE` (HTML) but the server's Content-Type header said
   `application/pdf` so the fetcher wrote it. The first-4-bytes check would
   have caught this if the byte check ran instead of OR'd alongside the
   Content-Type check; tighten to AND.

3. **Year-map extraction bug** (THE big bug from 2026-06-07): when
   `publication_date` field has ISO format (`2020-04-15T00:00:00Z`),
   `year = pd[-4:]` yields `:00Z` not `2020`. Result: 23,712 PDFs went to
   `/rbstor/.../unknown/` (empty year string) and 2,270 went to
   `/rbstor/.../:00Z/` (ISO tail). Files are fine, organization is broken.
   The fix is to use a regex (`re.search(r"(19|20)\d{2}", pd).group(0)`) or
   `datetime.fromisoformat(pd).year` for year extraction, never string
   slicing. Verify on 5 random records BEFORE building the year_map:

   ```bash
   python3 -c "import json; [print(json.loads(l).get('publication_date')) for l in open('candidates.jsonl').readlines()[:20]]"
   ```

   Both ISO and MM/DD/YYYY formats appear in OSTI responses, sometimes within
   the same query result set — assume mixed formats.

### The post-fetch audit script (run after every bulk-fetch wave)

```python
import pypdf, random, glob, os, signal, json
from collections import Counter

ROOT = "/rbstor/stevens/osti_fulltext_v2"  # or wherever
files = glob.glob(f"{ROOT}/**/*.pdf", recursive=True)
sample = random.sample(files, min(500, len(files)))

def handler(*a): raise TimeoutError()
signal.signal(signal.SIGALRM, handler)

buckets = Counter()
for f in sample:
    sz = os.path.getsize(f)
    if sz > 20_000_000:
        buckets["skipped_large"] += 1; continue
    try:
        with open(f, "rb") as fh: head = fh.read(8)
        if not head.startswith(b"%PDF"):
            buckets["bad_magic"] += 1; continue
        signal.alarm(5)
        r = pypdf.PdfReader(f, strict=False)
        if len(r.pages) == 0:
            signal.alarm(0); buckets["empty"] += 1; continue
        tot = sum(len((p.extract_text() or "").strip()) for p in r.pages[:2])
        signal.alarm(0)
        if tot < 60:
            buckets["ocr_only"] += 1
        else:
            buckets["text_ok"] += 1
    except Exception:
        signal.alarm(0); buckets["parse_err"] += 1

for k, v in buckets.most_common():
    print(f"  {k}: {v} ({100*v/sum(buckets.values()):.1f}%)")
```

Expected distribution on a healthy OSTI bulk fetch (300+ samples):
- `text_ok`: ~90-94%
- `ocr_only`: ~5-7% (downstream marker-pdf pipeline)
- `parse_err`, `bad_magic`, `empty`: <0.5% combined (refetch candidates)

**Make this part of the "is the bulk fetch done" checklist.** Run it once at
50% complete (catches systemic problems while there's still time to fix the
fetcher) and once at completion (final corpus-quality baseline).

### Parallel-worker delta pattern (already in main SKILL.md pitfalls)

When recon mid-fetch discovers a much larger candidate pool (e.g. 174K → 404K
backfilling earlier years), don't restart the running fetcher. Compute the
set difference, write `<x>_v2_additions.txt`, clone the fetcher with patched
LOG/META/IDS paths, launch as a separate process. Both workers run in
parallel on disjoint queues. Worked example 2026-06-07: spawned
`bulk_fetch_v2.py` on the 229,499-ID delta, ran cleanly alongside the v1
fetcher with no contention. Same pattern for the classifier:
`classify_22_v2add.py` against `candidates_papers_v2_additions.jsonl` with
`OUT = classifications_v2add.jsonl`. Merge `cat v1.jsonl v2add.jsonl` at end.
